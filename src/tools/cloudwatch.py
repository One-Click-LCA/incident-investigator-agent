"""CloudWatch metrics and alarms tools."""

from datetime import datetime, timezone, timedelta
from langchain_core.tools import tool
import boto3

from src.config import AWS_REGION, AWS_PROFILE, get_cluster_arn, get_actual_service_name
from src.utils import safe_call, tool_result


def _session():
    if AWS_PROFILE:
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


def _cw():
    return _session().client("cloudwatch")


def _get_metric(cw, namespace: str, metric: str, dimensions: list, start, end, period: int = 300, stat: str = "Average") -> list:
    resp = cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=[stat],
    )
    points = sorted(resp["Datapoints"], key=lambda x: x["Timestamp"])
    return [{"timestamp": str(p["Timestamp"]), "value": round(p[stat], 2)} for p in points]


@tool
def get_cloudwatch_metrics(service: str, env: str, minutes: int = 30) -> str:
    """
    Get CloudWatch metrics for an ECS service: CPU utilization %, memory utilization %,
    running task count, ALB 5xx error count, and target response time.
    Extends 15 minutes before the window to catch pre-incident buildup.
    Returns JSON with time-series data points per metric.
    """
    def _run():
        cw = _cw()
        # Use resolved cluster name (short name, not full ARN) from bootstrap singleton
        cluster_arn = get_cluster_arn()
        cluster = cluster_arn.split("/")[-1] if cluster_arn and "/" in cluster_arn else f"ecs-cluster-{env}"
        actual_svc = get_actual_service_name() or service
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes + 15)  # 15 min buffer before window

        dims_ecs = [
            {"Name": "ClusterName", "Value": cluster},
            {"Name": "ServiceName", "Value": actual_svc},
        ]

        result = {}

        # Try Container Insights first (richer metrics). If empty, fall back to
        # standard AWS/ECS namespace which works without Container Insights enabled.
        ci_metrics = {}
        any_ci_data = False
        for metric in ("CpuUtilized", "MemoryUtilized", "MemoryReserved", "RunningTaskCount"):
            try:
                points = _get_metric(cw, "ECS/ContainerInsights", metric, dims_ecs, start, end)
                ci_metrics[metric] = points
                if points:
                    any_ci_data = True
            except Exception as e:
                ci_metrics[metric] = {"error": str(e)}

        if any_ci_data:
            result.update(ci_metrics)
            result["metrics_source"] = "ECS/ContainerInsights"
        else:
            # Container Insights disabled — fall back to standard ECS metrics
            result["metrics_source"] = "AWS/ECS (standard — Container Insights not enabled on this cluster)"
            standard_map = {
                "CPUUtilization": "CPUUtilization",
                "MemoryUtilization": "MemoryUtilization",
            }
            for metric, label in standard_map.items():
                try:
                    result[label] = _get_metric(cw, "AWS/ECS", metric, dims_ecs, start, end)
                except Exception as e:
                    result[label] = {"error": str(e)}
            # RunningTaskCount is not in AWS/ECS standard — derive from describe_services
            try:
                ecs_client = _session().client("ecs")
                svc_resp = ecs_client.describe_services(cluster=cluster_arn or cluster, services=[actual_svc])
                if svc_resp["services"]:
                    s = svc_resp["services"][0]
                    result["RunningTaskCount"] = {
                        "running": s.get("runningCount"),
                        "desired": s.get("desiredCount"),
                        "pending": s.get("pendingCount"),
                    }
            except Exception as e:
                result["RunningTaskCount"] = {"error": str(e)}

        # ALB metrics — need to find the ALB/TG dimensions
        try:
            ecs_client = _session().client("ecs")
            elb_client = _session().client("elbv2")
            resp = ecs_client.describe_services(cluster=cluster_arn or cluster, services=[actual_svc])
            if resp["services"]:
                tg_arns = [lb["targetGroupArn"] for lb in resp["services"][0].get("loadBalancers", []) if "targetGroupArn" in lb]
                for tg_arn in tg_arns[:1]:
                    tg = elb_client.describe_target_groups(TargetGroupArns=[tg_arn])["TargetGroups"][0]
                    tg_dim = tg_arn.split("targetgroup/")[-1] if "targetgroup/" in tg_arn else tg_arn
                    alb_arn = tg.get("LoadBalancerArns", [""])[0]
                    alb_dim = alb_arn.split("loadbalancer/")[-1] if "loadbalancer/" in alb_arn else ""

                    dims_alb = [
                        {"Name": "TargetGroup", "Value": f"targetgroup/{tg_dim}"},
                        {"Name": "LoadBalancer", "Value": alb_dim},
                    ]
                    for metric in ("HTTPCode_Target_5XX_Count", "RequestCount", "TargetResponseTime"):
                        try:
                            stat = "Sum" if "Count" in metric else "Average"
                            result[f"ALB_{metric}"] = _get_metric(cw, "AWS/ApplicationELB", metric, dims_alb, start, end, stat=stat)
                        except Exception as e:
                            result[f"ALB_{metric}"] = {"error": str(e)}
        except Exception as e:
            result["alb_metrics"] = {"error": str(e)}

        return result

    return tool_result(safe_call("get_cloudwatch_metrics", _run))


@tool
def get_cloudwatch_alarms(service: str, env: str) -> str:
    """
    Get all CloudWatch alarms associated with the ECS service. Shows alarm state,
    the metric being monitored, threshold, and reason for ALARM state.
    Filters out TargetTracking AlarmLow alarms (expected to stay in ALARM when CPU is low).
    Returns JSON string.
    """
    def _run():
        cw = _cw()
        # Search by service name prefix — alarms are typically named {service}-*
        resp = cw.describe_alarms(AlarmNamePrefix=service, MaxRecords=50)
        alarms = []
        for a in resp.get("MetricAlarms", []):
            name = a["AlarmName"]
            # Filter known-benign TargetTracking AlarmLow alarms
            if "AlarmLow" in name and "TargetTracking" in name:
                continue
            alarms.append({
                "alarm_name": name,
                "state": a["StateValue"],
                "metric_name": a.get("MetricName"),
                "threshold": a.get("Threshold"),
                "comparison": a.get("ComparisonOperator"),
                "state_reason": a.get("StateReason", "")[:300],
                "updated_at": str(a.get("StateUpdatedTimestamp")),
            })
        # Also search by env prefix in case alarm naming uses env
        resp2 = cw.describe_alarms(AlarmNamePrefix=f"{service}-{env}", MaxRecords=20)
        seen = {a["alarm_name"] for a in alarms}
        for a in resp2.get("MetricAlarms", []):
            if a["AlarmName"] not in seen:
                if "AlarmLow" in a["AlarmName"] and "TargetTracking" in a["AlarmName"]:
                    continue
                alarms.append({
                    "alarm_name": a["AlarmName"],
                    "state": a["StateValue"],
                    "metric_name": a.get("MetricName"),
                    "threshold": a.get("Threshold"),
                    "state_reason": a.get("StateReason", "")[:300],
                })
        return {"alarm_count": len(alarms), "alarms": alarms}

    return tool_result(safe_call("get_cloudwatch_alarms", _run))
