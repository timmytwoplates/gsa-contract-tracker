"""
fetch_terminations.py — Monthly USASpending bulk archive termination pipeline.

Adapted from Abigail Haddad's fetch_awards.py (terminations-main).

Delta detection: compares the latest archive datestamp on files.usaspending.gov
against the last completed run in refresh_log. Only downloads agency/FY combos
that haven't been checkpointed yet (resume-safe).

Run:
    python -m pipeline.fetch_terminations                  # full run per config FY
    python -m pipeline.fetch_terminations --fy 2026        # single fiscal year
    python -m pipeline.fetch_terminations --agencies 047   # specific agency codes
    python -m pipeline.fetch_terminations --force-current-fy
    python -m pipeline.fetch_terminations --check-delta    # print datestamp info and exit
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from loguru import logger

CONFIG_PATH = Path("config/config.yaml")

# Sentinel return values for download_zip()
_NOT_FOUND = "NOT_FOUND"
_IP_BLOCKED = "IP_BLOCKED"
_FAILED = "FAILED"

# Increase CSV field size limit for large federal contract descriptions
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# ---------------------------------------------------------------------------
# USASpending field → internal DB column mapping
# ---------------------------------------------------------------------------
USA_COLUMN_MAP = {
    "contract_award_unique_key": "contract_award_unique_key",
    "award_id_piid": "piid",
    "modification_number": "mod_number",
    "action_type_code": "termination_code",
    "action_date": "termination_date",
    "federal_action_obligation": "federal_action_obligation",
    "total_dollars_obligated": "total_obligated",
    "potential_total_value_of_award": "ceiling",
    "recipient_name": "contractor",
    "recipient_parent_name": "contractor_parent",
    "awarding_toptier_agency_name": "department",
    "awarding_subtier_agency_name": "sub_agency",
    "awarding_office_name": "awarding_office",
    "naics_code": "naics",
    "product_or_service_code": "psc",
    "type_of_contract_pricing_code": "pricing",
    "type_set_aside_code": "set_aside",
    "primary_place_of_performance_state_code": "place_state",
    "usaspending_permalink": "link",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_db_connection(config: dict) -> sqlite3.Connection:
    db_path = Path(config["database"]["path"])
    if not db_path.exists():
        logger.error(
            f"Database not found at {db_path}. Run 'python -m pipeline.bootstrap' first."
        )
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Archive datestamp detection
# ---------------------------------------------------------------------------

def get_latest_datestamp(archive_base: str, fallback: str = "20260306") -> str:
    """
    Scrape the USASpending archive index to find the most recent bulk datestamp.
    Datestamp appears in filenames like: FY2026_097_Contracts_Full_20260306.zip
    """
    try:
        resp = requests.get(archive_base, timeout=15)
        resp.raise_for_status()
        dates = re.findall(r"Contracts_Full_(\d{8})\.zip", resp.text)
        if dates:
            latest = max(dates)
            logger.info(f"Latest USASpending archive datestamp: {latest}")
            return latest
    except Exception as e:
        logger.warning(f"Could not auto-detect datestamp ({e}), using fallback {fallback}")
    return fallback


def get_last_completed_datestamp(conn: sqlite3.Connection) -> str | None:
    """
    Check refresh_log for the datestamp of the last successful USASpending run.
    Returns None if no completed run exists.
    """
    row = conn.execute("""
        SELECT notes FROM refresh_log
        WHERE source = 'usaspending' AND status = 'completed'
        ORDER BY completed_at DESC
        LIMIT 1
    """).fetchone()

    if row and row["notes"]:
        # notes field stores JSON-ish context; look for datestamp pattern
        match = re.search(r"datestamp=(\d{8})", row["notes"] or "")
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Agency list
# ---------------------------------------------------------------------------

def get_agencies(agency_codes_url: str) -> dict[str, str]:
    """Fetch toptier agency codes from USASpending reference data."""
    resp = requests.get(agency_codes_url, timeout=30)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    agencies: dict[str, str] = {}
    for row in rows:
        code = row.get("CGAC AGENCY CODE", "").strip()
        if row.get("TOPTIER_FLAG", "").strip() == "TRUE" and code and code not in agencies:
            agencies[code] = row["AGENCY NAME"]
    logger.info(f"Loaded {len(agencies)} toptier agency codes")
    return agencies


# ---------------------------------------------------------------------------
# ZIP download (with retry)
# ---------------------------------------------------------------------------

def download_zip(url: str, max_retries: int = 3) -> str:
    """
    Download a ZIP to a temp file.
    Returns the temp file path, or a sentinel string (_NOT_FOUND, _IP_BLOCKED, _FAILED).
    """
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, stream=True, timeout=600)
            if resp.status_code == 404:
                return _NOT_FOUND
            if resp.status_code >= 500:
                return _IP_BLOCKED
            resp.raise_for_status()

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            downloaded = 0
            last_print = 0
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    mb = downloaded / 1024 / 1024
                    if mb - last_print >= 50:
                        logger.debug(f"  {mb:.0f}MB downloaded...")
                        last_print = mb
            tmp.close()
            return tmp.name

        except requests.exceptions.ConnectionError:
            return _IP_BLOCKED
        except Exception as e:
            wait = min(30 * (attempt + 1), 180)
            logger.warning(f"  Retry {attempt + 1}/{max_retries} in {wait}s ({e})")
            time.sleep(wait)
    return _FAILED


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def checkpoint_path(checkpoint_dir: Path, fy: int, code: str) -> Path:
    return checkpoint_dir / f"FY{fy}_{code}.csv"


def not_found_path(checkpoint_dir: Path, fy: int, code: str) -> Path:
    return checkpoint_dir / f"FY{fy}_{code}.not_found"


def is_done(checkpoint_dir: Path, fy: int, code: str) -> bool:
    return (
        checkpoint_path(checkpoint_dir, fy, code).exists()
        or not_found_path(checkpoint_dir, fy, code).exists()
    )


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------

def upsert_terminations(conn: sqlite3.Connection, rows: list[dict], fy: int) -> int:
    """
    Upsert termination rows into the DB.
    Uses INSERT OR IGNORE to avoid overwriting existing records.
    Returns count of rows inserted.
    """
    if not rows:
        return 0

    cursor = conn.cursor()
    inserted = 0
    for row in rows:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO terminations (
                    contract_award_unique_key, piid, mod_number,
                    termination_code, termination_reason, termination_date,
                    federal_action_obligation, total_obligated, ceiling,
                    contractor, contractor_parent, department, sub_agency,
                    awarding_office, naics, psc, pricing, set_aside,
                    place_state, fiscal_year, link
                ) VALUES (
                    :contract_award_unique_key, :piid, :mod_number,
                    :termination_code, :termination_reason, :termination_date,
                    :federal_action_obligation, :total_obligated, :ceiling,
                    :contractor, :contractor_parent, :department, :sub_agency,
                    :awarding_office, :naics, :psc, :pricing, :set_aside,
                    :place_state, :fiscal_year, :link
                )
            """, {**row, "fiscal_year": fy})
            if cursor.rowcount:
                inserted += 1
        except sqlite3.Error as e:
            logger.warning(f"Upsert error for {row.get('contract_award_unique_key')}: {e}")

    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Archive processing
