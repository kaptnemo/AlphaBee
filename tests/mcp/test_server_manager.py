"""PdfOcrMCPServerManager：自启动/自停止 MCP 服务子进程测试。"""

from __future__ import annotations

import os

import pytest

from alphabee.mcp.server_manager import (
    PdfOcrMCPServerManager,
    get_active_pdf_ocr_servers,
    stop_all_pdf_ocr_servers,
)

pytestmark = pytest.mark.integration


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)


async def _fetch_tool_names(url: str) -> set[str]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({"pdf_ocr": {"transport": "streamable-http", "url": url}})
    tools = await client.get_tools()
    return {t.name for t in tools}


def test_manager_start_connect_stop(tmp_path, monkeypatch):
    # 子进程导入 alphabee 需要可写 HOME（tushare 等包会在 HOME 写缓存）
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPHABEE_PDF_OCR_ROOT", str(tmp_path / "pdf_ocr"))

    manager = PdfOcrMCPServerManager(port=0, timeout=90)
    try:
        manager.start()
        assert manager.url is not None
        assert manager.process is not None
        assert manager in get_active_pdf_ocr_servers()

        names = _run_async(_fetch_tool_names(manager.url))
        expected = {
            "ocr_pdf_to_markdown",
            "ocr_pdf_to_documents",
            "ocr_pdf_to_jsonl",
            "upload_pdf",
            "list_uploaded_pdfs",
            "list_ocr_tasks",
            "get_ocr_task",
            "publish_report_sections",
        }
        assert names == expected
    finally:
        pid = manager.process.pid if manager.process else None
        manager.stop()

    assert manager not in get_active_pdf_ocr_servers()
    if pid is not None:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_manager_stop_all(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPHABEE_PDF_OCR_ROOT", str(tmp_path / "pdf_ocr"))

    m1 = PdfOcrMCPServerManager(port=0, timeout=90)
    m2 = PdfOcrMCPServerManager(port=0, timeout=90)
    try:
        m1.start()
        m2.start()
        assert len(get_active_pdf_ocr_servers()) == 2
    finally:
        stop_all_pdf_ocr_servers()
    assert get_active_pdf_ocr_servers() == []


def test_manager_restart_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPHABEE_PDF_OCR_ROOT", str(tmp_path / "pdf_ocr"))

    manager = PdfOcrMCPServerManager(port=0, timeout=90)
    try:
        manager.start()
        url1 = manager.url
        manager.start()  # 幂等：已启动时不再重复拉起
        assert manager.url == url1
    finally:
        manager.stop()
