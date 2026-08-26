"""单次查询的流式执行（从根 main.py 拆出）。

``run_query`` 是 CLI 的核心：用 ``subgraphs=True`` 流式消费 orchestrator 的 updates，
逐节点打印进度（委托 renderer）+ 记录日志 + 落 task records。不包含终端 UI 的静态渲染
（在 renderer.py），也不包含多轮循环（在 chat.py）。
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import CallbackHandler

from alphabee.apps.cli.colors import Color, color, hr
from alphabee.apps.cli.parsing import (
    extract_text,
    parse_namespace,
    parse_report_payload,
    tool_args_from_call,
    tool_label_from_call,
    tool_name_from_call,
)
from alphabee.apps.cli.renderer import (
    STAGE_MAP,
    print_error,
    print_footer,
    print_header,
    print_node_update_summary,
    print_stage_done,
    print_stage_start,
    print_step_model_thinking,
    print_step_tool_call,
    print_step_tool_result,
    render_final_report,
)
from alphabee.orchestrator.agent import alphabee_agent
from alphabee.orchestrator.state import OrchestratorState
from alphabee.tools.common import extract_symbols_from_query
from alphabee.utils import get_logger


def langfuse_available(timeout: float = 2.0) -> bool:
    """Check whether a Langfuse server is configured and reachable.

    Returns False when the ``enable`` flag is off, API keys are missing,
    or the server does not respond within *timeout* seconds.
    """
    import socket as _socket
    from urllib.parse import urlparse as _urlparse

    from alphabee.config import settings

    cfg = settings.langfuse
    if not cfg.enable:
        return False
    if not cfg.public_key or not cfg.secret_key:
        return False
    if not cfg.base_url:
        return False

    try:
        parsed = _urlparse(cfg.base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = _socket.create_connection((host, port), timeout=timeout)
        sock.close()
        print(f"Langfuse server reachable at {host}:{port}")
        return True
    except Exception:
        print(f"Langfuse server not reachable at {cfg.base_url}")
        return False


async def run_query(
    query: str,
    history: list[Any] | None = None,
    *,
    enhance: bool = False,
    llm_review: bool = False,
) -> str:
    logger = get_logger("main")
    start_ts = time.monotonic()
    conversation = [*(history or []), HumanMessage(content=query)]

    print_header(query, enhance, llm_review)

    # Track pipeline stages for progress reporting
    active_stage: str | None = None
    stage_entry_ts = start_ts
    step = 0
    final_answer = ""
    report_payload: dict[str, Any] | None = None

    # (namespace_tuple, tool_call_id) → display_name
    pending_calls: dict[tuple[tuple[str, ...], str], str] = {}

    logger.info(
        "query_start",
        query=query,
        history_messages=len(conversation) - 1,
        enhance=enhance,
        llm_review=llm_review,
    )

    callbacks: list[Any] = []
    if langfuse_available():
        callbacks.append(CallbackHandler())
    else:
        logger.info("langfuse_disabled")
        print(color("  ⚠  Langfuse 未启用或不可达，禁用追踪日志", Color.BOLD, Color.YELLOW))

    try:
        async for namespace, chunk in alphabee_agent.astream(
            cast(
                OrchestratorState,
                {"messages": conversation, "enhance": enhance, "llm_review": llm_review},
            ),
            config=cast(RunnableConfig, {"callbacks": callbacks} if callbacks else {}),
            stream_mode="updates",
            subgraphs=True,
        ):
            namespace = cast(tuple[str, ...], namespace)
            chunk = cast(dict[str, Any], chunk)
            elapsed = time.monotonic() - start_ts
            agent_path, depth = parse_namespace(namespace)

            for node_name, node_update in chunk.items():
                if not node_update:
                    continue

                # ── Stage transition detection ──
                if node_name in STAGE_MAP and depth == 0:
                    if active_stage is not None and active_stage != node_name:
                        print_stage_done(
                            active_stage,
                            time.monotonic() - stage_entry_ts,
                        )
                    if active_stage != node_name:
                        print_stage_start(node_name, elapsed)
                        active_stage = node_name
                        stage_entry_ts = time.monotonic()

                if depth == 0:
                    # Orchestrator node completed — print structured summary.
                    # finalize_message is special: it embeds the final JSON payload
                    # inside an AIMessage. Capture that before skipping message loop.
                    if node_name == "finalize_message":
                        for msg in node_update.get("messages", []):
                            if isinstance(msg, AIMessage):
                                text = extract_text(msg.content)
                                if text:
                                    final_answer = text
                                    break
                    print_node_update_summary(node_name, node_update, elapsed)
                    continue

                # depth > 0: messages from nested subagent graphs
                messages: list[Any] = node_update.get("messages", [])
                if not messages:
                    continue

                for msg in messages:
                    step += 1

                    # ── AIMessage: model thinking or tool dispatch ──
                    if isinstance(msg, AIMessage):
                        text = extract_text(msg.content)
                        tool_calls: list[Any] = msg.tool_calls or []

                        logger.info(
                            "model_output",
                            step=step,
                            agent=agent_path,
                            node=node_name,
                            has_text=bool(text),
                            tool_calls=[tool_label_from_call(tc) for tc in tool_calls],
                            text_length=len(text),
                            elapsed=round(elapsed, 2),
                        )

                        if text:
                            print_step_model_thinking(text, step, elapsed, agent_path, depth)
                            final_answer = text

                        for tc in tool_calls:
                            step += 1
                            tname = tool_name_from_call(tc)
                            targs = tool_args_from_call(tc)
                            tc_id = tc.get("id", "")
                            if tc_id:
                                pending_calls[(namespace, tc_id)] = tname
                            print_step_tool_call(tname, targs, step, elapsed, agent_path, depth)
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
                        content_text = extract_text(msg.content)

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
                        print_step_tool_result(tname, content_text, status, step, elapsed, agent_path, depth)

    except KeyboardInterrupt:
        print()
        print(color("  ⚠  已中断", Color.BOLD, Color.YELLOW))
        logger.warning("query_interrupted", elapsed=round(time.monotonic() - start_ts, 2))
        sys.exit(0)
    except Exception as exc:
        tb = traceback.format_exc()
        print_error(f"{type(exc).__name__}: {exc}\n\n{tb}")
        logger.error(
            "query_failed",
            error=str(exc),
            traceback=tb,
            elapsed=round(time.monotonic() - start_ts, 2),
        )
        sys.exit(1)

    total_time = time.monotonic() - start_ts

    # ── Final stage done ──
    if active_stage:
        print_stage_done(active_stage, time.monotonic() - stage_entry_ts)

    # ── Render report ──
    # Try to parse the final AIMessage as a JSON report payload
    report_payload = parse_report_payload(final_answer)
    if report_payload:
        render_final_report(report_payload)
    elif final_answer:
        # Fallback: raw answer display
        print()
        print(hr("─", 70, Color.GREEN))
        print(color("  💡 最终回答", Color.BOLD, Color.GREEN))
        print(hr("─", 70, Color.GREEN))
        print()
        # Truncate very long raw answers
        if len(final_answer) > 3000:
            print(final_answer[:3000])
            print(color("  ...(已截断，完整内容见日志)", Color.DIM))
        else:
            print(final_answer)
        print()

    # ── Record capture for task records ──
    if report_payload:
        try:
            from alphabee.task_records import TaskRecorder, TaskStore

            symbols = extract_symbols_from_query(query)
            symbol = list(symbols.values())[0] if symbols else None
            artifacts_list = report_payload.get("artifacts", [])
            recorder = TaskRecorder()
            record = recorder.capture(
                query=query,
                symbol=symbol,
                flags={"enhance": enhance, "llm_review": llm_review},
                payload=report_payload,
                artifacts=artifacts_list,
                start_ts=start_ts,
            )
            store = TaskStore()
            store.save(record)
            logger.info("task_record_saved", task_id=record.task_id, symbol=symbol)
        except Exception as exc:
            logger.warning("task_record_capture_failed", error=str(exc))

    print_footer(step, total_time, enhance, llm_review)
    logger.info(
        "query_done",
        total_steps=step,
        total_time=round(total_time, 2),
        enhance=enhance,
        llm_review=llm_review,
    )
    return final_answer
