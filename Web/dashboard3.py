# ec2_idle_dashboard_with_actions.py
import streamlit as st
import boto3
import pandas as pd
import plotly.express as px
from datetime import datetime
import time
import json

# -----------------------------
# Config
# -----------------------------
DDB_TABLE = "EC2IdleInstanceMetrics"         # table storing latest state per InstanceId
CLEANUP_LAMBDA = "cleanup_lambda"   # Lambda name you will create (or change)
CLEANUP_LOG_TABLE = "EC2CleanupLogs"        # optional logs table created by cleanup lambda

st.set_page_config(page_title="EC2 Idle Dashboard + Cleanup", layout="wide", page_icon="🧰")
st.title("🖥️ EC2 Idle Dashboard & Cleanup Console")

# -----------------------------
# AWS clients (cached)
# -----------------------------
@st.cache_resource
def get_clients():
    session = boto3.Session()
    return {
        "dynamodb": session.resource("dynamodb"),
        "lambda": session.client("lambda"),
        "ec2": session.client("ec2"),
        "ec2_resource": session.resource("ec2"),
    }

clients = get_clients()

# -----------------------------
# Utilities
# -----------------------------
def fetch_instances_from_dynamo(table_name=DDB_TABLE):
    try:
        table = clients["dynamodb"].Table(table_name)
        resp = table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return items
    except Exception as e:
        st.error(f"Error reading DynamoDB table {table_name}: {e}")
        return []

def call_cleanup_lambda(payload):
    try:
        resp = clients["lambda"].invoke(
            FunctionName=CLEANUP_LAMBDA,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode()
        )
        resp_payload = resp["Payload"].read().decode()
        return json.loads(resp_payload)
    except Exception as e:
        return {"error": str(e)}
# -----------------------------
# Cost estimation utilities
# -----------------------------
EC2_HOURLY_COST = {
    "t2.micro": 0.0116, "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416,
    "t3.large": 0.0832, "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34
}

def estimate_ec2_savings(df):
    """Estimate monthly savings from stopping idle instances."""
    total_savings = 0
    idle_instances = df[df["Status"] == "Idle"]
    for _, row in idle_instances.iterrows():
        cost_hr = EC2_HOURLY_COST.get(row["InstanceType"], 0.05)
        total_savings += cost_hr * 24 * 30  # assuming 30 days
    return total_savings

def estimate_ebs_savings(unattached_vols):
    """EBS ~$0.10/GB-month"""
    return sum(v["SizeGiB"] * 0.10 for v in unattached_vols)

def estimate_eip_savings(unassoc_eips):
    """Elastic IP ~$0.005/hour when not attached"""
    return len(unassoc_eips) * 0.005 * 24 * 30  # monthly

def estimate_total_savings(df, unattached_vols, unassoc_eips):
    return (
        estimate_ec2_savings(df) +
        estimate_ebs_savings(unattached_vols) +
        estimate_eip_savings(unassoc_eips)
    )

# -----------------------------
# Sidebar: refresh and stale resource scan options
# -----------------------------
st.sidebar.header("Controls")
refresh_interval = st.sidebar.number_input("Auto-refresh (seconds)", min_value=10, max_value=600, value=30, step=10)
st.sidebar.markdown("Actions are executed by a separate Lambda for safety.")
scan_stale = st.sidebar.checkbox("Scan stale resources (EBS/EIP/Unused SG)", value=True)

# Manual refresh
if st.sidebar.button("🔄 Refresh Now"):
    st.rerun()

# -----------------------------
# Load instance state data
# -----------------------------
raw_items = fetch_instances_from_dynamo(DDB_TABLE)

if not raw_items:
    st.warning("No data in DynamoDB table. Run your detection Lambda to populate latest instance state.")
    st.stop()

# Ensure consistent columns and fallback for LastUpdated
for it in raw_items:
    if "LastUpdated" not in it:
        # try EvaluationTimestamp fallback
        it["LastUpdated"] = it.get("EvaluationTimestamp", datetime.utcnow().isoformat())
    # Ensure numeric cpu exists
    it["AvgCPU"] = float(it.get("AvgCPU", 0) or 0)
    it["TotalNetworkBytes"] = int(it.get("TotalNetworkBytes", 0) or 0)
    it["Region"] = it.get("Region", "unknown")
    it["InstanceType"] = it.get("InstanceType", "unknown")
    it["Recommendation"] = it.get("Recommendation", "")

df = pd.DataFrame(raw_items)

# -----------------------------
# Top KPIs
# -----------------------------
total_instances = len(df)
idle_count = df[df["Status"] == "Idle"].shape[0]
active_count = df[df["Status"] == "Active"].shape[0]
last_update = df["LastUpdated"].max()

