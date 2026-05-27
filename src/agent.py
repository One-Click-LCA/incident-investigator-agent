"""Orchestrator assembly and investigation runner using DeepAgents."""

import json
from deepagents import create_deep_agent
from langchain_core.messages import SystemMessage

from src.agents._base import build_model
from src.agents.ecs_analyst import build_ecs_analyst_tool
from src.agents.log_analyst import build_log_analyst_tool
from src.agents.mongo_analyst import build_mongo_analyst_tool
from src.agents.redis_analyst import build_redis_analyst_tool
from src.agents.rds_analyst import build_rds_analyst_tool
from src.agents.code_analyst import build_code_analyst_tool
from src.agents.framework_analyst import build_framework_analyst_tool
from src.agents.data_domain_analyst import build_data_domain_analyst_tool
from src.agents.topology_analyst import build_topology_analyst_tool
from src.tools.rca import write_rca
from src.system_prompt import ORCHESTRATOR_PROMPT
from src.config import BEDROCK_MODEL_ID
from src.utils import is_prod_env


def _build_orchestrator_tools(deps: dict, env: str, has_github: bool, model):
    """Register only the specialist tools relevant to detected dependencies."""
    tools = [
        build_ecs_analyst_tool(model),
        build_log_analyst_tool(model),
    ]

    if has_github:
        tools.append(build_code_analyst_tool(model))
        tools.append(build_framework_analyst_tool(model))

    if deps.get("mongo", {}).get("detected"):
        tools.append(build_mongo_analyst_tool(model))

    if deps.get("redis", {}).get("detected") and not is_prod_env(env):
        tools.append(build_redis_analyst_tool(model))

    if deps.get("rds", {}).get("detected"):
        tools.append(build_rds_analyst_tool(model))

    # Topology + data domain always available — orchestrator decides when to invoke them
    tools.append(build_topology_analyst_tool(model))

    if has_github:
        tools.append(build_data_domain_analyst_tool(model))

    tools.append(write_rca)
    return tools


def build_initial_message(
    service_context: dict,
    dependencies: dict,
    framework: dict,
    incident_description: str,
    env: str,
    minutes: int,
    focus_area: str,
    base_service_name: str,
) -> str:
    svc = service_context.get("service", {})
    task_def = service_context.get("task_definition", {})

    image = ""
    for container in task_def.get("containerDefinitions", []):
        image = container.get("image", "")
        if image:
            break

    dep_summary = [k for k in ("mongo", "redis", "rds", "elk") if dependencies.get(k, {}).get("detected")]
    ext_urls = [u["name"] for u in dependencies.get("external_urls", [])[:8]]

    lines = [
        "## Incident Investigation Request",
        "",
        f"**Service:** {service_context.get('service_name', 'unknown')}",
        f"**Base repo name (GitHub):** {base_service_name}",
        f"**Environment:** {env}",
        f"**Investigation window:** last {minutes} minutes",
        f"**Focus area:** {focus_area}",
        "",
        f"**Incident description:** {incident_description or 'No description — perform a general health check.'}",
        "",
        "## Service Context",
        f"- Image: {image}",
        f"- Running tasks: {svc.get('runningCount', '?')} / {svc.get('desiredCount', '?')} desired",
        f"- Framework detected: {', '.join(framework.get('hints', [])) or 'unknown'}",
        f"- JVM service: {framework.get('is_jvm', False)}",
        f"- Memory warning threshold: {framework.get('memory_warning_pct', 83)}%",
        f"- Framework note: {framework.get('interpretation_note', '')}",
        "",
        "## Detected Dependencies",
        f"- Datastores: {', '.join(dep_summary) if dep_summary else 'none'}",
        f"- External URLs: {', '.join(ext_urls) if ext_urls else 'none'}",
        "",
        "Begin the investigation. Follow the orchestrator instructions.",
    ]

    if focus_area != "all":
        lines.insert(-1, f"**FOCUS OVERRIDE:** Prioritise `{focus_area}` — still run ECS + logs first.")

    return "\n".join(lines)


def run_investigation(
    service_context: dict,
    dependencies: dict,
    framework: dict,
    incident_description: str,
    env: str,
    minutes: int,
    focus_area: str = "all",
    base_service_name: str = "",
    has_github: bool = False,
) -> dict:
    model = build_model()
    tools = _build_orchestrator_tools(deps=dependencies, env=env, has_github=has_github, model=model)

    tool_names = [t.name for t in tools]
    print(f"\n[agent] Starting investigation for {service_context.get('service_name')} ({env})")
    print(f"[agent] Model: {BEDROCK_MODEL_ID}")
    print(f"[agent] Registered specialists: {', '.join(tool_names)}")
    print("-" * 60)

    orchestrator = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=ORCHESTRATOR_PROMPT,
    )

    initial_message = build_initial_message(
        service_context=service_context,
        dependencies=dependencies,
        framework=framework,
        incident_description=incident_description,
        env=env,
        minutes=minutes,
        focus_area=focus_area,
        base_service_name=base_service_name,
    )

    all_messages = []
    last_ai_message = ""
    action_count = 0

    for chunk in orchestrator.stream(
        {"messages": [{"role": "user", "content": initial_message}]},
        config={"recursion_limit": 80},
        stream_mode="updates",
    ):
        for _node, output in chunk.items():
            if output is None:
                continue
            msgs = output.get("messages", [])
            all_messages.extend(msgs)

            for msg in msgs:
                msg_type = getattr(msg, "type", None)

                if msg_type == "ai":
                    content = msg.content
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text += block.get("text", "")

                    if text.strip():
                        preview = text[:600] + ("..." if len(text) > 600 else "")
                        print(f"\n[thinking]\n{preview}")
                        last_ai_message = text

                    for tc in getattr(msg, "tool_calls", []):
                        action_count += 1
                        name = tc.get("name", "?")
                        args = json.dumps(tc.get("args", {}), separators=(",", ":"))
                        if len(args) > 200:
                            args = args[:200] + "..."
                        print(f"\n[action {action_count}] {name}({args})")

                elif msg_type == "tool":
                    name = getattr(msg, "name", "?")
                    raw = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
                    try:
                        parsed = json.loads(raw)
                        status = parsed.get("status", "?")
                        if status == "failed":
                            preview = f"FAILED — {parsed.get('error', '')[:200]}"
                        else:
                            data = parsed.get("data", parsed)
                            ds = json.dumps(data, separators=(",", ":"), default=str)
                            preview = ds[:400] + ("..." if len(ds) > 400 else "")
                    except Exception:
                        preview = raw[:400]
                    print(f"  [result] {name}: {preview}")

    return {"messages": all_messages, "summary": last_ai_message}
