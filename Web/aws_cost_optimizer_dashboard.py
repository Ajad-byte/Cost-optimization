import streamlit as st
import boto3
import pandas as pd
import plotly.express as px
from datetime import datetime
import time
import json
import math

# -----------------------------
# Config
# -----------------------------
DDB_TABLE = "EC2IdleInstanceMetrics"         # DynamoDB table storing latest state per InstanceId
CLEANUP_LAMBDA = "cleanup_lambda"            # Your cleanup Lambda function name
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
    """Estimate monthly savings from stopping idle instances (720 hours/month)."""
    total_savings = 0.0
    idle_instances = df[df["Status"] == "Idle"]
    for _, row in idle_instances.iterrows():
        cost_hr = EC2_HOURLY_COST.get(row.get("InstanceType"), 0.05)
        total_savings += cost_hr * 24 * 30  # 720 hours/month
    return total_savings

def estimate_ebs_savings(unattached_vols):
    """EBS ~$0.10/GB-month - uses SizeGiB value on volumes list"""
    return sum(v.get("SizeGiB", 0) * 0.10 for v in unattached_vols)

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
scan_regions = st.sidebar.text_input("Region (leave blank for default session region)", value="")

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
    try:
        it["AvgCPU"] = float(it.get("AvgCPU", 0) or 0)
    except Exception:
        it["AvgCPU"] = 0.0
    try:
        it["TotalNetworkBytes"] = int(it.get("TotalNetworkBytes", 0) or 0)
    except Exception:
        it["TotalNetworkBytes"] = 0
    it["Region"] = it.get("Region", "unknown")
    it["InstanceType"] = it.get("InstanceType", "unknown")
    
    # Enhanced recommendations based on status
    if it.get("Status") == "Idle":
        it["Recommendation"] = "Stop (recommended)"
    elif it.get("Status") == "Active":
        it["Recommendation"] = "Running normally"
    elif it.get("Status") == "Stopped":
        it["Recommendation"] = "Consider terminating if not needed"
    elif it.get("Status") == "Stopping":
        it["Recommendation"] = "Wait for stop completion"
    elif it.get("Status") == "Pending":
        it["Recommendation"] = "Wait for start completion"
    else:
        it["Recommendation"] = "Needs review"

df = pd.DataFrame(raw_items)

# -----------------------------
# Refresh live instance runtime state from EC2 (so stopped instances are shown correctly)
# -----------------------------
def enrich_with_live_instance_state(df_in):
    """Query EC2 DescribeInstances for instance IDs in df and add InstanceState column.
       For running instances, keep Idle/Active logic using AvgCPU; for non-running, set Status to stopped/stopping/etc.
    """
    if df_in.empty:
        return df_in

    instance_ids = df_in["InstanceId"].dropna().unique().tolist()
    if not instance_ids:
        df_in["InstanceState"] = "unknown"
        return df_in

    ec2 = clients["ec2"]
    # Describe in chunks of 100
    live_states = {}
    for i in range(0, len(instance_ids), 100):
        chunk = instance_ids[i:i+100]
        try:
            resp = ec2.describe_instances(InstanceIds=chunk)
            for res in resp.get("Reservations", []):
                for inst in res.get("Instances", []):
                    iid = inst["InstanceId"]
                    state = inst["State"]["Name"]  # e.g., running, stopped
                    live_states[iid] = state
        except Exception:
            # if describe fails (permissions or missing instance), leave unknown
            for iid in chunk:
                live_states.setdefault(iid, "unknown")

    # Map states back to df
    df_in["InstanceState"] = df_in["InstanceId"].map(lambda x: live_states.get(x, "unknown"))

    # Recompute Status: if not running, mark as that state; if running, determine Idle/Active by AvgCPU threshold (10)
    def compute_status(row):
        inst_state = row.get("InstanceState", "unknown")
        if inst_state != "running":
            return inst_state.capitalize()  # Stopped, Pending, etc.
        # running -> use AvgCPU to determine Idle/Active
        try:
            avg_cpu = float(row.get("AvgCPU", 0))
        except Exception:
            avg_cpu = 0.0
        return "Idle" if avg_cpu < 10.0 else "Active"

    df_in["Status"] = df_in.apply(compute_status, axis=1)
    return df_in

df = enrich_with_live_instance_state(df)

