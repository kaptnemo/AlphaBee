"""FastAPI 应用工厂：路由挂载 + CORS。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from alphabee.apps.web.api.chat import chat_router
from alphabee.apps.web.api.health import health_router
from alphabee.apps.web.api.sessions import sessions_router

# 开发期允许 Next.js（默认 http://localhost:3000）跨域访问；生产可收紧为具体域名。
_ALLOWED_ORIGINS = ["*"]


def create_app() -> FastAPI:
    app = FastAPI(title="AlphaBee Web", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(sessions_router)

    return app


app = create_app()
