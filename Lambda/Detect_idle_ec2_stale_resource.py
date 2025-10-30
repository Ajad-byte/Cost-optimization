import boto3
import json
from datetime import datetime, timedelta
import os
import logging
from decimal import Decimal

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
ec2 = boto3.resource('ec2')
cloudwatch = boto3.client('cloudwatch')
dynamodb = boto3.resource('dynamodb')

# DynamoDB table name (set in Lambda environment variable)
DYNAMO_TABLE_NAME = os.environ.get('DYNAMO_TABLE_NAME', 'EC2IdleInstanceMetrics')

def lambda_handler(event, context):
    """
    Lambda function to detect idle EC2 instances and store real-time analysis in DynamoDB.
    """
    # Settings
    CPU_THRESHOLD = 10.0             # %
    NETWORK_THRESHOLD = 1048576      # 1 MB in bytes
    EVALUATION_MINUTES = 5           # Evaluation window (5 mins demo)
    timestamp = datetime.utcnow().isoformat()

    logger.info(f"Starting EC2 idle scan at {timestamp}")
    
    # Get DynamoDB table reference
    table = dynamodb.Table(DYNAMO_TABLE_NAME)
    
    # Get all running EC2 instances
    instances = ec2.instances.filter(
        Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
    )

    idle_instances = []
    analyzed_instances = []

    for instance in instances:
        instance_info = {
            'InstanceId': instance.id,
            'InstanceType': instance.instance_type,
            'LaunchTime': instance.launch_time.isoformat(),
            'Region': instance.placement['AvailabilityZone'][:-1],
            'Tags': {tag['Key']: tag['Value'] for tag in (instance.tags or [])}
        }

        # Skip if tag AutoStop=false
        if instance_info['Tags'].get('AutoStop') == 'false':
            logger.info(f"Skipping instance {instance.id} (AutoStop=false)")
            continue

        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=EVALUATION_MINUTES)
        logger.info(f"Analyzing {instance.id} from {start_time} to {end_time}")

        try:
            # CPU utilization
            cpu_response = cloudwatch.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName='CPUUtilization',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance.id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=['Average', 'Maximum']
            )

            # Network In
            netin_response = cloudwatch.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName='NetworkIn',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance.id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=['Sum']
            )

            # Network Out
            netout_response = cloudwatch.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName='NetworkOut',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance.id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=['Sum']
            )

            cpu_datapoints = cpu_response.get('Datapoints', [])
            netin_datapoints = netin_response.get('Datapoints', [])
            netout_datapoints = netout_response.get('Datapoints', [])

            if not cpu_datapoints:
                instance_info.update({
                    'Status': 'NoData',
                    'Recommendation': 'Enable detailed monitoring'
                })
                analyzed_instances.append(instance_info)
                continue

            # Calculate averages and totals
            avg_cpu = sum(dp['Average'] for dp in cpu_datapoints) / len(cpu_datapoints)
            max_cpu = max(dp['Maximum'] for dp in cpu_datapoints)
            total_net_in = sum(dp['Sum'] for dp in netin_datapoints)
            total_net_out = sum(dp['Sum'] for dp in netout_datapoints)
            total_network = total_net_in + total_net_out

            instance_info.update({
                'AvgCPU': round(avg_cpu, 2),
                'MaxCPU': round(max_cpu, 2),
                'NetworkInBytes': int(total_net_in),
                'NetworkOutBytes': int(total_net_out),
                'TotalNetworkBytes': int(total_network),
                'EvaluationTimestamp': timestamp
            })

            # Determine idle/active
            if avg_cpu < CPU_THRESHOLD and total_network < NETWORK_THRESHOLD:
                instance_info['Status'] = 'Idle'
                instance_info['Recommendation'] = 'Consider stopping instance'
                idle_instances.append(instance_info)

                # Optional tagging
                try:
                    instance.create_tags(Tags=[
                        {'Key': 'CostOptimization', 'Value': 'CandidateForStop'},
                        {'Key': 'IdleDetectedAt', 'Value': timestamp}
                    ])
                except Exception as tag_err:
                    logger.warning(f"Tagging failed for {instance.id}: {tag_err}")
            else:
                instance_info['Status'] = 'Active'
                instance_info['Recommendation'] = 'Keep running'

            analyzed_instances.append(instance_info)

            # 🔹 Store record in DynamoDB (each instance's current status)
            table.put_item(
                Item={
                    'InstanceId': instance.id,
                    'EvaluationTimestamp': timestamp,
                    'InstanceType': instance.instance_type,
                    'AvgCPU': Decimal(str(round(avg_cpu, 2))),
                    'MaxCPU': Decimal(str(round(max_cpu, 2))),
                    'NetworkInBytes': Decimal(str(total_net_in)),
                    'NetworkOutBytes': Decimal(str(total_net_out)),
                    'TotalNetworkBytes': Decimal(str(total_network)),
                    'Status': instance_info['Status'],
                    'Recommendation': instance_info['Recommendation'],
                    'Region': instance_info['Region'],
                    'Tags': json.dumps(instance_info['Tags']),
                    'LaunchTime': instance.launch_time.isoformat()
                }
            )

        except Exception as e:
            logger.error(f"Error analyzing {instance.id}: {str(e)}")

    summary = {
        'timestamp': timestamp,
        'evaluation_period_minutes': EVALUATION_MINUTES,
        'summary': {
            'total_instances_analyzed': len(analyzed_instances),
            'idle_instances': len(idle_instances),
            'active_instances': len([i for i in analyzed_instances if i.get('Status') == 'Active'])
        },
        'idle_instances': idle_instances,
        'all_instances': analyzed_instances
    }

    logger.info(f"Scan complete. Idle: {len(idle_instances)} / Total: {len(analyzed_instances)}")

    return {
        'statusCode': 200,
        'body': json.dumps(summary, indent=2, default=str)
    }
