"""Terminal tool — agent calls this when investigation is complete to write RCA output."""

import json
import re
from langchain_core.tools import tool

from src.config import get_service, get_env, OUTPUT_DIR
from src.utils import safe_call, tool_result


@tool
def write_rca(rca_json: str) -> str:
    """
    CALL THIS TOOL when you have finished the investigation and are ready to write the RCA.
    Accepts a JSON string matching the RCA schema. Saves rca.json and rca.md to the output
    directory. This is the terminal tool — calling it ends the investigation loop.

    Required JSON schema:
    {
      "incident_summary": "string — 1-2 sentence summary",
      "symptoms": ["observed symptoms"],
      "most_likely_root_cause": "string — earliest event in causal chain",
      "confidence": "high|medium|low",
      "causal_chain": ["ordered list of events from first cause to final symptom"],
      "evidence": [{"source": "analyst-name", "detail": "string", "severity": "info|warning|critical"}],
      "contributing_factors": ["string"],
      "recommended_actions": ["specific action — owner"],
      "suspected_change": {"branch": "", "commit": "", "files": []},
      "collector_failures_or_gaps": ["tools or analysts that failed or were skipped"]
    }
    Returns JSON string with paths to the saved files.
    """
    def _run():
        import sys
        import pathlib
        _root = str(pathlib.Path(__file__).resolve().parent.parent.parent)
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from src.output.writer import write_rca as _write

        raw = rca_json.strip()
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            if m:
                raw = m.group(1)
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start: end + 1]

        try:
            rca = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"status": "failed", "error": f"Invalid JSON: {e}", "raw_preview": raw[:200]}

        service = get_service()
        env = get_env()
        paths = _write(service=service, env=env, rca=rca, output_dir=OUTPUT_DIR)

        print(f"\n[write_rca] RCA saved:")
        print(f"  JSON:     {paths['json']}")
        print(f"  Markdown: {paths['markdown']}")

        return {"status": "success", "saved_to": paths}

    return tool_result(safe_call("write_rca", _run))
