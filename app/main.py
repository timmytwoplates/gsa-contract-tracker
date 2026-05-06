"""
main.py — Streamlit app entry point for gsa-contract-tracker.

Run locally:
    streamlit run app/main.py

Deploy on Streamlit Community Cloud:
    Point to this file in your Streamlit Cloud project settings.
"""

import streamlit as st

st.set_page_config(
    page_title="GSA Contract Tracker",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from app.db import get_summary_stats, get_terminations_by_reason, get_terminations_trend
from app.db import get_active_vehicles

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🏛️ GSA Contract Tracker")
    st.caption("Tracking cancellations in GSA contract vehicles")

    vehicles = ["All"] + get_active_vehicles()
    selected_vehicle = st.selectbox("Contract Vehicle", vehicles)
    vehicle_filter = None if selected_vehicle == "All" else selected_vehicle

    st.divider()
    st.caption("Data Sources")
    st.markdown(
        "- [GSA eLibrary](https://www.gsaelibrary.gsa.gov)\n"
        "- [USASpending.gov](https://www.usaspending.gov)\n"
        "- [GSA D2D](https://d2d.gsa.gov) *(coming soon)*"
    )


# ---------------------------------------------------------------------------
# Overview page (main page — others will be in pages/)
# ---------------------------------------------------------------------------

st.header("Overview")

try:
    stats = get_summary_stats()
except Exception as e:
    st.error(f"Could not load data: {e}")
    st.info("Make sure the database has been bootstrapped and populated. "
            "Run `python -m pipeline.bootstrap` then `python -m pipeline.fetch_elib`.")
    st.stop()

# KPI row
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Terminations", f"{stats['total_terminations']:,}")
col2.metric("Unique Contracts", f"{stats['unique_contracts']:,}")
col3.metric(
    "Net $ Deobligated",
    f"${abs(stats['net_obligation']) / 1e6:.1f}M"
    if abs(stats["net_obligation"]) >= 1e6
    else f"${abs(stats['net_obligation']):,.0f}",
)
col4.metric("⚠️ Cancellation Pending", f"{stats['cancellation_pending']:,}")

st.divider()

# Second row
col5, col6, col7, col8 = st.columns(4)
col5.metric("Active MAS/BPA Vendors", f"{stats['active_vendors']:,}")
col6.metric("", "")  # placeholder for D2D metric (future)
col7.metric("eLib Last Updated", stats["last_elib_refresh"][:10] if stats["last_elib_refresh"] != "Never" else "Never")
col8.metric("USASpending Last Updated", stats["last_usaspending_refresh"][:10] if stats["last_usaspending_refresh"] != "Never" else "Never")

st.divider()

# Terminations by reason — pie/bar
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("Terminations by Reason")
    reason_df = get_terminations_by_reason()
    if not reason_df.empty:
        import plotly.express as px
        fig = px.pie(
            reason_df,
            names="termination_reason",
            values="count",
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No termination data yet.")

with col_right:
    st.subheader("Monthly Termination Trend")
    trend_df = get_terminations_trend(vehicle_filter)
    if not trend_df.empty:
        import plotly.express as px
        fig = px.bar(
            trend_df,
            x="month",
            y="terminations",
            color_discrete_sequence=["#d62728"],
            labels={"month": "Month", "terminations": "Terminations"},
        )
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No trend data yet.")
