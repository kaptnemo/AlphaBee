"""会话管理：``session_id`` ↔ 多轮对话 history。

history 元素与 ``cli.chat.run_chat_session`` 保持一致：``HumanMessage`` / ``AIMessage``。
内存实现（进程内 dict），重启后清空；适合单机开发/演示。若要持久化可替换为
``langgraph`` 的 checkpoint store 或 Redis，接口保持不变。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage


@dataclass
class ChatSession:
    session_id: str
    history: list[Any] = field(default_factory=list)
    turn: int = 1

    def append_turn(self, query: str, answer: str) -> None:
        self.history.append(HumanMessage(content=query))
        if answer:
            self.history.append(AIMessage(content=answer))
        self.turn += 1


class SessionStore:
    """线程安全的内存会话存储。"""

    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str | None = None) -> ChatSession:
        sid = session_id or uuid.uuid4().hex
        with self._lock:
            session = ChatSession(session_id=sid)
            self._sessions[sid] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def get_or_create(self, session_id: str | None) -> ChatSession:
        if session_id:
            existing = self.get(session_id)
            if existing is not None:
                return existing
        return self.create(session_id)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"session_id": s.session_id, "turn": s.turn, "history_messages": len(s.history)}
                for s in self._sessions.values()
            ]


# 模块级单例，供 app.py 注入路由。
session_store = SessionStore()
