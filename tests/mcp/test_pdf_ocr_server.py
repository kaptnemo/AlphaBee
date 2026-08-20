"""PDF OCR MCP 服务工具测试（fake OCR 管线，不依赖真实服务）。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from alphabee.mcp import pdf_ocr_server as pos


@pytest.fixture(autouse=True)
def _clean_reports_side_effects(tmp_path):
    """publish 类测试统一落到 tmp 目录，避免污染仓库 reports/。"""
    return tmp_path


def test_upload_and_list_pdfs(pdf_ocr_root):
    pdf_bytes = b"%PDF-1.4 fake pdf content"
    result = pos.upload_pdf(
        pdf_base64=base64.b64encode(pdf_bytes).decode(),
        file_name="测试财报.pdf",
    )
    assert result.file_name == "测试财报.pdf"
    assert result.pdf_path.endswith("测试财报.pdf")
    # 上传文件持久化在 uploads/<file_id>/
    assert pos._normalize_pdf_path(result.pdf_path).is_file()

    listed = pos.list_uploaded_pdfs(file_name="测试")
    assert listed.count == 1
    assert listed.files[0].file_id == result.file_id


def test_upload_rejects_non_pdf():
    with pytest.raises(ValueError, match="valid PDF"):
        pos.upload_pdf(pdf_base64=base64.b64encode(b"not a pdf").decode(), file_name="x.pdf")


def test_ocr_pdf_to_markdown_persists_files(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    fake_ocr_pipeline()
    result = pos.ocr_pdf_to_markdown(pdf_path=str(sample_pdf), task_id="mcp-001")

    assert result.metadata.task_id == "mcp-001"
    assert result.markdown_path.endswith("示例公司_2026一季报.cleaned.md")
    assert result.page_count == 3
    assert result.char_count > 0
    # 默认不内联正文
    assert result.markdown is None
    # 文件真实落盘
    from pathlib import Path

    assert Path(result.markdown_path).is_file()

    # manifest 可查询
    info = pos.get_ocr_task("mcp-001")
    assert info.manifest["status"] == "completed"
    assert info.manifest["markdown_path"] == result.markdown_path


def test_ocr_pdf_to_markdown_include_content(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    fake_ocr_pipeline()
    result = pos.ocr_pdf_to_markdown(
        pdf_path=str(sample_pdf),
        task_id="mcp-002",
        include_content=True,
    )
    assert result.markdown is not None
    assert "营业收入" in result.markdown


def test_ocr_via_uploaded_file_id(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    fake_ocr_pipeline()
    uploaded = pos.upload_pdf(
        pdf_base64=base64.b64encode(sample_pdf.read_bytes()).decode(),
        file_name=sample_pdf.name,
    )
    result = pos.ocr_pdf_to_markdown(file_id=uploaded.file_id, task_id="mcp-003")
    assert result.metadata.file_id == uploaded.file_id
    assert result.markdown_path.endswith(".cleaned.md")


def test_ocr_pdf_to_documents_writes_jsonl(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    fake_ocr_pipeline()
    result = pos.ocr_pdf_to_documents(pdf_path=str(sample_pdf), task_id="mcp-004", preview_size=5)

    assert result.document_count > 0
    assert result.documents_path is not None
    assert len(result.documents) <= 5
    from pathlib import Path

    assert Path(result.documents_path).is_file()


def test_ocr_pdf_to_jsonl(sample_pdf, pdf_ocr_root, fake_ocr_pipeline, tmp_path):
    fake_ocr_pipeline()
    out = tmp_path / "docs.jsonl"
    result = pos.ocr_pdf_to_jsonl(
        output_path=str(out),
        pdf_path=str(sample_pdf),
        task_id="mcp-005",
    )
    assert result.output_path == str(out.resolve())
    assert out.is_file()
    assert result.document_count > 0


def test_list_ocr_tasks(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    fake_ocr_pipeline()
    pos.ocr_pdf_to_markdown(pdf_path=str(sample_pdf), task_id="mcp-006")
    pos.ocr_pdf_to_markdown(pdf_path=str(sample_pdf), task_id="mcp-007")

    tasks = pos.list_ocr_tasks()
    assert tasks.count >= 2
    ids = [t["task_id"] for t in tasks.tasks]
    assert "mcp-007" in ids and "mcp-006" in ids
    # 按创建时间倒序
    assert ids[0] == "mcp-007"

    completed = pos.list_ocr_tasks(status="completed")
    assert all(t["status"] == "completed" for t in completed.tasks)


def test_publish_report_sections_writes_folder(sample_pdf, pdf_ocr_root, fake_ocr_pipeline, tmp_path):
    fake_ocr_pipeline()
    md_result = pos.ocr_pdf_to_markdown(pdf_path=str(sample_pdf), task_id="mcp-008")

    result = pos.publish_report_sections(
        markdown_path=md_result.markdown_path,
        report_name="示例公司：2026年一季报",
        save_dir=str(tmp_path),
    )
    assert result.report_name == "示例公司：2026年一季报"
    report_dir = tmp_path / "示例公司：2026年一季报"
    assert result.report_dir == str(report_dir)
    assert report_dir.is_dir()
    assert result.section_count >= 1
    assert result.file_count >= 1
    # 章节目录结构（与 report_parser 约定一致：父章节为目录、叶子章节为 .md 文件；
    # 目录名保留中文编号前缀，如 一、主要财务数据）
    assert (report_dir / "一、主要财务数据").is_dir()
    assert (report_dir / "一、主要财务数据" / "二、股东信息.md").exists()
    # 报告元数据
    manifest = json.loads((report_dir / ".report_manifest.json").read_text(encoding="utf-8"))
    assert manifest["report_name"] == "示例公司：2026年一季报"
    assert manifest["section_count"] >= 1
    # 完整全文副本保留
    assert (report_dir / "示例公司：2026年一季报.md").exists()


def test_publish_report_sections_overwrite_guard(sample_pdf, pdf_ocr_root, fake_ocr_pipeline, tmp_path):
    fake_ocr_pipeline()
    md_result = pos.ocr_pdf_to_markdown(pdf_path=str(sample_pdf), task_id="mcp-009")
    pos.publish_report_sections(
        markdown_path=md_result.markdown_path,
        report_name="重复报告",
        save_dir=str(tmp_path),
    )
    with pytest.raises(FileExistsError):
        pos.publish_report_sections(
            markdown_path=md_result.markdown_path,
            report_name="重复报告",
            save_dir=str(tmp_path),
            overwrite=False,
        )


# ── 异步任务三件套（submit / status / result / cancel） ────────────────────


def _wait_job_status(job_store, task_id: str, want: set[str], timeout: float = 60.0) -> dict:
    """轮询 job 状态直到进入 want 集合（worker 子进程拉起需要数秒）。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = job_store.status("pdf_ocr", task_id)
        assert st is not None
        if st["status"] in want:
            return st
        time.sleep(0.5)
    raise TimeoutError(f"task {task_id} did not reach {want}; last={st}")


