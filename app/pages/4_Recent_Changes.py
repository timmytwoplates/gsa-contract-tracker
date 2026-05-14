"""
Recent Changes — surfaces detected changes from the daily eLibrary diff.

Shows contract adds, removes, SIN changes, and field-level updates logged
in mas_change_log. Levenshtein-filtered so minor formatting noise is
already suppressed before this view — every row here is a real change.
"""

import streamlit as st
import pandas as pd
import plotly.express as px

from app.db import get_recent_changes, get_active_vehicles, get_daily_change_activity

st.set_page_config(page_title="Recent Changes", layout="wide")
st.title("🔄 Recent Changes")
st.caption(
    "Changes detected by the daily eLibrary diff. "
    "Minor text formatting differences (e.g. 'St' vs 'Street') are filtered out. "
    "Every row here crossed the significance threshold."
)

# --- Controls ---
col1, col2, col3 = st.columns(3)
with col1:
    days = st.selectbox("Time window", [7, 14, 30, 60, 90], index=2)
with col2:
    change_types = {
        "All": None,
        "➕ New Contracts (ADD)": "ADD",
        "➖ Removed Contracts (REMOVE)": "REMOVE",
        "📋 SIN Added": "SIN_ADD",
        "📋 SIN Removed": "SIN_REMOVE",
        "✏️ Field Update": "FIELD_UPDATE",
    }
    selected_label = st.selectbox("Change Type", list(change_types.keys()))
    change_type_filter = change_types[selected_label]
with col3:
    vehicles = ["All"] + get_active_vehicles()
    vehicle_filter = st.selectbox("Vehicle", vehicles)

df = get_recent_changes(days=days, change_type=change_type_filter)

if vehicle_filter != "All":
    df = df[df["vehicle"] == vehicle_filter]

if df.empty:
    st.info(f"No changes detected in the last {days} days.")
else:
    # Summary counts
    type_counts = df["change_type"].value_counts()
    cols = st.columns(5)
    for i, (ctype, label) in enumerate([
        ("ADD", "New Contracts"),
        ("REMOVE", "Removed"),
        ("SIN_ADD", "SIN Added"),
        ("SIN_REMOVE", "SIN Removed"),
        ("FIELD_UPDATE", "Field Updates"),
    ]):
        cols[i].metric(label, type_counts.get(ctype, 0))

    st.divider()

    # Daily activity timeline chart
    st.subheader("Daily Activity Timeline")
    activity_df = get_daily_change_activity(days=days)
    if not activity_df.empty:
        fig = px.bar(
            activity_df,
            x="date",
            y="count",
            color="change_type",
            title="Changes per Day",
            labels={"date": "Date", "count": "Changes", "change_type": "Type"},
            color_discrete_map={
                "ADD": "#28a745",
                "REMOVE": "#dc3545",
                "SIN_ADD": "#007bff",
                "SIN_REMOVE": "#ffc107",
                "FIELD_UPDATE": "#6c757d",
            },
        )
        fig.update_layout(
            margin=dict(t=40, b=10, l=10, r=10),
            xaxis_title="Date",
            yaxis_title="Number of Changes",
            barmode="stack",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No daily activity data available.")

    st.divider()

    # Color-code by change type
    type_colors = {
        "ADD": "#d4edda",        # green tint
        "REMOVE": "#f8d7da",     # red tint
        "SIN_ADD": "#cce5ff",    # blue tint
        "SIN_REMOVE": "#fff3cd", # yellow tint
        "FIELD_UPDATE": "#e2e3e5",  # grey
    }

    def highlight_change(row):
        color = type_colors.get(row.get("change_type", ""), "")
        return [f"background-color: {color}" if color else ""] * len(row)

    display_df = df.rename(columns={
        "detected_at": "Detected",
        "change_type": "Type",
        "contract_number": "Contract #",
        "vendor_name": "Vendor",
        "vehicle": "Vehicle",
        "field_name": "Field",
        "old_value": "Old Value",
        "new_value": "New Value",
        "levenshtein_distance": "Edit Dist.",
    })

    # Trim detected_at to readable format
    if "Detected" in display_df.columns:
        display_df["Detected"] = display_df["Detected"].str[:19]

    st.dataframe(
        display_df.style.apply(highlight_change, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    csv_data = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_data,
        file_name=f"changes_last_{days}_days.csv",
        mime="text/csv",
    )
