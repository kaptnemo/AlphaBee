"""SSE 响应封装。

基于 ``sse_starlette.sse.EventSourceResponse``：每个事件 dict 会被序列化成一条
``data: {json}`` 消息。前端用 fetch ReadableStream 解析 ``data:`` 行。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from sse_starlette.sse import EventSourceResponse


def sse_response(event_stream: AsyncIterator[dict[str, Any]]) -> EventSourceResponse:
    """把事件 dict 流包装成 SSE 响应。

    - ``ping=15``：每 15s 发一个心跳注释，防止代理/浏览器超时断连；
    - 每条事件作为 ``data: <json>`` 单行发送（EventSourceResponse 会把 dict 序列化）。
    """

    async def _encode() -> AsyncIterator[dict[str, str]]:
        async for event in event_stream:
            yield {"data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(_encode(), ping=15)