st.metric("Total Instances", total_instances)
col1, col2, col3 = st.columns(3)
col1.metric("Idle Instances", idle_count,
            delta=f"{(idle_count/total_instances*100):.1f}%" if total_instances else "0%")
col2.metric("Active Instances", active_count,
           delta=f"{(active_count/total_instances*100):.1f}%" if total_instances else "0%")
col3.metric("Last seen (most recent)", last_update if isinstance(last_update, str) else str(last_update))

st.markdown("---")


# -----------------------------
# Potential Savings Section
# -----------------------------
potential_savings = estimate_ec2_savings(df)
st.subheader("💰 Estimated Monthly Cost Savings")

col_s1, col_s2 = st.columns(2)
col_s1.metric("Potential EC2 Savings (Idle)", f"${potential_savings:,.2f}")

# Placeholders for stale resource savings (to be updated after scan)
savings_placeholder = col_s2.empty()


# -----------------------------
# Charts
# -----------------------------
left, right = st.columns([2, 1])

with left:
    status_counts = df["Status"].value_counts().reset_index()
    status_counts.columns = ["Status", "Count"]
    fig = px.pie(status_counts, names="Status", values="Count", title="Instance Status")
    st.plotly_chart(fig, use_container_width=True)

    fig2 = px.histogram(df, x="AvgCPU", nbins=20, title="Average CPU Distribution")
    fig2.update_layout(xaxis_title="Avg CPU (%)")
    st.plotly_chart(fig2, use_container_width=True)

with right:
    region_table = df.groupby(["Region", "Status"]).size().unstack(fill_value=0)
    st.subheader("By Region")
    st.dataframe(region_table)

st.markdown("---")

# -----------------------------
# Instance table and actions
# -----------------------------
st.subheader("Instance Inventory & Actions")
show_filters = st.expander("Filters")
with show_filters:
    regions = sorted(df["Region"].unique().tolist())
    sel_regions = st.multiselect("Regions", regions, default=regions)
    statuses = df["Status"].unique().tolist()
    sel_status = st.multiselect("Status", statuses, default=statuses)
    min_cpu = st.slider("Min Avg CPU (%)", 0.0, 100.0, 0.0)

filtered = df[
    (df["Region"].isin(sel_regions)) &
    (df["Status"].isin(sel_status)) &
    (df["AvgCPU"] >= min_cpu)
].sort_values(["Status", "Region"])

# Add selection checkbox column for bulk actions
filtered = filtered.reset_index(drop=True)
filtered["_select"] = False

# Render editable table using st.data_editor (Streamlit >= 1.24)
try:
    edited = st.data_editor(
        filtered[["InstanceId", "Region", "InstanceType", "AvgCPU", "Status", "Recommendation", "_select"]],
        column_config={
            "InstanceId": st.column_config.TextColumn("InstanceId"),
            "Region": st.column_config.TextColumn("Region"),
            "InstanceType": st.column_config.TextColumn("InstanceType"),
            "AvgCPU": st.column_config.NumberColumn("Avg CPU %", format="%.2f"),
            "Status": st.column_config.TextColumn("Status"),
            "_select": st.column_config.CheckboxColumn("Select")
        },
        use_container_width=True,
        num_rows="dynamic"
    )
except Exception:
    # fallback for older Streamlit versions
    st.dataframe(filtered[["InstanceId", "Region", "InstanceType", "AvgCPU", "Status", "Recommendation"]])
    st.warning("Editable table not available in this Streamlit version. Update Streamlit to use selection checkboxes.")
    edited = pd.DataFrame()

selected_ids = []
if not edited.empty and "_select" in edited.columns:
    selected_ids = edited[edited["_select"] == True]["InstanceId"].tolist()

col_a, col_b, col_c = st.columns(3)
with col_a:
    if st.button("🛑 Stop Selected Instances") and selected_ids:
        payload = {"action": "stop_instances", "instance_ids": selected_ids, "dry_run": False}
        res = call_cleanup_lambda(payload)
        st.success("Stop action invoked. See results below.")
        st.json(res)
with col_b:
    if st.button("▶️ Start Selected Instances") and selected_ids:
        payload = {"action": "start_instances", "instance_ids": selected_ids, "dry_run": False}
        res = call_cleanup_lambda(payload)
        st.success("Start action invoked. See results below.")
        st.json(res)
with col_c:
    if st.button("🗑️ Terminate Selected Instances (irreversible)") and selected_ids:
        if st.confirm("Are you sure you want to TERMINATE selected instances? This is irreversible."):
            payload = {"action": "terminate_instances", "instance_ids": selected_ids, "dry_run": False}
            res = call_cleanup_lambda(payload)
            st.json(res)

# -----------------------------
# Stale resource scan (EBS / EIP / unused SG)
# -----------------------------
st.markdown("---")
st.subheader("Stale Resource Detection")

