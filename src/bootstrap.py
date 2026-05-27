"""
Pre-agent setup: load credentials, discover ECS service, detect dependencies.
Results are injected into the agent's initial message — not passed through LangGraph state.
"""

import json
import re
import boto3
from src.config import AWS_REGION, AWS_PROFILE, SECRET_NAME, ECS_CLUSTER, set_runtime_context
from src.utils import is_prod_env


# ── AWS client factory ────────────────────────────────────────────────────────

def _session():
    if AWS_PROFILE:
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


def _client(service: str, region: str | None = None):
    return _session().client(service, region_name=region or AWS_REGION)


# ── Credentials ───────────────────────────────────────────────────────────────

def load_secret() -> dict:
    sm = _client("secretsmanager")
    resp = sm.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(resp["SecretString"])


# ── ECS Service Discovery ─────────────────────────────────────────────────────

_SKIP_EXTERNAL_CHECK = {"PUBLIC_HOST", "USER_MANAGEMENT_ADMIN_UI_URL", "FRONTEND_URL", "APP_URL"}
_CRITICAL_EXTERNAL_DEPS = {
    "KEYCLOAK_HOST", "KEYCLOAK_URL", "KEYCLOAK_SERVER_URL",
    "USER_MANAGEMENT_URL", "USER_MANAGEMENT_SERVICE_URL",
    "SEMANTIC_RESOURCE_SEARCH_URL", "GOTENBERG_URL", "TYPESENSE_URL",
}


def _env_score(match: tuple, env: str) -> int:
    """Score a (cluster_arn, service_arn, service_name) match for env relevance.
    Higher = better fit for the requested env.
    """
    cluster_arn, _, svc_name = match
    cluster_name = cluster_arn.split("/")[-1]  # e.g. ecs-cluster-preprod
    # Exact cluster name match: ecs-cluster-{env}
    if cluster_name == f"ecs-cluster-{env}":
        return 3
    # Service name ends with -{env} (e.g. supply-chain-service-preprod)
    if svc_name.endswith(f"-{env}"):
        return 2
    # Cluster name contains env as a segment
    if f"-{env}" in cluster_name or cluster_name.endswith(env):
        return 1
    return 0


def discover_ecs_service(service: str, env: str) -> dict:
    """
    Find an ECS service by scanning clusters for a service whose name starts with `service`.
    Actual service names may have suffixes e.g. supply-chain-service-comp-tools.
    If ECS_CLUSTER env var is set, only that cluster is searched.
    """
    ecs = _client("ecs")

    clusters_to_search = []
    if ECS_CLUSTER:
        # User pinned a specific cluster — skip scan
        clusters_to_search = [ECS_CLUSTER]
    else:
        # List all clusters in the account/region
        paginator = ecs.get_paginator("list_clusters")
        for page in paginator.paginate():
            clusters_to_search.extend(page.get("clusterArns", []))

    if not clusters_to_search:
        raise RuntimeError("No ECS clusters found. Check AWS_PROFILE and region.")

    matches = []  # list of (cluster_arn, service_arn, service_name)
    for cluster_arn in clusters_to_search:
        try:
            svc_paginator = ecs.get_paginator("list_services")
            for page in svc_paginator.paginate(cluster=cluster_arn):
                for svc_arn in page.get("serviceArns", []):
                    # Service name is the last segment of the ARN
                    svc_name = svc_arn.split("/")[-1]
                    # Match: exact name OR starts-with prefix (handles -comp-tools suffix etc.)
                    if svc_name == service or svc_name.startswith(f"{service}-"):
                        matches.append((cluster_arn, svc_arn, svc_name))
        except Exception:
            continue  # skip clusters we can't access

    if not matches:
        raise RuntimeError(
            f"Could not find ECS service '{service}' (or '{service}-*') in any cluster. "
            f"Searched {len(clusters_to_search)} cluster(s). "
            "Check service name spelling, AWS_PROFILE, and region."
        )

    if len(matches) > 1:
        names = [f"{m[2]} (in {m[0].split('/')[-1]})" for m in matches]
        print(f"[bootstrap] Multiple matches found: {names}")
        # Prefer the match whose cluster name is ecs-cluster-{env} (exact env match),
        # then one whose service name ends with -{env}, then first found.
        matches.sort(key=lambda m: _env_score(m, env), reverse=True)
        print(f"[bootstrap] Selected by env='{env}': {matches[0][2]} (in {matches[0][0].split('/')[-1]})")

    cluster_arn, service_arn, actual_service_name = matches[0]

    resp = ecs.describe_services(
        cluster=cluster_arn,
        services=[service_arn],
        include=["TAGS"],
    )
    svc_obj = resp["services"][0]
    task_def_arn = svc_obj["taskDefinition"]

    td_resp = ecs.describe_task_definition(taskDefinition=task_def_arn, include=["TAGS"])
    task_def = td_resp["taskDefinition"]

    tg_arns = [lb["targetGroupArn"] for lb in svc_obj.get("loadBalancers", []) if "targetGroupArn" in lb]

    print(f"[bootstrap] Resolved: cluster={cluster_arn.split('/')[-1]} service={actual_service_name}")
    return {
        "cluster_arn": cluster_arn,
        "service_arn": service_arn,
        "service_name": actual_service_name,
        "service": svc_obj,
        "task_definition": task_def,
        "task_definition_arn": task_def_arn,
        "target_group_arns": tg_arns,
    }


