"""MongoDB Atlas API + direct PyMongo tools (read-only)."""

import json
from langchain_core.tools import tool
import requests

from src.config import get_secret
from src.utils import safe_call, tool_result, is_prod_env, truncate

_ATLAS_BASE = "https://cloud.mongodb.com/api/atlas/v2"


def _atlas_token(env: str) -> str:
    import base64
    secret = get_secret()
    prefix = "MDB_PROD" if is_prod_env(env) else "MDB_NONPROD"
    client_id = secret.get(f"{prefix}_CLIENT_ID", "")
    client_secret = secret.get(f"{prefix}_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(f"MongoDB Atlas OAuth credentials not found ({prefix}_CLIENT_ID / {prefix}_CLIENT_SECRET)")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://cloud.mongodb.com/api/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _atlas_get(token: str, path: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{_ATLAS_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.atlas.2023-01-01+json"},
        params=params or {},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _project_id(env: str) -> str:
    secret = get_secret()
    prefix = "MDB_PROD" if is_prod_env(env) else "MDB_NONPROD"
    pid = secret.get(f"{prefix}_PROJECT_ID", "")
    if not pid:
        raise RuntimeError(f"MongoDB Atlas project ID not found ({prefix}_PROJECT_ID)")
    return pid


@tool
def get_mongo_cluster_health(env: str) -> str:
    """
    Get MongoDB Atlas cluster health via the Atlas Management API: connection count vs limit,
    CPU%, ops/sec, disk IOPS%, average latency, and cluster state for all clusters in the project.
    Use when MongoDB-related errors appear in logs (MongoTimeoutException, connection pool exhausted).
    Returns JSON string.
    """
    def _run():
        token = _atlas_token(env)
        project_id = _project_id(env)

        clusters_resp = _atlas_get(token, f"/groups/{project_id}/clusters")
        clusters = clusters_resp.get("results", [])

        results = []
        for c in clusters:
            cluster_name = c["name"]
            # Get realtime measurements
            processes = _atlas_get(token, f"/groups/{project_id}/processes", {"pageNum": 1, "itemsPerPage": 5})
            measurements = {}
            for proc in processes.get("results", []):
                if cluster_name.lower() in proc.get("hostname", "").lower():
                    pid = f"{proc['hostname']}:{proc['port']}"
                    m_resp = _atlas_get(
                        token,
                        f"/groups/{project_id}/processes/{pid}/measurements",
                        {
                            "granularity": "PT1M",
                            "period": "PT10M",
                            "m": [
                                "CONNECTIONS", "OPCOUNTER_CMD",
                                "QUERY_EXECUTOR_SCANNED", "QUERY_EXECUTOR_SCANNED_OBJECTS",
                                "CACHE_DIRTY_BYTES", "CACHE_USED_BYTES",
                                "SYSTEM_CPU_PERCENT", "DISK_PARTITION_IOPS_TOTAL",
                            ],
                        },
                    )
                    for ms in m_resp.get("measurements", []):
                        name = ms["name"]
                        vals = [dp["value"] for dp in ms.get("dataPoints", []) if dp.get("value") is not None]
                        measurements[name] = round(sum(vals) / len(vals), 2) if vals else None
                    break

            results.append({
                "name": cluster_name,
                "state": c.get("stateName"),
                "paused": c.get("paused", False),
                "mongo_version": c.get("mongoDBVersion"),
                "cluster_type": c.get("clusterType"),
                "measurements": measurements,
            })

        return {"project_id": project_id, "cluster_count": len(results), "clusters": results}

    return tool_result(safe_call("get_mongo_cluster_health", _run))


@tool
def get_mongo_slow_queries(env: str) -> str:
    """
    Get MongoDB slow query suggestions from the Atlas Performance Advisor.
    Returns queries with duration, plan type (COLLSCAN = no index — critical), docs examined
    vs returned ratio, and suggested index. Use when suspecting DB performance issues.
    Returns JSON string.
    """
    def _run():
        token = _atlas_token(env)
        project_id = _project_id(env)

        clusters_resp = _atlas_get(token, f"/groups/{project_id}/clusters")
        all_suggestions = []
        for c in clusters_resp.get("results", []):
            cluster_name = c["name"]
            try:
                suggestions_resp = _atlas_get(
                    token,
                    f"/groups/{project_id}/performanceAdvisor/suggestedIndexes",
                    {"clusterName": cluster_name},
                )
                for s in suggestions_resp.get("suggestedIndexes", []):
                    namespace = s.get("namespace", "")
                    impact = s.get("impact", [])
                    for op in s.get("operations", []):
                        all_suggestions.append({
                            "cluster": cluster_name,
                            "namespace": namespace,
                            "avg_ms": op.get("avgMs"),
                            "total_ms": op.get("totalMs"),
                            "plan_type": op.get("predicates", [{}])[0].get("type", "") if op.get("predicates") else "",
                            "docs_examined": op.get("docsExamined"),
                            "docs_returned": op.get("nReturned"),
                            "suggested_index": s.get("index"),
                        })
            except Exception as e:
                all_suggestions.append({"cluster": cluster_name, "error": str(e)})

        return {"suggestion_count": len(all_suggestions), "suggestions": all_suggestions[:20]}

    return tool_result(safe_call("get_mongo_slow_queries", _run))


