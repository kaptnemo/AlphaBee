"""AlphaBee MCP 服务。

``pdf_ocr_server``：PDF 财报 OCR MCP 服务（stdio / streamable-http 两种传输），
提供 PDF 上传、OCR 提取 Markdown/文档/JSONL、任务查询与发布到 ``reports/`` 的工具。

``server_manager``：在 agent 创建时自动拉起/回收 MCP 服务子进程的管理器。
"""

from alphabee.mcp.pdf_ocr_server import mcp
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
]
