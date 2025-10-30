# ec2_cleanup_automation.py
import boto3
import json
import os
from datetime import datetime

ec2 = boto3.client("ec2")
dynamodb = boto3.resource("dynamodb")
LOG_TABLE = os.environ.get("CLEANUP_LOG_TABLE", "EC2CleanupLogs")

def log_action(item):
    try:
        table = dynamodb.Table(LOG_TABLE)
        table.put_item(Item=item)
    except Exception:
        pass

def stop_instances(instance_ids, dry_run=True):
    resp = {"action":"stop_instances","requested":instance_ids,"results":[]}
    for i in instance_ids:
        try:
            # validate current state
            inst = ec2.describe_instances(InstanceIds=[i])["Reservations"][0]["Instances"][0]
            state = inst["State"]["Name"]
            if state == "stopped":
                resp["results"].append({i: "already stopped"})
                continue
            if dry_run:
                resp["results"].append({i: "dry_run_ok"})
            else:
                ec2.stop_instances(InstanceIds=[i])
                resp["results"].append({i: "stop_requested"})
        except Exception as e:
            resp["results"].append({i: f"error:{str(e)}"})
    return resp

def start_instances(instance_ids, dry_run=True):
    resp = {"action":"start_instances","requested":instance_ids,"results":[]}
    for i in instance_ids:
        try:
            inst = ec2.describe_instances(InstanceIds=[i])["Reservations"][0]["Instances"][0]
            state = inst["State"]["Name"]
            if state == "running":
                resp["results"].append({i: "already running"})
                continue
            if dry_run:
                resp["results"].append({i: "dry_run_ok"})
            else:
                ec2.start_instances(InstanceIds=[i])
                resp["results"].append({i: "start_requested"})
        except Exception as e:
            resp["results"].append({i: f"error:{str(e)}"})
    return resp

def terminate_instances(instance_ids, dry_run=True):
    resp = {"action":"terminate_instances","requested":instance_ids,"results":[]}
    for i in instance_ids:
        try:
            if dry_run:
                resp["results"].append({i:"dry_run_ok"})
            else:
                ec2.terminate_instances(InstanceIds=[i])
                resp["results"].append({i:"terminate_requested"})
        except Exception as e:
            resp["results"].append({i: f"error:{str(e)}"})
    return resp

def delete_volumes(volume_ids, dry_run=True):
    resp = {"action":"delete_volumes","requested":volume_ids,"results":[]}
    for v in volume_ids:
        try:
            vol = ec2.describe_volumes(VolumeIds=[v])["Volumes"][0]
            if vol["State"] != "available":
                resp["results"].append({v: f"not_available_state:{vol['State']}"})
                continue
            if dry_run:
                resp["results"].append({v:"dry_run_ok"})
            else:
                ec2.delete_volume(VolumeId=v)
                resp["results"].append({v:"deleted"})
        except Exception as e:
            resp["results"].append({v: f"error:{str(e)}"})
    return resp

def release_eips(allocation_ids, dry_run=True):
    resp = {"action":"release_eips","requested":allocation_ids,"results":[]}
    for a in allocation_ids:
        try:
            # ensure unassociated
            addrs = ec2.describe_addresses(AllocationIds=[a])["Addresses"]
            if not addrs:
                resp["results"].append({a:"not_found"})
                continue
            if "AssociationId" in addrs[0]:
                resp["results"].append({a:"associated"})
                continue
            if dry_run:
                resp["results"].append({a:"dry_run_ok"})
            else:
                ec2.release_address(AllocationId=a)
                resp["results"].append({a:"released"})
        except Exception as e:
            resp["results"].append({a: f"error:{str(e)}"})
    return resp

def delete_security_groups(group_ids, dry_run=True):
    resp = {"action":"delete_security_groups","requested":group_ids,"results":[]}
    for g in group_ids:
        try:
            # skip default
            gids = ec2.describe_security_groups(GroupIds=[g])["SecurityGroups"]
            if not gids:
                resp["results"].append({g:"not_found"})
                continue
            if gids[0]["GroupName"] == "default":
                resp["results"].append({g:"default_group_skip"})
                continue
            # ensure not used by network interfaces
            enis = ec2.describe_network_interfaces(Filters=[{"Name":"group-id","Values":[g]}])["NetworkInterfaces"]
            if enis:
                resp["results"].append({g:"in_use"})
                continue
            if dry_run:
                resp["results"].append({g:"dry_run_ok"})
            else:
                ec2.delete_security_group(GroupId=g)
                resp["results"].append({g:"deleted"})
        except Exception as e:
            resp["results"].append({g: f"error:{str(e)}"})
    return resp

# Lambda handler
def lambda_handler(event, context):
    # event expected as JSON
    action = event.get("action")
    dry_run = event.get("dry_run", True)

    result = {"action": action, "timestamp": datetime.utcnow().isoformat(), "dry_run": dry_run}

    try:
        if action == "stop_instances":
            result["details"] = stop_instances(event.get("instance_ids", []), dry_run=dry_run)
        elif action == "start_instances":
            result["details"] = start_instances(event.get("instance_ids", []), dry_run=dry_run)
        elif action == "terminate_instances":
            result["details"] = terminate_instances(event.get("instance_ids", []), dry_run=dry_run)
        elif action == "delete_volumes":
            result["details"] = delete_volumes(event.get("volume_ids", []), dry_run=dry_run)
        elif action == "release_eips":
            result["details"] = release_eips(event.get("allocation_ids", []), dry_run=dry_run)
        elif action == "delete_security_groups":
            result["details"] = delete_security_groups(event.get("group_ids", []), dry_run=dry_run)
        else:
            result["error"] = f"Unknown action: {action}"
    except Exception as e:
        result["error"] = str(e)

    # log to DynamoDB (best-effort)
    try:
        log_item = {
            "ActionId": f"{action}-{datetime.utcnow().isoformat()}",
            "Action": action,
            "Timestamp": datetime.utcnow().isoformat(),
            "DryRun": dry_run,
            "Result": str(result)
        }
        log_action(log_item)
    except Exception:
        pass

    return {
        "statusCode": 200,
        "body": json.dumps(result)
    }
