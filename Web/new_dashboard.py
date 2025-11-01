import streamlit as st
import boto3
import pandas as pd
from datetime import datetime, timedelta
import plotly.express as px
import json

# ---------- CONFIGURATION ----------
TABLE_NAME = "EC2IdleInstanceMetrics"
REFRESH_INTERVAL = 120  # seconds (auto-refresh every 2 minutes)
st.set_page_config(
    page_title="AWS Cost Optimization Dashboard",
    page_icon="💰",
    layout="wide"
)

# ---------- PAGE HEADER ----------
st.title("💰 AWS Cost Optimization Dashboard")
st.markdown("Real-time visualization of EC2 instance activity, fetched from **DynamoDB**.")

# ---------- AWS DYNAMODB CONNECTION ----------
@st.cache_data(ttl=REFRESH_INTERVAL)
def load_data():
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(TABLE_NAME)

    # Scan table (you can replace with query for optimization)
    response = table.scan()
    items = response.get("Items", [])

    # Handle pagination
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    # Convert to DataFrame
    if not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)
    # Convert numeric fields
    for col in ["AvgCPU", "MaxCPU", "NetworkInBytes", "NetworkOutBytes", "TotalNetworkBytes"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["EvaluationTimestamp"] = pd.to_datetime(df["EvaluationTimestamp"])
    return df


data = load_data()

if data.empty:
    st.warning("No data found in DynamoDB yet. Wait for the Lambda to insert records.")
    st.stop()

# ---------- FILTERS ----------
st.sidebar.header("🔍 Filters")
regions = sorted(data["Region"].dropna().unique())
selected_region = st.sidebar.multiselect("Select Region(s)", regions, default=regions)

statuses = sorted(data["Status"].dropna().unique())
selected_status = st.sidebar.multiselect("Select Status", statuses, default=statuses)

time_range = st.sidebar.slider("Show records from last (hours):", 1, 24, 6)
time_threshold = datetime.utcnow() - timedelta(hours=time_range)

filtered = data[
    (data["Region"].isin(selected_region))
    & (data["Status"].isin(selected_status))
    & (data["EvaluationTimestamp"] >= time_threshold)
].sort_values("EvaluationTimestamp", ascending=False)

# ---------- KPIs ----------
total_instances = len(filtered)
idle_count = len(filtered[filtered["Status"] == "Idle"])
active_count = len(filtered[filtered["Status"] == "Active"])
last_update = filtered["EvaluationTimestamp"].max()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Instances", total_instances)
col2.metric("Idle Instances", idle_count, delta=f"{round(idle_count / total_instances * 100, 1)}%")
col3.metric("Active Instances", active_count)

# Show last update based on the latest EvaluationTimestamp from the filtered data
try:
    last_update_str = last_update.isoformat() if hasattr(last_update, 'to_pydatetime') or hasattr(last_update, 'isoformat') else str(last_update)
except Exception:
    last_update_str = str(last_update)

st.caption(f"📅 Last updated: {last_update_str}")

st.divider()

# ---------- REGION-WISE DISTRIBUTION ----------
if not filtered.empty:
    region_chart = (
        filtered.groupby(["Region", "Status"])
        .size()
        .reset_index(name="Count")
    )

    fig = px.bar(
        region_chart,
        x="Region",
        y="Count",
        color="Status",
        barmode="group",
        title="Instance Distribution by Region",
        color_discrete_map={"Idle": "#ff6b6b", "Active": "#1dd1a1"}
    )
    st.plotly_chart(fig, use_container_width=True)

# ---------- CPU UTILIZATION TREND ----------
cpu_data = (
    filtered.groupby(["EvaluationTimestamp", "Status"])["AvgCPU"]
    .mean()
    .reset_index()
    .sort_values("EvaluationTimestamp")
)
fig2 = px.line(
    cpu_data,
    x="EvaluationTimestamp",
    y="AvgCPU",
    color="Status",
    title="Average CPU Utilization Trend",
    color_discrete_map={"Idle": "#ff9f43", "Active": "#54a0ff"}
)
st.plotly_chart(fig2, use_container_width=True)

# ---------- INSTANCE DETAILS TABLE ----------
st.subheader("📋 Instance Details")
search_id = st.text_input("Search by Instance ID or Tag:")
if search_id:
    filtered = filtered[
        filtered["InstanceId"].str.contains(search_id, case=False)
        | filtered["Tags"].str.contains(search_id, case=False)
    ]

# Expandable data view
with st.expander("Show instance data"):
    st.dataframe(
        filtered[
            [
                "InstanceId",
                "Region",
                "InstanceType",
                "Status",
                "AvgCPU",
                "TotalNetworkBytes",
                "Recommendation",
                "EvaluationTimestamp"
            ]
        ],
        use_container_width=True,
        hide_index=True
    )

# ---------- AUTO REFRESH ----------
st.markdown(f"🔄 Auto-refresh every {REFRESH_INTERVAL} seconds")
#st_autorefresh = st.rerun()
