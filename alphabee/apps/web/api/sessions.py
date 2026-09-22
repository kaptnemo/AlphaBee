"""会话相关路由：列表 / 清除。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from alphabee.apps.web.sessions import session_store

sessions_router = APIRouter(tags=["sessions"])


@sessions_router.get("/api/sessions")
async def list_sessions() -> dict:
    return {"sessions": session_store.list_sessions()}


@sessions_router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    if not session_id:
        raise HTTPException(status_code=400, detail="missing session_id")
    session_store.clear(session_id)
    return {"status": "ok", "session_id": session_id}
