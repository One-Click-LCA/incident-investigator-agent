"""ELK / Elasticsearch log query tools."""

import base64
import json
from datetime import datetime, timezone, timedelta
from langchain_core.tools import tool
import requests

from src.config import get_secret, get_env
from src.utils import safe_call, tool_result, is_prod_env, truncate

_ENV_SUFFIXES = ("-preprod", "-staging", "-features", "-dev", "-prod", "-qa", "-uat", "-mfg", "-devops", "-internal")


def _strip_env_suffix(name: str) -> str:
    """Strip ECS env suffix from a service name to get the base name.
    e.g. supply-chain-service-preprod → supply-chain-service"""
    for tag in _ENV_SUFFIXES:
        if name.endswith(tag):
            return name[: -len(tag)]
    return name


def _elk_creds(env: str) -> tuple[str, str]:
    """Return (elk_url, base64_basic_auth) for the given env.
    Token in secret is raw user:password — must be base64-encoded for Basic auth header."""
    secret = get_secret()
    prefix = "ELK_PROD" if is_prod_env(env) else "ELK_NONPROD"
    url = secret.get(f"{prefix}_URL", "")
    token = secret.get(f"{prefix}_TOKEN_ENV", "")
    if not url or not token:
        raise RuntimeError(f"ELK credentials not found in secret ({prefix}_URL / {prefix}_TOKEN_ENV)")
    encoded = base64.b64encode(token.encode("utf-8")).decode("utf-8")
    return url, encoded


def _elk_index(service: str, env: str) -> str:
    """Derive ELK index pattern from service name and env (Firelens naming convention).
    Strips ECS env suffix from service name before building index name."""
    base = _strip_env_suffix(service)
    return f".ds-ocl-{env}-{base}-*"


def _time_filter(minutes: int) -> dict:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    return {
        "range": {
            "@timestamp": {
                "gte": start.isoformat(),
                "lte": end.isoformat(),
            }
        }
    }


def _elk_post(url: str, token: str, index: str, body: dict) -> dict:
    resp = requests.post(
        f"{url}/{index}/_search",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _elk_count(url: str, token: str, index: str, body: dict) -> int:
    resp = requests.post(
        f"{url}/{index}/_count",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("count", 0)


def _level_filter(levels: list[str]) -> dict:
    """Build a term/terms filter for structured `level` field — avoids false positives
    from embedded JSON like {"level":"ERROR"} inside a WARN message body."""
    if len(levels) == 1:
        return {"term": {"level": levels[0]}}
    return {"terms": {"level": levels}}


@tool
def count_log_events(service: str, env: str, levels: list, minutes: int = 30) -> str:
    """
    Count log events at specified levels (e.g. ["ERROR","FATAL"] or ["WARN","WARNING"])
    in the ELK index for the given service and time window.
    Always call this before search_logs to understand the volume before sampling.
    Returns JSON with count per level group.
    """
    def _run():
        elk_url, token = _elk_creds(env)
        index = _elk_index(service, env)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        _level_filter(levels),
                        _time_filter(minutes),
                    ]
                }
            }
        }
        count = _elk_count(elk_url, token, index, query)
        return {
            "index": index,
            "levels": levels,
            "window_minutes": minutes,
            "count": count,
        }

    return tool_result(safe_call("count_log_events", _run))


@tool
def search_logs(service: str, env: str, levels: list, keywords: list, size: int = 20, minutes: int = 30) -> str:
    """
    Search ELK logs for the service at specified levels, optionally filtering by keywords
    (e.g. exception class names, error messages). Returns up to `size` log entries with
    timestamps, log level, logger name, message, and stack trace root if present.
    Use after count_log_events confirms errors exist.
    levels example: ["ERROR", "FATAL"]
    keywords example: ["MongoTimeoutException", "connection refused"] — pass [] for no keyword filter.
    Returns JSON string.
    """
    def _run():
        elk_url, token = _elk_creds(env)
        index = _elk_index(service, env)

        filters = [
            _level_filter(levels),
            _time_filter(minutes),
        ]

        if keywords:
            filters.append({
                "bool": {
                    "should": [
                        {"match": {"message": kw}} for kw in keywords
                    ],
                    "minimum_should_match": 1,
                }
            })

        body = {
            "query": {"bool": {"filter": filters}},
            "sort": [{"@timestamp": {"order": "desc"}}],
            "size": min(size, 50),
            "_source": [
                "@timestamp", "level", "loglevel", "logger_name",
                "message", "exception.message", "exception.stacktrace",
                "stack_trace", "trace.id",
            ],
        }

        data = _elk_post(elk_url, token, index, body)
        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value", 0)

        results = []
        for h in hits:
            src = h.get("_source", {})
            results.append({
                "timestamp": src.get("@timestamp"),
                "level": src.get("level") or src.get("loglevel"),
                "logger": src.get("logger_name", ""),
                "message": truncate(src.get("message", ""), 400),
                "exception_message": truncate(src.get("exception", {}).get("message", "") if isinstance(src.get("exception"), dict) else "", 300),
                "stack_trace_root": _extract_stack_root(src),
                "trace_id": src.get("trace", {}).get("id") if isinstance(src.get("trace"), dict) else None,
            })

        return {
            "index": index,
            "total_matching": total,
            "returned": len(results),
            "entries": results,
        }

    return tool_result(safe_call("search_logs", _run))


def _extract_stack_root(src: dict) -> str:
    """Extract first meaningful stack frame from exception or stack_trace fields."""
    exc = src.get("exception", {})
    if isinstance(exc, dict):
        st = exc.get("stacktrace", "")
        if st:
            lines = [l.strip() for l in st.split("\n") if l.strip() and not l.strip().startswith("...")]
            return lines[0] if lines else ""
    raw = src.get("stack_trace", "")
    if raw:
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        return lines[0] if lines else ""
    return ""
