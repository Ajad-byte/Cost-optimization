# dashboard.py (merged + polished)
import streamlit as st
import boto3
import pandas as pd
import plotly.express as px
from datetime import datetime
from functools import lru_cache
import time
import json
from botocore.exceptions import ClientError

# -----------------------------
# Config
# -----------------------------
DDB_TABLE = "EC2IdleInstanceMetrics"          # DynamoDB table storing latest state per InstanceId
CLEANUP_LAMBDA = "cleanup_lambda"             # Your cleanup Lambda function name
CLEANUP_LOG_TABLE = "EC2CleanupLogs"          # optional logs table created by cleanup lambda
DETECTION_LAMBDA = "detect_ec2_idle_and_stale"  # <— your detector lambda (invoked from UI)

st.set_page_config(page_title="EC2 Idle Dashboard + Cleanup", layout="wide", page_icon="🧰")

# -----------------------------
# UI Enhancements (CSS + header)
# -----------------------------
st.markdown(
    """
    <style>
        [data-testid="stAppViewContainer"] { background-color: #0e1117; color: #fafafa; }
        [data-testid="stSidebar"] { background-color: #1e2229; }
        h1, h2, h3, h4, h5, h6, .stMetric label, .stMetric { color: #ffffff !important; }
        .stButton button { background-color: #2b313e !important; color: white !important; border-radius: 10px !important; border: 1px solid #3b4252 !important; }
        .stButton button:hover { background-color: #3a404f !important; color: #00c4ff !important; }
        .stDataFrame { background-color: #181c25 !important; color: white !important; }
        .stMarkdown, .stTextInput, .stSelectbox, .stSlider { color: white !important; }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown(
    """
    <div class="app-header">
        <h1>🖥️ <span style='color:#2563eb;'>Intelligent AWS EC2 Cost Optimization</span> & Stale Resource Management</h1>
    </div>
    <div class="app-sub">
        <p style='font-size:1.0rem;'>Monitor EC2 instance state, identify idle resources, and run safe cleanup actions via Lambda</p>
    </div>
    """,
    unsafe_allow_html=True
)

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
# Utilities (Dynamo + Lambda)
# -----------------------------
def fetch_instances_from_dynamo(table_name=DDB_TABLE):
    """Fetch all items with strong consistency so fresh Lambda writes appear immediately."""
    try:
        table = clients["dynamodb"].Table(table_name)
        resp = table.scan(ConsistentRead=True)
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], ConsistentRead=True)
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


def call_detection_lambda(payload=None):
    """Invoke your detector Lambda to write fresh rows to DynamoDB, then reload UI."""
    try:
        resp = clients["lambda"].invoke(
            FunctionName=DETECTION_LAMBDA,
            InvocationType="RequestResponse",  # wait for completion
            Payload=json.dumps(payload or {}).encode(),
        )
        body = resp["Payload"].read().decode() or "{}"
        return json.loads(body)
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
    total_savings = 0.0
    idle_instances = df[df["Status"] == "Idle"]
    for _, row in idle_instances.iterrows():
        cost_hr = EC2_HOURLY_COST.get(row.get("InstanceType"), 0.05)
        total_savings += cost_hr * 24 * 30  # 720 hours/month
    return total_savings


def estimate_ebs_savings(unattached_vols):
    return sum(v.get("SizeGiB", 0) * 0.10 for v in unattached_vols)


def estimate_eip_savings(unassoc_eips):
    return len(unassoc_eips) * 0.005 * 24 * 30  # monthly


def estimate_total_savings(df, unattached_vols, unassoc_eips):
    return (
        estimate_ec2_savings(df) +
        estimate_ebs_savings(unattached_vols) +
        estimate_eip_savings(unassoc_eips)
    )


# -----------------------------
# Sidebar: controls
# -----------------------------
st.sidebar.header("Controls")
refresh_interval = st.sidebar.number_input("Auto-refresh (seconds)", min_value=10, max_value=600, value=30, step=10)
st.sidebar.markdown("Actions are executed by a separate Lambda for safety.")
scan_stale = st.sidebar.checkbox("Scan stale resources (EBS/EIP/Unused SG)", value=True)
scan_regions = st.sidebar.text_input("Region (leave blank for default session region)", value="")

# Run detection now (invokes your Lambda and reloads)
if st.sidebar.button("🛰️ Run Detection Now"):
    res = call_detection_lambda()
    if "error" in res:
        st.error(f"Detection Lambda failed: {res['error']}")
    else:
        st.success("Detection completed. Fetching latest data…")
        time.sleep(1.0)  # small pause; ConsistentRead usually enough
        st.rerun()

# Manual refresh
if st.sidebar.button("🔄 Refresh Now"):
    st.rerun()

# Optional: gentle auto-refresh by tweaking query params
try:
    st.query_params(_=int(time.time() // max(1, int(refresh_interval))))
except Exception:
    pass

# -----------------------------
# Load instance state data from DynamoDB
# -----------------------------
raw_items = fetch_instances_from_dynamo(DDB_TABLE)

# Keep only latest row per instance (so fresh data isn't masked by history rows)
def _parse_ts(x):
    try:
        return pd.to_datetime(x)
    except Exception:
        return pd.Timestamp(0)


def latest_per_instance(items):
    if not items:
        return []
    df_hist = pd.DataFrame(items)
    if "EvaluationTimestamp" not in df_hist.columns and "LastUpdated" in df_hist.columns:
        df_hist["EvaluationTimestamp"] = df_hist["LastUpdated"]
    if "EvaluationTimestamp" not in df_hist.columns:
        df_hist["EvaluationTimestamp"] = ""
    df_hist["__ts__"] = df_hist["EvaluationTimestamp"].apply(_parse_ts)
    df_hist = df_hist.sort_values("__ts__").drop_duplicates(subset=["InstanceId"], keep="last")
    return df_hist.drop(columns=["__ts__"], errors="ignore").to_dict(orient="records")


raw_items = latest_per_instance(raw_items)

# -----------------------------
# Include all currently running EC2 instances from AWS (live enrichment merge)
# -----------------------------

def fetch_all_live_ec2_instances():
    ec2 = clients["ec2"]
    all_instances = []
    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    all_instances.append(inst)
    except Exception as e:
        st.warning(f"Error fetching live EC2 instances (live enrichment): {e}")
    return all_instances


live_instances = fetch_all_live_ec2_instances()

# Build a set of instance IDs already in DynamoDB
ddb_ids = {item.get("InstanceId") for item in raw_items if "InstanceId" in item}

# Add any new (not yet in DynamoDB) instances — keep them visible but flagged as New
for inst in live_instances:
    iid = inst.get("InstanceId")
    if not iid:
        continue
    if iid not in ddb_ids:
        raw_items.append({
            "InstanceId": iid,
            "InstanceType": inst.get("InstanceType", "unknown"),
            "Region": inst.get("Placement", {}).get("AvailabilityZone", "")[:-1] or "unknown",
            "AvgCPU": 0.0,
            "TotalNetworkBytes": 0,
            "Status": inst.get("State", {}).get("Name", "unknown").capitalize(),
            "LastUpdated": datetime.utcnow().isoformat(),
            "Recommendation": "New instance (not yet tracked)",
        })

if not raw_items:
    st.warning("No data in DynamoDB table. Run your detection Lambda to populate latest instance state.")
    st.stop()

# -----------------------------
# Normalize and sanitize loaded items
# -----------------------------
cleaned_items = []
for it in raw_items:
    # Timestamps
    if "LastUpdated" not in it:
        it["LastUpdated"] = it.get("EvaluationTimestamp", datetime.utcnow().isoformat())

    # Ensure numeric fields
    try:
        it["AvgCPU"] = float(it.get("AvgCPU", 0) or 0)
    except Exception:
        it["AvgCPU"] = 0.0
    try:
        it["TotalNetworkBytes"] = int(it.get("TotalNetworkBytes", 0) or 0)
    except Exception:
        it["TotalNetworkBytes"] = 0

    # Region / Type defaults (don't drop unknowns)
    it["Region"] = it.get("Region") or "unknown"
    it["InstanceType"] = it.get("InstanceType", "unknown")

    # Recommendations based on Status
    status_val = it.get("Status")
    if status_val == "Idle":
        it["Recommendation"] = "Stop (recommended)"
    elif status_val == "Active":
        it["Recommendation"] = "Running normally"
    elif status_val == "Stopped":
        it["Recommendation"] = "Consider terminating if not needed"
    elif status_val == "Stopping":
        it["Recommendation"] = "Wait for stop completion"
    elif status_val == "Pending":
        it["Recommendation"] = "Wait for start completion"
    else:
        it["Recommendation"] = it.get("Recommendation", "Needs review")

    cleaned_items.append(it)


df = pd.DataFrame(cleaned_items)

# -----------------------------
# Refresh live instance runtime state from EC2 (status correctness)
# -----------------------------

@lru_cache(maxsize=32)
def ec2_client_for(region_name: str):
    """Return a cached EC2 client for a specific region."""
    try:
        return boto3.Session().client("ec2", region_name=region_name)
    except Exception:
        # Fallback to default client if region-specific creation fails
        return clients["ec2"]

def enrich_with_live_instance_state(df_in: pd.DataFrame):
    if df_in.empty:
        return df_in

    # Ensure Region column exists so we can look up in the correct region
    if "Region" not in df_in.columns:
        df_in["Region"] = ""

    df_in = df_in.copy()
    df_in["InstanceState"] = df_in.get("InstanceState", "unknown")

    # Build live state map by querying EC2 per-region using paginated describe (no InstanceIds filter)
    # This avoids InvalidInstanceID errors for terminated/unknown IDs and ensures we see stopped/running.
    live_states = {}
    regions = sorted([r for r in df_in["Region"].dropna().unique().tolist() if r])

    for region in regions:
        ec2_reg = ec2_client_for(region)
        try:
            paginator = ec2_reg.get_paginator("describe_instances")
            for page in paginator.paginate():
                for res in page.get("Reservations", []):
                    for inst in res.get("Instances", []):
                        iid = inst.get("InstanceId")
                        state = inst.get("State", {}).get("Name", "unknown")
                        if iid:
                            live_states[iid] = state
        except Exception:
            # On region fetch failure, leave those states as unknown
            pass

    # Apply states back to dataframe; if an ID not found in live map, assume terminated (better than unknown)
    def map_state(iid, prev):
        if not iid:
            return prev or "unknown"
        return live_states.get(iid, "terminated")

    df_in["InstanceState"] = df_in.apply(lambda r: map_state(r.get("InstanceId"), r.get("InstanceState")), axis=1)

    def compute_status(row):
        inst_state = row.get("InstanceState", "unknown")
        if inst_state != "running":
            return (inst_state or "unknown").capitalize()
        try:
            avg_cpu = float(row.get("AvgCPU", 0))
        except Exception:
            avg_cpu = 0.0
        return "Idle" if avg_cpu < 10.0 else "Active"

    df_in["Status"] = df_in.apply(compute_status, axis=1)
    return df_in


df = enrich_with_live_instance_state(df)

# -----------------------------
# Optional: filter out instances not returned by EC2 (which may hide terminated)
# -----------------------------

def list_current_instance_ids():
    ec2 = clients["ec2"]
    ids = []
    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    ids.append(inst.get("InstanceId"))
    except Exception as e:
        st.warning(f"Could not list current AWS instances for cleanup filtering: {e}")
    return set([i for i in ids if i])

st.sidebar.markdown("---")
hide_nonexistent = st.sidebar.checkbox("Hide instances not currently returned by EC2 (may hide terminated)", value=True)
if hide_nonexistent:
    current_ids = list_current_instance_ids()
    if current_ids:
        df = df[df["InstanceId"].isin(current_ids)]

# Remove obvious empties and tidy up
if "InstanceState" in df.columns:
    df = df[df["InstanceState"].notna()]

df = df.reset_index(drop=True)

# -----------------------------
# Top KPIs (UI-friendly card layout)
# -----------------------------
try:
    total_instances = len(df)
    running_count = df[df["InstanceState"] == "running"].shape[0]
    idle_count = df[df["Status"] == "Idle"].shape[0]
    active_count = df[df["Status"] == "Active"].shape[0]
except Exception:
    total_instances = running_count = idle_count = active_count = 0

st.markdown("### ⚙️ Instance Summary")
with st.container():
    k1, k2, k3 = st.columns(3)
    with k1:
        st.metric("Total Instances (tracked)", total_instances)
    with k2:
        st.metric("Running Instances", running_count)
    with k3:
        pct = (idle_count / running_count * 100) if running_count else 0
        st.metric("Idle Instances (running)", idle_count, delta=f"{pct:.1f}%")

st.markdown("---")

# -----------------------------
# Potential Savings Section
# -----------------------------
potential_savings = estimate_ec2_savings(df)
# default placeholders for stale resources if not scanned
unattached_vols, unassoc_eips, unused_sgs = [], [], []

st.subheader("💰 Estimated Monthly Cost Savings")
col_s1, col_s2 = st.columns([2, 1])
col_s1.metric("Potential EC2 Savings (Idle)", f"${potential_savings:,.2f}")
savings_placeholder = col_s2.empty()

# -----------------------------
# Charts
# -----------------------------
left, right = st.columns([2, 1])
with left:
    if not df.empty and "Status" in df.columns:
        status_counts = df["Status"].value_counts().reset_index()
        status_counts.columns = ["Status", "Count"]
        fig = px.pie(status_counts, names="Status", values="Count", title="Instance Status")
        fig.update_layout(template="plotly_white", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(size=13))
        st.plotly_chart(fig, use_container_width=True)

    running_df = df[df.get("InstanceState", "") == "running"] if not df.empty else pd.DataFrame()
    if not running_df.empty and "AvgCPU" in running_df.columns:
        fig2 = px.histogram(running_df, x="AvgCPU", nbins=20, title="Average CPU Distribution (running instances)")
        fig2.update_layout(xaxis_title="Avg CPU (%)", template="plotly_white", paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No running instances to show CPU distribution.")

with right:
    st.subheader("By Region")
    if "Region" in df.columns and not df.empty:
        region_table = df.groupby(["Region", "Status"]).size().unstack(fill_value=0)
        st.dataframe(region_table)
    else:
        st.text("No region data available.")

# health score
health_score = (active_count / total_instances) * 100 if total_instances else 0
st.progress(health_score / 100)
st.caption(f"🔹 Instance health score: {health_score:.1f}% active workload utilization")

st.markdown("---")

# -----------------------------
# Instance table and actions
# -----------------------------
st.subheader("Instance Inventory & Actions")
show_filters = st.expander("Filters")
with show_filters:
    regions = sorted(df["Region"].unique().tolist()) if "Region" in df.columns else []
    sel_regions = st.multiselect("Regions", regions, default=regions)
    statuses = df["Status"].unique().tolist() if "Status" in df.columns else []
    sel_status = st.multiselect("Status", statuses, default=statuses)
    min_cpu = st.slider("Min Avg CPU (%)", 0.0, 100.0, 0.0)

filtered = df[
    (df["Region"].isin(sel_regions)) &
    (df["Status"].isin(sel_status)) &
    (df["AvgCPU"] >= min_cpu)
].sort_values(["Status", "Region"]) if not df.empty else pd.DataFrame()

filtered = filtered.reset_index(drop=True)
filtered["_select"] = False if filtered.empty else filtered.get("_select", False)

try:
    if not filtered.empty:
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
    else:
        edited = pd.DataFrame()
except Exception:
    st.dataframe(filtered[["InstanceId", "Region", "InstanceType", "AvgCPU", "Status", "Recommendation"]] if not filtered.empty else filtered)
    st.warning("Editable table not available in this Streamlit version. Update Streamlit to use selection checkboxes.")
    edited = pd.DataFrame()

selected_ids = []
if not edited.empty and "_select" in edited.columns:
    selected_ids = edited[edited["_select"] == True]["InstanceId"].tolist()

st.markdown("### ⚡ Instance Actions")
st.caption("Select instances above and perform safe AWS operations below:")

col_a, col_b, col_c = st.columns(3)

with col_a:
    st.markdown("##### 🛑 Stop Instances")
    if st.button("Stop Selected"):
        if not selected_ids:
            st.warning("No instances selected.")
        else:
            payload = {"action": "stop_instances", "instance_ids": selected_ids, "dry_run": False}
            res = call_cleanup_lambda(payload)
            if "error" in res:
                st.error(f"❌ Failed to stop instances: {res['error']}")
            else:
                st.success(f"✅ {len(selected_ids)} instance(s) stop initiated.")

with col_b:
    st.markdown("##### ▶️ Start Instances")
    if st.button("Start Selected"):
        if not selected_ids:
            st.warning("No instances selected.")
        else:
            payload = {"action": "start_instances", "instance_ids": selected_ids, "dry_run": False}
            res = call_cleanup_lambda(payload)
            if "error" in res:
                st.error(f"❌ Failed to start instances: {res['error']}")
            else:
                st.success(f"🚀 {len(selected_ids)} instance(s) start initiated.")

with col_c:
    st.markdown("##### ☠️ Terminate Instances")
    confirm_phrase = st.text_input("Type TERMINATE to confirm permanent termination")
    if st.button("Terminate Selected"):
        if not selected_ids:
            st.warning("No instances selected.")
        elif confirm_phrase != "TERMINATE":
            st.warning("Confirmation text mismatch. Type TERMINATE.")
        else:
            payload = {"action": "terminate_instances", "instance_ids": selected_ids, "dry_run": False}
            res = call_cleanup_lambda(payload)
            if "error" in res:
                st.error(f"❌ Failed to terminate instances: {res['error']}")
            else:
                st.success(f"☠️ {len(selected_ids)} instance(s) termination initiated.")

# -----------------------------
# Stale resource detection (EBS/EIP/SG)
# -----------------------------
st.markdown("---")
st.subheader("🧭 Stale Resource Detection")

unattached_vols, unassoc_eips, unused_sgs = [], [], []

if scan_stale:
    st.info("🔍 Scanning your AWS account for stale resources (EBS, EIP, Security Groups)...")
    ec2_client = clients["ec2"]

    # Unattached EBS volumes
    try:
        vols = ec2_client.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]
        unattached_vols = [
            {
                "VolumeId": v.get("VolumeId"),
                "SizeGiB": v.get("Size", 0),
                "Region": (v.get("AvailabilityZone", "")[:-1]) if v.get("AvailabilityZone") else "unknown",
                "CreateTime": v.get("CreateTime").strftime("%Y-%m-%d") if v.get("CreateTime") else "N/A",
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
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            enis = ec2_client.describe_network_interfaces(Filters=[{"Name": "group-id", "Values": [sg.get("GroupId")] }])["NetworkInterfaces"]
            if len(enis) == 0:
                unused_sgs.append({
                    "GroupId": sg.get("GroupId"),
                    "GroupName": sg.get("GroupName", ""),
                    "Description": sg.get("Description", ""),
                })
    except Exception as e:
        st.error(f"Error evaluating security groups: {e}")

    # Quick summary metrics
    colm1, colm2, colm3 = st.columns(3)
    colm1.metric("🧾 Unattached Volumes", len(unattached_vols))
    colm2.metric("🌐 Unassociated EIPs", len(unassoc_eips))
    colm3.metric("🛡️ Unused Security Groups", len(unused_sgs))

    # Savings
    stale_savings = estimate_ebs_savings(unattached_vols) + estimate_eip_savings(unassoc_eips)
    total_savings = potential_savings + stale_savings

    st.markdown(f"### 💰 Potential Monthly Savings: **${total_savings:,.2f}**")
    st.progress(min(total_savings / 500, 1.0))
    st.caption("Includes idle EC2 instance savings + stale resources (EBS/EIP).")

    # Resource panels
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
# Savings Summary & Cleanup Logs
# -----------------------------
st.markdown("---")
st.subheader("💵 Savings Overview")
# Recompute total_savings if not set above
try:
    total_savings
except NameError:
    total_savings = potential_savings

st.metric("Total Potential Monthly Savings", f"${total_savings:,.2f}")
st.caption("Real-time view of potential cost savings opportunities across your AWS account.")

st.markdown("---")
st.subheader("🪵 Cleanup Logs")
if st.button("📜 Show last cleanup logs"):
    try:
        table = clients["dynamodb"].Table(CLEANUP_LOG_TABLE)
        resp = table.scan(Limit=20)
        st.dataframe(pd.DataFrame(resp.get("Items", [])))
    except Exception as e:
        st.error("No cleanup log table found or cannot access it: " + str(e))

# Gentle sleep to keep UI responsive
time.sleep(0.2)
