"""
fetch_elib.py -- Daily eLibrary data fetch and change detection pipeline.

DATA MODEL NOTE:
    The MAS CSV is structured as one row per vendor+SIN combination.
    A single contract (contract_number) appears once per SIN it holds.
    Example: GS-03F-077CA has 8 rows -- one per SIN (238160, 314110, etc.)

    This pipeline:
      1. Deduplicates to one row per contract for mas_vendors
         (vendor-level fields: name, address, UEI, set-asides, dates)
      2. Extracts all SINs into mas_vendor_sins
         (SIN-level fields: sin, large_category, sub_category)

For each enabled contract vehicle:
  1. Downloads the CSV from GSA eLibrary
  2. Parses and normalizes
  3. Splits into vendor-level and SIN-level data
  4. Diffs against DB snapshot (detect_changes.py)
  5. Writes changes to mas_change_log, mas_vendors, mas_vendor_sins

Run:
    python -m pipeline.fetch_elib                  # all enabled vehicles
    python -m pipeline.fetch_elib --vehicle MAS    # single vehicle
    python -m pipeline.fetch_elib --discover       # discover + print all vehicle CSVs
"""

from __future__ import annotations

import argparse
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
# MAS CSV column -> internal field mapping
# Keys must match exact column headers from the live CSV (whitespace-sensitive).
# ---------------------------------------------------------------------------
MAS_COLUMN_MAP = {
    # Identity / categorization
    "Large Category": "large_category",
    "Sub Category": "sub_category",
    "Category": "sin",  # SIN number (e.g. 238160) -- NOT an alias for large_category
    "Source": "source",
    "Vendor": "vendor_name",
    "Contract #": "contract_number",
    "Closed for New Award": "closed_for_new_award",
    # Contact / location
    "Address 1": "address_1",
    "Address 2": "address_2",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Country": "country",
    "Phone": "phone",
    "Email": "email",
    "URL": "url",
    # Dates
    "Current Option Period End Date": "option_period_end_date",
    "Ultimate Contract End Date": "ultimate_contract_end_date",
    # SAM
    "SAM UEI": "sam_uei",
    # Set-aside / socioeconomic flags (verified column headers from live CSV)
    "Small \nBusiness - s": "small_business",
    "Other\nthan\n Small \nBusiness - o": "other_than_small_business",
    "Woman Owned - w": "woman_owned",
    "Women Owned (WOSB) - wo": "wosb",
    "Women Owned (EDWOSB) - ew": "edwosb",
    "Veteran Owned - v": "veteran_owned",
    "Service Disabled Veteran Owned - dv": "sdvosb",
    "Small Disadv - d": "small_disadvantaged",
    "8(a) - 8a": "eight_a",
    "8(a) Sole Souce Pool - 8aS": "eight_a_sole_source",
    "Hub \nZone - h": "hub_zone",
    "Tribally\nOwned\n Firm - to": "tribally_owned",
    "American\nIndian\nOwned - ai": "american_indian_owned",
    "Alaskan Native Corporation Owned Firm - an": "alaskan_native_corp",
    "Native Hawaiian Organization Owned firm - hn": "native_hawaiian_org",
    "8(a) Joint Venture Eligible - 8ajv": "eight_a_joint_venture",
    "Women Owned Joint Venture Eligible - wojv": "woman_owned_joint_venture",
    "Service Disabled Veteran Owned Joint Venture Eligible - dvjv": "sdvosb_joint_venture",
    "HUBZone Joint Venture Eligible - hjv": "hubzone_joint_venture",
    "State & Local - Coop \nPurch": "state_local_coop",
    "Disast Recov": "disaster_recovery",
}