# -----------------------------
# Top KPIs
# -----------------------------
# total_instances = all rows in DynamoDB (includes stopped)
total_instances = len(df)
# running_count = only instances with runtime state 'running'
running_count = df[df["InstanceState"] == "running"].shape[0]
idle_count = df[df["Status"] == "Idle"].shape[0]
active_count = df[df["Status"] == "Active"].shape[0]
#last_update = df["LastUpdated"].max()----

st.metric("Total Instances (tracked)", total_instances)
col1, col2, col3 = st.columns(3)
col1.metric("Running Instances", running_count)
col2.metric("Idle Instances (running)", idle_count,
            delta=f"{(idle_count/running_count*100):.1f}%" if running_count else "0%")
#col3.metric("Last seen (most recent)", last_update if isinstance(last_update, str) else str(last_update))----

st.markdown("---")

# -----------------------------
# Potential Savings Section
# -----------------------------
potential_savings = estimate_ec2_savings(df)
st.subheader("💰 Estimated Monthly Cost Savings")

col_s1, col_s2 = st.columns(2)
col_s1.metric("Potential EC2 Savings (Idle)", f"${potential_savings:,.2f}")

# Prepare placeholders for stale resources (will be updated after scanning)
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

    fig2 = px.histogram(df[df["InstanceState"] == "running"], x="AvgCPU", nbins=20, title="Average CPU Distribution (running instances)")
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
    if selected_ids:
        confirm_terminate = st.checkbox("⚠️ Confirm terminate selected instances (irreversible)", key="confirm_term")
        if st.button("🗑️ Terminate Selected Instances (irreversible)") and confirm_terminate:
            payload = {"action": "terminate_instances", "instance_ids": selected_ids, "dry_run": False}
            res = call_cleanup_lambda(payload)
            st.json(res)

# -----------------------------sections2 -stale ressource detection
# -----------------------------
# 🧹 Enhanced Stale Resource Detection & Cleanup
# -----------------------------
st.markdown("---")
st.subheader("🧭 Stale Resource Detection")

# Ensure lists exist even if scan_stale is False
unattached_vols, unassoc_eips, unused_sgs = [], [], []

