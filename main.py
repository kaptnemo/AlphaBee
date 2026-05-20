"""AlphaBee Agent — Task Entry Point

Usage:
    python main.py "帮我分析一下宁德时代的投资价值"
    python main.py                         # 进入多轮对话模式
    python main.py --chat                  # 强制进入多轮对话模式
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
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from alphabee.agents.orchestrator.agent import alphabee_agent
from alphabee.utils import configure_logging, get_logger
from alphabee.workflow import render_monitor_report, run_framework_monitor


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


def _truncate_json(data: Any, limit: int = 200) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=None)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _classify_call(tool_name: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return (kind, display_name, display_args) for a tool/subagent call."""
    subagent_type = args.get("subagent_type")
    if tool_name == "task" and isinstance(subagent_type, str) and subagent_type.strip():
        display_args = {k: v for k, v in args.items() if k != "subagent_type"}
        return ("subagent", subagent_type.strip(), display_args)
    return ("tool", tool_name, args)


def _print_step_tool_call(tool_name: str, args: dict[str, Any], step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """LLM 决定调用某个工具/子代理。"""
    indent = "  " * depth
    kind, display_name, display_args = _classify_call(tool_name, args)
    title = "🤖 调用子代理" if kind == "subagent" else "🔧 调用工具"
    color = _C.MAGENTA if kind == "subagent" else _C.YELLOW
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c(title, _C.BOLD, color) + agent_tag
    print(f"{prefix}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    print(indent + "       " + _c(f"▶  {display_name}", _C.BOLD, color))
    if display_args:
        print(indent + "       " + _c(f"   入参: {_truncate_json(display_args)}", _C.DIM))
    print()


def _print_step_tool_result(tool_name: str, content: str, status: str, step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """工具调用结果返回。"""
    indent = "  " * depth
    is_subagent = tool_name.endswith("Agent")
    if is_subagent:
        title = "🤖 子代理结果"
        color = _C.MAGENTA if status != "error" else _C.RED
    else:
        icon = "✅" if status != "error" else "❌"
        title = f"{icon} 工具结果"
        color = _C.GREEN if status != "error" else _C.RED
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c(title, _C.BOLD, color) + agent_tag
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


def _print_chat_help() -> None:
    print()
    print(_c("  💬 多轮对话模式", _C.BOLD, _C.CYAN))
    print(_c("  直接输入问题继续追问；输入 /clear 清空上下文，/exit 结束会话。", _C.DIM))
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


def _tool_label_from_call(tool_call: dict) -> str:
    tool_name = _tool_name_from_call(tool_call)
    args = _tool_args_from_call(tool_call)
    kind, display_name, _ = _classify_call(tool_name, args)
    return f"{kind}:{display_name}"


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

def _append_turn_history(history: list[Any], query: str, answer: str) -> None:
    history.append(HumanMessage(content=query))
    if answer:
        history.append(AIMessage(content=answer))


async def run_query(query: str, history: list[Any] | None = None) -> str:
    logger = get_logger("main")
    start_ts = time.monotonic()
    conversation = [*(history or []), HumanMessage(content=query)]

    _print_header(query)

    # (namespace_tuple, tool_call_id) → display_name
    # namespace scoping avoids collisions across parallel subgraph branches
    pending_calls: dict[tuple, str] = {}

    step = 0
    final_answer = ""

    logger.info("query_start", query=query, history_messages=len(conversation) - 1)

    try:
        async for namespace, chunk in alphabee_agent.astream(
            {"messages": conversation},
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
                            tool_calls=[_tool_label_from_call(tc) for tc in tool_calls],
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
                            kind, display_name, _ = _classify_call(tname, targs)
                            tc_id = tc.get("id", "")
                            if tc_id:
                                pending_calls[(namespace, tc_id)] = display_name
                            _print_step_tool_call(tname, targs, step, elapsed, agent_path, depth)
                            logger.info(
                                "tool_call",
                                step=step,
                                agent=agent_path,
                                node=node_name,
                                tool=tname,
                                call_kind=kind,
                                display_name=display_name,
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
    return final_answer


async def run_chat_session(initial_query: str | None = None) -> None:
    history: list[Any] = []
    turn = 1

    _print_chat_help()

    if initial_query:
        initial_query = _normalize_query(initial_query)
        answer = await run_query(initial_query, history)
        _append_turn_history(history, initial_query, answer)
        turn += 1

    while True:
        try:
            raw = input(_c(f"[{turn:02d}] 你> ", _C.BOLD, _C.CYAN))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        query = raw.strip()
        if not query:
            continue

        command = query.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            print()
            print(_c("  👋 会话结束", _C.DIM))
            print()
            break
        if command == "/clear":
            history.clear()
            turn = 1
            print()
            print(_c("  ♻ 上下文已清空", _C.DIM))
            print()
            continue

        query = _normalize_query(query)
        answer = await run_query(query, history)
        _append_turn_history(history, query, answer)
        turn += 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AlphaBee — AI 投资分析助手",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="分析问题；不传则进入多轮对话模式",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        default=False,
        help="进入多轮对话模式；可与初始问题一起使用",
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
    parser.add_argument(
        "--monitor-framework",
        default=None,
        help="观察框架 Markdown 路径。提供后将进入持续跟踪模式。",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="监控模式下的股票代码，例如 300760 或 300760.SZ",
    )
    parser.add_argument(
        "--monitor-periods",
        type=int,
        default=8,
        help="监控模式拉取的财报期数（默认: 8）",
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
    if args.monitor_framework and not args.symbol:
        raise SystemExit("--monitor-framework 模式下必须同时提供 --symbol")

    if args.query:
        args.query = _normalize_query(args.query)

    if args.no_color or not sys.stdout.isatty():
        _USE_COLOR = False

    import logging

    configure_logging(log_dir=Path(args.log_dir))

    # Keep file logging but suppress the console handler so it doesn't
    # mix with our pretty-printed output.
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setLevel(logging.WARNING)

    if args.monitor_framework:
        start_ts = time.monotonic()
        _print_header(f"监控框架：{args.monitor_framework} | 标的：{args.symbol}")
        result = asyncio.run(
            run_framework_monitor(
                framework_path=args.monitor_framework,
                symbol=args.symbol,
                periods=args.monitor_periods,
            )
        )
        _print_final_answer(render_monitor_report(result))
        _print_footer(1, time.monotonic() - start_ts)
        return

    if args.chat or not args.query:
        asyncio.run(run_chat_session(args.query))
        return

    asyncio.run(run_query(args.query))


if __name__ == "__main__":
    main()