# Fields that are vendor-level (same value across all rows for a given contract)
VENDOR_LEVEL_FIELDS = [
    "contract_number",
    "vendor_name",
    "source",
    "closed_for_new_award",
    "address_1",
    "address_2",
    "city",
    "state",
    "zip",
    "country",
    "phone",
    "email",
    "url",
    "option_period_end_date",
    "ultimate_contract_end_date",
    "sam_uei",
    "small_business",
    "other_than_small_business",
    "woman_owned",
    "wosb",
    "edwosb",
    "veteran_owned",
    "sdvosb",
    "small_disadvantaged",
    "eight_a",
    "eight_a_sole_source",
    "hub_zone",
    "tribally_owned",
    "american_indian_owned",
    "alaskan_native_corp",
    "native_hawaiian_org",
    "eight_a_joint_venture",
    "woman_owned_joint_venture",
    "sdvosb_joint_venture",
    "hubzone_joint_venture",
    "state_local_coop",
    "disaster_recovery",
    "large_category",  # pipe-separated summary of all categories for this contract
]

# SIN-level fields (one row per SIN per contract)
SIN_LEVEL_FIELDS = ["contract_number", "sin", "large_category", "sub_category"]

# Set-aside flag columns -- presence of any non-empty value -> 1, blank -> 0
SET_ASIDE_FIELDS = {
    "small_business",
    "other_than_small_business",
    "woman_owned",
    "wosb",
    "edwosb",
    "veteran_owned",
    "sdvosb",
    "small_disadvantaged",
    "eight_a",
    "eight_a_sole_source",
    "hub_zone",
    "tribally_owned",
    "american_indian_owned",
    "alaskan_native_corp",
    "native_hawaiian_org",
    "eight_a_joint_venture",
    "woman_owned_joint_venture",
    "sdvosb_joint_venture",
    "hubzone_joint_venture",
    "state_local_coop",
    "disaster_recovery",
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
            f"Database not found at {db_path}. "
            "Run 'python -m pipeline.bootstrap' first."
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
    Scrape the GSA eLibrary home page to discover all vehicle CSV links.
    Uses BeautifulSoup for robustness (XPath can break on minor page changes).
    Returns {vehicle_code: csv_url}.
    """
    logger.info(f"Discovering vehicle CSVs from: {home_url}")
    try:
        resp = requests.get(home_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch eLib home page: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    discovered: dict[str, str] = {}

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "elib_contracts" in href and href.endswith(".csv"):
            csv_url = (
                href
                if href.startswith("http")
                else f"https://gsaelibrary.gsa.gov{href}"
            )
            filename = href.split("/")[-1]
            vehicle_code = filename.replace("schedule_", "").replace(".csv", "").strip()
            discovered[vehicle_code] = csv_url

    logger.info(
        f"Discovered {len(discovered)} vehicle CSV links: {list(discovered.keys())}"
    )
    return discovered


# ---------------------------------------------------------------------------
# CSV download
# ---------------------------------------------------------------------------


def download_csv(url: str, encoding: str = "latin-1") -> pd.DataFrame | None:
    """
    Download a CSV and return a DataFrame.
    Tries latin-1 first (MAS CSV has cp1252 characters historically),
    then falls back to utf-8-sig and utf-8.
    """
    logger.info(f"Downloading: {url}")
    try:
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to download {url}: {e}")
        return None

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


# ---------------------------------------------------------------------------
# Normalization and splitting
# ---------------------------------------------------------------------------


def normalize_raw_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rename columns per MAS_COLUMN_MAP, normalize set-aside flags to 0/1,
    and strip whitespace. Returns a DataFrame with all mapped columns.
    Retains one row per vendor+SIN (do NOT deduplicate here).
    """
    # Strip whitespace from column names
    df.columns = [c.strip() for c in df.columns]

    # Rename only the columns we care about
    rename_map = {k: v for k, v in MAS_COLUMN_MAP.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    # Keep only mapped columns that exist after renaming
    # Use a list to preserve order and handle any remaining duplicates
    seen, keep_cols = set(), []
    for v in MAS_COLUMN_MAP.values():
        if v in df.columns and v not in seen:
            keep_cols.append(v)
            seen.add(v)
    df = df[keep_cols].copy()

    # Strip all string values
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].str.strip()

    # Normalize set-aside flags: any non-empty, non-NaN value -> 1, else 0
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


def split_vendor_sins(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, set[tuple[str, str, str]]]]:
    """
    Split the normalized (vendor+SIN) DataFrame into:
      1. vendor_df  -- one row per contract_number (vendor-level fields)
      2. sin_map    -- {contract_number: {(sin, large_category, sub_category), ...}}

    vendor_df.large_category is a pipe-separated summary of all unique
    large categories for that contract (useful for UI filtering).
    """
    # Build SIN map using vectorized Pandas operations (no iterrows)
    sin_map: dict[str, set[tuple[str, str, str]]] = {}
    if "sin" in df.columns:
        sin_df = df[["contract_number", "sin", "large_category", "sub_category"]].copy()
        sin_df["sin"] = sin_df["sin"].fillna("").astype(str).str.strip()
        sin_df["large_category"] = sin_df["large_category"].fillna("").astype(str).str.strip()
        sin_df["sub_category"] = sin_df["sub_category"].fillna("").astype(str).str.strip()
        # Filter to rows with non-empty contract_number and sin
        mask = sin_df["contract_number"].fillna("").astype(bool) & sin_df["sin"].astype(bool)
        sin_df = sin_df[mask]
        for cn, group in sin_df.groupby("contract_number"):
            sin_map[cn] = set(
                zip(group["sin"], group["large_category"], group["sub_category"])
            )

    # Build vendor-level category summary
    if "sin" in df.columns and "large_category" in df.columns:
        cat_summary = (
            df.groupby("contract_number")["large_category"]
            .apply(lambda x: " | ".join(sorted(x.dropna().unique())))
            .reset_index()
            .rename(columns={"large_category": "large_category"})
        )
    else:
        cat_summary = None

    # Deduplicate to one row per contract (take first occurrence for all vendor fields)
    vendor_fields = [
        f for f in VENDOR_LEVEL_FIELDS if f in df.columns and f != "large_category"
    ]
    vendor_df = df.drop_duplicates(subset=["contract_number"], keep="first")[
        vendor_fields
    ].copy()

    # Attach category summary
    if cat_summary is not None:
        vendor_df = vendor_df.merge(cat_summary, on="contract_number", how="left")

    vendor_df = vendor_df.set_index("contract_number")
    return vendor_df, sin_map


