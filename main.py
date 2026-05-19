"""AlphaBee Agent — Task Entry Point

Usage:
    python main.py "帮我分析一下宁德时代的投资价值"
    python main.py                         # 使用内置默认问题
    python main.py --no-color              # 禁用终端颜色
    python main.py --log-dir ./logs        # 指定日志目录
"""

import argparse
import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from alphabee.agents.orchestrator.agent import alphabee_agent
from alphabee.utils import configure_logging, get_logger


# ---------------------------------------------------------------------------
# Terminal colors (ANSI, no extra dependency)
# ---------------------------------------------------------------------------

class _C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    GRAY    = "\033[90m"
    WHITE   = "\033[97m"


_USE_COLOR = True


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(codes) + text + _C.RESET


def _hr(char: str = "─", width: int = 70, color: str = _C.GRAY) -> str:
    return _c(char * width, color)


# ---------------------------------------------------------------------------
# Pretty console helpers
# ---------------------------------------------------------------------------

def _print_header(query: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print()
    print(_hr("═", 70, _C.CYAN))
    print(_c("  🐝  AlphaBee  ", _C.BOLD, _C.CYAN) + _c(f"  {now}", _C.GRAY))
    print(_hr("═", 70, _C.CYAN))
    print(_c("  📝 问题：", _C.BOLD, _C.WHITE) + query)
    print(_hr())
    print()


def _print_step_model_thinking(text: str, step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """LLM 正在推理 / 生成文字。"""
    indent = "  " * depth
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c("🤔 模型推理", _C.BOLD, _C.BLUE) + agent_tag
    print(f"{prefix}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    display = text.strip()
    if len(display) > 500:
        display = display[:500] + _c("  ...(已截断)", _C.DIM)
    for line in display.splitlines():
        print(indent + "       " + _c(line, _C.BLUE))
    print()


def _print_step_tool_call(tool_name: str, args: dict[str, Any], step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """LLM 决定调用某个工具/子代理。"""
    indent = "  " * depth
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c("🔧 调用工具", _C.BOLD, _C.YELLOW) + agent_tag
    print(f"{prefix}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    print(indent + "       " + _c(f"▶  {tool_name}", _C.BOLD, _C.YELLOW))
    if args:
        args_str = json.dumps(args, ensure_ascii=False, indent=None)
        if len(args_str) > 200:
            args_str = args_str[:200] + "..."
        print(indent + "       " + _c(f"   入参: {args_str}", _C.DIM))
    print()


def _print_step_tool_result(tool_name: str, content: str, status: str, step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """工具调用结果返回。"""
    indent = "  " * depth
    icon = "✅" if status != "error" else "❌"
    color = _C.GREEN if status != "error" else _C.RED
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c(f"{icon} 工具结果", _C.BOLD, color) + agent_tag
    print(f"{prefix}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    print(indent + "       " + _c(f"◀  {tool_name}", _C.BOLD, color))
    display = content.strip() if content else "(空)"
    if len(display) > 600:
        display = display[:600] + _c("  ...(已截断)", _C.DIM)
    lines = display.splitlines()
    for line in lines[:12]:
        print(indent + "       " + _c(line, color if status != "error" else _C.RED))
    if len(lines) > 12:
        print(indent + "       " + _c(f"   ... 共 {len(lines)} 行", _C.DIM))
    print()


def _print_final_answer(answer: str) -> None:
    print(_hr("─", 70, _C.GREEN))
    print(_c("  💡 最终回答", _C.BOLD, _C.GREEN))
    print(_hr("─", 70, _C.GREEN))
    print()
    print(answer)
    print()


def _print_footer(total_steps: int, total_time: float) -> None:
    print(_hr("═", 70, _C.CYAN))
    print(
        _c("  ✔  完成", _C.BOLD, _C.GREEN)
        + _c(f"   共 {total_steps} 步", _C.GRAY)
        + _c(f"   耗时 {total_time:.1f}s", _C.GRAY)
    )
    print(_hr("═", 70, _C.CYAN))
    print()


def _print_error(msg: str) -> None:
    print(_hr("═", 70, _C.RED))
    print(_c("  ✖  发生错误", _C.BOLD, _C.RED))
    print(_hr("─", 70, _C.RED))
    print(_c(msg, _C.RED))
    print(_hr("═", 70, _C.RED))
    print()


# ---------------------------------------------------------------------------
# Message content extractor
# ---------------------------------------------------------------------------

def _extract_text(content: Any) -> str:
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


def _tool_name_from_call(tool_call: dict) -> str:
    return tool_call.get("name", "unknown_tool")


def _tool_args_from_call(tool_call: dict) -> dict:
    args = tool_call.get("args", {})
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return {"raw": args}
    return args if isinstance(args, dict) else {}


def _parse_namespace(namespace: tuple) -> tuple[str, int]:
    """Convert a LangGraph namespace tuple into a human-readable path and depth.

    Namespace format: ("AgentName:uuid", "ChildAgent:uuid", ...)
    Returns: ("Orchestrator > CrossAnalysisAgent > FundamentalAgent", depth)
    """
    if not namespace:
        return "Orchestrator", 0
    parts = []
    for seg in namespace:
        # Strip the ":uuid" suffix that LangGraph appends
        name = seg.split(":")[0] if ":" in seg else seg
        parts.append(name)
    return " > ".join(parts), len(parts)


# ---------------------------------------------------------------------------
# Core streaming runner
# ---------------------------------------------------------------------------

async def run_query(query: str) -> None:
    logger = get_logger("main")
    start_ts = time.monotonic()

    _print_header(query)

    # (namespace_tuple, tool_call_id) → tool_name
    # namespace scoping avoids collisions across parallel subgraph branches
    pending_calls: dict[tuple, str] = {}

    step = 0
    final_answer = ""

    logger.info("query_start", query=query)

    try:
        async for namespace, chunk in alphabee_agent.astream(
            {"messages": [("user", query)]},
            stream_mode="updates",
            subgraphs=True,
        ):
            elapsed = time.monotonic() - start_ts
            agent_path, depth = _parse_namespace(namespace)

            for node_name, node_update in chunk.items():
                if not node_update:
                    continue
                messages: list = node_update.get("messages", [])
                if not messages:
                    continue

                for msg in messages:
                    step += 1

                    # ── AIMessage: model thinking or tool dispatch ──
                    if isinstance(msg, AIMessage):
                        text = _extract_text(msg.content)
                        tool_calls: list = msg.tool_calls or []

                        logger.info(
                            "model_output",
                            step=step,
                            agent=agent_path,
                            node=node_name,
                            has_text=bool(text),
                            tool_calls=[_tool_name_from_call(tc) for tc in tool_calls],
                            text_length=len(text),
                            elapsed=round(elapsed, 2),
                        )

                        if text:
                            _print_step_model_thinking(text, step, elapsed, agent_path, depth)
                            final_answer = text

                        for tc in tool_calls:
                            step += 1
                            tname = _tool_name_from_call(tc)
                            targs = _tool_args_from_call(tc)
                            tc_id = tc.get("id", "")
                            if tc_id:
                                pending_calls[(namespace, tc_id)] = tname
                            _print_step_tool_call(tname, targs, step, elapsed, agent_path, depth)
                            logger.info(
                                "tool_call",
                                step=step,
                                agent=agent_path,
                                node=node_name,
                                tool=tname,
                                args=targs,
                                call_id=tc_id,
                                elapsed=round(elapsed, 2),
                            )

                    # ── ToolMessage: result from a tool/subagent ──
                    elif isinstance(msg, ToolMessage):
                        tc_id = getattr(msg, "tool_call_id", "")
                        tname = pending_calls.pop(
                            (namespace, tc_id),
                            getattr(msg, "name", None) or "tool",
                        )
                        status = getattr(msg, "status", "success") or "success"
                        content_text = _extract_text(msg.content)

                        logger.info(
                            "tool_result",
                            step=step,
                            agent=agent_path,
                            node=node_name,
                            tool=tname,
                            status=status,
                            result_length=len(content_text),
                            elapsed=round(elapsed, 2),
                        )
                        _print_step_tool_result(
                            tname, content_text, status, step, elapsed, agent_path, depth
                        )

    except KeyboardInterrupt:
        print()
        print(_c("  ⚠  已中断", _C.BOLD, _C.YELLOW))
        logger.warning("query_interrupted", elapsed=round(time.monotonic() - start_ts, 2))
        sys.exit(0)
    except Exception as exc:
        tb = traceback.format_exc()
        _print_error(f"{type(exc).__name__}: {exc}\n\n{tb}")
        logger.error(
            "query_failed",
            error=str(exc),
            traceback=tb,
            elapsed=round(time.monotonic() - start_ts, 2),
        )
        sys.exit(1)

    total_time = time.monotonic() - start_ts

    if final_answer:
        _print_final_answer(final_answer)

    _print_footer(step, total_time)
    logger.info(
        "query_done",
        total_steps=step,
        total_time=round(total_time, 2),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_QUERY = "帮我分析一下宁德时代近期的投资价值，包括基本面、行情和风险"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AlphaBee — AI 投资分析助手",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=DEFAULT_QUERY,
        help="分析问题，例如：\"帮我分析一下贵州茅台的投资价值\"",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="禁用终端颜色输出",
    )
    parser.add_argument(
        "--log-dir",
        default="./logs",
        help="日志文件目录（默认: ./logs）",
    )
    return parser.parse_args()


def _normalize_query(query: str) -> str:
    """Strip accidental 'key=value' prefix if user ran: python main.py query=..."""
    if "=" in query and query.index("=") < 20 and not query.startswith("http"):
        key, _, rest = query.partition("=")
        if key.strip().isidentifier():
            return rest.strip()
    return query


def main() -> None:
    global _USE_COLOR

    args = _parse_args()
    args.query = _normalize_query(args.query)

    if args.no_color or not sys.stdout.isatty():
        _USE_COLOR = False

    import logging
    from pathlib import Path

    configure_logging(log_dir=Path(args.log_dir))

    # Keep file logging but suppress the console handler so it doesn't
    # mix with our pretty-printed output.
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setLevel(logging.WARNING)

    asyncio.run(run_query(args.query))


if __name__ == "__main__":
    main()
