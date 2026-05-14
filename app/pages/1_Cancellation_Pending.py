"""
Cancellation Pending — contracts with a USASpending termination record
that are still showing active in GSA eLibrary.
These are in the ~45-day lag window between USASpending and eLib updates.
"""

import streamlit as st
import pandas as pd
import plotly.express as px

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
    # KPI metrics row
    total_pending = len(df)
    total_obligation = (
        df["federal_action_obligation"].sum()
        if "federal_action_obligation" in df.columns
        else 0
    )
    unique_vehicles = df["vehicle"].nunique() if "vehicle" in df.columns else 0
    unique_agencies = df["department"].nunique() if "department" in df.columns else 0

    mcol1, mcol2, mcol3, mcol4 = st.columns(4)
    with mcol1:
        st.metric("Contracts Pending", f"{total_pending:,}")
    with mcol2:
        st.metric(
            "Total Obligation",
            f"${abs(total_obligation) / 1e6:.1f}M"
            if abs(total_obligation) >= 1e6
            else f"${abs(total_obligation):,.0f}",
        )
    with mcol3:
        st.metric("Vehicles Affected", f"{unique_vehicles:,}")
    with mcol4:
        st.metric("Agencies Affected", f"{unique_agencies:,}")

    st.divider()

    # Summary charts
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("By Termination Reason")
        if "termination_reason" in df.columns:
            reason_counts = (
                df["termination_reason"]
                .fillna("Unknown")
                .value_counts()
                .reset_index()
            )
            reason_counts.columns = ["reason", "count"]
            fig = px.pie(
                reason_counts,
                names="reason",
                values="count",
                color_discrete_sequence=px.colors.qualitative.Set2,
            )
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No reason data available.")

    with col_right:
        st.subheader("By Contract Vehicle")
        if "vehicle" in df.columns:
            vehicle_counts = (
                df["vehicle"]
                .fillna("Unknown")
                .value_counts()
                .reset_index()
            )
            vehicle_counts.columns = ["vehicle", "count"]
            fig = px.bar(
                vehicle_counts,
                x="vehicle",
                y="count",
                color_discrete_sequence=["#d62728"],
                labels={"vehicle": "Vehicle", "count": "Contracts"},
            )
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No vehicle data available.")

    st.divider()

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
