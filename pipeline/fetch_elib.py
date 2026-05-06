"""
fetch_elib.py — Daily eLibrary data fetch and change detection pipeline.

For each enabled contract vehicle:
  1. Downloads the CSV from GSA eLibrary
  2. Parses into a normalized DataFrame
  3. Diffs against the current DB snapshot (detect_changes.py)
  4. Writes changes to mas_change_log
  5. Updates the mas_vendors and mas_vendor_sins tables

Also performs dynamic vehicle discovery by scraping the GSA eLibrary home
page table to find all available contract vehicle CSV links.

Run:
    python -m pipeline.fetch_elib                  # all enabled vehicles
    python -m pipeline.fetch_elib --vehicle MAS    # single vehicle
    python -m pipeline.fetch_elib --discover       # discover + print all vehicle CSVs
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup
from loguru import logger

from pipeline.detect_changes import ChangeRecord, diff_vendor_snapshot

CONFIG_PATH = Path("config/config.yaml")

# ---------------------------------------------------------------------------
# MAS CSV column → internal DB field mapping
# Adjust if GSA changes column headers in the source CSV.
# ---------------------------------------------------------------------------
MAS_COLUMN_MAP = {
    "Large Category": "large_category",
    "Sub Category": "sub_category",
    "Source": "source",
    "Category": "large_category",          # fallback alias
    "Vendor": "vendor_name",
    "Contract #": "contract_number",
    "Closed for New Award": "closed_for_new_award",
    "Address 1": "address_1",
    "Address 2": "address_2",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Country": "country",
    "Phone": "phone",
    "Email": "email",
    "URL": "url",
    "Current Option Period End Date": "option_period_end_date",
    "Ultimate Contract End Date": "ultimate_contract_end_date",
    "SAM UEI": "sam_uei",
    # Set-aside flag columns (values: 's', 'o', 'w', etc. or blank)
    "Small\n Business - s": "small_business",
    "Other\nthan\n Small \nBusiness - o": "other_than_small_business",
    "Woman Owned - w": "woman_owned",
    "Women Owned (WOSB) - wo": "wosb",
    "Women Owned (EDWOSB) - ew": "edwosb",
    "Veteran Owned - v": "veteran_owned",
    "Service Disabled Veteran Owned - dv": "sdvosb",
    "Small Disadv - d": "small_disadvantaged",
    "8(a) - 8a": "eight_a",
    "8(a) Sole Souce Pool - 8aS": "eight_a_sole_source",
    "Hub \nZone - hz": "hub_zone",
}

# Set-aside flag columns — presence of any non-empty value = 1
SET_ASIDE_FIELDS = {
    "small_business", "other_than_small_business", "woman_owned", "wosb",
    "edwosb", "veteran_owned", "sdvosb", "small_disadvantaged",
    "eight_a", "eight_a_sole_source", "hub_zone",
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
# Vehicle discovery
# ---------------------------------------------------------------------------

def discover_vehicle_csvs(home_url: str, xpath_hint: str) -> dict[str, str]:
    """
    Scrape the GSA eLibrary home page to discover all vehicle CSV download links.

    Returns a dict of {vehicle_name_or_code: csv_url}.
    The xpath_hint is used to locate the correct table region; we use
    BeautifulSoup for robustness since XPath can break on minor page changes.
    """
    logger.info(f"Discovering vehicle CSVs from: {home_url}")

    try:
        resp = requests.get(home_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch eLib home page: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    discovered: dict[str, str] = {}

    # Find all links that point to CSV files under /elib_contracts/
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "elib_contracts" in href and href.endswith(".csv"):
            # Normalize to absolute URL
            if href.startswith("http"):
                csv_url = href
            else:
                csv_url = f"https://gsaelibrary.gsa.gov{href}"

            # Extract vehicle name from filename: schedule_MAS.csv → MAS
            filename = href.split("/")[-1]
            vehicle_code = filename.replace("schedule_", "").replace(".csv", "").strip()
            discovered[vehicle_code] = csv_url

    logger.info(f"Discovered {len(discovered)} vehicle CSV links: {list(discovered.keys())}")
    return discovered


# ---------------------------------------------------------------------------
# CSV download and parsing
# ---------------------------------------------------------------------------

def download_csv(url: str, encoding: str = "latin-1") -> pd.DataFrame | None:
    """
    Download a CSV from the given URL and return a DataFrame.

    Uses latin-1 encoding by default — the MAS CSV has historically had
    cp1252/latin-1 characters. Falls back to utf-8-sig if latin-1 fails.
    """
    logger.info(f"Downloading: {url}")
    try:
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to download {url}: {e}")
        return None

    # Try the specified encoding first, then utf-8-sig fallback
    for enc in [encoding, "utf-8-sig", "utf-8"]:
        try:
            text = resp.content.decode(enc)
            df = pd.read_csv(io.StringIO(text), dtype=str, low_memory=False)
            logger.info(f"Parsed {len(df):,} rows using {enc} encoding.")
            return df
        except (UnicodeDecodeError, Exception):
            continue

    logger.error(f"Could not decode CSV from {url} with any known encoding.")
    return None


def normalize_mas_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rename columns per MAS_COLUMN_MAP, normalize set-aside flags to 0/1,
    and strip whitespace from all string fields.
    """
    # Strip whitespace from column names (GSA CSVs often have trailing spaces)
    df.columns = [c.strip() for c in df.columns]

    # Rename to internal field names — skip columns not in our map
    rename_map = {k: v for k, v in MAS_COLUMN_MAP.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    # Keep only mapped columns that exist
    keep_cols = [v for v in MAS_COLUMN_MAP.values() if v in df.columns]
    df = df[keep_cols].copy()

    # Strip all string values
    str_cols = df.select_dtypes(include="object").columns
    df[str_cols] = df[str_cols].apply(lambda s: s.str.strip())

    # Normalize set-aside flags: any non-empty, non-NaN value → 1, else 0
    for col in SET_ASIDE_FIELDS:
        if col in df.columns:
            df[col] = df[col].notna() & (df[col] != "")
            df[col] = df[col].astype(int)
        else:
            df[col] = 0

    # Drop rows with no contract number
    if "contract_number" in df.columns:
        df = df.dropna(subset=["contract_number"])
        df = df[df["contract_number"].str.len() > 0]

    return df


# ---------------------------------------------------------------------------
# DB read helpers
# ---------------------------------------------------------------------------

def load_db_snapshot(
    conn: sqlite3.Connection, vehicle_id: int
) -> tuple[pd.DataFrame, dict[str, set[str]]]:
    """
    Load the current vendor snapshot and SINs for a vehicle from the DB.

    Returns:
        (vendor_df indexed by contract_number, {contract_number: {sin, ...}})
    """
    vendor_rows = conn.execute("""
        SELECT * FROM mas_vendors WHERE vehicle_id = ? AND status = 'active'
    """, (vehicle_id,)).fetchall()

    if not vendor_rows:
        return pd.DataFrame(), {}

    vendor_df = pd.DataFrame(
        [dict(r) for r in vendor_rows]
    ).set_index("contract_number")

    sin_rows = conn.execute("""
        SELECT contract_number, sin FROM mas_vendor_sins
        WHERE vehicle_id = ? AND active = 1
    """, (vehicle_id,)).fetchall()

    db_sins: dict[str, set[str]] = {}
    for row in sin_rows:
        db_sins.setdefault(row["contract_number"], set()).add(row["sin"])

    return vendor_df, db_sins


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def write_changes(
    conn: sqlite3.Connection,
    changes: list[ChangeRecord],
    vehicle_id: int,
    csv_df: pd.DataFrame,
    csv_sins: dict[str, set[str]],
) -> None:
    """
    Apply a list of ChangeRecords to the database:
      - Insert ADD rows into mas_vendors
      - Soft-delete REMOVE rows (status = 'removed')
      - Apply FIELD_UPDATE changes
      - Update mas_vendor_sins for SIN_ADD / SIN_REMOVE
      - Insert all records into mas_change_log
    """
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    add_contracts = {c.contract_number for c in changes if c.change_type == "ADD"}
    remove_contracts = {c.contract_number for c in changes if c.change_type == "REMOVE"}
    field_updates = [c for c in changes if c.change_type == "FIELD_UPDATE"]
    sin_adds = [c for c in changes if c.change_type == "SIN_ADD"]
    sin_removes = [c for c in changes if c.change_type == "SIN_REMOVE"]

    # --- INSERT new vendors ---
    for cn in add_contracts:
        if cn not in csv_df.index:
            continue
        row = csv_df.loc[cn].to_dict()
        row["contract_number"] = cn
        row["vehicle_id"] = vehicle_id
        row["status"] = "active"
        row["first_seen_at"] = now
        row["last_seen_at"] = now

        cols = [k for k in row if k not in ("id",)]
        placeholders = ",".join("?" for _ in cols)
        col_str = ",".join(cols)
        cursor.execute(
            f"INSERT OR REPLACE INTO mas_vendors ({col_str}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )

    # --- SOFT DELETE removed vendors ---
    for cn in remove_contracts:
        cursor.execute("""
            UPDATE mas_vendors
            SET status = 'removed', removed_at = ?, last_seen_at = ?
            WHERE contract_number = ? AND vehicle_id = ?
        """, (now, now, cn, vehicle_id))

    # --- FIELD UPDATES ---
    for change in field_updates:
        cursor.execute(f"""
            UPDATE mas_vendors
            SET {change.field_name} = ?, last_seen_at = ?
            WHERE contract_number = ? AND vehicle_id = ?
        """, (change.new_value, now, change.contract_number, vehicle_id))

    # Update last_seen_at for all contracts still present
    all_present = set(csv_df.index)
    cursor.executemany("""
        UPDATE mas_vendors
        SET last_seen_at = ?
        WHERE contract_number = ? AND vehicle_id = ? AND status = 'active'
    """, [(now, cn, vehicle_id) for cn in all_present])

    # --- SIN ADDS ---
    for change in sin_adds:
        cursor.execute("""
            INSERT INTO mas_vendor_sins (contract_number, vehicle_id, sin, active, added_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(contract_number, vehicle_id, sin)
            DO UPDATE SET active = 1, removed_at = NULL
        """, (change.contract_number, vehicle_id, change.new_value, now))

    # --- SIN REMOVES ---
    for change in sin_removes:
        cursor.execute("""
            UPDATE mas_vendor_sins
            SET active = 0, removed_at = ?
            WHERE contract_number = ? AND vehicle_id = ? AND sin = ?
        """, (now, change.contract_number, vehicle_id, change.old_value))

    # --- Write to mas_change_log ---
    cursor.executemany("""
        INSERT INTO mas_change_log
            (run_id, contract_number, vehicle_id, change_type,
             field_name, old_value, new_value, levenshtein_distance, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            c.run_id, c.contract_number, c.vehicle_id, c.change_type,
            c.field_name, c.old_value, c.new_value, c.levenshtein_distance, now,
        )
        for c in changes
    ])

    conn.commit()
    logger.info(
        f"DB updated: +{len(add_contracts)} added, "
        f"-{len(remove_contracts)} removed, "
        f"{len(field_updates)} field updates, "
        f"{len(sin_adds)} SIN adds, {len(sin_removes)} SIN removes."
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_vehicle(
    conn: sqlite3.Connection,
    config: dict,
    vehicle_code: str,
    vehicle_attrs: dict,
    vehicle_id: int,
) -> dict:
    """
    Full pipeline for a single vehicle: download → normalize → diff → write.
    Returns a summary dict for the refresh_log.
    """
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    change_cfg = config["change_detection"]

    # Log run start
    conn.execute("""
        INSERT INTO refresh_log (run_id, source, started_at, status)
        VALUES (?, ?, ?, 'running')
    """, (run_id, f"elib_{vehicle_code.lower()}", now))
    conn.commit()

    try:
        csv_url = vehicle_attrs.get("csv_url")
        if not csv_url:
            logger.warning(f"No csv_url configured for {vehicle_code} — skipping.")
            return {"status": "skipped", "rows_processed": 0, "rows_changed": 0}

        # Download and normalize
        raw_df = download_csv(csv_url)
        if raw_df is None:
            raise RuntimeError(f"Failed to download CSV for {vehicle_code}")

        csv_df = normalize_mas_df(raw_df)

        # For now: SINs assumed to be a single column or semi-colon delimited field
        # Adjust when BPA structure is confirmed
        csv_sins: dict[str, set[str]] = {}
        if "sin" in csv_df.columns:
            for cn, sin_str in zip(csv_df.get("contract_number", []), csv_df.get("sin", [])):
                if pd.notna(sin_str) and sin_str:
                    csv_sins[cn] = {s.strip() for s in str(sin_str).split(";") if s.strip()}

        # Index by contract_number for diff
        if "contract_number" not in csv_df.columns:
            raise RuntimeError(f"contract_number column missing in {vehicle_code} CSV")

        csv_df = csv_df.set_index("contract_number")

        # Load current DB snapshot
        db_df, db_sins = load_db_snapshot(conn, vehicle_id)

        # Diff
        changes = diff_vendor_snapshot(
            run_id=run_id,
            vehicle_id=vehicle_id,
            db_df=db_df,
            csv_df=csv_df,
            db_sins=db_sins,
            csv_sins=csv_sins,
            high_priority_fields=change_cfg["high_priority_fields"],
            fuzzy_fields=change_cfg["fuzzy_fields"],
            levenshtein_threshold=change_cfg["levenshtein_threshold"],
            levenshtein_ratio_threshold=change_cfg["levenshtein_ratio_threshold"],
        )

        # Write changes to DB
        write_changes(conn, changes, vehicle_id, csv_df, csv_sins)

        # Update refresh_log
        conn.execute("""
            UPDATE refresh_log
            SET completed_at = ?, rows_processed = ?, rows_changed = ?, status = 'completed'
            WHERE run_id = ?
        """, (datetime.now(timezone.utc).isoformat(), len(csv_df), len(changes), run_id))
        conn.commit()

        return {
            "status": "completed",
            "rows_processed": len(csv_df),
            "rows_changed": len(changes),
        }

    except Exception as e:
        logger.exception(f"Pipeline failed for {vehicle_code}: {e}")
        conn.execute("""
            UPDATE refresh_log
            SET completed_at = ?, status = 'failed', notes = ?
            WHERE run_id = ?
        """, (datetime.now(timezone.utc).isoformat(), str(e), run_id))
        conn.commit()
        return {"status": "failed", "error": str(e)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and diff eLibrary contract data")
    parser.add_argument(
        "--vehicle", help="Process only this vehicle code (e.g. MAS, BPA)"
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover all available vehicle CSV links from the eLib home page and exit.",
    )
    args = parser.parse_args()

    config = load_config()

    if args.discover:
        elib_cfg = config["elib"]
        discovered = discover_vehicle_csvs(
            elib_cfg["home_url"], elib_cfg["vehicle_table_xpath"]
        )
        for code, url in discovered.items():
            print(f"  {code:<20} {url}")
        return

    conn = get_db_connection(config)
    vehicles = config["elib"]["vehicles"]

    try:
        for code, attrs in vehicles.items():
            if args.vehicle and code != args.vehicle:
                continue
            if not attrs.get("enabled", False):
                logger.info(f"Skipping {code} (disabled in config)")
                continue

            # Look up vehicle_id from DB
            row = conn.execute(
                "SELECT id FROM contract_vehicles WHERE code = ?", (code,)
            ).fetchone()
            if not row:
                logger.error(
                    f"Vehicle '{code}' not found in contract_vehicles table. "
                    "Run 'python -m pipeline.bootstrap' to seed it."
                )
                continue

            vehicle_id = row["id"]
            logger.info(f"Processing vehicle: {code} (id={vehicle_id})")
            result = process_vehicle(conn, config, code, attrs, vehicle_id)
            logger.info(f"Result for {code}: {result}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
