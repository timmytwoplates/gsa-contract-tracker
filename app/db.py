"""
db.py — Shared database query helpers for the Streamlit app.

All SQL lives here. Pages import functions from this module rather than
writing raw SQL in the UI layer — keeps queries testable and maintainable.

Connection is cached via st.cache_resource so it's shared across rerenders.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

CONFIG_PATH = Path("config/config.yaml")


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    """
    Return a cached SQLite connection.
    Called once per app session; Streamlit reuses it across rerenders.
    """
    config = _load_config()
    db_path = Path(config["database"]["path"])

    if not db_path.exists():
        st.error(
            f"Database not found at `{db_path}`. "
            "Run `python -m pipeline.bootstrap` then `python -m pipeline.fetch_elib` first."
        )
        st.stop()

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Execute a SELECT and return a DataFrame."""
    conn = get_connection()
    return pd.read_sql_query(sql, conn, params=params)


# ---------------------------------------------------------------------------
# Overview / summary queries
# ---------------------------------------------------------------------------


def get_summary_stats() -> dict:
    """High-level KPIs for the Overview page."""
    conn = get_connection()
    cur = conn.cursor()

    stats = {}

    row = cur.execute("""
        SELECT COUNT(*) as total_terminations,
               COUNT(DISTINCT piid) as unique_contracts,
               SUM(federal_action_obligation) as net_obligation
        FROM terminations
    """).fetchone()
    stats["total_terminations"] = row["total_terminations"] or 0
    stats["unique_contracts"] = row["unique_contracts"] or 0
    stats["net_obligation"] = row["net_obligation"] or 0.0

    row = cur.execute("""
        SELECT COUNT(*) as total FROM mas_vendors WHERE status = 'active'
    """).fetchone()
    stats["active_vendors"] = row["total"] or 0

    row = cur.execute("""
        SELECT COUNT(*) as pending FROM mas_vendors v
        INNER JOIN terminations t ON UPPER(TRIM(t.piid)) = UPPER(TRIM(v.contract_number))
        WHERE v.status = 'active'
    """).fetchone()
    stats["cancellation_pending"] = row["pending"] or 0

    row = cur.execute("""
        SELECT completed_at FROM refresh_log
        WHERE source LIKE 'elib%' AND status = 'completed'
        ORDER BY completed_at DESC LIMIT 1
    """).fetchone()
    stats["last_elib_refresh"] = row["completed_at"] if row else "Never"

    row = cur.execute("""
        SELECT completed_at FROM refresh_log
        WHERE source = 'usaspending' AND status = 'completed'
        ORDER BY completed_at DESC LIMIT 1
    """).fetchone()
    stats["last_usaspending_refresh"] = row["completed_at"] if row else "Never"

    return stats


