"""聊天路由：POST /api/chat → SSE 流式响应。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from alphabee.apps.web.sessions import session_store
from alphabee.apps.web.sse import sse_response
from alphabee.apps.web.stream import stream_events

chat_router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    """POST /api/chat 的请求体。"""

    query: str
    session_id: str | None = None
    enhance: bool = False
    llm_review: bool = False
    midterm: bool = False


@chat_router.post("/api/chat")
async def chat(body: ChatRequest):
    """接收 JSON 请求体，返回 SSE 事件流。

    请求体：
        {
            "query": "帮我分析一下宁德时代的投资价值",
            "session_id": null,          // 可选，多轮对话时复用
            "enhance": false,
            "llm_review": false,
            "midterm": false
        }
    """
    if not body.query or not body.query.strip():
        return sse_response(_error_stream("缺少 query 字段"))

    session = session_store.get_or_create(body.session_id)

    event_stream = _chat_stream(
        query=body.query.strip(),
        history=list(session.history),
        enhance=body.enhance,
        llm_review=body.llm_review,
        midterm=body.midterm,
        session=session,
    )
    return sse_response(event_stream)


async def _chat_stream(
    query: str,
    history: list[Any],
    *,
    enhance: bool,
    llm_review: bool,
    midterm: bool,
    session: Any,
) -> AsyncIterator[dict[str, Any]]:
    """流式执行查询；结束后把本轮问答追加回会话 history。"""

    from alphabee.apps.web import events as ev

    final_answer = ""
    async for event in stream_events(
        query,
        history,
        enhance=enhance,
        llm_review=llm_review,
        midterm=midterm,
    ):
        if event.get("type") == ev.EventType.DONE:
            final_answer = event.get("final_answer", "")
        elif event.get("type") == ev.EventType.ERROR:
            # 出错时也把用户问题记入历史，避免下次重复；答案留空。
            final_answer = ""
        yield event

    # 追加到会话（出错时仅记用户输入）。
    session.append_turn(query, final_answer if final_answer else "")


async def _error_stream(message: str) -> AsyncIterator[dict[str, Any]]:
    """单条 error 事件的流（真正的异步生成器，可直接被 ``async for`` 消费）。"""
    from alphabee.apps.web import events as ev

    yield ev.error(message)
