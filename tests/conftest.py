"""共享 fixture：fake OCR 管线，让 loader/MCP 测试不依赖真实 PaddleOCR-VL 服务。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class FakeOCRResult:
    """模拟 PaddleOCR 的 result 对象（save_to_markdown / save_to_json）。

    默认按页生成不同内容（``<file>_ocr_<N>`` 的 N 决定章节），
    避免所有页内容相同而被页眉页脚去重逻辑误判。
    """

    def __init__(self, stem: str, markdown_text: str | None = None, block_label: str = "paragraph_title") -> None:
        self.stem = stem
        self.markdown_text = markdown_text or self._page_markdown(stem)
        self.block_label = block_label

    @staticmethod
    def _page_markdown(stem: str) -> str:
        import re

        m = re.search(r"_ocr_(\d+)$", stem)
        page = int(m.group(1)) if m else 1
        if page % 3 == 1:
            return f"## 一、主要财务数据\n营业收入 {1000 + page} 万元，同比增长 12.5%。\n"
        if page % 3 == 2:
            return f"## 二、股东信息\n股东名称：示例股东{page}\n"
        return f"## 三、其他重要事项\n其他事项内容（第{page}页）\n"

    def save_to_markdown(self, save_path: str | None = None) -> None:
        path = Path(save_path) / f"{self.stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.markdown_text, encoding="utf-8")

    def save_to_json(self, save_path: str | None = None) -> None:
        path = Path(save_path) / f"{self.stem}_res.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "parsing_res_list": [
                {
                    "block_content": "## 一、主要财务数据",
                    "block_label": self.block_label,
                },
                {
                    "block_content": "营业收入 1000 万元，同比增长 12.5%。",
                    "block_label": "paragraph_body",
                },
            ]
        }
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class FakeOCRPipeline:
    """按输入路径逐个返回 FakeOCRResult。"""

    def __init__(self, markdown_text: str | None = None) -> None:
        self.markdown_text = markdown_text

    def predict(self, paths: list, **kwargs: object) -> list[FakeOCRResult]:
        return [FakeOCRResult(Path(p).stem, markdown_text=self.markdown_text) for p in paths]


@pytest.fixture
def fake_ocr_pipeline(monkeypatch):
    """把 PDFOCRLoader._get_thread_pipeline 替换为假管线，返回构造器函数。"""

    def install(markdown_text: str | None = None):
        from alphabee.loader.pdf_ocr_loader import PDFOCRLoader

        pipeline = FakeOCRPipeline(markdown_text=markdown_text)
        monkeypatch.setattr(PDFOCRLoader, "_get_thread_pipeline", lambda self: pipeline)
        return pipeline

    return install


@pytest.fixture
def pdf_ocr_root(tmp_path, monkeypatch):
    """把 OCR 持久化根目录指向临时目录，返回根路径。"""
    root = tmp_path / "pdf_ocr"
    monkeypatch.setenv("ALPHABEE_PDF_OCR_ROOT", str(root))
    return root


@pytest.fixture
def sample_pdf(tmp_path) -> Path:
    """用 PyMuPDF 生成一个 2 页的测试 PDF（含标题 + 正文）。"""
    import fitz

    pdf_path = tmp_path / "示例公司_2026一季报.pdf"
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"## 一、主要财务数据  第{i + 1}页")
        page.insert_text((72, 100), f"营业收入 {1000 + i} 万元，同比增长 12.5%。")
    page = doc.new_page()
    page.insert_text((72, 72), "## 二、股东信息")
    page.insert_text((72, 100), "股东名称：示例股东")
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path
