"""Shared utilities for all specialist agents: model builder and streaming helper."""

import json
import boto3
from langchain_aws import ChatBedrock
from src.config import BEDROCK_MODEL_ID, BEDROCK_REGION, AWS_PROFILE


def build_model() -> ChatBedrock:
    kwargs = {
        "model_id": BEDROCK_MODEL_ID,
        "region_name": BEDROCK_REGION,
        "model_kwargs": {"max_tokens": 4096, "temperature": 0},
    }
    if AWS_PROFILE:
        session = boto3.Session(profile_name=AWS_PROFILE)
        kwargs["client"] = session.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return ChatBedrock(**kwargs)


def run_specialist(name: str, agent, task: str) -> str:
    """
    Stream a specialist DeepAgent and print its intermediate steps.
    Returns the last AI message content as a string (evidence bundle JSON or prose).
    """
    print(f"\n  ┌── {name} {'─' * max(1, 50 - len(name))}┐")
    all_messages = []
    last_content = ""
    tool_count = 0

    try:
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": task}]},
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
                            preview = text[:500] + ("..." if len(text) > 500 else "")
                            print(f"  [thinking] {preview}")
                            last_content = text

                        for tc in getattr(msg, "tool_calls", []):
                            tool_count += 1
                            tname = tc.get("name", "?")
                            args = json.dumps(tc.get("args", {}), separators=(",", ":"))
                            if len(args) > 180:
                                args = args[:180] + "..."
                            print(f"  [action {tool_count}] {tname}({args})")

                    elif msg_type == "tool":
                        tname = getattr(msg, "name", "?")
                        raw = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
                        try:
                            parsed = json.loads(raw)
                            status = parsed.get("status", "?")
                            if status == "failed":
                                preview = f"FAILED — {parsed.get('error', '')[:200]}"
                            else:
                                data = parsed.get("data", parsed)
                                ds = json.dumps(data, separators=(",", ":"), default=str)
                                preview = ds[:450] + ("..." if len(ds) > 450 else "")
                        except Exception:
                            preview = raw[:450]
                        print(f"    [result] {tname}: {preview}")

    except Exception as e:
        print(f"  [ERROR] {name} raised: {e}")
        print(f"  └── {name} failed {'─' * max(1, 48 - len(name))}┘")
        return json.dumps({"status": "failed", "source": name, "error": str(e)})

    print(f"  └── {name} complete {'─' * max(1, 46 - len(name))}┘")
    return last_content or json.dumps({"status": "no_output", "source": name})
