import streamlit as st
import boto3
import json
import pandas as pd
import plotly.express as px

# -------------------------------
# Streamlit Page Configuration
# -------------------------------
st.set_page_config(
    page_title="EC2 Idle Instance Analysis Dashboard",
    page_icon="🔍",
    layout="wide"
)

# -------------------------------
# AWS Clients
# -------------------------------
@st.cache_resource
def get_aws_clients():
    return {
        "s3": boto3.client("s3"),
        "lambda": boto3.client("lambda")
    }

# -------------------------------
# Helper to read JSON from S3
# -------------------------------

def get_lambda_results_from_s3(bucket_name, key):
    try:
        clients = get_aws_clients()
        response = clients["s3"].get_object(Bucket='cost-optimization-data-s3', Key="lambda-outputs/idle-instance-analysis.json")
        data = json.loads(response["Body"].read())
        return data
    except Exception as e:
        st.error(f"Error reading from S3: {str(e)}")
        return None

# -------------------------------
# Invoke Lambda for fresh analysis
# -------------------------------
def invoke_lambda_analysis(function_name="Detect_idle_ec2-instances"):
    try:
        clients = get_aws_clients()
        response = clients["lambda"].invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse"
        )
        return json.loads(response["Payload"].read())
    except Exception as e:
        st.error(f"Error invoking Lambda: {str(e)}")
        return None

# -------------------------------
# Main App
# -------------------------------
def main():
    st.title("🔍 EC2 Idle Instance Analysis Dashboard")
    st.markdown("Visualize results from Lambda idle instance analysis")

    # ---------------------------
    # User inputs (S3 config)
    # ---------------------------
    col1, col2 = st.columns([3, 1])
    with col1:
        s3_bucket = st.text_input(
            "S3 Bucket Name",
            value="cost-optimization-data-123456789012-us-east-1",
            help="S3 bucket where Lambda results are stored"
        )
    with col2:
        s3_key = st.text_input(
            "S3 Key",
            value="lambda-outputs/idle-instance-analysis.json",
            help="S3 key for the analysis results"
        )

    # ---------------------------
    # Action buttons
    # ---------------------------
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("🔄 Run New Analysis"):
            with st.spinner("Invoking Lambda function..."):
                result = invoke_lambda_analysis()
                if result:
                    st.success("Analysis completed! Refreshing data...")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error("Lambda invocation failed")

    with col2:
        if st.button("📊 Refresh Dashboard"):
            st.cache_data.clear()
            st.rerun()

    # ---------------------------
    # Load Data from S3
    # ---------------------------
    if "auto_loaded" not in st.session_state:
        st.session_state.auto_loaded = True
        data = get_lambda_results_from_s3(s3_bucket, s3_key)
    else:
        data = get_lambda_results_from_s3(s3_bucket, s3_key)

    if not data:
        st.warning("No analysis data found. Please run the Lambda function first.")
        return

    # ---------------------------
    # Summary Section
    # ---------------------------
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

    with st.expander("Analysis Metadata"):
        st.write(f"**Timestamp:** {metadata.get('timestamp','N/A')}")
        st.write(f"**Evaluation Period:** {metadata.get('evaluation_period_minutes',0)} minutes")
        st.write(f"**CPU Threshold:** {metadata.get('cpu_threshold',0)}%")
        st.write(f"**Network Threshold:** {metadata.get('network_threshold',0)} bytes")

    # ---------------------------
    # Detailed Instance Analysis
    # ---------------------------
    st.subheader("📋 Instance Analysis Details")
    detailed_analysis = data.get("detailed_analysis", [])
    if detailed_analysis:
        df = pd.DataFrame(detailed_analysis)

        # Ensure numeric columns for charts
        for col in ["avg_cpu", "max_cpu", "total_network", "estimated_savings"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

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

        # -----------------------
        # Visualizations
        # -----------------------
        st.subheader("📊 Visualizations")
        col1, col2 = st.columns(2)

        with col1:
            status_counts = df["status"].value_counts()
            if not status_counts.empty:
                fig_pie = px.pie(
                    values=status_counts.values,
                    names=status_counts.index,
                    title="Instance Status Distribution"
                )
                st.plotly_chart(fig_pie, use_container_width=True)

        with col2:
            if "avg_cpu" in df.columns:
                fig_hist = px.histogram(
                    df[df["status"] != "error"],
                    x="avg_cpu",
                    nbins=20,
                    title="CPU Utilization Distribution"
                )
                fig_hist.update_layout(xaxis_title="Average CPU %", yaxis_title="Count")
                st.plotly_chart(fig_hist, use_container_width=True)

        # -----------------------
        # Idle Instances (Actions)
        # -----------------------
        idle_instances = data.get("idle_instances", [])
        if idle_instances:
            st.subheader("💤 Idle Instances - Action Required")
            idle_df = pd.DataFrame(idle_instances)

            # Add a checkbox column for selection
            idle_df["select"] = False
            edited_df = st.data_editor(
                idle_df[["instance_id", "instance_type", "avg_cpu",
                         "total_network", "estimated_savings", "select"]],
                column_config={
                    "select": st.column_config.CheckboxColumn("Select for Action"),
                    "estimated_savings": st.column_config.NumberColumn(
                        "Monthly Savings", format="$%.2f"
                    ),
                    "avg_cpu": st.column_config.NumberColumn("Avg CPU %", format="%.2f")
                },
                use_container_width=True
            )

            selected_instances = edited_df[edited_df["select"]]["instance_id"].tolist()
            if selected_instances:
                if st.button(f"🛑 Stop {len(selected_instances)} Selected Instances"):
                    st.warning("⚠️ Action not yet implemented. You can wire this to a cleanup Lambda.")
    else:
        st.info("No detailed analysis available.")

    # ---------------------------
    # Raw JSON
    # ---------------------------
    with st.expander("📂 Raw JSON Output"):
        st.json(data)


# Run the app
if __name__ == "__main__":
    main()
