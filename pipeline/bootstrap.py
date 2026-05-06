"""
bootstrap.py -- First-run database setup for gsa-contract-tracker.

Creates the SQLite database and all tables. Safe to re-run: uses
CREATE TABLE IF NOT EXISTS throughout. Also seeds the contract_vehicles
lookup table from config.yaml.

Run:
    python -m pipeline.bootstrap
    python -m pipeline.bootstrap --reset   # drops and recreates all tables (destructive!)
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml
from loguru import logger

CONFIG_PATH = Path("config/config.yaml")
SCHEMA_VERSION = 1  # increment when making breaking schema changes


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error(f"Config file not found: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_db_path(config: dict) -> Path:
    db_path = Path(config["database"]["path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def create_tables(conn: sqlite3.Connection) -> None:
    """Create all tables. Safe to call on an existing database."""
    cursor = conn.cursor()

    # Enable WAL mode for better concurrent read performance
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")

    # -------------------------------------------------------------------------
    # schema_version
    # -------------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # -------------------------------------------------------------------------
    # contract_vehicles -- registry of all supported GSA contract vehicles.
    # Adding a new vehicle here (plus config.yaml) enables it project-wide.
    # -------------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contract_vehicles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    UNIQUE NOT NULL,
            name        TEXT    NOT NULL,
            csv_url     TEXT,
            enabled     INTEGER DEFAULT 1,
            notes       TEXT,
            created_at  TEXT    DEFAULT (datetime('now')),
            updated_at  TEXT    DEFAULT (datetime('now'))
        )
    """)

    # -------------------------------------------------------------------------
    # mas_vendors -- current snapshot of active vendors for each vehicle.
    # Rebuilt daily from eLibrary CSVs; removals are soft-deleted (status).
    # -------------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mas_vendors (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_number             TEXT    NOT NULL,
            vehicle_id                  INTEGER NOT NULL REFERENCES contract_vehicles(id),
            vendor_name                 TEXT,
            large_category              TEXT,
            sub_category                TEXT,
            source                      TEXT,
            closed_for_new_award        TEXT,
            address_1                   TEXT,
            address_2                   TEXT,
            city                        TEXT,
            state                       TEXT,
            zip                         TEXT,
            country                     TEXT,
            phone                       TEXT,
            email                       TEXT,
            url                         TEXT,
            option_period_end_date      TEXT,
            ultimate_contract_end_date  TEXT,
            sam_uei                     TEXT,
            -- Set-aside / socioeconomic flags (0/1, verified from live MAS CSV)
            small_business              INTEGER DEFAULT 0,
            other_than_small_business   INTEGER DEFAULT 0,
            woman_owned                 INTEGER DEFAULT 0,
            wosb                        INTEGER DEFAULT 0,
            edwosb                      INTEGER DEFAULT 0,
            veteran_owned               INTEGER DEFAULT 0,
            sdvosb                      INTEGER DEFAULT 0,
            small_disadvantaged         INTEGER DEFAULT 0,
            eight_a                     INTEGER DEFAULT 0,
            eight_a_sole_source         INTEGER DEFAULT 0,
            hub_zone                    INTEGER DEFAULT 0,
            tribally_owned              INTEGER DEFAULT 0,
            american_indian_owned       INTEGER DEFAULT 0,
            alaskan_native_corp         INTEGER DEFAULT 0,
            native_hawaiian_org         INTEGER DEFAULT 0,
            eight_a_joint_venture       INTEGER DEFAULT 0,
            woman_owned_joint_venture   INTEGER DEFAULT 0,
            sdvosb_joint_venture        INTEGER DEFAULT 0,
            hubzone_joint_venture       INTEGER DEFAULT 0,
            state_local_coop            INTEGER DEFAULT 0,
            disaster_recovery           INTEGER DEFAULT 0,
            -- Lifecycle tracking
            status                      TEXT    DEFAULT 'active',
            first_seen_at               TEXT    DEFAULT (datetime('now')),
            last_seen_at                TEXT    DEFAULT (datetime('now')),
            removed_at                  TEXT,
            UNIQUE(contract_number, vehicle_id)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mas_vendors_contract
            ON mas_vendors(contract_number)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mas_vendors_status
            ON mas_vendors(status)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mas_vendors_vehicle
            ON mas_vendors(vehicle_id)
    """)

    # -------------------------------------------------------------------------
    # mas_vendor_sins -- SINs are many-per-vendor; stored separately.
    # SIN changes are high-priority and tracked independently of field diffs.
    # -------------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mas_vendor_sins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_number TEXT    NOT NULL,
            vehicle_id      INTEGER NOT NULL REFERENCES contract_vehicles(id),
            sin             TEXT    NOT NULL,
            large_category  TEXT,
            sub_category    TEXT,
            active          INTEGER DEFAULT 1,
            added_at        TEXT    DEFAULT (datetime('now')),
            removed_at      TEXT,
            UNIQUE(contract_number, vehicle_id, sin)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sins_contract
            ON mas_vendor_sins(contract_number)
    """)

    # -------------------------------------------------------------------------
    # terminations -- termination modifications from USASpending bulk archives.
    # One row per termination modification (a contract can have multiple).
    # -------------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS terminations (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_award_unique_key   TEXT    UNIQUE NOT NULL,
            piid                        TEXT,
            mod_number                  TEXT,
            termination_code            TEXT    NOT NULL,
            termination_reason          TEXT,
            termination_date            TEXT,
            federal_action_obligation   REAL,
            total_obligated             REAL,
            ceiling                     REAL,
            contractor                  TEXT,
            contractor_parent           TEXT,
            department                  TEXT,
            sub_agency                  TEXT,
            awarding_office             TEXT,
            naics                       TEXT,
            psc                         TEXT,
            pricing                     TEXT,
            set_aside                   TEXT,
            place_state                 TEXT,
            fiscal_year                 INTEGER,
            link                        TEXT,
            loaded_at                   TEXT    DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_terminations_piid
            ON terminations(piid)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_terminations_date
            ON terminations(termination_date)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_terminations_code
            ON terminations(termination_code)
    """)

    # -------------------------------------------------------------------------
    # mas_change_log -- audit trail for all detected eLibrary changes.
    # Levenshtein distance stored so thresholds can be tuned post-hoc.
    # Valid change_type values: ADD, REMOVE, SIN_ADD, SIN_REMOVE, FIELD_UPDATE
    # -------------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mas_change_log (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id               TEXT    NOT NULL,
            contract_number      TEXT    NOT NULL,
            vehicle_id           INTEGER NOT NULL REFERENCES contract_vehicles(id),
            change_type          TEXT    NOT NULL,
            field_name           TEXT,
            old_value            TEXT,
            new_value            TEXT,
            levenshtein_distance INTEGER,
            detected_at          TEXT    DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_change_log_contract
            ON mas_change_log(contract_number)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_change_log_type
            ON mas_change_log(change_type)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_change_log_date
            ON mas_change_log(detected_at)
    """)

    # -------------------------------------------------------------------------
    # refresh_log -- operational log of every pipeline run.
    # Valid status values: running, completed, failed
    # -------------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS refresh_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT    UNIQUE NOT NULL,
            source          TEXT    NOT NULL,
            started_at      TEXT    NOT NULL,
            completed_at    TEXT,
            rows_processed  INTEGER DEFAULT 0,
            rows_changed    INTEGER DEFAULT 0,
            status          TEXT    DEFAULT 'running',
            notes           TEXT
        )
    """)

    # Record schema version
    cursor.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (SCHEMA_VERSION,),
    )

    conn.commit()
    logger.info("All tables created / verified.")


def seed_vehicles(conn: sqlite3.Connection, config: dict) -> None:
    """
    Seed the contract_vehicles table from config.yaml.
    Uses INSERT OR IGNORE so re-runs are safe and won't overwrite manual edits.
    """
    vehicles = config.get("elib", {}).get("vehicles", {})
    cursor = conn.cursor()

    for code, attrs in vehicles.items():
        cursor.execute(
            """
            INSERT OR IGNORE INTO contract_vehicles (code, name, csv_url, enabled, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                code,
                attrs.get("name", code),
                attrs.get("csv_url"),
                1 if attrs.get("enabled", False) else 0,
                attrs.get("notes"),
            ),
        )

    conn.commit()
    count = cursor.execute("SELECT COUNT(*) FROM contract_vehicles").fetchone()[0]
    logger.info(f"contract_vehicles seeded: {count} vehicles registered.")


