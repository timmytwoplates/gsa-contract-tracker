"""
Terminations — full filterable/sortable table of MAS/BPA terminations
sourced from USASpending bulk data, enriched with eLibrary vendor metadata.
"""

import streamlit as st
import pandas as pd

from app.db import get_terminations, get_active_vehicles, get_large_categories, get_departments

st.set_page_config(page_title="Terminations", layout="wide")
st.title("📋 Terminations")

# --- Filters ---
with st.expander("Filters", expanded=True):
    col1, col2, col3 = st.columns(3)

    with col1:
        vehicles = ["All"] + get_active_vehicles()
        vehicle = st.selectbox("Vehicle", vehicles)
        vehicle_filter = None if vehicle == "All" else vehicle

        codes = {"All": None, "F — Convenience": "F", "E — Default": "E", "X — Cause": "X", "N — No-Cost Settlement": "N"}
        code_label = st.selectbox("Termination Type", list(codes.keys()))
        code_filter = codes[code_label]

    with col2:
        categories = ["All"] + get_large_categories(vehicle_filter)
        category = st.selectbox("Large Category", categories)
        category_filter = None if category == "All" else category

        departments = ["All"] + get_departments()
        dept = st.selectbox("Agency / Department", departments)
        dept_filter = None if dept == "All" else dept

    with col3:
        date_from = st.date_input("From Date", value=None)
        date_to = st.date_input("To Date", value=None)

# --- Query ---
df = get_terminations(
    vehicle=vehicle_filter,
    termination_code=code_filter,
    department=dept_filter,
    date_from=str(date_from) if date_from else None,
    date_to=str(date_to) if date_to else None,
    large_category=category_filter,
)

if df.empty:
    st.info("No terminations match the selected filters.")
else:
    st.metric("Matching Records", f"{len(df):,}")

    if "federal_action_obligation" in df.columns:
        total = df["federal_action_obligation"].sum()
        st.metric(
            "Total Obligation (filtered)",
            f"${total / 1e6:.1f}M" if abs(total) >= 1e6 else f"${total:,.0f}",
        )

    display_cols = [
        "contract_number", "vendor_name", "vehicle", "large_category",
        "termination_reason", "termination_date", "federal_action_obligation",
        "department", "sub_agency", "set_aside", "place_state", "link",
    ]
    display_df = df[[c for c in display_cols if c in df.columns]].rename(columns={
        "contract_number": "Contract #",
        "vendor_name": "Vendor",
        "vehicle": "Vehicle",
        "large_category": "Category",
        "termination_reason": "Reason",
        "termination_date": "Date",
        "federal_action_obligation": "Obligation ($)",
        "department": "Agency",
        "sub_agency": "Sub-Agency",
        "set_aside": "Set-Aside",
        "place_state": "State",
        "link": "Link",
    })

    st.dataframe(display_df, use_container_width=True, hide_index=True)

    csv_data = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_data,
        file_name="terminations.csv",
        mime="text/csv",
    )
