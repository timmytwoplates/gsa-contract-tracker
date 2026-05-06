"""
Vendor Roster — Active MAS/BPA vendor list enriched with termination status.
Contracts flagged as terminated in USASpending are highlighted.
"""

import streamlit as st

from app.db import get_active_vehicles, get_vendor_roster

st.set_page_config(page_title="Vendor Roster", layout="wide")
st.title("🏢 Vendor Roster")

col1, col2, col3 = st.columns(3)
with col1:
    vehicles = ["All"] + get_active_vehicles()
    vehicle = st.selectbox("Vehicle", vehicles)
    vehicle_filter = None if vehicle == "All" else vehicle
with col2:
    status_options = {"Active": "active", "Removed": "removed"}
    status_label = st.selectbox("Status", list(status_options.keys()))
    status_filter = status_options[status_label]
with col3:
    search = st.text_input("Search vendor / contract #")
    search_filter = search if search else None

df = get_vendor_roster(
    vehicle=vehicle_filter,
    status=status_filter,
    search=search_filter,
)

if df.empty:
    st.info("No vendors match the selected filters.")
else:
    flagged = df[df["termination_flag"] == 1]
    st.metric("Vendors", len(df))
    if len(flagged):
        st.warning(
            f"⚠️ {len(flagged)} vendor(s) have a USASpending termination record."
        )

    # Highlight rows with termination flag
    def highlight_terminated(row):
        if row.get("termination_flag") == 1:
            return ["background-color: #fff3cd"] * len(row)
        return [""] * len(row)

    display_cols = [
        "contract_number",
        "vendor_name",
        "vehicle",
        "large_category",
        "state",
        "option_period_end_date",
        "ultimate_contract_end_date",
        "small_business",
        "sdvosb",
        "eight_a",
        "termination_flag",
        "termination_date",
        "termination_reason",
    ]
    display_df = df[[c for c in display_cols if c in df.columns]].rename(
        columns={
            "contract_number": "Contract #",
            "vendor_name": "Vendor",
            "vehicle": "Vehicle",
            "large_category": "Category",
            "state": "State",
            "option_period_end_date": "Option End",
            "ultimate_contract_end_date": "Ultimate End",
            "small_business": "SB",
            "sdvosb": "SDVOSB",
            "eight_a": "8(a)",
            "termination_flag": "⚠️ Terminated",
            "termination_date": "Term. Date",
            "termination_reason": "Term. Reason",
        }
    )

    st.dataframe(
        display_df.style.apply(highlight_terminated, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    csv_data = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV", data=csv_data, file_name="vendor_roster.csv", mime="text/csv"
    )
