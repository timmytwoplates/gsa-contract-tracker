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

    # All available columns with friendly names
    all_columns = {
        "contract_number": "Contract #",
        "vendor_name": "Vendor",
        "vehicle": "Vehicle",
        "large_category": "Category",
        "sub_category": "Sub-Category",
        "termination_code": "Term. Code",
        "termination_reason": "Reason",
        "termination_date": "Date",
        "mod_number": "Mod #",
        "mod_count": "# of Mods",
        "federal_action_obligation": "Obligation ($)",
        "total_obligated": "Total Obligated ($)",
        "ceiling": "Ceiling ($)",
        "department": "Agency",
        "sub_agency": "Sub-Agency",
        "awarding_office": "Contracting Office",
        "contractor": "Contractor",
        "contractor_parent": "Parent Company",
        "naics": "NAICS",
        "psc": "PSC",
        "pricing": "Pricing Type",
        "set_aside": "Set-Aside",
        "place_state": "State",
        "fiscal_year": "Fiscal Year",
        "cancellation_description": "Description",
        "link": "Link",
    }

    # Only show columns that exist in the data
    available_cols = {k: v for k, v in all_columns.items() if k in df.columns}

    # Default visible columns
    default_cols = [
        "contract_number", "vendor_name", "vehicle", "large_category",
        "termination_reason", "termination_date", "mod_count",
        "federal_action_obligation", "awarding_office",
        "department", "set_aside", "place_state", "link",
    ]
    default_cols = [c for c in default_cols if c in available_cols]

    # Column selector
    with st.expander("Configure Columns", expanded=False):
        selected_cols = st.multiselect(
            "Choose columns to display",
            options=list(available_cols.keys()),
            default=default_cols,
            format_func=lambda x: available_cols.get(x, x),
        )

    if not selected_cols:
        selected_cols = default_cols

    display_df = df[[c for c in selected_cols if c in df.columns]].rename(
        columns=available_cols
    )

    st.dataframe(display_df, use_container_width=True, hide_index=True)

    csv_data = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_data,
        file_name="terminations.csv",
        mime="text/csv",
    )