# ---------------------------------------------------------------------------

def process_agency_fy(
    archive_base: str,
    datestamp: str,
    fy: int,
    agency_code: str,
    agency_name: str,
    checkpoint_dir: Path,
    termination_codes: set[str],
    termination_code_labels: dict[str, str],
    conn: sqlite3.Connection,
) -> tuple[int, int]:
    """
    Download and process one agency/FY ZIP.
    Returns (rows_scanned, rows_kept).
    """
    url = f"{archive_base}FY{fy}_{agency_code}_Contracts_Full_{datestamp}.zip"
    logger.info(f"  [{agency_code}] {agency_name[:40]:<40} FY{fy}...")

    resp = download_zip(url)

    if resp is _NOT_FOUND:
        logger.debug(f"  → 404 (no data for this agency/FY)")
        not_found_path(checkpoint_dir, fy, agency_code).touch()
        return 0, 0

    if resp is _IP_BLOCKED:
        logger.error("  → IP blocked. Stopping run.")
        raise RuntimeError("IP_BLOCKED")

    if resp is _FAILED:
        logger.warning("  → Download failed after retries. Will retry next run.")
        return 0, 0

    zip_path = resp
    zip_mb = os.path.getsize(zip_path) / 1024 / 1024
    logger.info(f"  → {zip_mb:.1f} MB | scanning...")

    rows_scanned = 0
    kept_rows: list[dict] = []
    cp = checkpoint_path(checkpoint_dir, fy, agency_code)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csv_names:
                logger.warning(f"  → No CSV in ZIP for {agency_code} FY{fy}")
                cp.touch()
                return 0, 0

            for csv_name in csv_names:
                with zf.open(csv_name) as raw:
                    reader = csv.DictReader(
                        io.TextIOWrapper(raw, encoding="utf-8-sig")
                    )
                    for row in reader:
                        rows_scanned += 1
                        action = (row.get("action_type_code") or "").strip().upper()
                        if action not in termination_codes:
                            continue

                        # Map to internal schema
                        mapped = {
                            internal: row.get(usa_col, "")
                            for usa_col, internal in USA_COLUMN_MAP.items()
                        }
                        mapped["termination_reason"] = termination_code_labels.get(
                            action, action
                        )

                        # Convert numeric fields
                        for num_col in ("federal_action_obligation", "total_obligated", "ceiling"):
                            try:
                                mapped[num_col] = float(mapped[num_col]) if mapped[num_col] else None
                            except (ValueError, TypeError):
                                mapped[num_col] = None

                        kept_rows.append(mapped)

        # Checkpoint the raw rows
        if kept_rows:
            with open(cp, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=kept_rows[0].keys())
                writer.writeheader()
                writer.writerows(kept_rows)

            # Upsert into DB
            inserted = upsert_terminations(conn, kept_rows, fy)
            logger.info(
                f"  → scanned {rows_scanned:,} | kept {len(kept_rows):,} | "
                f"new in DB {inserted:,}"
            )
        else:
            cp.touch()  # Mark done even if no terminations found

    except RuntimeError:
        raise  # Re-raise IP_BLOCKED
    except Exception as e:
        logger.error(f"  → Error processing {agency_code} FY{fy}: {e}")
        if cp.exists():
            cp.unlink()
        return rows_scanned, 0
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass

    return rows_scanned, len(kept_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download federal contract terminations from USASpending bulk archives"
    )
    parser.add_argument("--fy", nargs="+", type=int, help="Fiscal year(s) to process")
    parser.add_argument("--agencies", nargs="+", help="Specific agency CGAC codes")
    parser.add_argument("--force", action="store_true", help="Re-download all (ignore checkpoints)")
    parser.add_argument(
        "--force-current-fy", action="store_true", help="Re-download current FY only"
    )
    parser.add_argument(
        "--check-delta",
        action="store_true",
        help="Print archive datestamp info and exit (no download)",
    )
    args = parser.parse_args()

    config = load_config()
    usa_cfg = config["usaspending"]
    archive_base = usa_cfg["archive_base"]
    termination_codes = set(usa_cfg["termination_codes"].keys())
    termination_code_labels = usa_cfg["termination_codes"]
    fiscal_years = args.fy or usa_cfg["fiscal_years"]
    checkpoint_dir = Path(usa_cfg["checkpoint_dir"])
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db_connection(config)

    latest_datestamp = get_latest_datestamp(archive_base)

    if args.check_delta:
        last = get_last_completed_datestamp(conn)
        print(f"Latest archive datestamp : {latest_datestamp}")
        print(f"Last completed datestamp : {last or 'none'}")
        print(f"Delta available          : {'YES' if latest_datestamp != last else 'NO'}")
        conn.close()
        return

    last_datestamp = get_last_completed_datestamp(conn)
    if latest_datestamp == last_datestamp and not args.force and not args.force_current_fy:
        logger.info(
            f"No new data available (datestamp={latest_datestamp}). "
            "Use --force to re-run anyway."
        )
        conn.close()
        return

    agencies = get_agencies(usa_cfg["agency_codes_url"])
    if args.agencies:
        agencies = {c: agencies.get(c, f"Agency {c}") for c in args.agencies}

    if args.force:
        for fy in fiscal_years:
            for code in agencies:
                for p in [
                    checkpoint_path(checkpoint_dir, fy, code),
                    not_found_path(checkpoint_dir, fy, code),
                ]:
                    if p.exists():
                        p.unlink()

    if args.force_current_fy:
        current = max(fiscal_years)
        for code in agencies:
            for p in [
                checkpoint_path(checkpoint_dir, current, code),
                not_found_path(checkpoint_dir, current, code),
            ]:
                if p.exists():
                    p.unlink()

    run_id = str(uuid.uuid4())
    run_start = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO refresh_log (run_id, source, started_at, status, notes)
        VALUES (?, 'usaspending', ?, 'running', ?)
    """, (run_id, run_start, f"datestamp={latest_datestamp}"))
    conn.commit()

    total_scanned = 0
    total_kept = 0
    ip_blocked = False

    for fy in fiscal_years:
        if ip_blocked:
            break
        fy_todo = sum(1 for c in agencies if not is_done(checkpoint_dir, fy, c))
        fy_done = len(agencies) - fy_todo
        logger.info(f"\nFY{fy} — {fy_done} done, {fy_todo} to process")

        for code, name in agencies.items():
            if is_done(checkpoint_dir, fy, code):
                continue
            try:
                scanned, kept = process_agency_fy(
                    archive_base=archive_base,
                    datestamp=latest_datestamp,
                    fy=fy,
                    agency_code=code,
                    agency_name=name,
                    checkpoint_dir=checkpoint_dir,
                    termination_codes=termination_codes,
                    termination_code_labels=termination_code_labels,
                    conn=conn,
                )
                total_scanned += scanned
                total_kept += kept
            except RuntimeError as e:
                if "IP_BLOCKED" in str(e):
                    ip_blocked = True
                    break
                logger.error(f"Unexpected error for {code} FY{fy}: {e}")

    status = "failed" if ip_blocked else "completed"
    conn.execute("""
        UPDATE refresh_log
        SET completed_at = ?, status = ?, rows_processed = ?, rows_changed = ?
        WHERE run_id = ?
    """, (
        datetime.now(timezone.utc).isoformat(),
        status,
        total_scanned,
        total_kept,
        run_id,
    ))
    conn.commit()
    conn.close()

    if ip_blocked:
        logger.warning("Run stopped due to IP block. Progress is checkpointed — re-run to continue.")
    else:
        logger.success(
            f"Done. Scanned {total_scanned:,} rows, kept {total_kept:,} terminations."
        )


if __name__ == "__main__":
    main()