# ── Dependency Detection ──────────────────────────────────────────────────────

def detect_dependencies(task_def: dict) -> dict:
    env_vars = {}
    secret_names = set()  # secret names from Secrets Manager references (not values)
    for container in task_def.get("containerDefinitions", []):
        for ev in container.get("environment", []):
            env_vars[ev["name"]] = ev.get("value", "")
        for secret in container.get("secrets", []):
            sname = secret["name"]
            env_vars[sname] = secret.get("valueFrom", "")
            # The secret name itself (e.g. MONGO_URI, REDIS_URL) is the signal
            secret_names.add(sname)

    all_names = set(env_vars.keys())
    all_values = [v for v in env_vars.values() if v]

    # MongoDB detection — match on env var / secret name OR valueFrom ARN path
    _mongo_kws = ("MONGO", "MDB", "MONGODB")
    mongo_keys = {k for k in all_names if any(x in k.upper() for x in _mongo_kws)}
    mongo_detected = bool(mongo_keys) or any(
        any(x in v.lower() for x in ("mongodb", "mongo")) for v in all_values
    )

    # Redis detection
    redis_keys = {k for k in all_names if "REDIS" in k.upper()}
    redis_detected = bool(redis_keys) or any("redis" in v.lower() for v in all_values)

    # RDS / PostgreSQL detection
    _rds_kws = ("RDS", "POSTGRES", "POSTGRESQL", "DB_HOST", "DATABASE_URL", "DB_URL", "JDBC")
    rds_keys = {k for k in all_names if any(x in k.upper() for x in _rds_kws)}
    rds_vals = [env_vars[k] for k in rds_keys if env_vars.get(k)]
    rds_detected = bool(rds_keys)
    detected_rds_hosts = [v for v in rds_vals if "rds.amazonaws" in v or "postgres" in v.lower()]

    # ELK detection
    elk_detected = any("ELK" in k or "ELASTIC" in k.upper() for k in all_names)
    elk_indexes = []

    # External URLs
    external_urls = []
    for k, v in env_vars.items():
        if k in _SKIP_EXTERNAL_CHECK:
            continue
        if v and v.startswith("http") and "amazonaws" not in v and "localhost" not in v:
            external_urls.append({
                "name": k,
                "value": v,
                "criticality": "critical" if k in _CRITICAL_EXTERNAL_DEPS else "informational",
            })

    return {
        "mongo": {"detected": mongo_detected, "keys": list(mongo_keys)},
        "redis": {"detected": redis_detected, "keys": list(redis_keys)},
        "rds": {
            "detected": rds_detected,
            "keys": list(rds_keys),
            "detected_hosts": detected_rds_hosts,
        },
        "elk": {"detected": elk_detected, "indexes": elk_indexes},
        "external_urls": external_urls,
        "all_env_var_names": sorted(all_names),
    }


# ── Framework Detection ───────────────────────────────────────────────────────