def get_data_age_hours() -> dict[str, float | None]:
    """Return age in hours for each data source since last successful refresh."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow()
    ages: dict[str, float | None] = {}

    for source, pattern in [("elib", "elib%"), ("usaspending", "usaspending")]:
        row = cur.execute(
            """
            SELECT completed_at FROM refresh_log
            WHERE source LIKE ? AND status = 'completed'
            ORDER BY completed_at DESC LIMIT 1
            """,
            (pattern,),
        ).fetchone()
        if row and row["completed_at"]:
            try:
                completed = datetime.fromisoformat(row["completed_at"])
                ages[source] = (now - completed).total_seconds() / 3600.0
            except (ValueError, TypeError):
                ages[source] = None
        else:
            ages[source] = None

    return ages


def get_terminations_by_reason() -> pd.DataFrame:
    return query("""
        SELECT termination_reason, COUNT(*) as count,
               SUM(federal_action_obligation) as total_obligation
        FROM terminations
        GROUP BY termination_reason
        ORDER BY count DESC
    """)


def get_terminations_trend(vehicle_filter: str | None = None) -> pd.DataFrame:
    """Monthly termination count, optionally filtered to MAS/BPA contracts only."""
    if vehicle_filter:
        return query(
            """
            SELECT strftime('%Y-%m', t.termination_date) as month,
                   COUNT(*) as terminations,
                   SUM(t.federal_action_obligation) as obligation
            FROM terminations t
            INNER JOIN mas_vendors v
                ON UPPER(TRIM(t.piid)) = UPPER(TRIM(v.contract_number))
            INNER JOIN contract_vehicles cv ON v.vehicle_id = cv.id
            WHERE cv.code = ?
              AND t.termination_date IS NOT NULL
            GROUP BY month
            ORDER BY month
        """,
            (vehicle_filter,),
        )
    return query("""
        SELECT strftime('%Y-%m', termination_date) as month,
               COUNT(*) as terminations,
               SUM(federal_action_obligation) as obligation
        FROM terminations
        WHERE termination_date IS NOT NULL
        GROUP BY month
        ORDER BY month
    """)


# ---------------------------------------------------------------------------
# Cancellation Pending
# ---------------------------------------------------------------------------


def get_cancellation_pending(vehicle: str | None = None) -> pd.DataFrame:
    """
    Contracts that appear in USASpending terminations but are still
    active in the eLibrary vendor roster.
    These are in the lag window (typically ~45 days).
    Optionally filtered by vehicle code.
    """
    conditions = ["v.status = 'active'"]
    params: list = []

    if vehicle:
        conditions.append("cv.code = ?")
        params.append(vehicle)

    where = " AND ".join(conditions)

    return query(f"""
        SELECT
            v.contract_number,
            v.vendor_name,
            cv.name         AS vehicle,
            v.large_category,
            v.sub_category,
            t.termination_code,
            t.termination_reason,
            t.termination_date,
            t.federal_action_obligation,
            t.department,
            t.contractor,
            v.ultimate_contract_end_date,
            t.link
        FROM mas_vendors v
        INNER JOIN terminations t
            ON UPPER(TRIM(t.piid)) = UPPER(TRIM(v.contract_number))
        INNER JOIN contract_vehicles cv ON v.vehicle_id = cv.id
        WHERE {where}
        ORDER BY t.termination_date DESC
    """, tuple(params))


# ---------------------------------------------------------------------------
# Terminations table
# ---------------------------------------------------------------------------


def get_terminations(
    vehicle: str | None = None,
    termination_code: str | None = None,
    department: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    large_category: str | None = None,
) -> pd.DataFrame:
    """
    Filterable terminations query. Only returns terminations where the PIID
    matched a known MAS/BPA contract (inner join with mas_vendors).
    Pass vehicle=None to get all vehicles.
    """
    conditions = ["1=1"]
    params: list = []

    if vehicle:
        conditions.append("cv.code = ?")
        params.append(vehicle)
    if termination_code:
        conditions.append("t.termination_code = ?")
        params.append(termination_code)
    if department:
        conditions.append("t.department LIKE ?")
        params.append(f"%{department}%")
    if date_from:
        conditions.append("t.termination_date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("t.termination_date <= ?")
        params.append(date_to)
    if large_category:
        conditions.append("v.large_category = ?")
        params.append(large_category)

    where = " AND ".join(conditions)

    return query(
        f"""
        SELECT
            t.piid              AS contract_number,
            v.vendor_name,
            cv.name             AS vehicle,
            v.large_category,
            v.sub_category,
            t.termination_code,
            t.termination_reason,
            t.termination_date,
            t.federal_action_obligation,
            t.total_obligated,
            t.department,
            t.sub_agency,
            t.contractor,
            t.naics,
            t.psc,
            t.set_aside,
            t.place_state,
            t.link
        FROM terminations t
        INNER JOIN mas_vendors v
            ON UPPER(TRIM(t.piid)) = UPPER(TRIM(v.contract_number))
        INNER JOIN contract_vehicles cv ON v.vehicle_id = cv.id
        WHERE {where}
        ORDER BY t.termination_date DESC
    """,
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Vendor roster
# ---------------------------------------------------------------------------


def get_vendor_roster(
    vehicle: str | None = None,
    status: str = "active",
    search: str | None = None,
) -> pd.DataFrame:
    conditions = ["v.status = ?"]
    params: list = [status]

    if vehicle:
        conditions.append("cv.code = ?")
        params.append(vehicle)
    if search:
        conditions.append("(v.vendor_name LIKE ? OR v.contract_number LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = " AND ".join(conditions)

    return query(
        f"""
        SELECT
            v.contract_number,
            v.vendor_name,
            cv.name             AS vehicle,
            v.large_category,
            v.sub_category,
            v.state,
            v.option_period_end_date,
            v.ultimate_contract_end_date,
            v.sam_uei,
            v.small_business,
            v.sdvosb,
            v.eight_a,
            v.status,
            -- Flag if a termination record exists in USASpending
            CASE WHEN t.piid IS NOT NULL THEN 1 ELSE 0 END AS termination_flag,
            t.termination_date,
            t.termination_reason
        FROM mas_vendors v
        INNER JOIN contract_vehicles cv ON v.vehicle_id = cv.id
        LEFT JOIN terminations t
            ON UPPER(TRIM(t.piid)) = UPPER(TRIM(v.contract_number))
        WHERE {where}
        ORDER BY v.vendor_name
    """,
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Recent changes
# ---------------------------------------------------------------------------


def get_recent_changes(days: int = 30, change_type: str | None = None) -> pd.DataFrame:
    conditions = [f"cl.detected_at >= datetime('now', '-{days} days')"]
    params: list = []

    if change_type:
        conditions.append("cl.change_type = ?")
        params.append(change_type)

    where = " AND ".join(conditions)

    return query(
        f"""
        SELECT
            cl.detected_at,
            cl.change_type,
            cl.contract_number,
            v.vendor_name,
            cv.name     AS vehicle,
            cl.field_name,
            cl.old_value,
            cl.new_value,
            cl.levenshtein_distance
        FROM mas_change_log cl
        LEFT JOIN mas_vendors v ON cl.contract_number = v.contract_number
        INNER JOIN contract_vehicles cv ON cl.vehicle_id = cv.id
        WHERE {where}
        ORDER BY cl.detected_at DESC
    """,
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Filter option lists (for Streamlit selectboxes)
# ---------------------------------------------------------------------------


def get_active_vehicles() -> list[str]:
    df = query("SELECT code FROM contract_vehicles WHERE enabled = 1 ORDER BY code")
    return df["code"].tolist()


def get_large_categories(vehicle: str | None = None) -> list[str]:
    if vehicle:
        df = query(
            """
            SELECT DISTINCT v.large_category FROM mas_vendors v
            INNER JOIN contract_vehicles cv ON v.vehicle_id = cv.id
            WHERE cv.code = ? AND v.large_category IS NOT NULL
            ORDER BY large_category
            """,
            (vehicle,),
        )
    else:
        df = query("""
            SELECT DISTINCT large_category FROM mas_vendors
            WHERE large_category IS NOT NULL
            ORDER BY large_category
            """)
    return df["large_category"].tolist()


def get_departments() -> list[str]:
    df = query("""
        SELECT DISTINCT department FROM terminations
        WHERE department IS NOT NULL
        ORDER BY department
        """)
    return df["department"].tolist()