# ---------------------------------------------------------------------------
# DB read helpers
# ---------------------------------------------------------------------------


def load_db_snapshot(
    conn: sqlite3.Connection, vehicle_id: int
) -> tuple[pd.DataFrame, dict[str, set[tuple[str, str, str]]]]:
    """
    Load the current vendor snapshot and SINs for a vehicle from the DB.
    Returns (vendor_df indexed by contract_number,
             {contract_number: {(sin, large_category, sub_category), ...}})
    """
    vendor_rows = conn.execute(
        "SELECT * FROM mas_vendors WHERE vehicle_id = ? AND status = 'active'",
        (vehicle_id,),
    ).fetchall()

    if not vendor_rows:
        return pd.DataFrame(), {}

    vendor_df = pd.DataFrame([dict(r) for r in vendor_rows]).set_index(
        "contract_number"
    )

    sin_rows = conn.execute(
        """SELECT contract_number, sin, large_category, sub_category
           FROM mas_vendor_sins
           WHERE vehicle_id = ? AND active = 1""",
        (vehicle_id,),
    ).fetchall()

    db_sins: dict[str, set[tuple[str, str, str]]] = {}
    for row in sin_rows:
        cn = row["contract_number"]
        db_sins.setdefault(cn, set()).add(
            (
                row["sin"] or "",
                row["large_category"] or "",
                row["sub_category"] or "",
            )
        )

    return vendor_df, db_sins


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------


