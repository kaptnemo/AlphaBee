"""Web 事件契约（前后端唯一对齐面）。

``stream.py`` 产出的事件统一是 ``dict``，带 ``type`` 字段；前端 ``web/src/lib/types.ts``
用 TS 类型镜像这里的事件类型。任何一端修改字段都必须同步另一端。

事件类型一览：

============= ========================== ======================================================
事件类型       语义                       载荷字段
============= ========================== ======================================================
stage_start   流水线阶段开始              node, label, icon, elapsed
stage_done    流水线阶段结束              node, label, icon, elapsed
thinking      模型"思考"文本             step, agent, text
tool_call     工具/子代理调用开始         step, agent, tool, kind, args
tool_result   工具/子代理返回             step, agent, tool, status, length
report        最终报告负载（结构化 JSON） payload
done          整次查询结束                final_answer, total_steps, total_time, flags
error         查询失败/中断               message, traceback
============= ========================== ======================================================
"""

from __future__ import annotations

from typing import Any


class EventType:
    """事件类型常量。用类内常量而非枚举，便于 ``make_event`` 直接序列化。"""

    STAGE_START = "stage_start"
    STAGE_DONE = "stage_done"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REPORT = "report"
    DONE = "done"
    ERROR = "error"


def make_event(event_type: str, **payload: Any) -> dict[str, Any]:
    """构造一个带 ``type`` 字段的事件 dict，便于 SSE 直接序列化。"""
    return {"type": event_type, **payload}


def stage_start(node: str, label: str, icon: str, elapsed: float) -> dict[str, Any]:
    return make_event(EventType.STAGE_START, node=node, label=label, icon=icon, elapsed=round(elapsed, 2))


def stage_done(node: str, label: str, icon: str, elapsed: float) -> dict[str, Any]:
    return make_event(EventType.STAGE_DONE, node=node, label=label, icon=icon, elapsed=round(elapsed, 2))


def thinking(step: int, agent: str, text: str) -> dict[str, Any]:
    return make_event(EventType.THINKING, step=step, agent=agent, text=text)


def tool_call(step: int, agent: str, tool: str, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    return make_event(EventType.TOOL_CALL, step=step, agent=agent, tool=tool, kind=kind, args=args)


def tool_result(step: int, agent: str, tool: str, status: str, length: int) -> dict[str, Any]:
    return make_event(EventType.TOOL_RESULT, step=step, agent=agent, tool=tool, status=status, length=length)


def report(payload: dict[str, Any]) -> dict[str, Any]:
    return make_event(EventType.REPORT, payload=payload)


def done(final_answer: str, total_steps: int, total_time: float, flags: dict[str, bool]) -> dict[str, Any]:
    return make_event(
        EventType.DONE,
        final_answer=final_answer,
        total_steps=total_steps,
        total_time=round(total_time, 2),
        flags=flags,
    )


def error(message: str, traceback: str | None = None) -> dict[str, Any]:
    return make_event(EventType.ERROR, message=message, traceback=traceback)
