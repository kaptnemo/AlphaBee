"""PdfOcrMCPServerManager：自启动/自停止 MCP 服务子进程测试（streamable-http / stdio）。"""

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


def _expected_tools() -> set[str]:
    return {
        "ocr_pdf_to_markdown",
        "ocr_pdf_to_documents",
        "ocr_pdf_to_jsonl",
        "upload_pdf",
        "list_uploaded_pdfs",
        "list_ocr_tasks",
        "get_ocr_task",
        "publish_report_sections",
        # 异步任务工具
        "submit_pdf_ocr",
        "get_pdf_ocr_status",
        "wait_pdf_ocr_task",
        "get_pdf_ocr_result",
        "list_pdf_ocr_tasks",
        "cancel_pdf_ocr_task",
    }


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
        assert names == _expected_tools()
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


# ── stdio 传输 ─────────────────────────────────────────────────────────────


def test_manager_stdio_invalid_transport():
    with pytest.raises(ValueError, match="transport"):
        PdfOcrMCPServerManager(transport="http")


def test_manager_start_transport_override(tmp_path, monkeypatch):
    """start(transport=...) 可覆盖构造时的传输模式（仅首次启动前生效）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPHABEE_PDF_OCR_ROOT", str(tmp_path / "pdf_ocr"))

    manager = PdfOcrMCPServerManager(transport="streamable-http", timeout=30)
    manager.start(transport="stdio")  # 覆盖为 stdio
    try:
        assert manager.transport == "stdio"
        assert manager.url is None
        assert manager.servers_config["pdf_ocr"]["transport"] == "stdio"
        # 已启动后再次指定 transport 被忽略
        manager.start(transport="streamable-http")
        assert manager.transport == "stdio"
    finally:
        manager.stop()

    with pytest.raises(ValueError, match="transport"):
        manager.start(transport="bogus")


def test_manager_stdio_servers_config(tmp_path, monkeypatch):
    """stdio 模式：servers_config 生成 MultiServerMCPClient 可用的 stdio 连接配置。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPHABEE_PDF_OCR_ROOT", str(tmp_path / "pdf_ocr"))

    manager = PdfOcrMCPServerManager(transport="stdio", timeout=30)
    try:
        manager.start()  # 启动冒烟探测
        assert manager.url is None  # stdio 无 HTTP URL
        assert manager in get_active_pdf_ocr_servers()

        config = manager.servers_config["pdf_ocr"]
        assert config["transport"] == "stdio"
        assert config["command"] == manager.python_executable
        assert config["args"][:2] == ["-m", "alphabee.mcp.pdf_ocr_server"]
        assert "--transport" in config["args"]
        assert "stdio" in config["args"]
        assert isinstance(config["env"], dict)
        assert "PYTHONPATH" in config["env"]
    finally:
        manager.stop()
    assert manager not in get_active_pdf_ocr_servers()


def test_manager_stdio_end_to_end_tool_call(tmp_path, monkeypatch):
    """stdio 端到端：通过 MultiServerMCPClient 走 stdio 协议列出并调用工具。

    验证：stdio 子进程协议通道不被任何 stdout 输出污染。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPHABEE_PDF_OCR_ROOT", str(tmp_path / "pdf_ocr"))

    manager = PdfOcrMCPServerManager(transport="stdio", timeout=60)
    try:
        manager.start()

        async def _run():
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient(manager.servers_config)
            tools = await client.get_tools()
            names = {t.name for t in tools}
            assert names == _expected_tools()

            # 实际调用一个工具（stdio 会为本次调用拉起新的服务子进程）
            tool = next(t for t in tools if t.name == "list_ocr_tasks")
            result = await tool.ainvoke({"limit": 5})
            return result

        result = _run_async(_run())
        # MCP 工具返回 content 块列表，文本内容应包含 JSON 结果
        text = "".join(
            block.get("text", "") for block in result if isinstance(block, dict) and block.get("type") == "text"
        )
        assert '"count"' in text and '"tasks"' in text
    finally:
        manager.stop()