def write_changes(
    conn: sqlite3.Connection,
    changes: list[ChangeRecord],
    vehicle_id: int,
    vendor_df: pd.DataFrame,
    sin_map: dict[str, set[tuple[str, str, str]]],
) -> None:
    """
    Apply ChangeRecords to the database:
      - ADD: insert new vendor + SINs
      - REMOVE: soft-delete vendor, deactivate SINs
      - FIELD_UPDATE: update specific field
      - SIN_ADD / SIN_REMOVE: update mas_vendor_sins
    Log all changes to mas_change_log.
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
        if cn not in vendor_df.index:
            continue
        row = vendor_df.loc[cn].to_dict()
        row["contract_number"] = cn
        row["vehicle_id"] = vehicle_id
        row["status"] = "active"
        row["first_seen_at"] = now
        row["last_seen_at"] = now
        # Exclude computed/internal fields that aren't real columns
        exclude = {"id"}
        cols = [k for k in row if k not in exclude]
        placeholders = ",".join("?" for _ in cols)
        col_str = ",".join(cols)
        try:
            cursor.execute(
                f"INSERT OR REPLACE INTO mas_vendors ({col_str}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
        except sqlite3.Error as e:
            logger.warning(f"Insert failed for {cn}: {e}")

        # Insert SINs for new vendor
        for sin, lc, sc in sin_map.get(cn, set()):
            cursor.execute(
                """INSERT INTO mas_vendor_sins
                   (contract_number, vehicle_id, sin, large_category, sub_category, active, added_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(contract_number, vehicle_id, sin)
                   DO UPDATE SET active=1, large_category=excluded.large_category,
                                 sub_category=excluded.sub_category, removed_at=NULL""",
                (cn, vehicle_id, sin, lc, sc, now),
            )

    # --- SOFT DELETE removed vendors ---
    for cn in remove_contracts:
        cursor.execute(
            """UPDATE mas_vendors
               SET status='removed', removed_at=?, last_seen_at=?
               WHERE contract_number=? AND vehicle_id=?""",
            (now, now, cn, vehicle_id),
        )
        cursor.execute(
            """UPDATE mas_vendor_sins SET active=0, removed_at=?
               WHERE contract_number=? AND vehicle_id=?""",
            (now, cn, vehicle_id),
        )

    # --- FIELD UPDATES ---
    for change in field_updates:
        try:
            cursor.execute(
                f"""UPDATE mas_vendors SET {change.field_name}=?, last_seen_at=?
                    WHERE contract_number=? AND vehicle_id=?""",
                (change.new_value, now, change.contract_number, vehicle_id),
            )
        except sqlite3.OperationalError as e:
            logger.warning(f"Field update failed ({change.field_name}): {e}")

    # Update last_seen_at for all active contracts still in the CSV
    cursor.executemany(
        """UPDATE mas_vendors SET last_seen_at=?
           WHERE contract_number=? AND vehicle_id=? AND status='active'""",
        [(now, cn, vehicle_id) for cn in vendor_df.index],
    )

    # --- SIN ADDS ---
    for change in sin_adds:
        # new_value is "sin|large_category|sub_category"
        parts = (change.new_value or "||").split("|", 2)
        sin = parts[0] if len(parts) > 0 else ""
        lc = parts[1] if len(parts) > 1 else ""
        sc = parts[2] if len(parts) > 2 else ""
        cursor.execute(
            """INSERT INTO mas_vendor_sins
               (contract_number, vehicle_id, sin, large_category, sub_category, active, added_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(contract_number, vehicle_id, sin)
               DO UPDATE SET active=1, large_category=excluded.large_category,
                             sub_category=excluded.sub_category, removed_at=NULL""",
            (change.contract_number, vehicle_id, sin, lc, sc, now),
        )
    # --- SIN REMOVES ---
    for change in sin_removes:
        parts = (change.old_value or "|").split("|", 2)
        sin = parts[0] if len(parts) > 0 else ""
        cursor.execute(
            """UPDATE mas_vendor_sins SET active=0, removed_at=?
               WHERE contract_number=? AND vehicle_id=? AND sin=?""",
            (now, change.contract_number, vehicle_id, sin),
        )

    # --- Write change log ---
    cursor.executemany(
        """INSERT INTO mas_change_log
           (run_id, contract_number, vehicle_id, change_type,
            field_name, old_value, new_value, levenshtein_distance, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                c.run_id,
                c.contract_number,
                c.vehicle_id,
                c.change_type,
                c.field_name,
                c.old_value,
                c.new_value,
                c.levenshtein_distance,
                now,
            )
            for c in changes
        ],
    )

    conn.commit()
    logger.info(
        f"DB updated | +{len(add_contracts)} added | -{len(remove_contracts)} removed | "
        f"{len(field_updates)} field updates | "
        f"{len(sin_adds)} SIN adds | {len(sin_removes)} SIN removes"
    )


