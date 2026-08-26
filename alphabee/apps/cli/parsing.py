"""消息 / 工具调用 / 命名空间 / 报告负载的解析辅助。

从根 ``main.py`` 拆出：把「从 LangGraph 事件流里提取文本 / 工具信息 / agent 路径」这类
纯函数集中到一处，供 renderer 与 streaming 复用（原 ``_extract_text`` / ``_parse_namespace`` 等）。
"""

from __future__ import annotations

import json
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage


def truncate_json(data: Any, limit: int = 200) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=None)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def classify_call(tool_name: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return (kind, display_name, display_args) for a tool/subagent call."""
    subagent_type = args.get("subagent_type")
    if tool_name == "task" and isinstance(subagent_type, str) and subagent_type.strip():
        display_args = {k: v for k, v in args.items() if k != "subagent_type"}
        return ("subagent", subagent_type.strip(), display_args)
    return ("tool", tool_name, args)


def extract_text(content: Any) -> str:
    """Extract plain text from a message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "thinking"):
                    parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def tool_name_from_call(tool_call: dict[str, Any]) -> str:
    name = tool_call.get("name", "unknown_tool")
    return name if isinstance(name, str) else "unknown_tool"


def tool_args_from_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    args = tool_call.get("args", {})
    if isinstance(args, str):
        try:
            return cast(dict[str, Any], json.loads(args))
        except Exception:
            return {"raw": args}
    return args if isinstance(args, dict) else {}


def tool_label_from_call(tool_call: dict[str, Any]) -> str:
    tool_name = tool_name_from_call(tool_call)
    args = tool_args_from_call(tool_call)
    kind, display_name, _ = classify_call(tool_name, args)
    return f"{kind}:{display_name}"


def parse_namespace(namespace: tuple[str, ...]) -> tuple[str, int]:
    """Convert a LangGraph namespace tuple into a human-readable path and depth.

    Namespace format: ("AgentName:uuid", "ChildAgent:uuid", ...)
    Returns: ("Orchestrator > CrossAnalysisAgent > FundamentalAgent", depth)
    """
    if not namespace:
        return "Orchestrator", 0
    parts = []
    for seg in namespace:
        name = seg.split(":")[0] if ":" in seg else seg
        parts.append(name)
    return " > ".join(parts), len(parts)


def parse_report_payload(content: Any) -> dict[str, Any] | None:
    """Try to parse the final AIMessage content as a JSON report payload."""
    text = extract_text(content)
    if not text:
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and "final_report" in payload:
            return payload
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def append_turn_history(history: list[Any], query: str, answer: str) -> None:
    history.append(HumanMessage(content=query))
    if answer:
        history.append(AIMessage(content=answer))
