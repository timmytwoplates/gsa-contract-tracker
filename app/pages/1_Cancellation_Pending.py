"""
Cancellation Pending — contracts with a USASpending termination record
that are still showing active in GSA eLibrary.
These are in the ~45-day lag window between USASpending and eLib updates.
"""

import streamlit as st
import pandas as pd

from app.db import get_cancellation_pending, get_active_vehicles

st.set_page_config(page_title="Cancellation Pending", layout="wide")
st.title("⚠️ Cancellation Pending")
st.caption(
    "Contracts with a termination modification recorded in USASpending "
    "but still appearing as active in GSA eLibrary. "
    "eLibrary typically lags USASpending by ~30–45 days."
)

vehicles = ["All"] + get_active_vehicles()
vehicle_filter = st.selectbox("Filter by vehicle", vehicles)

df = get_cancellation_pending(
    vehicle=vehicle_filter if vehicle_filter != "All" else None
)

if df.empty:
    st.success("No pending cancellations detected.")
else:
    st.metric("Contracts in Pending Status", len(df))

    # Format dollar amounts
    if "federal_action_obligation" in df.columns:
        df["obligation_fmt"] = df["federal_action_obligation"].apply(
            lambda x: f"${x:,.0f}" if pd.notna(x) else ""
        )

    st.dataframe(
        df[[
            "contract_number", "vendor_name", "vehicle", "large_category",
            "termination_reason", "termination_date", "obligation_fmt",
            "department", "ultimate_contract_end_date", "link",
        ]].rename(columns={
            "contract_number": "Contract #",
            "vendor_name": "Vendor",
            "vehicle": "Vehicle",
            "large_category": "Category",
            "termination_reason": "Reason",
            "termination_date": "Termination Date",
            "obligation_fmt": "Obligation",
            "department": "Agency",
            "ultimate_contract_end_date": "End Date",
            "link": "USASpending Link",
        }),
        use_container_width=True,
        hide_index=True,
    )

    csv_data = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_data,
        file_name="cancellation_pending.csv",
        mime="text/csv",
    )