def _call_tool(tool, *args):
    """调用 FastMCP Tool（fn 可能是同步包装或协程函数，统一处理）。"""
    import asyncio
    import inspect

    result = tool.fn(*args)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_async_submit_status_result(sample_pdf, pdf_ocr_root, monkeypatch, tmp_path):
    """异步三件套成功路径：submit → 轮询 succeeded → get result。

    worker 子进程以 ALPHABEE_PDF_OCR_FAKE=1 运行（不调用真实 OCR）。
    """
    monkeypatch.setenv("ALPHABEE_PDF_OCR_FAKE", "1")
    monkeypatch.setenv("HOME", str(tmp_path))

    submitted = pos.submit_pdf_ocr(pdf_path=str(sample_pdf), task_id="job-001")
    assert submitted.task_id == "job-001"
    assert submitted.status == "queued"
    assert submitted.markdown_path.endswith("示例公司_2026一季报.cleaned.md")

    store = pos._get_ocr_job_store()
    st = _wait_job_status(store, "job-001", want={"succeeded", "failed"})
    assert st["status"] == "succeeded", st

    # 直接调用生成的工具（FastMCP Tool.fn 同步包装）
    tools = {t.name: t for t in pos.mcp._tool_manager.list_tools()}
    res = _call_tool(tools["get_pdf_ocr_result"], "job-001")
    assert res["markdown_path"].endswith("示例公司_2026一季报.cleaned.md")
    assert Path(res["markdown_path"]).is_file()
    assert res["page_count"] >= 1

    # job.json 与 OCR manifest 都在任务工作区
    workspace = pos.get_task_workspace("job-001")
    assert (workspace / "job.json").is_file()
    assert (workspace / "manifest.json").is_file()


def test_async_submit_cancel(sample_pdf, pdf_ocr_root, monkeypatch, tmp_path):
    """异步取消：submit 后立即 cancel，任务最终停在 cancelled。"""
    monkeypatch.setenv("ALPHABEE_PDF_OCR_FAKE", "1")
    monkeypatch.setenv("HOME", str(tmp_path))

    submitted = pos.submit_pdf_ocr(pdf_path=str(sample_pdf), task_id="job-002")
    store = pos._get_ocr_job_store()

    tools = {t.name: t for t in pos.mcp._tool_manager.list_tools()}
    cancelled = _call_tool(tools["cancel_pdf_ocr_task"], submitted.task_id)
    assert cancelled["cancelled"] is True

    st = _wait_job_status(store, "job-002", want={"cancelled", "succeeded"})
    assert st["status"] == "cancelled", st


def test_async_status_missing_task():
    tools = {t.name: t for t in pos.mcp._tool_manager.list_tools()}
    with pytest.raises(FileNotFoundError):
        _call_tool(tools["get_pdf_ocr_status"], "no-such-task")


def test_validate_pdf_sources_rules():
    with pytest.raises(ValueError, match="exactly one"):
        pos._validate_pdf_sources(None, None, None, None)
    with pytest.raises(ValueError, match="Only one"):
        pos._validate_pdf_sources("a.pdf", "base64str", None, None)
    assert pos._validate_pdf_sources("a.pdf", None, None, None) == ("path", "a.pdf")
