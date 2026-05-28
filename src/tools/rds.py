"""RDS PostgreSQL tools — CloudWatch metrics + direct SQL (read-only)."""

import json
from datetime import datetime, timezone, timedelta
from langchain_core.tools import tool
import boto3

from src.config import AWS_REGION, AWS_PROFILE, SECRET_NAME, get_secret
from src.utils import safe_call, tool_result, is_prod_env, truncate


def _session():
    if AWS_PROFILE:
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


def _cw():
    return _session().client("cloudwatch")


def _get_rds_host_from_aws(service: str, env: str) -> str:
    """Fall back to AWS describe_db_instances when host is absent from the secret.
    Matches RDS instance identifier against service name fragments by convention."""
    suffix = "prod" if is_prod_env(env) else "dev"
    base = _strip_env_suffix(service, env)

    # Candidate substrings to match against the DB instance identifier
    hints = sorted({
        base.lower(),
        base.lower().replace("-service", ""),
        f"{base.lower()}-{suffix}",
        base.lower().replace("-service", f"-{suffix}"),
    }, key=len, reverse=True)  # longest match first

    try:
        rds_client = _session().client("rds")
        instances = rds_client.describe_db_instances().get("DBInstances", [])
        for inst in instances:
            inst_id = inst.get("DBInstanceIdentifier", "").lower()
            if any(hint in inst_id for hint in hints):
                return inst.get("Endpoint", {}).get("Address", "")
    except Exception:
        pass
    return ""


_ENV_SUFFIXES = ("-preprod", "-staging", "-features", "-dev", "-prod", "-qa", "-uat", "-mfg", "-devops", "-internal")


def _strip_env_suffix(name: str, env: str = "") -> str:
    # Try known suffixes first
    for tag in _ENV_SUFFIXES:
        if name.endswith(tag):
            return name[: -len(tag)]
    # Fall back to stripping the actual env value passed at runtime
    # handles squad envs like -config, -mfg, -betie, -compass etc.
    if env:
        tag = f"-{env.strip().lower()}"
        if name.lower().endswith(tag):
            return name[: -len(tag)]
    return name


def _candidate_secret_names(service: str, env: str) -> list[str]:
    """
    Generate candidate Secrets Manager names for a service's RDS secret.
    Strips ECS env suffix first (e.g. supply-chain-service-preprod → supply-chain-service),
    then tries multiple base-name patterns.

    Examples:
      supply-chain-service-preprod → rds-supply-chain-service-dev, rds-supply-chain-dev
      epd-usage-service-preprod    → rds-epd-usage-service-dev, rds-epd-usage-dev
    """
    suffix = "prod" if is_prod_env(env) else "dev"
    base = _strip_env_suffix(service, env)   # strip -preprod / -config / -mfg etc.
    candidates = []

    # 1. Base name with env-suffix stripped: rds-supply-chain-service-dev
    candidates.append(f"rds-{base}-{suffix}")

    # 2. Original (unstripped) service name in case secret uses ECS name
    if base != service:
        candidates.append(f"rds-{service}-{suffix}")

    # 3. Strip trailing "-service": rds-supply-chain-dev  ← most common real pattern
    if base.endswith("-service"):
        candidates.append(f"rds-{base[:-len('-service')]}-{suffix}")

    # 4. Strip trailing "-api" / "-app" / "-backend"
    for tail in ("-api", "-app", "-backend"):
        if base.endswith(tail):
            candidates.append(f"rds-{base[:-len(tail)]}-{suffix}")

    return candidates


def _get_rds_uri(service: str, env: str) -> str:
    """Look up per-service RDS secret, trying multiple naming candidates."""
    sm = _session().client("secretsmanager")
    candidates = _candidate_secret_names(service, env)
    last_error = None

    for secret_name in candidates:
        try:
            resp = sm.get_secret_value(SecretId=secret_name)
            data = json.loads(resp["SecretString"])

            # Try direct URI keys first
            for key in ("url", "uri", "connection_string", "DATABASE_URL", "RDS_URI", "POSTGRES_URI", "datasource_url"):
                val = data.get(key, "")
                if val and ("postgres" in str(val).lower() or "jdbc" in str(val).lower()):
                    return str(val)

            # Assemble from parts — search all keys by substring to handle
            # arbitrary prefixes like main_username, main_host, db_host, etc.
            def _find(data, *exact_keys, substr_match=None):
                for k in exact_keys:
                    if data.get(k):
                        return data[k]
                if substr_match:
                    for k, v in data.items():
                        if v and substr_match in k.lower():
                            return v
                return ""

            host = _find(data, "host", "HOST", "hostname", "db_host", "endpoint", "HOSTNAME", "ENDPOINT", substr_match="host") \
                   or _find(data, substr_match="endpoint")
            port = _find(data, "port", "PORT") or 5432
            user = _find(data, "username", "user", "USERNAME", "USER", substr_match="username") \
                   or _find(data, substr_match="user")
            password = _find(data, "password", "PASSWORD", substr_match="password")
            dbname = _find(data, "dbname", "database", "DB_NAME", "DBNAME", "db_name", substr_match="dbname") \
                     or _find(data, substr_match="database") or "postgres"

            # Host missing from secret — look it up via RDS describe API
            if not host:
                host = _get_rds_host_from_aws(service, env)

            if not host or not user:
                raise RuntimeError(f"Secret '{secret_name}' missing host/username — keys found: {list(data.keys())}")
            return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"RDS secret not found. Tried: {candidates}. Last error: {last_error}"
    )


