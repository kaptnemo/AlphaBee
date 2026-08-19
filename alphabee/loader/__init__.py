"""PDF OCR 加载器：把扫描版 PDF（财报/研报）转成清洗后的 Markdown 与文档块。

由 ``alphabee.mcp.pdf_ocr_server`` 的 MCP 工具调用；也可以直接作为库使用：:

    from alphabee.loader.pdf_ocr_loader import PDFOCRLoader

    loader = PDFOCRLoader(task_id="t-001", pdf_path="/path/to/report.pdf")
    markdown = loader.load_full_text()   # 同时把最终文件落到 outputs/pdf_ocr/<task_id>/
"""

from alphabee.loader.markdown_table_handler import (
    extract_tables_from_text,
    merge_tables,
)
from alphabee.loader.pdf_ocr_loader import DEFAULT_OCR_SERVER_URL, PDFOCRLoader

__all__ = [
    "DEFAULT_OCR_SERVER_URL",
    "PDFOCRLoader",
    "extract_tables_from_text",
    "merge_tables",
]