def detect_framework(service_context: dict, dependencies: dict) -> dict:
    hints = []
    task_def = service_context["task_definition"]

    for container in task_def.get("containerDefinitions", []):
        image = container.get("image", "").lower()
        env_names = {ev["name"].upper() for ev in container.get("environment", [])}
        secret_names = {s["name"].upper() for s in container.get("secrets", [])}
        all_keys = env_names | secret_names
        health_cmd = " ".join(str(x) for x in container.get("healthCheck", {}).get("command", []))

        # Spring Boot — look for actuator health check or Spring-specific env vars
        if (
            "spring" in image
            or any(k.startswith("SPRING") for k in all_keys)
            or "/actuator/health" in health_cmd
        ):
            hints.append("spring_boot")

        # Tomcat / Grails — AJP port or grails in image name
        if "tomcat" in image or "grails" in image or any("AJP" in k for k in all_keys):
            hints.append("java_tomcat_or_grails")

        # Node.js
        if "node" in image or "npm" in image or any(k in ("NODE_ENV", "NPM_CONFIG_PREFIX") for k in all_keys):
            hints.append("nodejs")

        # Python
        if any(x in image for x in ("python", "django", "flask", "fastapi", "uvicorn", "gunicorn")):
            hints.append("python")

        # Generic Java (ECR images often don't have "java" in the name — check for JAVA_OPTS or JVM env vars)
        if (
            any(x in image for x in ("java", ".jar", "jre", "jdk", "openjdk", "corretto"))
            or any(k in all_keys for k in ("JAVA_OPTS", "JAVA_TOOL_OPTIONS", "JVM_OPTS", "CATALINA_OPTS"))
        ):
            if "spring_boot" not in hints and "java_tomcat_or_grails" not in hints:
                hints.append("java_generic")

    if dependencies["mongo"]["detected"]:
        hints.append("mongo_backed_service")
    if dependencies["redis"]["detected"]:
        hints.append("redis_session_or_cache_dependency")
    if dependencies["rds"]["detected"]:
        hints.append("rds_postgres_backed_service")

    is_jvm = any(h in hints for h in ("spring_boot", "java_tomcat_or_grails", "java_generic"))

    if is_jvm:
        memory_ceiling_pct = 90
        memory_warning_pct = 94
        memory_critical_pct = 97
        bedrock_note = (
            "JVM memory at 85-93% is NORMAL — JVM pre-allocates heap. "
            "Only flag memory pressure above 94%. "
            "Thread names 'ajp-nio-*' are Tomcat request threads (normal). "
            "Thread names 'redisMessageListenerContainer-*' are Spring Session listeners (normal)."
        )
        jvm_error_patterns = [
            "OutOfMemoryError", "GC overhead limit exceeded",
            "Metaspace", "BrokenPipeException", "SocketTimeoutException",
        ]
    elif "nodejs" in hints:
        memory_ceiling_pct = 70
        memory_warning_pct = 80
        memory_critical_pct = 90
        bedrock_note = "Node.js: memory above 70% is elevated."
        jvm_error_patterns = []
    else:
        memory_ceiling_pct = 70
        memory_warning_pct = 83
        memory_critical_pct = 92
        bedrock_note = "Python: memory above 70% is elevated."
        jvm_error_patterns = []

    return {
        "hints": hints,
        "is_jvm": is_jvm,
        "memory_ceiling_pct": memory_ceiling_pct,
        "memory_warning_pct": memory_warning_pct,
        "memory_critical_pct": memory_critical_pct,
        "interpretation_note": bedrock_note,
        "jvm_error_patterns": jvm_error_patterns,
    }


# ── Main Bootstrap Entry ──────────────────────────────────────────────────────

def bootstrap(service: str, env: str, minutes: int = 30) -> dict:
    print(f"[bootstrap] Loading credentials from Secrets Manager...")
    secret = load_secret()

    has_github = bool(secret.get("GITHUB_TOKEN", ""))
    print(f"[bootstrap] GitHub token: {'found' if has_github else 'NOT FOUND — code/framework analysis disabled'}")

    print(f"[bootstrap] Discovering ECS service '{service}' in '{env}'...")
    svc_ctx = discover_ecs_service(service, env)

    print(f"[bootstrap] Detecting dependencies from task definition...")
    deps = detect_dependencies(svc_ctx["task_definition"])

    print(f"[bootstrap] Detecting framework...")
    framework = detect_framework(svc_ctx, deps)

    # Store credentials + runtime context in module singleton so tools can access them
    set_runtime_context(
        secret=secret,
        service=service,
        env=env,
        minutes=minutes,
        cluster_arn=svc_ctx["cluster_arn"],
        actual_service_name=svc_ctx["service_name"],
    )

    # Derive base service name (strip env suffix) for GitHub repo lookup
    base_service = service
    for suffix in ("-preprod", "-staging", "-prod", "-qa", "-features", "-devops"):
        if base_service.endswith(suffix):
            base_service = base_service[: -len(suffix)]
            break

    print(f"[bootstrap] Done. Dependencies: {_dep_summary(deps)} | Framework: {framework['hints']}")
    return {
        "service_context": svc_ctx,
        "dependencies": deps,
        "framework": framework,
        "base_service_name": base_service,
        "has_github": has_github,
    }


def _dep_summary(deps: dict) -> str:
    parts = []
    for k in ("mongo", "redis", "rds", "elk"):
        if deps.get(k, {}).get("detected"):
            parts.append(k)
    return ", ".join(parts) or "none detected"
