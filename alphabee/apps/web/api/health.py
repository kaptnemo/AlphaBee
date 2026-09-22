"""健康检查路由。"""

from __future__ import annotations

from fastapi import APIRouter

health_router = APIRouter(tags=["health"])


@health_router.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "alphabee-web"}