def drop_tables(conn: sqlite3.Connection) -> None:
    """
    Drop all application tables. DESTRUCTIVE -- only used with --reset flag.
    """
    tables = [
        "mas_change_log",
        "refresh_log",
        "terminations",
        "mas_vendor_sins",
        "mas_vendors",
        "contract_vehicles",
        "schema_version",
    ]
    cursor = conn.cursor()
    for table in tables:
        cursor.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    logger.warning(f"Dropped {len(tables)} tables.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap the gsa-contract-tracker SQLite database."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DROP and recreate all tables. DESTRUCTIVE -- requires confirmation.",
    )
    args = parser.parse_args()

    config = load_config()
    db_path = get_db_path(config)

    logger.info(f"Database path: {db_path.resolve()}")

    conn = sqlite3.connect(db_path)

    try:
        if args.reset:
            confirm = input(
                f"\nRESET requested. This will DELETE ALL DATA in {db_path}.\n"
                "Type 'yes' to confirm: "
            ).strip()
            if confirm.lower() != "yes":
                logger.info("Reset cancelled.")
                return
            logger.warning("Resetting database...")
            drop_tables(conn)

        create_tables(conn)
        seed_vehicles(conn, config)
        logger.success(f"Bootstrap complete. Database ready at: {db_path.resolve()}")

    except Exception as e:
        logger.exception(f"Bootstrap failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