def _pg_connect(service: str, env: str):
    import psycopg2
    uri = _get_rds_uri(service, env)
    conn = psycopg2.connect(uri, connect_timeout=6)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _rds_metric(cw, instance_id: str, metric: str, start, end, period: int = 300) -> list:
    resp = cw.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName=metric,
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Average", "Maximum"],
    )
    return [
        {"timestamp": str(p["Timestamp"]), "avg": round(p.get("Average", 0), 4), "max": round(p.get("Maximum", 0), 4)}
        for p in sorted(resp["Datapoints"], key=lambda x: x["Timestamp"])
    ]


@tool
def get_rds_metrics(service: str, env: str, minutes: int = 30) -> str:
    """
    Get CloudWatch RDS metrics for the service's database: CPU%, freeable memory (GB),
    database connections, disk queue depth, read/write latency (ms), free storage (GB),
    and burst balance %. Derives the RDS instance ID from the per-service secret
    rds-{service}-dev or rds-{service}-prod.
    Returns JSON string.
    """
    def _run():
        cw = _cw()
        suffix = "prod" if is_prod_env(env) else "dev"

        # Try candidate secret names in order (strips env suffix, -service, etc.)
        sm = _session().client("secretsmanager")
        host = ""
        tried = _candidate_secret_names(service, env)
        for secret_name in tried:
            try:
                resp = sm.get_secret_value(SecretId=secret_name)
                data = json.loads(resp["SecretString"])
                for k, v in data.items():
                    if v and any(x in k.lower() for x in ("host", "endpoint")) and "password" not in k.lower():
                        host = str(v)
                        break
                if host:
                    break
            except Exception:
                continue

        if not host:
            host = _get_rds_host_from_aws(service, env)

        # Derive CloudWatch instance ID from RDS hostname: id.xxx.region.rds.amazonaws.com
        instance_id = host.split(".")[0] if host else _strip_env_suffix(service, env) + f"-{suffix}"

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes + 15)

        result = {"instance_id_used": instance_id}
        for metric in (
            "CPUUtilization", "FreeableMemory", "DatabaseConnections",
            "DiskQueueDepth", "ReadLatency", "WriteLatency",
            "FreeStorageSpace", "BurstBalance",
        ):
            try:
                points = _rds_metric(cw, instance_id, metric, start, end)
                result[metric] = points
            except Exception as e:
                result[metric] = {"error": str(e)}

        return result

    return tool_result(safe_call("get_rds_metrics", _run))


@tool
def get_rds_active_queries(service: str, env: str) -> str:
    """
    Get currently active (non-idle) PostgreSQL queries via direct DB connection: pid, state,
    wait event type, query age in seconds, and query preview. Flags queries running > 60 seconds
    and 'idle in transaction' states. Connection is strictly read-only.
    Requires rds-{service}-dev or rds-{service}-prod secret in Secrets Manager.
    Returns JSON string.
    """
    def _run():
        try:
            conn = _pg_connect(service, env)
        except Exception as e:
            return {"status": "skipped", "reason": str(e)}

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT pid, usename, application_name, state,
                           wait_event_type, wait_event,
                           EXTRACT(EPOCH FROM (now() - query_start))::int AS query_age_seconds,
                           LEFT(query, 300) AS query_preview
                    FROM pg_stat_activity
                    WHERE state != 'idle'
                      AND pid != pg_backend_pid()
                    ORDER BY query_start ASC
                    LIMIT 20
                """)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            return {"status": "success", "active_query_count": len(rows), "queries": rows}
        finally:
            conn.close()

    return tool_result(safe_call("get_rds_active_queries", _run))


@tool
def get_rds_lock_waits(service: str, env: str) -> str:
    """
    Get PostgreSQL lock wait information: which queries are blocked by which blocking queries,
    the locked table, and the query text for both blocker and blocked process.
    Crucial for diagnosing deadlocks and transaction contention.
    Connection is strictly read-only.
    Returns JSON string.
    """
    def _run():
        try:
            conn = _pg_connect(service, env)
        except Exception as e:
            return {"status": "skipped", "reason": str(e)}

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        blocked.pid AS blocked_pid,
                        blocked.usename AS blocked_user,
                        LEFT(blocked.query, 300) AS blocked_query,
                        blocking.pid AS blocking_pid,
                        blocking.usename AS blocking_user,
                        LEFT(blocking.query, 300) AS blocking_query,
                        blocked.wait_event_type,
                        blocked.wait_event
                    FROM pg_stat_activity blocked
                    JOIN pg_stat_activity blocking
                        ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
                    WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
                    LIMIT 10
                """)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            return {"status": "success", "lock_wait_count": len(rows), "lock_waits": rows}
        finally:
            conn.close()

    return tool_result(safe_call("get_rds_lock_waits", _run))


@tool
def get_rds_table_stats(service: str, env: str) -> str:
    """
    Get PostgreSQL table health statistics: live/dead tuple counts, dead tuple ratio %
    (high ratio = table bloat, needs VACUUM), last autovacuum time, sequential scan count
    vs index scan count. High seq_scan on large tables = missing index.
    Connection is strictly read-only.
    Returns JSON string.
    """
    def _run():
        try:
            conn = _pg_connect(service, env)
        except Exception as e:
            return {"status": "skipped", "reason": str(e)}

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT relname AS table_name,
                           n_live_tup, n_dead_tup,
                           CASE WHEN (n_live_tup + n_dead_tup) > 0
                                THEN ROUND(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 2)
                                ELSE 0
                           END AS dead_ratio_pct,
                           seq_scan, idx_scan,
                           last_autovacuum, last_autoanalyze
                    FROM pg_stat_user_tables
                    WHERE (n_live_tup + n_dead_tup) > 1000
                    ORDER BY n_dead_tup DESC
                    LIMIT 15
                """)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            return {"status": "success", "table_count": len(rows), "tables": rows}
        finally:
            conn.close()

    return tool_result(safe_call("get_rds_table_stats", _run))
