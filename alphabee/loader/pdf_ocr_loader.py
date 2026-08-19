"""PDF OCR 加载器：把扫描版 PDF 财报/研报转成清洗后的 Markdown 与文档块。

从 BidGenius 的 ``pdf_ocr_loader.py`` 移植，并针对「中间文件与最终文件的保存」做了重构：

文件保存优化（相对旧实现）
--------------------------
旧实现把所有产物写进 ``/tmp/task_<id>/``（易失、无结构），每页的 md/json 用完即删，
最终文件名带随机 task_id（``<stem>_<task_id>_final_output.md``），上传文件散落在
``/tmp/bidgenius_pdf_uploads/``，调用方拿不到稳定的结果路径。

新实现把每个任务收敛到一个**持久化工作区**（默认 ``<PROJECT_ROOT>/outputs/pdf_ocr/tasks/<task_id>/``，
可通过环境变量 ``ALPHABEE_PDF_OCR_ROOT`` 覆盖）：

::

    outputs/pdf_ocr/tasks/<task_id>/
    ├── pdf/                     # 原始 PDF 副本（本地路径输入时记录路径，不复制）
    │   └── <file_name>.pdf
    ├── images/                  # 页面渲染图（OCR 完成后自动清理，属真正的临时文件）
    ├── pages/                   # 每页 OCR 原始结果：<stem>_ocr_N.md + <stem>_ocr_N_res.json
    │                            # （keep_pages=False 时处理完即清理）
    │   └── _raw_concatenated.md # 未清洗的全文拼接（便于排查 OCR 质量问题）
    ├── <stem>.cleaned.md        # ★ 最终清洗后的 Markdown（主交付物，文件名稳定）
    ├── <stem>.documents.jsonl   # 可选：文档块（表格+文本）序列化
    └── manifest.json            # 任务元数据：来源、页数、产物路径、耗时、状态

文件名不再拼接随机 task_id；``manifest.json`` 让 MCP 工具/下游能按 task_id 找回
所有产物路径。OCR 管线本身（标题层级重建、页眉页脚去重、表格合并）与旧实现保持一致。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from threading import local
from typing import Any

import fitz
from langchain_core.documents import Document
from markdown_it import MarkdownIt
from PIL import Image
from tqdm import tqdm

from alphabee import PROJECT_ROOT
from alphabee.loader.markdown_table_handler import (
    extract_tables_from_text,
    merge_tables,
)

Image.MAX_IMAGE_PIXELS = None

DEFAULT_OCR_SERVER_URL = os.getenv("PADDLE_VL_SERVER_URL", "http://localhost:8118/v1")

# 可覆盖的持久化根目录（默认 <PROJECT_ROOT>/outputs/pdf_ocr）
DEFAULT_PDF_OCR_ROOT = os.getenv("ALPHABEE_PDF_OCR_ROOT", str(PROJECT_ROOT / "outputs" / "pdf_ocr"))

# ---------------------------------------------------------------------------
# 标题层级重建（A：编号分级 + 去伪）与 OCR 版面信息（B：block_label）相结合。
#
# 背景：PaddleOCR-VL 把几乎所有的 paragraph_title 块统一渲染成 "##"，从不保留
# 真实层级，还会把复选框 / 引导句等误判成标题。这里在 markdown 后处理阶段：
#   1) 用中文编号前缀（第X节 / 一、/（一）/ 1、/ (1) / 1) / A.）重建标题层级；
#   2) 用正文特征（复选框、句号结尾、引导句）过滤误识别；
#   3) 用 OCR 保存的 *_res.json 中 block_label 二次过滤（B 部分）。
# ---------------------------------------------------------------------------

_HEADING_NUM_PATTERNS = [
    # (rank, regex)：rank 从浅到深
    (1, re.compile(r"^(第[零一二三四五六七八九十百\d]+[节章篇部分])\s*(.*)$")),
    (2, re.compile(r"^([零一二三四五六七八九十]+、)\s*(.*)$")),
    (3, re.compile(r"^([（(][零一二三四五六七八九十]+[）)])\s*(.*)$")),
    (4, re.compile(r"^(\d{1,3}\s*[、.．])\s*(.*)$")),
    (5, re.compile(r"^([（(]\d{1,3}[）)])\s*(.*)$")),
    (6, re.compile(r"^(\d{1,3}\s*[)）])\s*(.*)$")),
    (7, re.compile(r"^([A-Za-z][.、．])\s*(.*)$")),
]

_TITLE_BLOCK_LABELS = {
    "paragraph_title",
    "doc_title",
    "document_title",
    "table_title",
    "title",
}

_CHECKBOX_CHARS = "√✓□☐☑×"
_HEADING_END_SENTENCE = re.compile(r"[。！？；]$")
_HEADING_LEADIN = re.compile(r"如下[:：]$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+)$")


def get_pdf_ocr_root() -> Path:
    """返回 PDF OCR 持久化根目录（``outputs/pdf_ocr``），并确保其存在。

    可通过环境变量 ``ALPHABEE_PDF_OCR_ROOT`` 覆盖（每次调用读取，便于测试隔离）。
    """
    root = Path(os.getenv("ALPHABEE_PDF_OCR_ROOT", DEFAULT_PDF_OCR_ROOT))
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_upload_root() -> Path:
    """返回 PDF 上传存储根目录（``outputs/pdf_ocr/uploads``）。"""
    root = get_pdf_ocr_root() / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_task_workspace(task_id: str) -> Path:
    """返回某个 task 的工作区目录（``outputs/pdf_ocr/tasks/<task_id>``）。

    task_id 会被清洗（只保留 ``[A-Za-z0-9_.-]``），防止路径穿越。
    """
    safe_id = re.sub(r"[^A-Za-z0-9_.\-]", "_", task_id or "unnamed")
    if safe_id in {"", ".", ".."}:
        safe_id = "unnamed"
    workspace = get_pdf_ocr_root() / "tasks" / safe_id
    return workspace


def _match_heading_pattern(title: str) -> tuple[int | None, str, str]:
    """识别中文财报常见的编号前缀，返回 (rank, number, rest)。无法识别时 (None, "", "")。"""
    for rank, pattern in _HEADING_NUM_PATTERNS:
        m = pattern.match(title)
        if m:
            return rank, m.group(1), m.group(2)
    return None, "", ""


def _normalize_layout_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def enhance_for_table_ocr(
    img_bgr: Any,
    scale: float = 1.5,
    strengthen_lines: bool = True,
    strengthen_vertical: bool = True,
) -> Any:
    """表格 OCR 图像增强：偏结构恢复，尽量避免漏空列（依赖 opencv，惰性导入）。"""
    import cv2
    import numpy as np

    h, w = img_bgr.shape[:2]

    # Step 1) 放大
    sx, sy = 1.8, 1.5
    img_bgr = cv2.resize(img_bgr, (int(w * sx), int(h * sy)), interpolation=cv2.INTER_CUBIC)

    # Step 2) 灰度 + CLAHE + 去噪
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    g = clahe.apply(gray)
    g = cv2.fastNlMeansDenoising(g, None, h=7, templateWindowSize=7, searchWindowSize=21)

    # Step 3) 二值化：文字/线条为白色前景
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 10)

    # Step 4) 轻度闭运算
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 2))
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, close_kernel, iterations=1)

    if strengthen_lines:
        hh, ww = bw.shape

        # Step 5) 水平线增强
        hk = max(25, ww // 25)
        horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (hk, 1))
        horiz = cv2.erode(bw, horiz_kernel, iterations=1)
        horiz = cv2.dilate(horiz, horiz_kernel, iterations=2)
        horiz = cv2.bitwise_and(horiz, bw)

        num, lab, st, _ = cv2.connectedComponentsWithStats(horiz, 8)
        horiz_thick = np.zeros_like(horiz)
        thick_kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 1))
        for i in range(1, num):
            x, y, w_, h_, area = st[i]
            if w_ < 0.5 * ww:
                continue
            roi = horiz[y : y + h_, x : x + w_]
            roi2 = cv2.dilate(roi, thick_kernel_h, iterations=1)
            horiz_thick[y : y + h_, x : x + w_] = cv2.bitwise_or(horiz_thick[y : y + h_, x : x + w_], roi2)
        bw = cv2.bitwise_or(bw, horiz_thick)

        # Step 6) 竖线增强：保守版，避免乱加竖线
        if strengthen_vertical:
            vk = max(20, hh // 55)
            vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk))
            vert = cv2.erode(bw, vert_kernel, iterations=1)
            vert = cv2.dilate(vert, vert_kernel, iterations=2)
            vert = cv2.bitwise_and(vert, bw)
            repair_kernel_v = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, hh // 90)))
            vert = cv2.morphologyEx(vert, cv2.MORPH_CLOSE, repair_kernel_v, iterations=1)

            num, lab, st, _ = cv2.connectedComponentsWithStats(vert, 8)
            vert_keep = np.zeros_like(vert)
            min_h = int(0.35 * hh)
            max_w = 10
            for i in range(1, num):
                x, y, w_, h_, area = st[i]
                aspect_ok = h_ / max(w_, 1) > 20
                height_ok = h_ >= min_h
                width_ok = w_ <= max_w
                area_ok = area >= 0.35 * h_
                if aspect_ok and height_ok and width_ok and area_ok:
                    roi = vert[y : y + h_, x : x + w_]
                    vert_keep[y : y + h_, x : x + w_] = cv2.bitwise_or(vert_keep[y : y + h_, x : x + w_], roi)
            vert_keep = cv2.dilate(vert_keep, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1)), iterations=1)
            bw = cv2.bitwise_or(bw, vert_keep)

    # Step 7) 输出白底黑字
    out = 255 - bw
    return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)


def enhance_image_file(in_path: Path, out_path: Path, strengthen_lines: bool = True) -> None:
    import cv2

    img = cv2.imread(str(in_path))
    if img is None:
        raise RuntimeError(f"Failed to read image: {in_path}")
    enhanced = enhance_for_table_ocr(img, strengthen_lines=strengthen_lines)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), enhanced)


class PDFOCRLoader:
    """把 PDF 渲染成页面图 → PaddleOCR-VL（vLLM server）识别 → Markdown 后处理。

    所有产物写入持久化工作区（见模块 docstring 的目录结构），
    最终清洗后的 Markdown 路径固定为 ``<workspace>/<stem>.cleaned.md``。
    """

    def __init__(
        self,
        task_id: str,
        pdf_path: str | Path,
        ocr_server_url: str = DEFAULT_OCR_SERVER_URL,
        dpi: int = 144,
        image_format: str = "PNG",
        max_workers: int = 2,
        batch_size: int = 64,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        workspace: str | Path | None = None,
        keep_pages: bool = True,
    ) -> None:
        self.task_id = task_id
        self.pdf_path = Path(pdf_path)
        self.file_name = self.pdf_path.stem
        self.file_name_with_ext = self.pdf_path.name
        self.ocr_server_url = ocr_server_url
        self.dpi = dpi
        self.image_format = image_format
        self.max_workers = max(1, max_workers)
        self.batch_size = max(1, batch_size)
        self.max_retries = max(1, max_retries)
        self.retry_delay = max(0.0, retry_delay)
        self.keep_pages = keep_pages

        # ── 持久化工作区（优化点：不再使用 /tmp） ────────────────────────
        self.workspace = Path(workspace) if workspace else get_task_workspace(task_id)
        self.image_dir = self.workspace / "images"
        self.pages_dir = self.workspace / "pages"
        self.pdf_dir = self.workspace / "pdf"
        self.markdown_path = self.workspace / f"{self.file_name}.cleaned.md"
        self.jsonl_path = self.workspace / f"{self.file_name}.documents.jsonl"
        self.manifest_path = self.workspace / "manifest.json"

        self._thread_state = local()
        self._manifest: dict[str, Any] = {
            "task_id": task_id,
            "file_name": self.file_name_with_ext,
            "pdf_path": str(self.pdf_path.resolve()) if self.pdf_path.exists() else str(self.pdf_path),
            "created_at": datetime.now(UTC).isoformat(),
            "status": "pending",
        }

    # ── manifest 辅助 ─────────────────────────────────────────────────────

    def update_manifest(self, **updates: Any) -> None:
        """就地更新并持久化 manifest.json。"""
        self._manifest.update(updates)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_source_pdf(self, pdf_bytes: bytes | None = None) -> Path | None:
        """把原始 PDF 落到工作区 pdf/ 目录。

        本地路径输入时直接复用原文件（不复制）；bytes 输入（base64/URL/上传）时写副本。
        返回 PDF 路径（bytes 输入返回工作区副本；本地路径返回原文件；文件缺失时 None）。
        """
        if pdf_bytes is not None:
            self.pdf_dir.mkdir(parents=True, exist_ok=True)
            dest = self.pdf_dir / self.file_name_with_ext
            dest.write_bytes(pdf_bytes)
            return dest
        if self.pdf_path.exists():
            return self.pdf_path
        return None

    # ── OCR pipeline helpers ──────────────────────────────────────────────

    def _get_thread_pipeline(self):
        # 惰性导入：模块 import 时不加载 paddleocr（启动 MCP server 更快）
        from paddleocr import PaddleOCRVL

        pipeline = getattr(self._thread_state, "pipeline", None)
        if pipeline is None:
            self._thread_state.pipeline = PaddleOCRVL(
                pipeline_version="v1.6",
                vl_rec_backend="vllm-server",
                vl_rec_server_url=self.ocr_server_url,
                vl_rec_max_concurrency=32,
            )
            pipeline = self._thread_state.pipeline
        return pipeline

    def _ocr_batch(self, image_paths: list[Path]) -> int:
        """对一个批次的页面图做 OCR：写回 pages/ 目录（md + *_res.json），返回处理页数。"""
        pipeline = self._get_thread_pipeline()

        paths = [str(p) for p in image_paths]
        outputs: list | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                outputs = pipeline.predict(paths, temperature=0.0, top_p=1.0)
                if not isinstance(outputs, list):
                    outputs = [outputs]
                if len(outputs) < len(image_paths):
                    raise RuntimeError(f"OCR 返回条目少于输入: {len(outputs)}/{len(image_paths)}")
                break
            except Exception as exc:
                if attempt >= self.max_retries:
                    print(
                        f"[OCR重试] 批次 {len(image_paths)} 张失败，重试耗尽"
                        f"({self.max_retries}次): {type(exc).__name__}: {exc}"
                    )
                    raise
                delay = self.retry_delay * (2 ** (attempt - 1))
                print(
                    f"[OCR重试] 批次 {len(image_paths)} 张第 {attempt}/{self.max_retries} 次失败: "
                    f"{type(exc).__name__}: {exc}；{delay:.1f}s 后重试"
                )
                time.sleep(delay)

        assert outputs is not None
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        for src_path, result in zip(image_paths, outputs):
            result.save_to_json(save_path=str(self.pages_dir))
            result.save_to_markdown(save_path=str(self.pages_dir))
            src_path.unlink(missing_ok=True)
        return len(image_paths)

    # ── PDF to markdown pipeline ──────────────────────────────────────────

    def _render_page(self, pdf_document: fitz.Document, page_num: int, matrix: fitz.Matrix) -> Path:
        page = pdf_document[page_num]
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        img_data = pixmap.tobytes("png")
        img = Image.open(io.BytesIO(img_data))

        if self.image_format.upper() != "PNG" and img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            mask = img.split()[-1] if img.mode == "RGBA" else None
            background.paste(img, mask=mask)
            img = background

        self.image_dir.mkdir(parents=True, exist_ok=True)
        image_path = self.image_dir / f"{self.file_name}_ocr_{page_num + 1}.{self.image_format.lower()}"
        img.save(str(image_path), format=self.image_format, dpi=(self.dpi, self.dpi))
        return image_path

    def _concat_markdown_files(self, markdown_files: list[Path], output_file: Path) -> None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as outfile:
            for md_file in markdown_files:
                with open(md_file, encoding="utf-8") as infile:
                    outfile.write(infile.read())
                    outfile.write("\n\n")

    def _cleanup_images(self) -> None:
        """清理页面渲染图（真正的一次性临时文件）。"""
        shutil.rmtree(self.image_dir, ignore_errors=True)

    def _cleanup_pages(self) -> None:
        """清理每页 OCR 原始结果（keep_pages=False 时调用）。"""
        shutil.rmtree(self.pages_dir, ignore_errors=True)

    # ── Markdown post-processing（与旧实现一致） ──────────────────────────

    def section_to_documents(self, sections: list[dict], llm_format_table: bool = False) -> list[Document]:
        docs: list[Document] = []
        for content in sections:
            section = content.get("metadata", {}).get("title", "未知章节")
            texts = content.get("content", [])
            if texts:
                blocks = extract_tables_from_text("\n".join(texts))
                for block in blocks:
                    if block["type"] == "table":
                        table_md = block["content"]
                        table_name = block.get("table_name", "未知表格")
                        if llm_format_table:
                            from alphabee.loader.markdown_table_handler import llm_format_html_table

                            table_md = llm_format_html_table(table_md, table_name=table_name)
                        docs.append(
                            Document(
                                page_content=table_md,
                                metadata={
                                    "type": "table",
                                    "section": section,
                                    "section_path": content.get("metadata", {}).get("section_path", []),
                                    "section_path_nums": content.get("metadata", {}).get("section_path_nums", []),
                                    "source": self.file_name_with_ext,
                                    "table_name": table_name,
                                },
                            )
                        )
                    else:
                        docs.append(
                            Document(
                                page_content=block["content"],
                                metadata={
                                    "type": "text",
                                    "section": section,
                                    "section_path": content.get("metadata", {}).get("section_path", []),
                                    "section_path_nums": content.get("metadata", {}).get("section_path_nums", []),
                                    "source": self.file_name_with_ext,
                                },
                            )
                        )
                for text in texts:
                    docs.append(
                        Document(
                            page_content=text,
                            metadata={
                                "type": "text",
                                "section": section,
                                "section_path": content.get("metadata", {}).get("section_path", []),
                                "section_path_nums": content.get("metadata", {}).get("section_path_nums", []),
                                "source": self.file_name_with_ext,
                            },
                        )
                    )
        return docs

    @staticmethod
    def _split_markdown_by_sections(
        md_text: str, file_name: str | None = None, file_type: str | None = None
    ) -> list[dict]:
        md = MarkdownIt()
        tokens = md.parse(md_text)

        sections: list[dict] = []
        current_section = {
            "metadata": {
                "source": file_name,
                "file_type": file_type,
                "title": "前言",
                "level": 0,
                "section_path": [],
                "section_path_nums": [],
            },
            "title": "前言",
            "level": 0,
            "content": [],
        }

        section_stack: list[str] = []
        section_numbers_stack: list[str] = []
        for idx, token in enumerate(tokens):
            if token.type == "heading_open":
                if current_section.get("content"):
                    current_section["content"] = [
                        line for line in current_section["content"] if line != current_section["title"]
                    ]
                    sections.append(current_section)

                level = int(token.tag[1])
                title = tokens[idx + 1].content if idx + 1 < len(tokens) else ""
                _rank, section_number, section_rest = _match_heading_pattern(title)
                if _rank is not None:
                    section_text = section_rest.strip()
                    section_number = section_number.strip()
                else:
                    section_text = title.strip()
                    section_number = ""

                section_stack = section_stack[: level - 1]
                section_stack.append(section_text)

                section_numbers_stack = section_numbers_stack[: level - 1]
                section_numbers_stack.append(section_number)

                current_section = {
                    "metadata": {
                        "source": file_name,
                        "file_type": file_type,
                        "title": title,
                        "level": level,
                        "section_path": section_stack.copy(),
                        "section_path_nums": section_numbers_stack.copy(),
                    },
                    "title": title,
                    "level": level,
                    "content": [],
                }
            elif token.type == "inline" and token.content:
                current_section.setdefault("content", []).append(token.content)
            elif token.type == "html_block" and token.content:
                current_section.setdefault("content", []).append(token.content)

        if current_section.get("content"):
            current_section["content"] = [
                line for line in current_section["content"] if line != current_section["title"]
            ]
            sections.append(current_section)

        return sections

    def _collect_layout_blocks(self, stems: list[str]) -> dict[str, dict[str, int]]:
        """读取 OCR 阶段保存的 ``*_res.json``，汇总版面块内容 -> block_label 计数。"""
        layout_map: dict[str, dict[str, int]] = {}
        for stem in stems:
            json_path = self.pages_dir / f"{stem}_res.json"
            if not json_path.exists():
                continue
            try:
                with open(json_path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                continue
            for block in data.get("parsing_res_list", []):
                content = block.get("block_content")
                if not content:
                    continue
                key = _normalize_layout_text(content)
                label = block.get("block_label", "")
                labels = layout_map.setdefault(key, {})
                labels[label] = labels.get(label, 0) + 1
        return layout_map

    @staticmethod
    def _heading_layout_labels(title: str, layout_map: dict[str, dict[str, int]]) -> set[str] | None:
        """在布局信息中查找标题对应的 block_label 集合。找不到返回 None。"""
        key = _normalize_layout_text(title)
        if key in layout_map:
            return set(layout_map[key].keys())
        matched: set[str] = set()
        for candidate, labels in layout_map.items():
            if len(candidate) >= 6 and (candidate.startswith(key) or key.startswith(candidate)):
                matched |= set(labels.keys())
        return matched or None

    @classmethod
    def _relevel_markdown_headers(
        cls,
        md_text: str,
        layout_map: dict[str, dict[str, int]],
        doc_title: str | None = None,
    ) -> str:
        """A + B 结合的标题后处理：过滤误识别 + 依据中文编号前缀重建标题层级。"""
        lines = md_text.splitlines()
        head_entries: list[dict] = []
        for idx, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m:
                continue
            title = m.group(2).strip()
            rank, _number, _rest = _match_heading_pattern(title)
            head_entries.append({"idx": idx, "title": title, "rank": rank, "demoted": False, "reason": None})

        for h in head_entries:
            t = h["title"]
            if any(c in t for c in _CHECKBOX_CHARS):
                h["demoted"], h["reason"] = True, "checkbox"
                continue
            labels = cls._heading_layout_labels(t, layout_map)
            if labels is not None and not (labels & _TITLE_BLOCK_LABELS):
                h["demoted"], h["reason"] = True, f"label:{sorted(labels)}"
                continue
            if _HEADING_END_SENTENCE.search(t):
                h["demoted"], h["reason"] = True, "end-sentence"
                continue
            if h["rank"] is None and _HEADING_LEADIN.search(t):
                h["demoted"], h["reason"] = True, "leadin"
                continue

        shift = 1 if doc_title else 0

        stack: list[tuple[int, int]] = []
        prev_level: int | None = None
        for h in head_entries:
            if h["demoted"]:
                h["new_level"] = None
                continue
            if h["rank"] is not None:
                while stack and stack[-1][0] > h["rank"]:
                    stack.pop()
                if stack and stack[-1][0] == h["rank"]:
                    level = stack[-1][1]
                else:
                    level = (stack[-1][1] + 1) if stack else 1
                    stack.append((h["rank"], level))
                prev_level = level
            else:
                level = prev_level if prev_level else 1
            h["new_level"] = min(level + shift, 6)

        new_lines = list(lines)
        for h in head_entries:
            if h["demoted"]:
                new_lines[h["idx"]] = h["title"]
            else:
                new_lines[h["idx"]] = "#" * h["new_level"] + " " + h["title"]

        if shift and doc_title:
            clean_title = re.sub(r"#+", "", doc_title).strip()
            if clean_title:
                new_lines.insert(0, f"# {clean_title}")

        return "\n".join(new_lines)

    @staticmethod
    def _ngram_split(text: str, n: int = 3) -> list[str]:
        length = len(text)
        if length < n:
            return [text]
        return [text[i : i + n] for i in range(length - n + 1)]

    @classmethod
    def _ngram_split_lines(cls, text_lines: list[str], n: int = 3, threshold: int = 2) -> list[str]:
        ngrams: list[str] = []
        for line in text_lines:
            ngrams.extend(cls._ngram_split(line, n))
        ngram_counts = Counter(ngrams)
        return [ngram for ngram, count in ngram_counts.items() if count > threshold]

    @classmethod
    def _parse_markdown(
        cls, md_text: str, page_count: int, file_name: str | None, file_type: str | None
    ) -> list[dict]:
        """按章节切分 + ngram 页眉页脚去重，返回清洗后的 sections。"""
        sections = cls._split_markdown_by_sections(md_text, file_name=file_name, file_type=file_type)
        if not sections or page_count <= 0:
            return sections

        title_counts: dict[str, int] = {}
        for section in sections:
            title = section.get("title")
            title_counts[title] = title_counts.get(title, 0) + 1

        header_footer_titles = [
            title
            for title, count in title_counts.items()
            if count >= 2 and count / page_count >= 0.6
        ]

        content_lines: dict[str, list[str]] = {}
        for section in sections:
            if section.get("title") in header_footer_titles:
                content_lines.setdefault(section["title"], []).extend(section.get("content", []))

        ngram_dict = {
            title: cls._ngram_split_lines(lines, n=5, threshold=5)
            for title, lines in content_lines.items()
        }

        ngram_threshold = 3
        cleaned_sections: list[dict] = []
        for section in sections:
            if section.get("title") in header_footer_titles:
                section_ngrams = ngram_dict.get(section["title"], [])
                cleaned_content: list[str] = []
                for content_line in section.get("content", []):
                    content_ngrams = cls._ngram_split(content_line, n=5)
                    content_ngrams_set = set(content_ngrams)
                    ngram_count = sum(1 for ngram in content_ngrams_set if ngram in section_ngrams)
                    dynamic_threshold = max(ngram_threshold, len(content_ngrams) // 5)
                    if ngram_count < dynamic_threshold:
                        cleaned_content.append(content_line)
                if cleaned_content and cleaned_sections:
                    cleaned_sections[-1].setdefault("content", []).extend(cleaned_content)
            else:
                cleaned_sections.append(section)

        return cleaned_sections

    @staticmethod
    def _concat_markdown_from_sections(sections: list[dict]) -> str:
        md_lines: list[str] = []
        for section in sections:
            level = section.get("level", 0)
            title = section.get("title", "")
            if level > 0:
                md_lines.append(f"{'#' * level} {title}")
            for content in section.get("content", []):
                md_lines.append(content)
            md_lines.append("")
        return "\n".join(md_lines)

    # ── Public API ────────────────────────────────────────────────────────

    def load_full_text(
        self,
        start_page: int = 0,
        *,
        source_pdf_bytes: bytes | None = None,
        source_type: str = "path",
    ) -> str:
        """OCR 整个 PDF，返回清洗 + 合并表格后的最终 Markdown 文本。

        副作用（持久化到工作区，文件名稳定）：
        - ``<workspace>/pages/*``           每页 OCR 原始结果（keep_pages=False 时清理）
        - ``<workspace>/pages/_raw_concatenated.md``  未清洗的全文拼接
        - ``<workspace>/<stem>.cleaned.md`` 最终清洗后的 Markdown（★ 主交付物）
        - ``<workspace>/manifest.json``     任务元数据

        Args:
            start_page: 起始页（0-based）。
            source_pdf_bytes: 当 PDF 来自内存 bytes（base64/URL/上传）时传入，落到工作区 pdf/。
            source_type: 输入来源标识（path/base64/url/file_id），写入 manifest。

        Returns:
            最终 Markdown 文本（与 ``markdown_path`` 文件内容一致）。
        """
        file_type = self.pdf_path.suffix.lstrip(".").lower()

        self.workspace.mkdir(parents=True, exist_ok=True)
        saved_pdf = self.save_source_pdf(source_pdf_bytes)
        self.update_manifest(
            source_type=source_type,
            # bytes 输入（base64/url/上传）时，manifest 指向工作区持久副本（临时路径会被清理）
            pdf_path=str(saved_pdf) if saved_pdf else self._manifest.get("pdf_path"),
            source_pdf_path=str(saved_pdf) if saved_pdf else None,
            status="running",
        )

        zoom = self.dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        stems: list[str] = []

        started_at = time.monotonic()
        try:
            with fitz.open(self.pdf_path) as pdf_document:
                total_pages = pdf_document.page_count
                if total_pages == 0:
                    self.update_manifest(status="failed", error="PDF has 0 pages")
                    return ""

                pending = set()
                batch: list[Path] = []
                progress = tqdm(total=total_pages, desc="OCR", unit="page")

                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    for page_num in range(start_page, total_pages):
                        image_path = self._render_page(pdf_document, page_num, matrix)
                        stems.append(image_path.stem)
                        batch.append(image_path)

                        if len(batch) >= self.batch_size:
                            future = executor.submit(self._ocr_batch, batch[:])
                            pending.add(future)
                            batch.clear()

                        if len(pending) >= self.max_workers * 4:
                            done, pending = wait(pending, return_when=FIRST_COMPLETED)
                            for completed in done:
                                progress.update(completed.result())

                    if batch:
                        future = executor.submit(self._ocr_batch, batch[:])
                        pending.add(future)
                        batch.clear()

                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for completed in done:
                            progress.update(completed.result())

                progress.close()
        finally:
            self._cleanup_images()

        # ── 拼接每页 md → 未清洗全文（保留一份便于排查） ─────────────────
        markdown_files = [self.pages_dir / f"{stem}.md" for stem in stems]
        raw_output_file = self.pages_dir / "_raw_concatenated.md"
        self._concat_markdown_files(markdown_files, raw_output_file)
        md_text = raw_output_file.read_text(encoding="utf-8")

        # ── 标题重定级 + 去伪（A：编号分级；B：OCR 版面 block_label） ─────
        layout_map = self._collect_layout_blocks(stems)
        md_text = self._relevel_markdown_headers(md_text, layout_map, doc_title=self.file_name)

        sections = self._parse_markdown(
            md_text,
            page_count=len(stems),
            file_name=self.file_name,
            file_type=file_type,
        )
        cleaned_md_text = self._concat_markdown_from_sections(sections)
        final_md_text = merge_tables(cleaned_md_text)

        # ── 写最终交付物：固定文件名 <stem>.cleaned.md ───────────────────
        self.markdown_path.write_text(final_md_text, encoding="utf-8")

        if not self.keep_pages:
            self._cleanup_pages()

        elapsed = time.monotonic() - started_at
        self.update_manifest(
            status="completed",
            completed_at=datetime.now(UTC).isoformat(),
            start_page=start_page,
            page_count=len(stems),
            markdown_path=str(self.markdown_path),
            raw_concatenated_path=str(raw_output_file) if raw_output_file.exists() else None,
            char_count=len(final_md_text),
            duration_seconds=round(elapsed, 2),
        )
        return final_md_text

    def load(self, start_page: int = 0, *, save_documents: bool = True, **kwargs: Any) -> list[Document]:
        """OCR 并返回文档块列表（表格块 + 文本块）。

        ``save_documents=True`` 时同时把文档序列化到 ``<workspace>/<stem>.documents.jsonl``，
        便于下游（RAG / query_financial_report 等）直接按路径消费。
        """
        print(f"开始加载PDF文件: {self.pdf_path}, task_id: {self.task_id}")
        final_md_text = self.load_full_text(start_page=start_page, **kwargs)
        sections = self._split_markdown_by_sections(
            final_md_text,
            file_name=self.file_name_with_ext,
            file_type=self.pdf_path.suffix.lstrip(".").lower(),
        )
        docs = self.section_to_documents(sections)
        if save_documents:
            self.save_documents_to_jsonl(docs, self.jsonl_path)
            self.update_manifest(jsonl_path=str(self.jsonl_path), document_count=len(docs))
        return docs

    @staticmethod
    def save_documents_to_jsonl(documents: list[Document], output_path: str | Path) -> None:
        """把文档块序列化为标准 JSONL（每行一个完整 JSON 对象：page_content + metadata）。"""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for doc in documents:
                record = {
                    "page_content": doc.page_content,
                    "metadata": doc.metadata,
                }
                # 紧凑序列化：JSONL 要求一行一个完整 JSON，不能用 indent 多行输出
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
