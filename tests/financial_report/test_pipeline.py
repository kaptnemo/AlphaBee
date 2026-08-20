"""财报处理全链路（下载 → OCR → 解析 → 问答）测试。

OCR 与问答阶段用 mock 隔离（不依赖真实 OCR 服务 / LLM），
下载阶段 mock 东方财富工具，覆盖编排与产物路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alphabee.financial_report import pipeline
from alphabee.financial_report.report_parser import write_markdown_report_folder

SAMPLE_MARKDOWN = """# 示例公司：2026年一季报
## 一、主要财务数据
营业收入 1000 万元，同比增长 12.5%。
## 二、股东信息
股东名称：示例股东
"""


# ── 解析成文件夹结构（report_parser.py） ──────────────────────────────────


def test_write_markdown_report_folder_structure(tmp_path):
    report_dir = write_markdown_report_folder(
        SAMPLE_MARKDOWN,
        "示例公司：2026年一季报",
        page_count=4,
        save_dir=tmp_path,
    )
    assert report_dir == tmp_path / "示例公司：2026年一季报"
    # 章节目录 + 叶子文件（保留中文编号前缀，与既有 reports/ 约定一致）
    assert (report_dir / "一、主要财务数据").is_dir()
    assert (report_dir / "一、主要财务数据" / "二、股东信息.md").exists()
    # 完整全文副本 + 元数据
    assert (report_dir / "示例公司：2026年一季报.md").exists()
    manifest = report_dir / ".report_manifest.json"
    assert manifest.is_file()
    import json

    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["report_name"] == "示例公司：2026年一季报"
    assert meta["section_count"] >= 1


def test_write_markdown_report_folder_overwrite_guard(tmp_path):
    write_markdown_report_folder(SAMPLE_MARKDOWN, "重复报告", save_dir=tmp_path)
    with pytest.raises(FileExistsError):
        write_markdown_report_folder(SAMPLE_MARKDOWN, "重复报告", save_dir=tmp_path, overwrite=False)
    # overwrite=True 时覆盖重建
    report_dir = write_markdown_report_folder(SAMPLE_MARKDOWN, "重复报告", save_dir=tmp_path, overwrite=True)
    assert report_dir.is_dir()


def test_sanitize_report_name_rejects_path_traversal(tmp_path):
    from alphabee.financial_report.report_parser import _sanitize_report_name

    assert _sanitize_report_name("宁德时代：2026年半年度报告.md") == "宁德时代：2026年半年度报告"
    assert _sanitize_report_name("a/b/c") == "a_b_c"
    assert _sanitize_report_name("x.cleaned.md") == "x"
    with pytest.raises(ValueError):
        _sanitize_report_name("..")


# ── 下载 ───────────────────────────────────────────────────────────────────


def test_download_report_pdf_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one"):
        pipeline.download_report_pdf()
    with pytest.raises(ValueError, match="exactly one"):
        pipeline.download_report_pdf(info_code="A", pdf_url="https://x/y.pdf")


def test_download_report_pdf_by_info_code(tmp_path, monkeypatch):
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    def fake_download(info_code, save_dir=None, filename=None, timeout=30):
        dest = Path(save_dir) / (filename or f"{info_code}.pdf")
        dest.write_bytes(fake_pdf.read_bytes())
        return {"downloaded": True, "path": str(dest), "size_bytes": dest.stat().st_size}

    monkeypatch.setattr(pipeline, "download_eastmoney_report_pdf_by_info_code", fake_download)
    path = pipeline.download_report_pdf(info_code="AP123", dest_dir=tmp_path / "out", file_name="r.pdf")
    assert path.name == "r.pdf"
    assert path.read_bytes() == b"%PDF-1.4 fake"


def test_download_report_pdf_from_url(tmp_path, monkeypatch):
    class FakeResponse:
        content = b"%PDF-1.4 from url"

        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeRequests:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, stream=True, timeout=60):
            assert url.startswith("http")
            return FakeResponse()

    monkeypatch.setattr(pipeline.requests, "get", FakeRequests().get)
    path = pipeline.download_report_pdf(pdf_url="https://example.com/报告.pdf", dest_dir=tmp_path / "out")
    assert path.name == "报告.pdf"
    assert path.read_bytes() == b"%PDF-1.4 from url"


# ── 全链路 ─────────────────────────────────────────────────────────────────


async def test_run_report_pipeline_full_chain(sample_pdf, tmp_path, monkeypatch):
    """本地 PDF → OCR（mock）→ 解析 → 问答（mock）全链路。"""
    md_path = tmp_path / "示例公司_2026一季报.cleaned.md"
    md_path.write_text(SAMPLE_MARKDOWN, encoding="utf-8")

    def fake_ocr(pdf_path, *, task_id=None, ocr_server_url=None, keep_pages=True, include_content=False):
        assert Path(pdf_path).is_file()
        return {
            "markdown_path": str(md_path),
            "metadata": {"task_id": "pipeline-test-1"},
            "page_count": 4,
        }

    async def fake_answer(report_dir, question, *, max_steps=40):
        assert Path(report_dir).is_dir()
        assert "营收" in question
        return "营业收入 1000 万元，同比增长 12.5%。"

    monkeypatch.setattr(pipeline, "ocr_markdown", fake_ocr)
    monkeypatch.setattr(pipeline, "answer_report_question", fake_answer)

    result = await pipeline.run_report_pipeline(
        pdf_path=str(sample_pdf),
        report_name="示例公司：2026年一季报",
        question="营收是多少？",
        save_dir=tmp_path / "reports",
        verbose=False,
    )

    assert result["task_id"] == "pipeline-test-1"
    assert result["markdown_path"] == str(md_path)
    assert result["page_count"] == 4
    report_dir = Path(result["report_dir"])
    assert report_dir == tmp_path / "reports" / "示例公司：2026年一季报"
    assert (report_dir / "一、主要财务数据").is_dir()
    assert (report_dir / "示例公司：2026年一季报.md").exists()
    assert result["answer"] == "营业收入 1000 万元，同比增长 12.5%。"
    assert len(result["steps"]) == 3  # download / ocr / parse（question 单独步骤不入 steps）


async def test_run_report_pipeline_without_question(sample_pdf, tmp_path, monkeypatch):
    """不传 question 时只做下载 → OCR → 解析，不进入问答。"""
    md_path = tmp_path / "x.cleaned.md"
    md_path.write_text(SAMPLE_MARKDOWN, encoding="utf-8")

    def fake_ocr(pdf_path, **kwargs):
        return {"markdown_path": str(md_path), "metadata": {"task_id": "t2"}, "page_count": 4}

    monkeypatch.setattr(pipeline, "ocr_markdown", fake_ocr)
    result = await pipeline.run_report_pipeline(
        pdf_path=str(sample_pdf),
        report_name="示例报告",
        save_dir=tmp_path / "reports",
        verbose=False,
    )
    assert "answer" not in result
    assert Path(result["report_dir"]).is_dir()


async def test_answer_report_question_extracts_final_text(monkeypatch):
    """answer_report_question 提取最终 AIMessage 文本；无答案时返回原因码。"""
    from langchain_core.messages import AIMessage, HumanMessage

    class FakeAgent:
        def __init__(self, messages):
            self.messages = messages

        async def ainvoke(self, payload, config=None):
            return {"messages": self.messages}

    # create_report_fetch_agent 是同步工厂，mock 也必须是同步函数
    def make_fake_agent(report_dir, **kwargs):
        return FakeAgent(
            [
                HumanMessage(content="q"),
                AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "1"}]),
                AIMessage(content="最终答案：净利润 200 亿元"),
            ]
        )

    monkeypatch.setattr(pipeline, "create_report_fetch_agent", make_fake_agent)
    report_dir = Path(__import__("tempfile").mkdtemp())
    answer = await pipeline.answer_report_question(report_dir, "净利润？")
    assert answer == "最终答案：净利润 200 亿元"


async def test_answer_report_question_no_answer(monkeypatch, tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage

    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [HumanMessage(content="q"), AIMessage(content="")]}

    def make_fake_agent(report_dir, **kwargs):
        return FakeAgent()

    monkeypatch.setattr(pipeline, "create_report_fetch_agent", make_fake_agent)
    answer = await pipeline.answer_report_question(tmp_path, "问题")
    assert answer.startswith("AGENT_NO_ANSWER")


async def test_answer_report_question_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        await pipeline.answer_report_question(tmp_path / "不存在", "问题")
