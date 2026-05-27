"""Redis investigation tools (preprod only — prod access disabled)."""

from langchain_core.tools import tool

from src.config import get_secret
from src.utils import safe_call, tool_result, is_prod_env, truncate


def _redis_client(env: str):
    if is_prod_env(env):
        raise RuntimeError("Redis access is disabled in prod (security policy)")
    secret = get_secret()
    url = secret.get("REDIS_NONPROD_URL", "")
    if not url:
        raise RuntimeError("REDIS_NONPROD_URL not found in secret")
    import redis
    return redis.from_url(url, socket_connect_timeout=5, socket_timeout=8, decode_responses=True)


@tool
def get_redis_info(env: str) -> str:
    """
    Get Redis server info: version, role (master/replica), connected clients,
    blocked clients, memory usage, fragmentation ratio, evicted keys (ANY > 0 means
    session loss occurred if policy is volatile-lru), expired keys, and uptime.
    Only available for preprod — prod Redis access is disabled by security policy.
    Returns JSON string.
    """
    def _run():
        if is_prod_env(env):
            return {"status": "skipped", "reason": "Redis access disabled in prod (security policy)"}
        r = _redis_client(env)
        try:
            info = r.info("all")
            return {
                "status": "success",
                "redis_version": info.get("redis_version"),
                "role": info.get("role"),
                "uptime_seconds": info.get("uptime_in_seconds"),
                "connected_clients": info.get("connected_clients"),
                "blocked_clients": info.get("blocked_clients"),
                "maxclients": info.get("maxclients"),
                "used_memory_human": info.get("used_memory_human"),
                "used_memory_peak_human": info.get("used_memory_peak_human"),
                "maxmemory_human": info.get("maxmemory_human"),
                "mem_fragmentation_ratio": info.get("mem_fragmentation_ratio"),
                "maxmemory_policy": info.get("maxmemory_policy"),
                "evicted_keys": info.get("evicted_keys"),
                "expired_keys": info.get("expired_keys"),
                "keyspace_hits": info.get("keyspace_hits"),
                "keyspace_misses": info.get("keyspace_misses"),
                "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec"),
                "rdb_last_bgsave_status": info.get("rdb_last_bgsave_status"),
                "aof_enabled": info.get("aof_enabled"),
                "master_link_status": info.get("master_link_status"),
                "db_sizes": {k: v for k, v in info.items() if k.startswith("db")},
            }
        finally:
            r.close()

    return tool_result(safe_call("get_redis_info", _run))


@tool
def get_redis_slowlog(env: str, count: int = 25) -> str:
    """
    Get Redis slowlog entries — commands that took longer than slowlog-log-slower-than threshold.
    Commands over 10ms are concerning. Watch for: KEYS * (O(N) blocking), SMEMBERS on large sets,
    HGETALL on large hashes. Only available for preprod.
    Returns JSON string.
    """
    def _run():
        if is_prod_env(env):
            return {"status": "skipped", "reason": "Redis access disabled in prod (security policy)"}
        r = _redis_client(env)
        def _decode_cmd(raw) -> str:
            if isinstance(raw, (bytes, bytearray)):
                return raw.decode("utf-8", errors="replace")
            if isinstance(raw, (list, tuple)):
                parts = []
                for t in raw[:5]:
                    if isinstance(t, (bytes, bytearray)):
                        parts.append(t.decode("utf-8", errors="replace"))
                    elif isinstance(t, int):
                        parts.append(chr(t) if 32 <= t <= 126 else f"\\x{t:02x}")
                    else:
                        parts.append(str(t))
                return " ".join(parts)
            return str(raw)

        try:
            entries = r.slowlog_get(count)
            return {
                "status": "success",
                "entry_count": len(entries),
                "entries": [
                    {
                        "id": e.get("id"),
                        "duration_microseconds": e.get("duration"),
                        "command": truncate(_decode_cmd(e.get("command") or []), 200),
                    }
                    for e in entries
                ],
            }
        finally:
            r.close()

    return tool_result(safe_call("get_redis_slowlog", _run))


@tool
def get_redis_memory_doctor(env: str) -> str:
    """
    Get Redis MEMORY DOCTOR diagnostic report which identifies known memory issues
    such as fragmentation, high AOF buffer usage, or high replication backlog.
    Only available for preprod.
    Returns JSON string.
    """
    def _run():
        if is_prod_env(env):
            return {"status": "skipped", "reason": "Redis access disabled in prod (security policy)"}
        r = _redis_client(env)
        try:
            report = r.memory_doctor()
            return {"status": "success", "diagnosis": report}
        except Exception as e:
            msg = str(e)
            if "not implemented" in msg.lower() or "MEMORY DOCTOR" in msg:
                return {"status": "skipped", "reason": "MEMORY DOCTOR not available on this Redis provider (ElastiCache/Valkey)"}
            raise
        finally:
            r.close()

    return tool_result(safe_call("get_redis_memory_doctor", _run))