@tool
def get_mongo_alerts(env: str) -> str:
    """
    Get all open MongoDB Atlas alerts for the project (connection count exceeded,
    slow query threshold, disk IOPS, replica set election, memory usage alerts).
    Returns JSON string.
    """
    def _run():
        token = _atlas_token(env)
        project_id = _project_id(env)
        alerts_resp = _atlas_get(token, f"/groups/{project_id}/alerts", {"status": "OPEN"})
        alerts = [
            {
                "id": a["id"],
                "event_type": a.get("eventTypeName"),
                "severity": a.get("severity"),
                "created": str(a.get("created")),
                "metric_name": a.get("metricName"),
                "cluster_name": a.get("clusterName"),
                "current_value": a.get("currentValue", {}).get("number"),
                "threshold": a.get("currentValue", {}).get("units"),
            }
            for a in alerts_resp.get("results", [])
        ]
        return {"open_alert_count": len(alerts), "alerts": alerts}

    return tool_result(safe_call("get_mongo_alerts", _run))


@tool
def get_mongo_server_stats(env: str) -> str:
    """
    Get direct MongoDB server statistics via PyMongo: current connections, available connections,
    opcounters (queries/inserts/updates/deletes per sec), memory usage, and WiredTiger cache.
    Only available if MDB_NONPROD_URI (or MDB_PROD_URI) is configured.
    Returns JSON string.
    """
    def _run():
        secret = get_secret()
        prefix = "MDB_PROD" if is_prod_env(env) else "MDB_NONPROD"
        uri = secret.get(f"{prefix}_URI", "")
        if not uri:
            return {"status": "skipped", "reason": f"{prefix}_URI not configured in secret"}

        from pymongo import MongoClient, ReadPreference
        client = MongoClient(
            uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=8000,
            appName="incident-investigator-readonly",
            read_preference=ReadPreference.SECONDARY_PREFERRED,
        )
        try:
            status = client.admin.command("serverStatus")
            conn = status.get("connections", {})
            mem = status.get("mem", {})
            cache = status.get("wiredTiger", {}).get("cache", {})
            opcounters = status.get("opcounters", {})
            return {
                "status": "success",
                "connections": {
                    "current": conn.get("current"),
                    "available": conn.get("available"),
                    "total_created": conn.get("totalCreated"),
                },
                "opcounters": opcounters,
                "memory_mb": {
                    "resident": mem.get("resident"),
                    "virtual": mem.get("virtual"),
                },
                "wiredtiger_cache": {
                    "used_bytes": cache.get("bytes currently in the cache"),
                    "dirty_bytes": cache.get("tracked dirty bytes in the cache"),
                    "max_bytes": cache.get("maximum bytes configured"),
                },
            }
        finally:
            client.close()

    return tool_result(safe_call("get_mongo_server_stats", _run))


@tool
def get_mongo_db_stats(env: str, database: str) -> str:
    """
    Get MongoDB database statistics via direct PyMongo connection: size in MB, storage size,
    index size, collection count, and estimated document counts per collection.
    Only available if MDB_NONPROD_URI (or MDB_PROD_URI) is configured.
    Use when investigating data growth or collection-level issues.
    Returns JSON string.
    """
    def _run():
        secret = get_secret()
        prefix = "MDB_PROD" if is_prod_env(env) else "MDB_NONPROD"
        uri = secret.get(f"{prefix}_URI", "")
        if not uri:
            return {"status": "skipped", "reason": f"{prefix}_URI not configured in secret"}

        from pymongo import MongoClient
        client = MongoClient(
            uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            appName="incident-investigator-readonly",
        )
        try:
            db = client[database]
            stats = db.command("dbStats", scale=1024 * 1024)
            collections = []
            for coll_name in db.list_collection_names():
                try:
                    count = db[coll_name].estimated_document_count()
                    collections.append({"collection": coll_name, "estimated_count": count})
                except Exception:
                    pass
            return {
                "status": "success",
                "database": database,
                "size_mb": round(stats.get("dataSize", 0), 2),
                "storage_mb": round(stats.get("storageSize", 0), 2),
                "index_size_mb": round(stats.get("indexSize", 0), 2),
                "collection_count": stats.get("collections", 0),
                "collections": sorted(collections, key=lambda x: x["estimated_count"], reverse=True)[:20],
            }
        finally:
            client.close()

    return tool_result(safe_call("get_mongo_db_stats", _run))