if scan_stale:
    st.info("🔍 Scanning your AWS account for stale resources (EBS, EIP, Security Groups)...")
    ec2_client = clients["ec2"]

    # Unattached EBS volumes
    try:
        vols = ec2_client.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]
        unattached_vols = [
            {
                "VolumeId": v["VolumeId"],
                "SizeGiB": v["Size"],
                "Region": v.get("AvailabilityZone", "")[:-1],
                "CreateTime": v.get("CreateTime", "").strftime("%Y-%m-%d") if v.get("CreateTime") else "N/A",
            }
            for v in vols
        ]
    except Exception as e:
        st.error(f"Error fetching volumes: {e}")

    # Unassociated Elastic IPs
    try:
        addrs = ec2_client.describe_addresses()["Addresses"]
        unassoc_eips = [a for a in addrs if "AssociationId" not in a]
    except Exception as e:
        st.error(f"Error fetching Elastic IPs: {e}")

    # Unused Security Groups
    try:
        sgs = ec2_client.describe_security_groups()["SecurityGroups"]
        unused_sgs = []
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            enis = ec2_client.describe_network_interfaces(Filters=[{"Name": "group-id", "Values": [sg["GroupId"]]}])["NetworkInterfaces"]
            if len(enis) == 0:
                unused_sgs.append(
                    {
                        "GroupId": sg["GroupId"],
                        "GroupName": sg.get("GroupName", ""),
                        "Description": sg.get("Description", ""),
                    }
                )
    except Exception as e:
        st.error(f"Error evaluating security groups: {e}")

    # Quick summary metrics
    colm1, colm2, colm3 = st.columns(3)
    colm1.metric("🧾 Unattached Volumes", len(unattached_vols))
    colm2.metric("🌐 Unassociated EIPs", len(unassoc_eips))
    colm3.metric("🛡️ Unused Security Groups", len(unused_sgs))

    # Calculate savings
    stale_savings = estimate_ebs_savings(unattached_vols) + estimate_eip_savings(unassoc_eips)
    total_savings = potential_savings + stale_savings

    st.markdown(f"### 💰 Potential Monthly Savings: **${total_savings:,.2f}**")
    st.progress(min(total_savings / 500, 1.0))
    st.caption("Includes idle EC2 instance savings + stale resources (EBS/EIP).")

    # -----------------------------
    # Resource Panels (Collapsible)
    # -----------------------------
    with st.expander("💾 Unattached EBS Volumes", expanded=False):
        if unattached_vols:
            vdf = pd.DataFrame(unattached_vols)
            st.dataframe(vdf, use_container_width=True)
            dry_run_vol = st.checkbox("🔎 Dry Run Delete Volumes (simulate)", key="dry_vol")
            if st.button("🧹 Delete Unattached Volumes (dry_run)"):
                payload = {"action":"delete_volumes","volume_ids":[v["VolumeId"] for v in unattached_vols],"dry_run":True}
                st.json(call_cleanup_lambda(payload))
            confirm_delete_vol = st.checkbox("⚠️ Confirm delete unattached volumes (permanent)", key="confirm_delete_vol")
            if st.button("🧹 Delete Unattached Volumes (execute)") and confirm_delete_vol:
                payload = {"action":"delete_volumes","volume_ids":[v["VolumeId"] for v in unattached_vols],"dry_run":False}
                st.json(call_cleanup_lambda(payload))
        else:
            st.success("✅ No unattached volumes found!")

    with st.expander("🌐 Unassociated Elastic IPs", expanded=False):
        if unassoc_eips:
            eip_df = pd.DataFrame([{"PublicIp": a.get("PublicIp"), "AllocationId": a.get("AllocationId")} for a in unassoc_eips])
            st.dataframe(eip_df, use_container_width=True)
            dry_run_eip = st.checkbox("🔎 Dry Run Release EIPs", key="dry_eip")
            if st.button("🔓 Release Unassociated EIPs (dry_run)"):
                payload = {"action":"release_eips","allocation_ids":[a.get("AllocationId") for a in unassoc_eips],"dry_run":True}
                st.json(call_cleanup_lambda(payload))
            confirm_release_eip = st.checkbox("⚠️ Confirm release selected Elastic IPs", key="confirm_release_eip")
            if st.button("🔓 Release Unassociated EIPs (execute)") and confirm_release_eip:
                payload = {"action":"release_eips","allocation_ids":[a.get("AllocationId") for a in unassoc_eips],"dry_run":False}
                st.json(call_cleanup_lambda(payload))
        else:
            st.success("✅ No unassociated Elastic IPs found!")

    with st.expander("🧱 Unused Security Groups", expanded=False):
        if unused_sgs:
            sgs_df = pd.DataFrame(unused_sgs)
            st.dataframe(sgs_df, use_container_width=True)
            dry_run_sg = st.checkbox("🔎 Dry Run Delete Security Groups", key="dry_sg")
            if st.button("🧾 Delete Unused Security Groups (dry_run)"):
                payload = {"action":"delete_security_groups","group_ids":[g["GroupId"] for g in unused_sgs],"dry_run":True}
                st.json(call_cleanup_lambda(payload))
            confirm_delete_sg = st.checkbox("⚠️ Confirm delete unused security groups", key="confirm_delete_sg")
            if st.button("🧾 Delete Unused Security Groups (execute)") and confirm_delete_sg:
                payload = {"action":"delete_security_groups","group_ids":[g["GroupId"] for g in unused_sgs],"dry_run":False}
                st.json(call_cleanup_lambda(payload))
        else:
            st.success("✅ No unused security groups found!")            

else:
    st.info("Enable the 'Scan Stale Resources' option to fetch current EBS, EIP, and Security Group data.")

# -----------------------------
# Savings Summary
# -----------------------------
st.markdown("---")
st.subheader("💵 Savings Overview")
st.metric("Total Potential Monthly Savings", f"${total_savings:,.2f}")
st.caption("Real-time view of potential cost savings opportunities across your AWS account.")

# -----------------------------
# Cleanup Logs (Optional)
# -----------------------------
st.markdown("---")
st.subheader("🪵 Cleanup Logs")
if st.button("📜 Show last cleanup logs"):
    try:
        table = clients["dynamodb"].Table(CLEANUP_LOG_TABLE)
        resp = table.scan(Limit=20)
        st.dataframe(pd.DataFrame(resp.get("Items", [])))
    except Exception as e:
        st.error("No cleanup log table found or cannot access it: " + str(e))


# Auto refresh
time.sleep(0.5)
# st.experimental_rerun() -- avoid continuous looping; user can use Refresh button
#st_autorefresh = st.empty()
#st_autorefresh.code(f"# Auto-refresh disabled to avoid loops. Use the Refresh button or