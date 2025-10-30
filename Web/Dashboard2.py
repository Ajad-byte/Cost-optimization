import streamlit as st
import boto3
import json
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta

# -------------------------------
# Streamlit Page Configuration
# -------------------------------
st.set_page_config(
    page_title="AWS Cost Optimization Dashboard",
    page_icon="💰",
    layout="wide"
)

# -------------------------------
# AWS Clients
# -------------------------------
@st.cache_resource(ttl=300)  # Cache for 5 minutes
def get_aws_clients():
    return {
        "s3": boto3.client("s3"),
        "lambda": boto3.client("lambda"),
        "ce": boto3.client("ce")
    }

def invoke_lambda_analysis():
    """Trigger fresh analysis via Lambda"""
    try:
        clients = get_aws_clients()
        response = clients["lambda"].invoke(
            FunctionName='EC2IdleDetection',  # Use your actual Lambda function name
            InvocationType='RequestResponse'
        )
        return True
    except Exception as e:
        st.error(f"Error invoking Lambda: {str(e)}")
        return False

# -------------------------------
# Helper: Read JSON from S3
# -------------------------------
def get_lambda_results_from_s3(bucket_name, key):
    try:
        clients = get_aws_clients()
        response = clients["s3"].get_object(Bucket=bucket_name.strip(), Key=key.strip())
        data = json.loads(response["Body"].read())
        return data
    except Exception as e:
        st.error(f"Error reading from S3: {str(e)}")
        return None

# -------------------------------
# Fetch Cost Explorer Data
# -------------------------------
def fetch_cost_explorer_data(start_date=None, end_date=None, granularity="DAILY"):
    ce = get_aws_clients()["ce"]

    if not end_date:# Helper
        end_date = datetime.utcnow().date()
    if not start_date:
        start_date = end_date - timedelta(days=7)

    response = ce.get_cost_and_usage(
        TimePeriod={
            "Start": start_date.strftime("%Y-%m-%d"),
            "End": end_date.strftime("%Y-%m-%d")
        },
        Granularity=granularity,
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}]
    )

    rows = []
    for result in response["ResultsByTime"]:
        time_period = result["TimePeriod"]["Start"]
        for group in result.get("Groups", []):
            service = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            rows.append({"Date": time_period, "Service": service, "Cost": amount})

    return pd.DataFrame(rows)

# -------------------------------
# Main App
# -------------------------------
def main():
    st.title("💰 AWS Cost Optimization Dashboard")

    # -------------------------------
    # SECTION 1: Idle EC2 Analysis
    # -------------------------------
    st.header("🔍 EC2 Idle Instance Analysis")

    col1, col2 = st.columns([3, 1])
    with col1:
        s3_bucket = st.text_input(
            "cost-optimization-data-s3",
            value="cost-optimization-data-s3",
            help="S3 bucket where Lambda results are stored"
        )
    with col2:
        s3_key = st.text_input(
            "S3 Key",
            value="lambda-outputs/idle-instance-analysis.json",
            help="S3 key for the analysis results"
        )

    if st.button("Refresh Idle EC2 Analysis"):
        with st.spinner("Running fresh EC2 analysis..."):
            if invoke_lambda_analysis():
                st.success("Analysis complete! Fetching results...")
                # Clear any cached data
                st.cache_resource.clear()
                data = get_lambda_results_from_s3(s3_bucket, s3_key)
            else:
                st.error("Failed to trigger analysis")
    else:
        data = get_lambda_results_from_s3(s3_bucket, s3_key)
    if data:
        metadata = data.get("metadata", {})
        summary = data.get("summary", {})

        st.subheader("📈 Analysis Summary")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Instances", summary.get("total_instances_analyzed", 0))
        with col2:
            st.metric("Idle Instances", summary.get("idle_instances", 0))
        with col3:
            st.metric("Active Instances", summary.get("active_instances", 0))
        with col4:
            st.metric("Potential Savings", f"${summary.get('potential_monthly_savings', 0):,.2f}")

        st.caption(f"📅 Last updated: {metadata.get('timestamp','N/A')}")

        detailed_analysis = data.get("detailed_analysis", [])
        if detailed_analysis:
            df = pd.DataFrame(detailed_analysis)
            for col in ["avg_cpu", "max_cpu", "total_network", "estimated_savings"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            st.subheader("📋 Instance Details")
            st.dataframe(
                df[["instance_id", "instance_type", "status", "avg_cpu",
                    "max_cpu", "total_network", "recommendation", "estimated_savings"]]
                .style.format({
                    "avg_cpu": "{:.2f}%",
                    "max_cpu": "{:.2f}%",
                    "total_network": "{:,.0f} bytes",
                    "estimated_savings": "${:,.2f}"
                }),
                use_container_width=True
            )

            # Visualizations
            st.subheader("📊 Instance Visualizations")
            col1, col2 = st.columns(2)
            with col1:
                status_counts = df["status"].value_counts()
                fig_pie = px.pie(status_counts, values=status_counts.values, names=status_counts.index,
                                 title="Instance Status Distribution")
                st.plotly_chart(fig_pie, use_container_width=True)
            with col2:
                fig_hist = px.histogram(df[df["status"] != "error"], x="avg_cpu", nbins=20,
                                        title="CPU Utilization Distribution")
                fig_hist.update_layout(xaxis_title="Average CPU %", yaxis_title="Count")
                st.plotly_chart(fig_hist, use_container_width=True)
    else:
        st.info("No idle instance data found in S3.")

    # ----------------------------------
        # SECTION 2: AWS Cost Explorer
    # ----------------------------------
    st.header("💵 AWS Cost Explorer Breakdown")

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date", datetime.utcnow().date() - timedelta(days=7))
    with col2:
        end_date = st.date_input("End Date", datetime.utcnow().date())

    if start_date >= end_date:
        st.error("⚠️ End Date must be after Start Date")
        return

    if st.button("Refresh Cost Explorer Data"):
        st.rerun()

    with st.spinner("Fetching cost data..."):
        df_cost = fetch_cost_explorer_data(start_date=start_date, end_date=end_date)

    if not df_cost.empty:
        total_cost = df_cost["Cost"].sum()
        st.metric(f"Total Cost ({start_date} → {end_date})", f"${total_cost:,.2f}")

        service_summary = df_cost.groupby("Service")["Cost"].sum().reset_index()
        st.subheader("📋 Cost by Service")
        st.dataframe(service_summary.sort_values("Cost", ascending=False))

        col1, col2 = st.columns(2)
        with col1:
            fig_pie = px.pie(service_summary, values="Cost", names="Service",
                             title=f"Cost by AWS Service ({start_date} → {end_date})")
            st.plotly_chart(fig_pie, use_container_width=True)
        with col2:
            fig_line = px.line(df_cost, x="Date", y="Cost", color="Service",
                               title=f"Daily Cost by Service ({start_date} → {end_date})")
            st.plotly_chart(fig_line, use_container_width=True)
    else:
        st.warning("No cost data available. Make sure Cost Explorer is enabled.")

# -------------------------------
# Run app
# -------------------------------
if __name__ == "__main__":
    main()
