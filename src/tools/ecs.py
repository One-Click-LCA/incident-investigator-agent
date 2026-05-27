"""ECS + ALB investigation tools."""

from datetime import datetime, timezone, timedelta
from langchain_core.tools import tool
import boto3

from src.config import AWS_REGION, AWS_PROFILE, get_env, get_service, get_cluster_arn, get_actual_service_name
from src.utils import safe_call, tool_result, is_prod_env


def _session():
    if AWS_PROFILE:
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


def _ecs():
    return _session().client("ecs")


def _elb():
    return _session().client("elbv2")


def _aas():
    return _session().client("application-autoscaling")


def _cw():
    return _session().client("cloudwatch")


def _resolve(service: str, env: str):
    """Return (cluster_arn, actual_service_name) from the bootstrap-resolved singleton."""
    cluster = get_cluster_arn() or f"ecs-cluster-{env}"
    svc = get_actual_service_name() or service
    return cluster, svc


@tool
def get_ecs_service_status(service: str, env: str) -> str:
    """
    Get current ECS service status: running/desired task counts, deployment rollout state,
    and the last 10 service events. Use this first when investigating any ECS service incident.
    Returns JSON string.
    """
    def _run():
        cluster, svc = _resolve(service, env)
        ecs = _ecs()
        resp = ecs.describe_services(cluster=cluster, services=[svc], include=["TAGS"])
        if not resp["services"]:
            return {"error": f"Service '{svc}' not found in cluster '{cluster}'"}
        s = resp["services"][0]
        deployments = [
            {
                "id": d["id"],
                "status": d["status"],
                "task_definition": d["taskDefinition"].split("/")[-1],
                "desired": d["desiredCount"],
                "running": d["runningCount"],
                "pending": d["pendingCount"],
                "rollout_state": d.get("rolloutState"),
                "rollout_reason": d.get("rolloutStateReason"),
                "created_at": str(d.get("createdAt")),
                "updated_at": str(d.get("updatedAt")),
            }
            for d in s.get("deployments", [])
        ]
        events = [
            {"created_at": str(e["createdAt"]), "message": e["message"]}
            for e in s.get("events", [])[:10]
        ]
        return {
            "service_name": s["serviceName"],
            "status": s["status"],
            "desired_count": s["desiredCount"],
            "running_count": s["runningCount"],
            "pending_count": s["pendingCount"],
            "deployments": deployments,
            "last_events": events,
        }

    return tool_result(safe_call("get_ecs_service_status", _run))


@tool
def get_ecs_service_version(service: str, env: str) -> str:
    """
    Get the currently deployed version of the ECS service: Docker image tag, git branch,
    commit SHA, task definition revision, and when it was deployed. Use to correlate incidents
    with recent deployments (flag if deployed within last 4 hours).
    Returns JSON string.
    """
    def _run():
        cluster, svc = _resolve(service, env)
        ecs = _ecs()
        resp = ecs.describe_services(cluster=cluster, services=[svc])
        if not resp["services"]:
            return {"error": f"Service '{svc}' not found"}
        s = resp["services"][0]
        td_arn = s["taskDefinition"]
        td = ecs.describe_task_definition(taskDefinition=td_arn)["taskDefinition"]

        image = ""
        branch = ""
        commit = ""
        for container in td.get("containerDefinitions", []):
            img = container.get("image", "")
            if img:
                image = img
                # Parse branch/commit from image tag like: image:branch-commitsha
                tag_part = img.split(":")[-1] if ":" in img else ""
                if "-" in tag_part:
                    parts = tag_part.rsplit("-", 1)
                    branch = parts[0]
                    commit = parts[1][:12]
                break

        active_dep = next(
            (d for d in s.get("deployments", []) if d["status"] == "PRIMARY"), {}
        )
        deployed_at = str(active_dep.get("createdAt", "unknown"))

        return {
            "image": image,
            "branch": branch,
            "commit_sha": commit,
            "task_definition_arn": td_arn,
            "task_definition_revision": td_arn.split(":")[-1],
            "deployed_at": deployed_at,
        }

    return tool_result(safe_call("get_ecs_service_version", _run))


