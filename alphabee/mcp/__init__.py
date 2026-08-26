"""AlphaBee MCP 服务。

``pdf_ocr_server``：PDF 财报 OCR MCP 服务（stdio / streamable-http 两种传输），
提供 PDF 上传、OCR 提取 Markdown/文档/JSONL、任务查询与发布到 ``reports/`` 的工具，
以及面向长耗时 OCR 的**异步任务三件套**（submit/status/result/cancel）。

``server_manager``：在 agent 创建时自动拉起/回收 MCP 服务子进程的管理器
（支持 ``transport="streamable-http" | "stdio"``）。

``jobs``：通用异步任务框架（JobStore + 三件套工具注册），可扩展到其它长任务域。

注意：``pdf_ocr_server`` 采用**惰性导入**（``mcp`` 属性经 ``__getattr__`` 提供），
避免 ``python -m alphabee.mcp.pdf_ocr_server`` 时包初始化抢先导入子模块
（否则会触发 runpy 的 "found in sys.modules" 警告并在每次 stdio 会话重复执行）。
"""

from typing import Any

from alphabee.mcp.jobs import (
    JobCancelledError,
    JobStatus,
    JobStore,
    TaskStatus,
    register_job_tools,
)
from alphabee.mcp.server_manager import (
    PdfOcrMCPServerManager,
    get_active_pdf_ocr_servers,
    stop_all_pdf_ocr_servers,
)

__all__ = [
    "mcp",
    "PdfOcrMCPServerManager",
    "get_active_pdf_ocr_servers",
    "stop_all_pdf_ocr_servers",
    "JobStore",
    "JobStatus",
    "TaskStatus",
    "JobCancelledError",
    "register_job_tools",
]


def __getattr__(name: str) -> Any:
    """惰性导出 ``mcp``（FastMCP 实例），避免包导入时加载整个 OCR 服务模块。"""
    if name == "mcp":
        from alphabee.mcp.pdf_ocr_server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
