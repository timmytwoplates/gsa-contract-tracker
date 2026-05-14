"""
Admin Panel — System status, pipeline run history, and manual data refresh.

Shows last run times, results, and provides buttons to trigger pipeline
scripts manually.
"""

import subprocess
import sys
from pathlib import Path

import streamlit as st
import pandas as pd

from app.db import get_refresh_log, get_table_counts, get_data_age_hours

st.set_page_config(page_title="Admin Panel", layout="wide")
st.title("🔧 Admin Panel")

# ---------------------------------------------------------------------------
# Data freshness summary
# ---------------------------------------------------------------------------
st.header("Data Freshness")

ages = get_data_age_hours()
age_cols = st.columns(len(ages) if ages else 1)
for i, (source, hours) in enumerate(ages.items()):
    with age_cols[i]:
        if hours is None:
            st.metric(f"{source}", "Never refreshed")
            st.error("No data")
        elif hours > 48:
            st.metric(f"{source}", f"{hours:.1f} hours ago")
            st.warning("Stale (>48h)")
        else:
            st.metric(f"{source}", f"{hours:.1f} hours ago")
            st.success("Fresh")

st.divider()

# ---------------------------------------------------------------------------
# Table row counts
# ---------------------------------------------------------------------------
st.header("Database Tables")

try:
    counts = get_table_counts()
    count_cols = st.columns(len(counts))
    for i, (table, count) in enumerate(counts.items()):
        count_cols[i].metric(table, f"{count:,}")
except Exception as e:
    st.error(f"Could not load table counts: {e}")

st.divider()

# ---------------------------------------------------------------------------
# Manual pipeline triggers
# ---------------------------------------------------------------------------
st.header("Manual Pipeline Run")
st.caption(
    "Run pipeline scripts manually. These are the same scripts that run "
    "on the daily/monthly GitHub Actions schedule."
)

_project_root = Path(__file__).resolve().parent.parent.parent

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("eLibrary Refresh")
    st.caption("Fetches latest vendor roster from GSA eLibrary")
    if st.button("▶️ Run eLib Refresh", key="run_elib"):
        with st.spinner("Running eLibrary refresh..."):
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pipeline.fetch_elib"],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    cwd=str(_project_root),
                )
                if result.returncode == 0:
                    st.success("eLib refresh completed successfully!")
                    if result.stdout:
                        with st.expander("Output"):
                            st.code(result.stdout[-2000:])
                else:
                    st.error(f"eLib refresh failed (exit code {result.returncode})")
                    if result.stderr:
                        with st.expander("Error output"):
                            st.code(result.stderr[-2000:])
            except subprocess.TimeoutExpired:
                st.error("Pipeline timed out after 10 minutes.")
            except Exception as e:
                st.error(f"Failed to run pipeline: {e}")

with col2:
    st.subheader("Terminations Refresh")
    st.caption("Fetches termination data from USASpending.gov")
    if st.button("▶️ Run Terminations Refresh", key="run_term"):
        with st.spinner("Running terminations refresh (this may take a while)..."):
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pipeline.fetch_terminations"],
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    cwd=str(_project_root),
                )
                if result.returncode == 0:
                    st.success("Terminations refresh completed!")
                    if result.stdout:
                        with st.expander("Output"):
                            st.code(result.stdout[-2000:])
                else:
                    st.error(f"Terminations refresh failed (exit code {result.returncode})")
                    if result.stderr:
                        with st.expander("Error output"):
                            st.code(result.stderr[-2000:])
            except subprocess.TimeoutExpired:
                st.error("Pipeline timed out after 60 minutes.")
            except Exception as e:
                st.error(f"Failed to run pipeline: {e}")

with col3:
    st.subheader("Bootstrap DB")
    st.caption("Re-run database schema setup (safe, non-destructive)")
    if st.button("▶️ Run Bootstrap", key="run_bootstrap"):
        with st.spinner("Running bootstrap..."):
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pipeline.bootstrap"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(_project_root),
                )
                if result.returncode == 0:
                    st.success("Bootstrap completed!")
                    if result.stdout:
                        with st.expander("Output"):
                            st.code(result.stdout[-2000:])
                else:
                    st.error(f"Bootstrap failed (exit code {result.returncode})")
                    if result.stderr:
                        with st.expander("Error output"):
                            st.code(result.stderr[-2000:])
            except Exception as e:
                st.error(f"Failed to run bootstrap: {e}")

st.divider()

# ---------------------------------------------------------------------------
# Schedule info
# ---------------------------------------------------------------------------
st.header("Scheduled Runs")
st.info(
    "**Daily eLibrary Refresh**: 9:00 AM UTC (~5:00 AM ET) every day\n\n"
    "**Monthly Terminations Refresh**: 8:00 AM UTC (~4:00 AM ET) on the 1st of each month\n\n"
    "These schedules are configured in GitHub Actions workflows. "
    "You can also trigger them manually above or from the GitHub Actions UI."
)

st.divider()

# ---------------------------------------------------------------------------
# Pipeline run history
# ---------------------------------------------------------------------------
st.header("Pipeline Run History")

try:
    log_df = get_refresh_log(limit=100)
    if log_df.empty:
        st.info("No pipeline runs recorded yet.")
    else:
        # Add duration column
        if "started_at" in log_df.columns and "completed_at" in log_df.columns:
            try:
                started = pd.to_datetime(log_df["started_at"], utc=True)
                completed = pd.to_datetime(log_df["completed_at"], utc=True)
                log_df["duration"] = (completed - started).dt.total_seconds().apply(
                    lambda x: f"{x:.0f}s" if pd.notna(x) else "—"
                )
            except Exception:
                log_df["duration"] = "—"

        # Color-code status
        def highlight_status(row):
            status = row.get("status", "")
            if status == "completed":
                return ["background-color: #d4edda"] * len(row)
            elif status == "failed":
                return ["background-color: #f8d7da"] * len(row)
            elif status == "running":
                return ["background-color: #fff3cd"] * len(row)
            return [""] * len(row)

        display_cols = [
            "source", "status", "started_at", "completed_at",
            "duration", "rows_processed", "rows_changed", "notes",
        ]
        display_cols = [c for c in display_cols if c in log_df.columns]

        display_df = log_df[display_cols].rename(columns={
            "source": "Source",
            "status": "Status",
            "started_at": "Started",
            "completed_at": "Completed",
            "duration": "Duration",
            "rows_processed": "Rows Processed",
            "rows_changed": "Rows Changed",
            "notes": "Notes",
        })

        # Truncate timestamps for readability
        for col in ["Started", "Completed"]:
            if col in display_df.columns:
                display_df[col] = display_df[col].astype(str).str[:19]

        st.dataframe(
            display_df.style.apply(highlight_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
except Exception as e:
    st.error(f"Could not load run history: {e}")