@tool
def get_stopped_tasks(service: str, env: str, minutes: int = 60) -> str:
    """
    List recently stopped ECS tasks for the service with their exit codes and stop reasons.
    Exit code 137 = OOMKilled. Exit code != 0 = application crash. Use to diagnose
    task failures, health check failures, and OOM events.
    Returns JSON string.
    """
    def _run():
        cluster, svc = _resolve(service, env)
        ecs = _ecs()
        stopped = ecs.list_tasks(cluster=cluster, serviceName=svc, desiredStatus="STOPPED", maxResults=20)
        arns = stopped.get("taskArns", [])
        if not arns:
            return {"stopped_task_count": 0, "tasks": []}

        resp = ecs.describe_tasks(cluster=cluster, tasks=arns)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        tasks = []
        for t in resp["tasks"]:
            stopped_at = t.get("stoppedAt")
            if stopped_at and stopped_at < cutoff:
                continue
            containers = [
                {
                    "name": c["name"],
                    "exit_code": c.get("exitCode"),
                    "reason": c.get("reason", ""),
                }
                for c in t.get("containers", [])
            ]
            tasks.append({
                "task_arn": t["taskArn"].split("/")[-1],
                "stopped_at": str(stopped_at),
                "stopped_reason": t.get("stoppedReason", ""),
                "containers": containers,
            })

        return {"stopped_task_count": len(tasks), "tasks": tasks}

    return tool_result(safe_call("get_stopped_tasks", _run))


@tool
def get_alb_health(service: str, env: str) -> str:
    """
    Get ALB target group health for the ECS service. Shows which targets are healthy,
    unhealthy, or draining and the reason for unhealthy targets. Use to confirm whether
    traffic is reaching the service.
    Returns JSON string.
    """
    def _run():
        cluster, svc = _resolve(service, env)
        ecs = _ecs()
        elb = _elb()

        resp = ecs.describe_services(cluster=cluster, services=[svc])
        if not resp["services"]:
            return {"error": f"Service '{svc}' not found"}
        s = resp["services"][0]
        tg_arns = [lb["targetGroupArn"] for lb in s.get("loadBalancers", []) if "targetGroupArn" in lb]

        if not tg_arns:
            return {"message": "No ALB target groups found for this service"}

        results = []
        for tg_arn in tg_arns:
            tg_info = elb.describe_target_groups(TargetGroupArns=[tg_arn])["TargetGroups"][0]
            health = elb.describe_target_health(TargetGroupArn=tg_arn)
            targets = [
                {
                    "id": t["Target"]["Id"],
                    "port": t["Target"].get("Port"),
                    "health_state": t["TargetHealth"]["State"],
                    "reason": t["TargetHealth"].get("Reason", ""),
                    "description": t["TargetHealth"].get("Description", ""),
                }
                for t in health["TargetHealthDescriptions"]
            ]
            results.append({
                "target_group_name": tg_info["TargetGroupName"],
                "protocol": tg_info["Protocol"],
                "health_check_path": tg_info.get("HealthCheckPath", ""),
                "targets": targets,
            })
        return results

    return tool_result(safe_call("get_alb_health", _run))


@tool
def get_autoscaling_events(service: str, env: str, minutes: int = 120) -> str:
    """
    Get recent autoscaling events for the ECS service including scale-in/out activity,
    trigger alarm names, and min/max capacity. Use to detect if the service was scale-blocked
    (desired hit max) or if scale events correlate with the incident window.
    Returns JSON string.
    """
    def _run():
        cluster = get_cluster_arn() or f"ecs-cluster-{env}"
        actual_svc = get_actual_service_name() or service
        # resource_id uses the cluster short name (not full ARN) for Application Autoscaling
        cluster_name = cluster.split("/")[-1] if "/" in cluster else cluster
        resource_id = f"service/{cluster_name}/{actual_svc}"
        aas = _aas()
        cw = _cw()

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        activities = aas.describe_scaling_activities(
            ServiceNamespace="ecs",
            ResourceId=resource_id,
        ).get("ScalingActivities", [])

        events = []
        for a in activities:
            start = a.get("StartTime")
            if start and start < cutoff:
                continue
            alarm_name = ""
            cause = a.get("Cause", "")
            # Extract alarm name from cause string like "triggered by alarm: AlarmName"
            import re
            m = re.search(r"alarm[:\s]+([^\s,]+)", cause, re.IGNORECASE)
            if m:
                alarm_name = m.group(1)

            alarm_detail = {}
            if alarm_name:
                try:
                    alarms = cw.describe_alarms(AlarmNames=[alarm_name])["MetricAlarms"]
                    if alarms:
                        al = alarms[0]
                        alarm_detail = {
                            "metric": al.get("MetricName"),
                            "threshold": al.get("Threshold"),
                            "comparison": al.get("ComparisonOperator"),
                        }
                except Exception:
                    pass

            events.append({
                "start_time": str(start),
                "status": a.get("StatusCode"),
                "cause": cause[:300],
                "alarm_triggered": alarm_name,
                "alarm_detail": alarm_detail,
            })

        targets = aas.describe_scalable_targets(
            ServiceNamespace="ecs",
            ResourceIds=[resource_id],
        ).get("ScalableTargets", [])
        capacity = {}
        if targets:
            t = targets[0]
            capacity = {"min": t["MinCapacity"], "max": t["MaxCapacity"]}

        return {"scaling_events": events, "capacity": capacity}

    return tool_result(safe_call("get_autoscaling_events", _run))
