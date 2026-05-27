"""ECS Analyst — service health, deployments, stopped tasks, resource utilization."""

from deepagents import create_deep_agent
from langchain_core.tools import tool

from src.agents._base import run_specialist
from src.tools.ecs import (
    get_ecs_service_status,
    get_ecs_service_version,
    get_stopped_tasks,
    get_alb_health,
    get_autoscaling_events,
)
from src.tools.cloudwatch import get_cloudwatch_metrics, get_cloudwatch_alarms

_PROMPT = """
You are an ECS infrastructure analyst. Investigate ECS service state during an incident window.

## Investigation steps

1. Call get_ecs_service_status — check running vs desired count, deployments array,
   recent events. If running < desired → tasks were crashing. If deployment in progress
   or recently completed (< 4h) → suspected deployment regression.

2. Call get_ecs_service_version — extract branch, commit, deployed_at from image tag.
   Surface as: "Running: {branch} @ {commit}, deployed {deployed_at}".
   Note if deployment time is within 4 hours before the incident.

3. Call get_stopped_tasks — for each stopped task extract stoppedReason and exitCode.
   exitCode=137 → OOMKilled. exitCode!=0 → application crash.
   "health check" in stoppedReason → health check failure.

4. Call get_cloudwatch_metrics — fetch CPU%, memory%, running task count over the window.
   JVM services (Spring/Tomcat/Grails): memory 85–93% is NORMAL (heap pre-allocation).
   Only flag memory pressure above 94% for JVM. For Node/Python: flag above 70%.
   Flag CPU sustained > 80%.

5. Call get_cloudwatch_alarms — report any alarms in ALARM state with threshold and reason.

6. Call get_autoscaling_events — check if scaling was triggered during the window.
   Note if desired_count hit max_capacity (scale-out was blocked).

7. Call get_alb_health — check ALB target health per target group.

## Output

Return a JSON evidence bundle:
{
  "source": "ecs-analyst",
  "running_vs_desired": "N/N",
  "deployment_suspected": true/false,
  "deploy_commit": "sha or empty",
  "deploy_branch": "branch or empty",
  "stopped_tasks": [{"reason": "", "exit_code": 0}],
  "cpu_peak_pct": 0,
  "memory_peak_pct": 0,
  "alb_healthy": true/false,
  "alarms_firing": [],
  "scaling_events": [],
  "findings": ["plain-English finding 1", "finding 2"],
  "severity": "ok|warning|critical"
}

## Rules
- Read-only: describe, list, get operations only. Never mutate.
- If a tool fails, record it as a gap in findings — do not abort.
""".strip()


def build_ecs_analyst_tool(model) -> tool:
    analyst = create_deep_agent(
        model=model,
        tools=[
            get_ecs_service_status,
            get_ecs_service_version,
            get_stopped_tasks,
            get_alb_health,
            get_autoscaling_events,
            get_cloudwatch_metrics,
            get_cloudwatch_alarms,
        ],
        system_prompt=_PROMPT,
    )

    @tool
    def invoke_ecs_analyst(task: str) -> str:
        """
        Invoke the ECS analyst to investigate service health, deployment state,
        stopped tasks, CPU/memory utilization, ALB targets, and autoscaling events.
        Pass the incident description and time window as the task.
        """
        return run_specialist("ecs-analyst", analyst, task)

    return invoke_ecs_analyst