def build_sin_sets(
    sin_map: dict[str, set[tuple[str, str, str]]],
) -> dict[str, set[str]]:
    """Convert {cn: {(sin, lc, sc)}} to {cn: {sin|lc|sc}} for opaque diffing."""
    return {
        cn: {f"{sin}|{lc}|{sc}" for sin, lc, sc in sins} for cn, sins in sin_map.items()
    }


def process_vehicle(
    conn: sqlite3.Connection,
    config: dict,
    vehicle_code: str,
    vehicle_attrs: dict,
    vehicle_id: int,
) -> dict:
    """Full pipeline for one vehicle: download -> normalize -> diff -> write."""
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    change_cfg = config["change_detection"]

    conn.execute(
        "INSERT INTO refresh_log (run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
        (run_id, f"elib_{vehicle_code.lower()}", now),
    )
    conn.commit()

    try:
        csv_url = vehicle_attrs.get("csv_url")
        if not csv_url:
            logger.warning(f"No csv_url configured for {vehicle_code} -- skipping.")
            return {"status": "skipped"}

        raw_df = download_csv(csv_url)
        if raw_df is None:
            raise RuntimeError(f"Failed to download CSV for {vehicle_code}")

        normalized = normalize_raw_df(raw_df)
        vendor_df, csv_sin_map = split_vendor_sins(normalized)

        logger.info(
            f"{vehicle_code}: {len(vendor_df):,} unique contracts, "
            f"{sum(len(v) for v in csv_sin_map.values()):,} total SINs"
        )

        db_vendor_df, db_sin_map = load_db_snapshot(conn, vehicle_id)
        csv_sin_sets = build_sin_sets(csv_sin_map)
        db_sin_sets = build_sin_sets(db_sin_map)

        changes = diff_vendor_snapshot(
            run_id=run_id,
            vehicle_id=vehicle_id,
            db_df=db_vendor_df,
            csv_df=vendor_df,
            db_sins=db_sin_sets,
            csv_sins=csv_sin_sets,
            high_priority_fields=change_cfg["high_priority_fields"],
            fuzzy_fields=change_cfg["fuzzy_fields"],
            levenshtein_threshold=change_cfg["levenshtein_threshold"],
            levenshtein_ratio_threshold=change_cfg["levenshtein_ratio_threshold"],
        )

        write_changes(conn, changes, vehicle_id, vendor_df, csv_sin_map)

        conn.execute(
            """UPDATE refresh_log
               SET completed_at=?, rows_processed=?, rows_changed=?, status='completed'
               WHERE run_id=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                len(vendor_df),
                len(changes),
                run_id,
            ),
        )
        conn.commit()
        return {
            "status": "completed",
            "rows_processed": len(vendor_df),
            "rows_changed": len(changes),
        }

    except Exception as e:
        logger.exception(f"Pipeline failed for {vehicle_code}: {e}")
        conn.execute(
            "UPDATE refresh_log SET completed_at=?, status='failed', notes=? WHERE run_id=?",
            (datetime.now(timezone.utc).isoformat(), str(e), run_id),
        )
        conn.commit()
        return {"status": "failed", "error": str(e)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and diff eLibrary contract data"
    )
    parser.add_argument("--vehicle", help="Process only this vehicle code (e.g. MAS)")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover all vehicle CSV links from eLib home page and exit.",
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
            row = conn.execute(
                "SELECT id FROM contract_vehicles WHERE code=?", (code,)
            ).fetchone()
            if not row:
                logger.error(
                    f"Vehicle '{code}' not in contract_vehicles table. "
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
