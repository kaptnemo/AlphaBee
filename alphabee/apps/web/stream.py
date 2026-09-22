"""Web 专用流式消费者：``alphabee_agent.astream`` → ``AsyncIterator[dict]``。

与 ``alphabee.apps.cli.streaming`` 的区别：
- CLI 版把事件 ``print`` 成彩色文本，并顺带写日志、落 task records；
- 本模块只产出结构化事件（纯数据），不做任何渲染 / 日志 / 落盘副作用。

复用（只读、不改动）：
- ``alphabee.apps.cli.parsing`` 的纯函数（``extract_text`` / ``parse_namespace`` / tool 解析）；
- ``alphabee.apps.cli.renderer.STAGE_MAP`` 的阶段元数据（icon / label）。

langfuse 追踪在此沿用 CLI 的逻辑：可用则挂载 CallbackHandler，否则静默跳过
（Web 场景不向 stdout 打印提示，避免污染日志）。
"""

from __future__ import annotations

import time
import traceback
from collections.abc import AsyncIterator
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import CallbackHandler

from alphabee.apps.cli.parsing import (
    classify_call,
    extract_text,
    parse_namespace,
    parse_report_payload,
    tool_args_from_call,
    tool_name_from_call,
)
from alphabee.apps.cli.renderer import STAGE_MAP
from alphabee.orchestrator.agent import alphabee_agent
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import get_logger

from . import events as ev


def _langfuse_available(timeout: float = 2.0) -> bool:
    """检测 Langfuse 是否可用（配置启用 + 服务可达）。Web 场景静默处理。"""
    import socket as _socket
    from urllib.parse import urlparse as _urlparse

    from alphabee.config import settings

    cfg = settings.langfuse
    if not (cfg.enable and cfg.public_key and cfg.secret_key and cfg.base_url):
        return False

    try:
        parsed = _urlparse(cfg.base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = _socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def _stage_info(node_name: str) -> tuple[str, str] | None:
    """返回 (label, icon)，若节点不在 STAGE_MAP 中则返回 None。"""
    info = STAGE_MAP.get(node_name)
    if info is None:
        return None
    icon, label, _color = info
    return label, icon


async def stream_events(
    query: str,
    history: list[Any] | None = None,
    *,
    enhance: bool = False,
    llm_review: bool = False,
    midterm: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """流式消费 orchestrator 的事件，产出结构化事件 dict。

    参数与 ``cli.streaming.run_query`` 一致，便于前端/CLI 共用同一语义。
    """
    logger = get_logger("web")
    start_ts = time.monotonic()
    conversation: list[Any] = [*(history or []), HumanMessage(content=query)]

    active_stage: str | None = None
    stage_entry_ts = start_ts
    step = 0
    final_answer = ""
    report_payload: dict[str, Any] | None = None

    # (namespace_tuple, tool_call_id) → tool 显示名
    pending_calls: dict[tuple[tuple[str, ...], str], str] = {}

    logger.info(
        "web_query_start",
        query=query,
        history_messages=len(conversation) - 1,
        enhance=enhance,
        llm_review=llm_review,
        midterm=midterm,
    )

    callbacks: list[Any] = []
    if _langfuse_available():
        callbacks.append(CallbackHandler())
    else:
        logger.info("web_langfuse_disabled")

    try:
        async for namespace, chunk in alphabee_agent.astream(
            cast(
                OrchestratorState,
                {"messages": conversation, "enhance": enhance, "llm_review": llm_review, "midterm": midterm},
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

                # ── 顶层阶段切换 ──
                if node_name in STAGE_MAP and depth == 0:
                    stage_info = _stage_info(node_name)
                    if stage_info is not None:
                        label, icon = stage_info
                        if active_stage is not None and active_stage != node_name:
                            yield ev.stage_done(
                                active_stage, *_stage_info(active_stage) or ("", ""), time.monotonic() - stage_entry_ts
                            )
                        if active_stage != node_name:
                            yield ev.stage_start(node_name, label, icon, elapsed)
                            active_stage = node_name
                            stage_entry_ts = time.monotonic()

                if depth == 0:
                    # 顶层节点完成：finalize_message 特殊处理，提取最终 JSON 载荷。
                    if node_name == "finalize_message":
                        for msg in node_update.get("messages", []):
                            if isinstance(msg, AIMessage):
                                text = extract_text(msg.content)
                                if text:
                                    final_answer = text
                                    break
                    continue

                # depth > 0：子代理图的消息
                messages: list[Any] = node_update.get("messages", [])
                if not messages:
                    continue

                for msg in messages:
                    step += 1

                    if isinstance(msg, AIMessage):
                        text = extract_text(msg.content)
                        tool_calls: list[Any] = msg.tool_calls or []

                        if text:
                            yield ev.thinking(step, agent_path, text)
                            final_answer = text

                        for tc in tool_calls:
                            step += 1
                            tname = tool_name_from_call(tc)
                            targs = tool_args_from_call(tc)
                            kind, display_name, display_args = classify_call(tname, targs)
                            tc_id = tc.get("id", "")
                            if tc_id:
                                pending_calls[(namespace, tc_id)] = display_name or tname
                            yield ev.tool_call(step, agent_path, display_name or tname, kind, display_args)

                    elif isinstance(msg, ToolMessage):
                        tc_id = getattr(msg, "tool_call_id", "")
                        tname = pending_calls.pop(
                            (namespace, tc_id),
                            getattr(msg, "name", None) or "tool",
                        )
                        status = getattr(msg, "status", "success") or "success"
                        content_text = extract_text(msg.content)
                        yield ev.tool_result(step, agent_path, tname, status, len(content_text))

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("web_query_failed", error=str(exc), traceback=tb)
        yield ev.error(f"{type(exc).__name__}: {exc}", tb)
        return

    total_time = time.monotonic() - start_ts

    # ── 末段 stage done ──
    if active_stage:
        yield ev.stage_done(active_stage, *_stage_info(active_stage) or ("", ""), time.monotonic() - stage_entry_ts)

    # ── 最终报告 ──
    report_payload = parse_report_payload(final_answer)
    if report_payload:
        yield ev.report(report_payload)

    yield ev.done(
        final_answer=final_answer,
        total_steps=step,
        total_time=total_time,
        flags={"enhance": enhance, "llm_review": llm_review, "midterm": midterm},
    )
    logger.info("web_query_done", total_steps=step, total_time=round(total_time, 2))
