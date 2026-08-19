"""PDFOCRLoader 的持久化文件保存与后处理逻辑测试（不依赖真实 OCR 服务）。"""

from __future__ import annotations

import json

from alphabee.loader.pdf_ocr_loader import PDFOCRLoader


def test_load_full_text_writes_persistent_workspace(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    """验证工作区布局：pages/ + <stem>.cleaned.md + manifest.json，文件名稳定。"""
    fake_ocr_pipeline()

    loader = PDFOCRLoader(task_id="task-001", pdf_path=sample_pdf, max_workers=2, batch_size=1)
    markdown = loader.load_full_text()

    assert loader.workspace == pdf_ocr_root / "tasks" / "task-001"
    assert loader.markdown_path.exists()
    assert loader.markdown_path.name == "示例公司_2026一季报.cleaned.md"
    assert loader.markdown_path.read_text(encoding="utf-8") == markdown
    assert len(markdown) > 0

    # pages/ 保留每页 OCR 原始结果（默认 keep_pages=True）
    page_mds = list((loader.workspace / "pages").glob("*_ocr_*.md"))
    page_jsons = list((loader.workspace / "pages").glob("*_ocr_*_res.json"))
    assert len(page_mds) == 3  # sample_pdf 共 3 页
    assert len(page_jsons) == 3
    assert (loader.workspace / "pages" / "_raw_concatenated.md").exists()

    # 页面渲染图（一次性临时文件）应被清理
    assert not (loader.workspace / "images").exists()

    # manifest 记录任务元数据与产物路径
    manifest = json.loads(loader.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["task_id"] == "task-001"
    assert manifest["page_count"] == 3
    assert manifest["markdown_path"] == str(loader.markdown_path)
    assert manifest["char_count"] == len(markdown)


def test_keep_pages_false_cleans_page_outputs(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    """keep_pages=False 时清理 pages/，但保留最终 Markdown 与 manifest。"""
    fake_ocr_pipeline()

    loader = PDFOCRLoader(
        task_id="task-002",
        pdf_path=sample_pdf,
        max_workers=2,
        batch_size=1,
        keep_pages=False,
    )
    loader.load_full_text()

    assert not (loader.workspace / "pages").exists()
    assert loader.markdown_path.exists()
    assert loader.manifest_path.exists()
    manifest = json.loads(loader.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


def test_single_page_content_not_dropped(pdf_ocr_root, fake_ocr_pipeline, tmp_path):
    """单页 PDF 的内容不应被页眉页脚检测误删（count>=2 守卫）。"""
    import fitz

    pdf_path = tmp_path / "single.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "## 一、主要财务数据")
    page.insert_text((72, 100), "营业收入 1000 万元，同比增长 12.5%。")
    doc.save(str(pdf_path))
    doc.close()

    fake_ocr_pipeline()
    loader = PDFOCRLoader(task_id="task-003", pdf_path=pdf_path, max_workers=1, batch_size=1)
    markdown = loader.load_full_text()

    assert "营业收入" in markdown


def test_source_pdf_bytes_copy_into_workspace(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    """base64/URL 类输入：原始 PDF bytes 副本落到工作区 pdf/。"""
    fake_ocr_pipeline()

    loader = PDFOCRLoader(task_id="task-004", pdf_path=sample_pdf, max_workers=1, batch_size=1)
    loader.load_full_text(source_pdf_bytes=sample_pdf.read_bytes(), source_type="base64")

    copied = loader.workspace / "pdf" / sample_pdf.name
    assert copied.exists()
    assert copied.read_bytes() == sample_pdf.read_bytes()
    manifest = json.loads(loader.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_type"] == "base64"
    assert manifest["source_pdf_path"] == str(copied)


def test_load_documents_and_jsonl(sample_pdf, pdf_ocr_root, fake_ocr_pipeline):
    """load() 返回文档块并写出 documents.jsonl。"""
    fake_ocr_pipeline()

    loader = PDFOCRLoader(task_id="task-005", pdf_path=sample_pdf, max_workers=1, batch_size=1)
    documents = loader.load()

    assert len(documents) > 0
    assert loader.jsonl_path.exists()
    first_line = json.loads(loader.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert "page_content" in first_line
    assert "metadata" in first_line
    manifest = json.loads(loader.manifest_path.read_text(encoding="utf-8"))
    assert manifest["document_count"] == len(documents)


def test_loader_writes_nothing_to_stdout(sample_pdf, pdf_ocr_root, fake_ocr_pipeline, capfd):
    """stdio 协议安全：loader 的所有诊断输出都走 stderr，stdout 必须保持干净。

    这是 stdio 模式 MCP 服务能正常工作的前提（stdout 是 MCP 协议通道）。
    """
    fake_ocr_pipeline()

    loader = PDFOCRLoader(task_id="task-006", pdf_path=sample_pdf, max_workers=2, batch_size=1)
    loader.load()

    captured = capfd.readouterr()
    assert captured.out == ""  # stdout 不得有任何输出
    assert "开始加载PDF文件" in captured.err  # 诊断信息进 stderr


def test_relevel_markdown_headers():
    """标题重建：去伪（复选框/句号结尾）+ 编号分级 + 文档标题整体降一级。"""
    md_text = "\n".join(
        [
            "## 一、主要财务数据",
            "正文内容",
            "## 一、主要财务数据",  # 页眉重复
            "## （一）营业收入",
            "营收数据",
            "## 二、股东信息",
            "股东名称",
            "## √ 这是复选框误判标题",
            "## 具体内容如下：",  # 引导句误判标题
            "内容",
        ]
    )
    layout_map = {}  # 无 OCR 版面信息，走纯编号路径

    result = PDFOCRLoader._relevel_markdown_headers(md_text, layout_map, doc_title="示例公司：2026年一季报")

    lines = result.splitlines()
    assert lines[0] == "# 示例公司：2026年一季报"
    # 文档标题降一级后：一、→ level 2；二、→ level 2；（一）→ level 3
    assert lines[1] == "## 一、主要财务数据"
    assert lines[3] == "## 一、主要财务数据"
    assert "### （一）营业收入" in result
    assert lines[6] == "## 二、股东信息"
    # 误识别标题被去伪为普通文本
    assert "## √ 这是复选框误判标题" not in result
    assert "√ 这是复选框误判标题" in result
    assert "## 具体内容如下：" not in result
    assert "具体内容如下：" in result
