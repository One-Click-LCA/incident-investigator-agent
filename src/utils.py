import json
import traceback
from datetime import datetime, timezone
from typing import Any, Callable


def is_prod_env(env: str) -> bool:
    return str(env).strip().lower() == "prod"


def safe_call(name: str, fn: Callable) -> dict:
    try:
        result = fn()
        return {"status": "success", "data": result}
    except Exception as e:
        return {
            "status": "failed",
            "collector": name,
            "error": str(e),
            "traceback": traceback.format_exc()[-800:],
        }


def truncate(text: str, max_chars: int = 500) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def minutes_ago_iso(minutes: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def tool_result(data: Any) -> str:
    """Serialize tool output to compact JSON string for LLM consumption."""
    return json.dumps(data, default=str, separators=(",", ":"))
