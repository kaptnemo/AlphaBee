"""AlphaBee Web 包（ENGINEERING_ROADMAP 后续阶段的 Web 界面后端）。

与 ``alphabee.apps.cli`` 并行：CLI 把 ``alphabee_agent.astream`` 的事件流 ``print`` 成彩色文本，
Web 则把同一事件流序列化成 SSE 推给浏览器。两者互不依赖、互不改动。

- ``events``    事件类型常量 + 事件构造辅助（前后端契约）
- ``stream``    核心流式消费者：``astream`` → ``AsyncIterator[dict]``（纯数据，不做渲染）
- ``sessions``  会话管理：``session_id`` ↔ 多轮 history
- ``sse``       SSE 响应封装
- ``app``       FastAPI 应用工厂（路由 + CORS）
- ``server``    uvicorn 启动入口

后端基于 FastAPI + sse-starlette（FastAPI 底层是 Starlette，SSE 由 sse-starlette 提供）。
"""

# 必须在此处（任何 alphabee 子模块被 import 之前）加载 .env。
# 原因：Python 执行 `import alphabee.apps.web.server` 时会先执行本包 `__init__.py`，
# 而下面 `from alphabee.apps.web.app import create_app` 会级联 import alphabee.config，
# 触发 `settings = get_settings()` 立即求值。若此时 .env 未加载，
# config.yaml 里的 `${LLM_API_KEY}` 会被替换成字面量 "None" 并被固化。
from dotenv import load_dotenv

load_dotenv()  # 必须在加载app之前导入env

from alphabee.apps.web.app import create_app

__all__ = ["create_app"]
