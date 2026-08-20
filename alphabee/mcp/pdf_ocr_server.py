"""AlphaBee PDF OCR MCP 服务。

从 BidGenius 的 ``pdf_ocr_server.py`` 移植，并针对文件保存做了系统化优化：

文件保存优化（相对旧实现）
--------------------------
- 上传的 PDF 不再进 ``/tmp/bidgenius_pdf_uploads/``，改为持久化
  ``<PROJECT_ROOT>/outputs/pdf_ocr/uploads/<file_id>/``；
- OCR 任务产物收敛到 ``outputs/pdf_ocr/tasks/<task_id>/``（见 loader 模块 docstring），
  每个工具结果都返回**稳定的文件路径**（``markdown_path`` / ``documents_path`` / ``pages_dir``），
  模型/下游按路径读取磁盘文件，不再把整份 Markdown 塞进对话上下文；
- ``ocr_pdf_to_markdown`` 默认只返回路径 + 统计信息（``include_content=True`` 时才内联正文）；
- 新增 ``list_ocr_tasks`` / ``get_ocr_task``：按 task_id 找回任意历史任务的全部产物；
- 新增 ``publish_report_sections``：把清洗后的 Markdown 按章节拆分写入
  ``reports/<报告名>/``，与 AlphaBee 现有的 ``query_financial_report`` 工具打通——
  OCR 完成后可以继续"拆章节 → 检索/问答 → 生成分析"的下游操作；
- **异步任务三件套**（面向大文件/长耗时 OCR）：``submit_pdf_ocr`` 提交后立即返回
  ``task_id``，worker 子进程后台执行，``get_pdf_ocr_status`` 轮询进度，
  ``get_pdf_ocr_result`` 取结果，``cancel_pdf_ocr_task`` 取消——
  基于 ``alphabee.mcp.jobs`` 通用任务框架，可扩展到其它长任务域。

工具清单：``upload_pdf`` / ``list_uploaded_pdfs`` / ``ocr_pdf_to_markdown`` /
``ocr_pdf_to_documents`` / ``ocr_pdf_to_jsonl`` / ``list_ocr_tasks`` /
``get_ocr_task`` / ``publish_report_sections`` / ``submit_pdf_ocr`` /
``get_pdf_ocr_status`` / ``wait_pdf_ocr_task`` / ``get_pdf_ocr_result`` /
``list_pdf_ocr_tasks`` / ``cancel_pdf_ocr_task``。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default as default_email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from alphabee import PROJECT_ROOT
from alphabee.financial_report.report_parser import write_markdown_report_folder
from alphabee.loader.pdf_ocr_loader import (
    DEFAULT_OCR_SERVER_URL,
    PDFOCRLoader,
    get_pdf_ocr_root,
    get_task_workspace,
    get_upload_root,
)
from alphabee.mcp.jobs import (
    JobStatus,
    JobStore,
    register_job_tools,
)

mcp = FastMCP("alphabee-pdf-ocr", json_response=True)
DEFAULT_UPLOAD_HOST = "127.0.0.1"
DEFAULT_UPLOAD_PORT = 8766

# ── 异步任务域：pdf_ocr ────────────────────────────────────────────────────
OCR_JOB_KIND = "pdf_ocr"


def _get_ocr_job_store() -> JobStore:
    """OCR 任务的 JobStore：job.json 与 OCR manifest 同目录（任务工作区内）。"""
    return JobStore(
        workspace_resolver=get_task_workspace,
        list_dir=get_pdf_ocr_root() / "tasks",
    )


_job_store = _get_ocr_job_store()

# 下游 query_financial_report 读取的报告根目录
REPORT_DIR = PROJECT_ROOT / "reports"


# ── 结果模型 ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ResolvedPDFInput:
    pdf_path: Path
    source_type: str
    source_value: str
    pdf_bytes: bytes | None = None
    cleanup_dir: Path | None = None


class OCRMetadata(BaseModel):
    task_id: str
    input_source: str
    pdf_path: str
    file_name: str
    start_page: int
    source_pdf_path: str | None = None
    file_id: str | None = None
    pdf_url: str | None = None


class UploadedFileInfo(BaseModel):
    file_id: str
    file_name: str
    pdf_path: str
    created_at: str | None = None


class DocumentItem(BaseModel):
    page_content: str
    metadata: dict[str, Any]


class OCRMarkdownResult(BaseModel):
    metadata: OCRMetadata
    markdown_path: str = Field(..., description="清洗后 Markdown 的落盘路径（读取此文件获取全文）")
    char_count: int = Field(0, description="Markdown 字符数")
    page_count: int = Field(0, description="OCR 处理页数")
    raw_concatenated_path: str | None = Field(None, description="未清洗的全文拼接路径（排查用）")
    markdown: str | None = Field(
        None,
        description="内联 Markdown 正文。仅当 include_content=True 时返回，否则为 null——请读取 markdown_path",
    )


class OCRDocumentsResult(BaseModel):
    metadata: OCRMetadata
    document_count: int
    documents_path: str | None = Field(None, description="完整文档块 JSONL 文件路径")
    documents: list[DocumentItem] = Field(
        default_factory=list,
        description="文档块预览（受 preview_size 限制）；完整数据在 documents_path",
    )


class OCRJSONLResult(BaseModel):
    metadata: OCRMetadata
    document_count: int
    output_path: str


class UploadPDFResult(BaseModel):
    file_id: str
    file_name: str
    pdf_path: str
    created_at: str


class ListUploadedPDFsResult(BaseModel):
    count: int
    files: list[UploadedFileInfo]


class OCRTaskInfo(BaseModel):
    task_id: str
    manifest: dict[str, Any]


class ListOCRTasksResult(BaseModel):
    count: int
    tasks: list[dict[str, Any]]


class SubmitOCRJobResult(BaseModel):
    task_id: str = Field(..., description="任务 id：轮询/取结果/取消时使用")
    status: str = Field(..., description="初始状态（queued）")
    pdf_path: str = Field(..., description="OCR 使用的 PDF 路径")
    markdown_path: str = Field(..., description="任务成功后的最终 Markdown 路径（预期）")
    hint: str = Field(
        ...,
        description="后续操作提示：用 get_pdf_ocr_status 轮询，成功后用 get_pdf_ocr_result 取结果",
    )


class PublishReportResult(BaseModel):
    report_name: str
    report_dir: str
    file_count: int
    section_count: int


# ── PDF 来源解析（与旧实现一致） ───────────────────────────────────────────


def _normalize_pdf_path(pdf_path: str) -> Path:
    path = Path(pdf_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"PDF file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"PDF path is not a file: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file, got: {path}")
    return path


def _validate_pdf_sources(
    pdf_path: str | None,
    pdf_base64: str | None,
    pdf_url: str | None,
    file_id: str | None,
) -> tuple[str, str]:
    sources = {
        "path": pdf_path,
        "base64": pdf_base64,
        "url": pdf_url,
        "file_id": file_id,
    }
    provided = [(source_type, value) for source_type, value in sources.items() if value]
    if not provided:
        raise ValueError("Provide exactly one PDF source: pdf_path, pdf_url, file_id, or pdf_base64.")
    if len(provided) > 1:
        raise ValueError("Only one PDF source is allowed: pdf_path, pdf_url, file_id, or pdf_base64.")
    return provided[0]


def _normalize_pdf_file_name(file_name: str | None, fallback: str) -> str:
    candidate = Path(file_name).name if file_name else fallback
    if not candidate.lower().endswith(".pdf"):
        candidate = f"{Path(candidate).stem or fallback}.pdf"
    return candidate


def _validate_pdf_bytes(pdf_bytes: bytes, source_label: str) -> None:
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError(f"{source_label} does not contain a valid PDF file.")


def _decode_pdf_base64(pdf_base64: str) -> bytes:
    encoded = pdf_base64.strip()
    if encoded.startswith("data:"):
        _, _, encoded = encoded.partition(",")
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("pdf_base64 is not valid base64-encoded content.") from exc


def _create_temp_pdf(pdf_bytes: bytes, file_name: str) -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="alphabee_pdf_"))
    pdf_path = temp_dir / file_name
    pdf_path.write_bytes(pdf_bytes)
    return pdf_path, temp_dir


def _get_upload_dir(file_id: str) -> Path:
    return get_upload_root() / file_id


def _get_upload_metadata_path(file_id: str) -> Path:
    return _get_upload_dir(file_id) / "metadata.json"


def _store_uploaded_pdf(pdf_bytes: bytes, file_name: str) -> dict[str, Any]:
    normalized_name = _normalize_pdf_file_name(file_name, "uploaded.pdf")
    file_id = str(uuid4())
    upload_dir = _get_upload_dir(file_id)
    upload_dir.mkdir(parents=True, exist_ok=False)
    pdf_path = upload_dir / normalized_name
    pdf_path.write_bytes(pdf_bytes)

    metadata = {
        "file_id": file_id,
        "file_name": normalized_name,
        "pdf_path": str(pdf_path),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _get_upload_metadata_path(file_id).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata


def _load_uploaded_pdf(file_id: str) -> tuple[Path, dict[str, Any]]:
    metadata_path = _get_upload_metadata_path(file_id)
    if not metadata_path.exists():
        raise FileNotFoundError(f"Uploaded PDF does not exist for file_id: {file_id}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    pdf_path = _normalize_pdf_path(metadata["pdf_path"])
    return pdf_path, metadata


def _list_all_uploaded_metadata() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    upload_root = get_upload_root()
    if not upload_root.is_dir():
        return results
    for entry in sorted(upload_root.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if "file_id" in meta and "file_name" in meta:
                results.append(meta)
        except (json.JSONDecodeError, OSError):
            continue
    return results


def _resolve_pdf_url_file_name(pdf_url: str, file_name: str | None) -> str:
    if file_name:
        return _normalize_pdf_file_name(file_name, "downloaded.pdf")
    parsed = urlparse(pdf_url)
    url_name = Path(unquote(parsed.path)).name
    return _normalize_pdf_file_name(url_name or "downloaded.pdf", "downloaded.pdf")


def _download_pdf_bytes(pdf_url: str) -> bytes:
    parsed = urlparse(pdf_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported PDF URL scheme: {parsed.scheme or '<empty>'}")

    request = Request(
        pdf_url,
        headers={
            "User-Agent": "alphabee-pdf-ocr-mcp/1.0",
            "Accept": "application/pdf,*/*",
        },
    )
    with urlopen(request, timeout=60) as response:
        pdf_bytes = response.read()
    _validate_pdf_bytes(pdf_bytes, f"URL {pdf_url}")
    return pdf_bytes


@contextmanager
def _resolve_pdf_input(
    *,
    pdf_path: str | None = None,
    pdf_base64: str | None = None,
    pdf_url: str | None = None,
    file_id: str | None = None,
    file_name: str | None = None,
):
    """把四种输入来源统一解析为本地 PDF 路径，返回 ResolvedPDFInput。

    base64 / url 来源会先落到临时目录（loader 会把副本持久化到工作区后再清理）。
    """
    source_type, source_value = _validate_pdf_sources(pdf_path, pdf_base64, pdf_url, file_id)

    if source_type == "path":
        yield ResolvedPDFInput(
            pdf_path=_normalize_pdf_path(source_value),
            source_type=source_type,
            source_value=source_value,
        )
        return

    if source_type == "file_id":
        uploaded_pdf_path, _ = _load_uploaded_pdf(source_value)
        yield ResolvedPDFInput(
            pdf_path=uploaded_pdf_path,
            source_type=source_type,
            source_value=source_value,
        )
        return

    temp_dir: Path | None = None
    try:
        if source_type == "base64":
            pdf_bytes = _decode_pdf_base64(source_value)
            _validate_pdf_bytes(pdf_bytes, "pdf_base64")
            normalized_name = _normalize_pdf_file_name(file_name, "uploaded.pdf")
        else:
            pdf_bytes = _download_pdf_bytes(source_value)
            normalized_name = _resolve_pdf_url_file_name(source_value, file_name)

        resolved_path, temp_dir = _create_temp_pdf(pdf_bytes, normalized_name)
        yield ResolvedPDFInput(
            pdf_path=resolved_path,
            source_type=source_type,
            source_value=source_value,
            pdf_bytes=pdf_bytes,
            cleanup_dir=temp_dir,
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ── loader / 结果组装 ──────────────────────────────────────────────────────


def _create_loader(
    pdf_path: str,
    task_id: str | None = None,
    ocr_server_url: str = DEFAULT_OCR_SERVER_URL,
    dpi: int = 144,
    image_format: str = "PNG",
    max_workers: int = 2,
    batch_size: int = 16,
    keep_pages: bool = True,
) -> PDFOCRLoader:
    return PDFOCRLoader(
        task_id=task_id or str(uuid4()),
        pdf_path=_normalize_pdf_path(pdf_path),
        ocr_server_url=ocr_server_url,
        dpi=dpi,
        image_format=image_format,
        max_workers=max_workers,
        batch_size=batch_size,
        keep_pages=keep_pages,
    )


def _serialize_documents(documents: list[Any], limit: int | None = None) -> list[DocumentItem]:
    docs = documents if limit is None else documents[:limit]
    return [
        DocumentItem(
            page_content=document.page_content,
            metadata=document.metadata,
        )
        for document in docs
    ]


def _build_result_metadata(
    loader: PDFOCRLoader, pdf_input: ResolvedPDFInput, start_page: int
) -> OCRMetadata:
    return OCRMetadata(
        task_id=loader.task_id,
        input_source=pdf_input.source_type,
        pdf_path=str(loader.pdf_path),
        file_name=loader.file_name_with_ext,
        start_page=start_page,
        source_pdf_path=pdf_input.source_value if pdf_input.source_type == "path" else None,
        file_id=pdf_input.source_value if pdf_input.source_type == "file_id" else None,
        pdf_url=pdf_input.source_value if pdf_input.source_type == "url" else None,
    )


def _source_kwargs(pdf_input: ResolvedPDFInput) -> dict[str, Any]:
    """loader 持久化需要的来源信息（base64/url 时把 PDF bytes 副本写入工作区）。"""
    kwargs: dict[str, Any] = {"source_type": pdf_input.source_type}
    if pdf_input.pdf_bytes is not None:
        kwargs["source_pdf_bytes"] = pdf_input.pdf_bytes
    return kwargs


# ── MCP 工具 ───────────────────────────────────────────────────────────────


@mcp.tool()
def ocr_pdf_to_markdown(
    pdf_path: str | None = None,
    pdf_url: str | None = None,
    file_id: str | None = None,
    pdf_base64: str | None = None,
    file_name: str | None = None,
    start_page: int = 0,
    task_id: str | None = None,
    ocr_server_url: str = DEFAULT_OCR_SERVER_URL,
    dpi: int = 144,
    image_format: str = "PNG",
    max_workers: int = 2,
    batch_size: int = 64,
    keep_pages: bool = True,
    include_content: bool = False,
) -> OCRMarkdownResult:
    """对 PDF 财报/研报执行 OCR，返回清洗后的 Markdown。

    Provide exactly one of: pdf_path (local file), pdf_url (download), pdf_base64 (inline),
    or file_id (reuse a previously uploaded PDF).

    文件保存（推荐流程）：
    1. 结果默认**不内联正文**：完整 Markdown 已保存到 ``markdown_path``（稳定路径），
       直接读取该文件即可，避免把大段文本塞进对话上下文；
    2. 每页 OCR 原始结果在 ``<task 工作区>/pages/``（``keep_pages=False`` 时清理）；
    3. 需要全文内容时才传 ``include_content=True``（返回 ``markdown`` 字段）。

    后续操作：把 ``markdown_path`` 传给 ``publish_report_sections`` 可拆分章节写入
    ``reports/``，之后即可用 AlphaBee 的 ``query_financial_report`` 继续检索问答。
    """
    with _resolve_pdf_input(
        pdf_path=pdf_path,
        pdf_url=pdf_url,
        file_id=file_id,
        pdf_base64=pdf_base64,
        file_name=file_name,
    ) as pdf_input:
        loader = _create_loader(
            pdf_path=str(pdf_input.pdf_path),
            task_id=task_id,
            ocr_server_url=ocr_server_url,
            dpi=dpi,
            image_format=image_format,
            max_workers=max_workers,
            batch_size=batch_size,
            keep_pages=keep_pages,
        )
        markdown = loader.load_full_text(
            start_page=start_page,
            **_source_kwargs(pdf_input),
        )
        manifest = loader._manifest
        return OCRMarkdownResult(
            metadata=_build_result_metadata(loader, pdf_input, start_page),
            markdown_path=str(loader.markdown_path),
            char_count=manifest.get("char_count", len(markdown)),
            page_count=manifest.get("page_count", 0),
            raw_concatenated_path=manifest.get("raw_concatenated_path"),
            markdown=markdown if include_content else None,
        )


@mcp.tool()
def ocr_pdf_to_documents(
    pdf_path: str | None = None,
    pdf_url: str | None = None,
    file_id: str | None = None,
    pdf_base64: str | None = None,
    file_name: str | None = None,
    start_page: int = 0,
    task_id: str | None = None,
    ocr_server_url: str = DEFAULT_OCR_SERVER_URL,
    dpi: int = 144,
    image_format: str = "PNG",
    max_workers: int = 2,
    batch_size: int = 64,
    keep_pages: bool = True,
    preview_size: int = 20,
    save_documents: bool = True,
) -> OCRDocumentsResult:
    """OCR 并把文档块（表格块 + 文本块）序列化为 JSONL + 预览。

    Provide exactly one of: pdf_path / pdf_url / pdf_base64 / file_id.

    文件保存：完整文档默认写入 ``documents_path``（JSONL，每行一个
    ``{page_content, metadata}``）；``documents`` 字段只返回前 ``preview_size`` 条
    预览，避免返回体过大。``save_documents=False`` 时只返回预览。
    """
    with _resolve_pdf_input(
        pdf_path=pdf_path,
        pdf_url=pdf_url,
        file_id=file_id,
        pdf_base64=pdf_base64,
        file_name=file_name,
    ) as pdf_input:
        loader = _create_loader(
            pdf_path=str(pdf_input.pdf_path),
            task_id=task_id,
            ocr_server_url=ocr_server_url,
            dpi=dpi,
            image_format=image_format,
            max_workers=max_workers,
            batch_size=batch_size,
            keep_pages=keep_pages,
        )
        documents = loader.load(
            start_page=start_page,
            save_documents=save_documents,
            **_source_kwargs(pdf_input),
        )
        return OCRDocumentsResult(
            metadata=_build_result_metadata(loader, pdf_input, start_page),
            document_count=len(documents),
            documents_path=str(loader.jsonl_path) if save_documents else None,
            documents=_serialize_documents(documents, limit=preview_size),
        )


@mcp.tool()
def ocr_pdf_to_jsonl(
    output_path: str,
    pdf_path: str | None = None,
    pdf_url: str | None = None,
    file_id: str | None = None,
    pdf_base64: str | None = None,
    file_name: str | None = None,
    start_page: int = 0,
    task_id: str | None = None,
    ocr_server_url: str = DEFAULT_OCR_SERVER_URL,
    dpi: int = 144,
    image_format: str = "PNG",
    max_workers: int = 2,
    batch_size: int = 64,
    keep_pages: bool = True,
) -> OCRJSONLResult:
    """OCR 并把文档块保存到指定 JSONL 文件，返回输出路径。

    Provide exactly one of: pdf_path / pdf_url / pdf_base64 / file_id.
    文档同时写入任务工作区的 ``<stem>.documents.jsonl``。
    """
    with _resolve_pdf_input(
        pdf_path=pdf_path,
        pdf_url=pdf_url,
        file_id=file_id,
        pdf_base64=pdf_base64,
        file_name=file_name,
    ) as pdf_input:
        loader = _create_loader(
            pdf_path=str(pdf_input.pdf_path),
            task_id=task_id,
            ocr_server_url=ocr_server_url,
            dpi=dpi,
            image_format=image_format,
            max_workers=max_workers,
            batch_size=batch_size,
            keep_pages=keep_pages,
        )
        documents = loader.load(
            start_page=start_page,
            save_documents=True,
            **_source_kwargs(pdf_input),
        )
        destination = Path(output_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        loader.save_documents_to_jsonl(documents, destination)
        return OCRJSONLResult(
            metadata=_build_result_metadata(loader, pdf_input, start_page),
            document_count=len(documents),
            output_path=str(destination),
        )


@mcp.tool()
def upload_pdf(
    pdf_base64: str,
    file_name: str | None = None,
) -> UploadPDFResult:
    """上传 base64 编码的 PDF，返回 file_id 供后续 OCR 调用复用。

    检查 ``list_uploaded_pdfs(file_name=...)`` 可避免重复上传。上传文件持久化在
    ``outputs/pdf_ocr/uploads/<file_id>/``。
    """
    pdf_bytes = _decode_pdf_base64(pdf_base64)
    _validate_pdf_bytes(pdf_bytes, "Uploaded PDF")
    metadata = _store_uploaded_pdf(
        pdf_bytes,
        _normalize_pdf_file_name(file_name, "uploaded.pdf"),
    )
    return UploadPDFResult(
        file_id=metadata["file_id"],
        file_name=metadata["file_name"],
        pdf_path=metadata["pdf_path"],
        created_at=metadata["created_at"],
    )


@mcp.tool()
def list_uploaded_pdfs(
    file_name: str | None = None,
) -> ListUploadedPDFsResult:
    """列出已上传的 PDF，可选按文件名（大小写不敏感的部分匹配）过滤。"""
    all_metadata = _list_all_uploaded_metadata()
    if file_name:
        lowered = file_name.lower()
        all_metadata = [m for m in all_metadata if lowered in m["file_name"].lower()]
    return ListUploadedPDFsResult(
        count=len(all_metadata),
        files=[
            UploadedFileInfo(
                file_id=m["file_id"],
                file_name=m["file_name"],
                pdf_path=m["pdf_path"],
                created_at=m.get("created_at"),
            )
            for m in all_metadata
        ],
    )


@mcp.tool()
def list_ocr_tasks(
    limit: int = 20,
    status: str | None = None,
) -> ListOCRTasksResult:
    """列出历史 OCR 任务（按创建时间倒序），可选按 status 过滤（completed/running/failed）。

    每个任务包含 ``manifest.json``（task_id、来源、页数、markdown_path、jsonl_path 等），
    可用于找回此前解析过的财报产物继续后续操作。
    """
    root = get_pdf_ocr_root() / "tasks"
    tasks: list[dict[str, Any]] = []
    if root.is_dir():
        for entry in root.iterdir():
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if status and manifest.get("status") != status:
                continue
            tasks.append(manifest)
    tasks.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return ListOCRTasksResult(count=len(tasks), tasks=tasks[:limit])


@mcp.tool()
def get_ocr_task(
    task_id: str,
) -> OCRTaskInfo:
    """按 task_id 读取某个 OCR 任务的 manifest（产物路径、状态、页数等）。"""
    manifest_path = get_task_workspace(task_id) / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"OCR task does not exist: {task_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return OCRTaskInfo(task_id=task_id, manifest=manifest)


@mcp.tool()
def publish_report_sections(
    markdown_path: str,
    report_name: str | None = None,
    save_dir: str | None = None,
    overwrite: bool = True,
    page_count: int | None = None,
) -> PublishReportResult:
    """把清洗后的财报 Markdown 按章节拆分，发布到 ``reports/<报告名>/`` 目录。

    这是「下载 → OCR → 解析 → 检索问答」链路中的解析步骤：解析逻辑复用
    ``alphabee.financial_report.report_parser.write_markdown_report_folder``
    （章节切分 + 页眉页脚 ngram 去重 + 文件夹结构生成），发布后即可用 AlphaBee 的
    ``query_financial_report`` 工具对该报告做章节级检索/问答。

    Args:
        markdown_path: ``ocr_pdf_to_markdown`` 返回的 markdown_path（或任意清洗后的 md 文件）。
        report_name: 报告目录名，如 "宁德时代：2026年半年度报告"；缺省取 markdown 文件名（去扩展名）。
        save_dir: 目标根目录，默认 ``<PROJECT_ROOT>/reports``。
        overwrite: 同名目录已存在时是否覆盖重建（默认 True）。
        page_count: 原始 PDF 页数（``ocr_pdf_to_markdown`` 返回的 page_count）；
            提供时启用页眉页脚 ngram 去重。

    目录结构：每个章节（按标题层级）生成对应目录/文件，与 ``report_parser`` 一致；
    同时在报告目录下保留完整 ``<报告名>.md`` 全文副本。
    """
    md_path = Path(markdown_path).expanduser().resolve()
    if not md_path.is_file():
        raise FileNotFoundError(f"Markdown file does not exist: {md_path}")

    report_name = (report_name or md_path.stem).strip()
    if not report_name:
        raise ValueError("report_name must not be empty")

    report_dir = write_markdown_report_folder(
        md_path.read_text(encoding="utf-8"),
        report_name,
        page_count=page_count,
        save_dir=save_dir or REPORT_DIR,
        overwrite=overwrite,
    )
    # 章节目录树中的文件数（含全文副本；不含目录本身）
    file_count = sum(1 for p in report_dir.rglob("*") if p.is_file())
    section_count = 0
    manifest_path = report_dir / ".report_manifest.json"
    if manifest_path.exists():
        try:
            section_count = json.loads(manifest_path.read_text(encoding="utf-8")).get("section_count", 0)
        except (json.JSONDecodeError, OSError):
            section_count = 0
    return PublishReportResult(
        report_name=report_name,
        report_dir=str(report_dir),
        file_count=file_count,
        section_count=section_count,
    )


# ── 异步任务三件套（submit / status / result / cancel） ─────────────────────
#
# 面向大文件/长耗时 OCR：submit 登记任务并拉起独立 worker 子进程后立即返回
# task_id；worker 通过 --run-job 执行并回写 job.json（状态机 + 进度 + 产物路径）。
# 通用三件套（status/list/cancel）由 alphabee.mcp.jobs.register_job_tools 生成，
# 本模块只负责 submit 与结果渲染两个域专属部分。


def _spawn_worker(task_id: str) -> int:
    """以独立进程拉起 OCR worker（--run-job <task_id>），返回 pid。

    worker 与 MCP 服务进程隔离：vLLM/OCR 崩溃不影响服务；stdio 传输下
    任务也不会随"每次工具调用新建子进程"的调用进程退出而丢失。
    """
    args = [sys.executable, "-m", "alphabee.mcp.pdf_ocr_server", "--run-job", task_id]
    env = dict(os.environ)
    project_root = str(PROJECT_ROOT)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = project_root + os.pathsep + existing if existing else project_root

    log_path = get_task_workspace(task_id) / "worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            args,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 脱离调用进程会话，防止随 stdio 调用结束被回收
        )
        return proc.pid
    finally:
        log_handle.close()


def _render_ocr_job_result(store: JobStore, task_id: str) -> dict[str, Any]:
    """渲染 OCR 任务结果：job.json 的 result + OCR manifest 的详情合并。"""
    job = store.load(OCR_JOB_KIND, task_id) or {}
    result = dict(job.get("result") or {})
    manifest_path = get_task_workspace(task_id) / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result.setdefault("file_name", manifest.get("file_name"))
            result.setdefault("pages_dir", str(manifest_path.parent / "pages") if (manifest_path.parent / "pages").is_dir() else None)
            result.setdefault("jsonl_path", manifest.get("jsonl_path"))
        except (json.JSONDecodeError, OSError):
            pass
    result["task_id"] = task_id
    return result


def _run_fake_ocr_job(store: JobStore, task_id: str, payload: dict[str, Any]) -> None:
    """测试/演示用假 worker：不调用真实 OCR，直接产出假 Markdown 并标记成功。

    仅当环境变量 ``ALPHABEE_PDF_OCR_FAKE=1`` 时启用（worker 子进程继承该变量）。
    同时写一份与 loader 同格式的 manifest.json，保证下游工具（get_ocr_task 等）可用。
    """
    workspace = get_task_workspace(task_id)
    workspace.mkdir(parents=True, exist_ok=True)
    pdf_path = Path(payload.get("pdf_path", ""))
    stem = pdf_path.stem if pdf_path else task_id
    markdown_path = workspace / f"{stem}.cleaned.md"
    markdown_text = f"# {stem}\n\n## 一、主要财务数据\n（fake OCR）营业收入 1000 万元，同比增长 12.5%。\n"
    markdown_path.write_text(markdown_text, encoding="utf-8")

    (workspace / "manifest.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "file_name": pdf_path.name if pdf_path else f"{stem}.pdf",
                "pdf_path": str(pdf_path) if pdf_path else None,
                "status": "completed",
                "page_count": 1,
                "markdown_path": str(markdown_path),
                "char_count": len(markdown_text),
                "source_type": payload.get("source_type", "path"),
                "fake": True,
                "created_at": datetime.now(UTC).isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    store.update(OCR_JOB_KIND, task_id, progress=100.0, message="OCR 1/1 页")
    store.succeed(
        OCR_JOB_KIND,
        task_id,
        result={
            "markdown_path": str(markdown_path),
            "page_count": 1,
            "char_count": len(markdown_text),
            "raw_concatenated_path": None,
            "fake": True,
        },
    )


def _run_job_worker(task_id: str) -> None:
    """worker 进程入口（--run-job）：执行一个异步 OCR 任务并回写 job.json。

    独立进程运行（由 submit_pdf_ocr 拉起），stdout 不参与 MCP 协议，
    所有诊断输出走 stderr / worker.log。
    """
    store = _get_ocr_job_store()
    job = store.load(OCR_JOB_KIND, task_id)
    if job is None:
        print(f"[worker] task {task_id} not found", file=sys.stderr)
        sys.exit(1)

    store.start(OCR_JOB_KIND, task_id)
    # 取消可能在 start 之前/之间发生（cancel 工具置位）；取消后直接退出
    if store.is_cancelled(OCR_JOB_KIND, task_id):
        return
    try:
        payload = job.get("payload", {})
        pdf_path = payload.get("pdf_path")
        if not pdf_path or not Path(pdf_path).is_file():
            raise FileNotFoundError(f"job payload pdf_path not found: {pdf_path}")

        # 测试/演示模式：ALPHABEE_PDF_OCR_FAKE=1 时不调用真实 OCR
        if os.getenv("ALPHABEE_PDF_OCR_FAKE") == "1":
            _run_fake_ocr_job(store, task_id, payload)
            return

        loader = _create_loader(
            pdf_path=pdf_path,
            task_id=task_id,
            ocr_server_url=payload.get("ocr_server_url") or DEFAULT_OCR_SERVER_URL,
            dpi=payload.get("dpi", 144),
            image_format=payload.get("image_format", "PNG"),
            max_workers=payload.get("max_workers", 2),
            batch_size=payload.get("batch_size", 64),
            keep_pages=payload.get("keep_pages", True),
        )

        def progress_cb(processed: int, total: int) -> bool:
            """每批次完成后：上报进度 + 检查取消（取消时返回 True 让 loader 中止）。"""
            store.update(
                OCR_JOB_KIND,
                task_id,
                progress=round(100.0 * processed / max(total, 1), 1),
                message=f"OCR {processed}/{total} 页",
            )
            return store.is_cancelled(OCR_JOB_KIND, task_id)

        markdown = loader.load_full_text(
            start_page=payload.get("start_page", 0),
            source_type=payload.get("source_type", "path"),
            progress_cb=progress_cb,
        )

        if store.is_cancelled(OCR_JOB_KIND, task_id):
            return  # 已取消：保持 cancelled 状态，不覆盖为 succeeded

        store.succeed(
            OCR_JOB_KIND,
            task_id,
            result={
                "markdown_path": str(loader.markdown_path),
                "page_count": loader._manifest.get("page_count", 0),
                "char_count": loader._manifest.get("char_count", len(markdown)),
                "raw_concatenated_path": loader._manifest.get("raw_concatenated_path"),
            },
        )
    except Exception as exc:  # noqa: BLE001 - worker 兜底：任何异常都落为 failed
        if not store.is_cancelled(OCR_JOB_KIND, task_id):
            store.fail(OCR_JOB_KIND, task_id, error=f"{type(exc).__name__}: {exc}")
        else:
            print(f"[worker] task {task_id} cancelled", file=sys.stderr)


@mcp.tool()
def submit_pdf_ocr(
    pdf_path: str | None = None,
    pdf_url: str | None = None,
    file_id: str | None = None,
    pdf_base64: str | None = None,
    file_name: str | None = None,
    start_page: int = 0,
    task_id: str | None = None,
    ocr_server_url: str = DEFAULT_OCR_SERVER_URL,
    dpi: int = 144,
    image_format: str = "PNG",
    max_workers: int = 2,
    batch_size: int = 64,
    keep_pages: bool = True,
) -> SubmitOCRJobResult:
    """**异步**提交 PDF OCR 任务，立即返回 task_id（不阻塞等待 OCR 完成）。

    适合大文件/长耗时 OCR。Provide exactly one of: pdf_path / pdf_url / pdf_base64 / file_id.

    文件保存：产物持久化在 ``outputs/pdf_ocr/tasks/<task_id>/``；任务完成后
    ``get_pdf_ocr_result(task_id)`` 返回 markdown_path 等路径。

    后续操作（异步三件套）：
    1. ``get_pdf_ocr_status(task_id)``：轮询 status/progress（queued/running/succeeded/failed/cancelled）；
    2. status == succeeded 后 ``get_pdf_ocr_result(task_id)`` 取结果路径；
    3. 不需要时 ``cancel_pdf_ocr_task(task_id)`` 取消（终止 worker 进程）。
    """
    source_type, source_value = _validate_pdf_sources(pdf_path, pdf_base64, pdf_url, file_id)
    task_id = task_id or f"{OCR_JOB_KIND}-{uuid4().hex[:12]}"

    # 解析 PDF 源并持久化：base64/url 的 bytes 先落到任务工作区，
    # worker 子进程直接按本地文件执行（不依赖调用方的临时数据）。
    if source_type == "path":
        resolved = _normalize_pdf_path(source_value)
    elif source_type == "file_id":
        resolved, _ = _load_uploaded_pdf(source_value)
    else:
        if source_type == "base64":
            pdf_bytes = _decode_pdf_base64(source_value)
            _validate_pdf_bytes(pdf_bytes, "pdf_base64")
            name = _normalize_pdf_file_name(file_name, "uploaded.pdf")
        else:
            pdf_bytes = _download_pdf_bytes(source_value)
            name = _resolve_pdf_url_file_name(source_value, file_name)
        workspace = get_task_workspace(task_id)
        pdf_dir = workspace / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        resolved = pdf_dir / name
        resolved.write_bytes(pdf_bytes)

    store = _get_ocr_job_store()
    store.create(
        OCR_JOB_KIND,
        task_id=task_id,
        payload={
            "pdf_path": str(resolved),
            "source_type": "path",  # worker 统一按本地文件执行
            "start_page": start_page,
            "ocr_server_url": ocr_server_url,
            "dpi": dpi,
            "image_format": image_format,
            "max_workers": max_workers,
            "batch_size": batch_size,
            "keep_pages": keep_pages,
        },
    )
    pid = _spawn_worker(task_id)
    store.update(OCR_JOB_KIND, task_id, pid=pid)

    return SubmitOCRJobResult(
        task_id=task_id,
        status=JobStatus.QUEUED,
        pdf_path=str(resolved),
        markdown_path=str(get_task_workspace(task_id) / f"{resolved.stem}.cleaned.md"),
        hint=(
            "用 get_pdf_ocr_status 轮询到 succeeded 后，"
            "再调用 get_pdf_ocr_result 取结果路径。"
        ),
    )


# 通用三件套 + 结果工具：get_pdf_ocr_status / list_pdf_ocr_tasks /
# cancel_pdf_ocr_task / get_pdf_ocr_result
register_job_tools(
    mcp,
    kind=OCR_JOB_KIND,
    store=_job_store,
    human_name="PDF OCR",
    result_renderer=_render_ocr_job_result,
)


# ── HTTP 上传 API（兼容旧调用方） ──────────────────────────────────────────


def _parse_upload_multipart(content_type: str, body: bytes) -> tuple[str, bytes]:
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("Content-Type must be multipart/form-data.")

    parser_input = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode() + body
    message = BytesParser(policy=default_email_policy).parsebytes(parser_input)
    if not message.is_multipart():
        raise ValueError("Request body is not a valid multipart form.")

    uploaded_name: str | None = None
    uploaded_bytes: bytes | None = None
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = part.get_param("name", header="content-disposition")
        if field_name != "file":
            continue
        uploaded_name = part.get_filename()
        uploaded_bytes = part.get_payload(decode=True) or b""
        break

    if uploaded_bytes is None:
        raise ValueError("Missing multipart file field 'file'.")

    _validate_pdf_bytes(uploaded_bytes, "Uploaded file")
    return _normalize_pdf_file_name(uploaded_name, "uploaded.pdf"), uploaded_bytes


class PDFUploadHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "AlphaBeePDFUpload/1.0"

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/upload":
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"error": "Not found. Use POST /upload with multipart/form-data field 'file'."},
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length header."})
            return

        if content_length <= 0:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Empty request body."})
            return

        content_type = self.headers.get("Content-Type")
        if not content_type:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Missing Content-Type header."})
            return

        try:
            body = self.rfile.read(content_length)
            file_name, pdf_bytes = _parse_upload_multipart(content_type, body)
            metadata = _store_uploaded_pdf(pdf_bytes, file_name)
        except ValueError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        self._write_json(
            HTTPStatus.CREATED,
            {
                "file_id": metadata["file_id"],
                "file_name": metadata["file_name"],
                "pdf_path": metadata["pdf_path"],
                "created_at": metadata["created_at"],
                "mcp_input": {"file_id": metadata["file_id"]},
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._write_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "upload_root": str(get_upload_root()),
                },
            )
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the AlphaBee PDF OCR MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind for streamable-http transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind for streamable-http transport (default: 8000)",
    )
    parser.add_argument(
        "--run-job",
        default=None,
        metavar="TASK_ID",
        help="worker 模式：执行指定 task_id 的异步任务并回写 job.json（submit_pdf_ocr 内部拉起）",
    )
    args = parser.parse_args()

    if args.run_job:
        _run_job_worker(args.run_job)
        return

    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


def main_upload_api() -> None:
    parser = argparse.ArgumentParser(description="Run the AlphaBee PDF upload API.")
    parser.add_argument("--host", default=DEFAULT_UPLOAD_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_UPLOAD_PORT)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), PDFUploadHTTPRequestHandler)
    print(f"Serving PDF upload API on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