if scan_stale:
    st.write("Scanning account for unattached EBS volumes, unattached Elastic IPs and unused Security Groups...")
    ec2_client = clients["ec2"]
    # Unattached EBS volumes
    vols = ec2_client.describe_volumes(Filters=[{"Name":"status", "Values":["available"]}])["Volumes"]
    unattached_vols = [{"VolumeId": v["VolumeId"], "SizeGiB": v["Size"], "Region": v.get("AvailabilityZone","")[:-1] } for v in vols]

    # Unassociated EIPs
    addrs = ec2_client.describe_addresses()["Addresses"]
    unassoc_eips = [a for a in addrs if "AssociationId" not in a]

    # Unused security groups (not in use by ENI) -- list SGs then check descriptions
    sgs = ec2_client.describe_security_groups()["SecurityGroups"]
    unused_sgs = []
    for sg in sgs:
        # skip default SG
        if sg["GroupName"] == "default":
            continue
        # check if any ENIs reference this sg
        enis = ec2_client.describe_network_interfaces(Filters=[{"Name":"group-id","Values":[sg["GroupId"]]}])["NetworkInterfaces"]
        if len(enis) == 0:
            unused_sgs.append({"GroupId": sg["GroupId"], "GroupName": sg.get("GroupName",""), "Description": sg.get("Description","")})

    st.write(f"Unattached EBS volumes: {len(unattached_vols)}")
    st.write(f"Unassociated EIPs: {len(unassoc_eips)}")
    st.write(f"Unused non-default Security Groups: {len(unused_sgs)}")

    col_v, col_e, col_s = st.columns(3)
    with col_v:
        if unattached_vols:
            st.dataframe(pd.DataFrame(unattached_vols), use_container_width=True)
            if st.button("🧹 Delete Unattached Volumes (dry_run)"):
                payload = {"action":"delete_volumes","volume_ids":[v["VolumeId"] for v in unattached_vols],"dry_run":True}
                st.json(call_cleanup_lambda(payload))
            if st.button("🧹 Delete Unattached Volumes (execute)"):
                if st.confirm("Delete unattached volumes? This will permanently delete data on those volumes."):
                    payload = {"action":"delete_volumes","volume_ids":[v["VolumeId"] for v in unattached_vols],"dry_run":False}
                    st.json(call_cleanup_lambda(payload))
    with col_e:
        if unassoc_eips:
            eip_df = pd.DataFrame([{"PublicIp":a.get("PublicIp"), "AllocationId":a.get("AllocationId")} for a in unassoc_eips])
            st.dataframe(eip_df, use_container_width=True)
            if st.button("🔓 Release Unassociated EIPs (dry_run)"):
                payload = {"action":"release_eips","allocation_ids":[a.get("AllocationId") for a in unassoc_eips],"dry_run":True}
                st.json(call_cleanup_lambda(payload))
            if st.button("🔓 Release Unassociated EIPs (execute)"):
                if st.confirm("Release selected Elastic IPs?"):
                    payload = {"action":"release_eips","allocation_ids":[a.get("AllocationId") for a in unassoc_eips],"dry_run":False}
                    st.json(call_cleanup_lambda(payload))
    with col_s:
        if unused_sgs:
            st.dataframe(pd.DataFrame(unused_sgs), use_container_width=True)
            if st.button("🧾 Delete Unused Security Groups (dry_run)"):
                payload = {"action":"delete_security_groups","group_ids":[g["GroupId"] for g in unused_sgs],"dry_run":True}
                st.json(call_cleanup_lambda(payload))
            if st.button("🧾 Delete Unused Security Groups (execute)"):
                if st.confirm("Delete unused security groups?"):
                    payload = {"action":"delete_security_groups","group_ids":[g["GroupId"] for g in unused_sgs],"dry_run":False}
                    st.json(call_cleanup_lambda(payload))

# Calculate potential stale resource savings
stale_savings = estimate_ebs_savings(unattached_vols) + estimate_eip_savings(unassoc_eips)
total_savings = potential_savings + stale_savings

savings_placeholder.metric("Total Potential Monthly Savings", f"${total_savings:,.2f}")

st.markdown("---")
st.subheader("Logs / Cleanup Results")
if st.button("Show last cleanup logs"):
    # optional: fetch logs from a DynamoDB table the cleanup Lambda writes to
    try:
        table = clients["dynamodb"].Table(CLEANUP_LOG_TABLE)
        resp = table.scan(Limit=20)
        st.dataframe(pd.DataFrame(resp.get("Items", [])))
    except Exception as e:
        st.error("No cleanup log table found or cannot access it: " + str(e))

# Auto refresh
time.sleep(0.5)
#st.rerun() if refresh_interval and refresh_interval > 0 else None
