"""财报处理全链路：下载 → OCR → 解析成文件夹结构 → 搜索回答问题。

把「下载研报 PDF → OCR 提取 Markdown → 按章节解析成 ``reports/<报告名>/``
文件夹结构（复用 :mod:`alphabee.financial_report.report_parser`）→ 在解析结果上
搜索回答问题」串成一条可直接调用的管线，并提供 CLI。

典型用法（CLI）::

    # 从东方财富 infoCode 下载 + OCR + 解析 + 问答
    python -m alphabee.financial_report.pipeline \\
        --info-code AP202607101826864211 \\
        --report-name "宁德时代：2026年半年度报告" \\
        --question "宁德时代 2026 年上半年营业收入和净利润分别是多少？"

    # 本地 PDF
    python -m alphabee.financial_report.pipeline --pdf-path ./report.pdf --question "..."

    # 直链下载
    python -m alphabee.financial_report.pipeline --pdf-url "https://..." --question "..."

代码用法（async）::

    from alphabee.financial_report.pipeline import run_report_pipeline

    result = await run_report_pipeline(
        info_code="AP202607101826864211",
        report_name="宁德时代：2026年半年度报告",
        question="海外业务的最新进展如何？",
    )
    print(result["answer"])

管线各步产物：
- 下载：``data/eastmoney_reports/<file>.pdf``（或调用方指定路径）
- OCR：``outputs/pdf_ocr/tasks/<task_id>/<file>.cleaned.md``（+ manifest）
- 解析：``reports/<报告名>/``（章节目录树 + 全文副本 + `.report_manifest.json`）
- 问答：基于报告目录构建受限 deep agent（``create_report_fetch_agent``）检索回答
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import requests

from alphabee.financial_report.fetch_deepagents import create_report_fetch_agent
from alphabee.financial_report.report_parser import write_markdown_report_folder
from alphabee.mcp.pdf_ocr_server import (
    DEFAULT_OCR_SERVER_URL,
)
from alphabee.mcp.pdf_ocr_server import (
    ocr_pdf_to_markdown as _ocr_pdf_to_markdown_tool,
)
from alphabee.tools.eastmoney import (
    download_eastmoney_report_pdf,
    download_eastmoney_report_pdf_by_info_code,
)
from alphabee.utils.storage import get_data_root

# 下载 PDF 的默认落盘目录
DEFAULT_PDF_DIR = get_data_root() / "eastmoney_reports"


# ── 步骤 1：下载 ──────────────────────────────────────────────────────────


def download_report_pdf(
    *,
    info_code: str | None = None,
    encoded_url: str | None = None,
    pdf_url: str | None = None,
    dest_dir: str | Path | None = None,
    file_name: str | None = None,
    timeout: int = 60,
) -> Path:
    """从三种来源下载 PDF 财报/研报，返回本地路径。

    恰好提供一种来源：``info_code``（东方财富研报 infoCode）、
    ``encoded_url``（东方财富研报 encodeUrl）、``pdf_url``（任意直链）。
    """
    provided = [v for v in (info_code, encoded_url, pdf_url) if v]
    if len(provided) != 1:
        raise ValueError("Provide exactly one source: info_code, encoded_url, or pdf_url.")

    target_dir = Path(dest_dir).expanduser().resolve() if dest_dir else DEFAULT_PDF_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    if info_code is not None:
        output = download_eastmoney_report_pdf_by_info_code(
            info_code,
            save_dir=target_dir,
            filename=file_name,
            timeout=timeout,
        )
        if not output.get("downloaded") or not output.get("path"):
            raise RuntimeError(f"Failed to download report PDF by info_code: {info_code}")
        return Path(output["path"])

    if encoded_url is not None:
        output = download_eastmoney_report_pdf(
            encoded_url,
            save_dir=target_dir,
            filename=file_name,
            timeout=timeout,
        )
        if not output.get("downloaded") or not output.get("path"):
            raise RuntimeError(f"Failed to download report PDF by encoded_url: {encoded_url}")
        return Path(output["path"])

    # pdf_url 直链下载
    assert pdf_url is not None
    if not pdf_url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Unsupported PDF URL scheme: {pdf_url}")
    with requests.get(pdf_url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        content = response.content
    if not content.startswith(b"%PDF-"):
        raise ValueError(f"URL {pdf_url} does not contain a valid PDF file.")

    name = file_name or Path(pdf_url.split("?")[0]).name or "downloaded.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{Path(name).stem}.pdf"
    dest = target_dir / name
    dest.write_bytes(content)
    return dest


# ── 步骤 2：OCR ───────────────────────────────────────────────────────────


def ocr_markdown(
    pdf_path: str | Path,
    *,
    task_id: str | None = None,
    ocr_server_url: str | None = None,
    keep_pages: bool = True,
    include_content: bool = False,
) -> dict[str, Any]:
    """对 PDF 执行 OCR，返回结果 dict（含 markdown_path / page_count / task_id 等）。

    直接调用 MCP 工具函数（与 agent 通过 MCP 服务调用的行为完全一致），
    无需拉起子进程；产物持久化在 ``outputs/pdf_ocr/tasks/<task_id>/``。
    """
    result = _ocr_pdf_to_markdown_tool(
        pdf_path=str(Path(pdf_path).expanduser().resolve()),
        task_id=task_id or f"pipeline-{uuid.uuid4().hex[:12]}",
        ocr_server_url=ocr_server_url or DEFAULT_OCR_SERVER_URL,
        keep_pages=keep_pages,
        include_content=include_content,
    )
    return result.model_dump(mode="json")


# ── 步骤 3：解析成 reports/ 文件夹结构（report_parser.py） ────────────────


def parse_to_report_folder(
    markdown_path: str | Path,
    report_name: str,
    *,
    page_count: int | None = None,
    save_dir: str | Path | None = None,
    overwrite: bool = True,
) -> Path:
    """把清洗后的 Markdown 按章节解析成 ``reports/<报告名>/`` 文件夹结构。

    复用 :func:`alphabee.financial_report.report_parser.write_markdown_report_folder`：
    章节切分 + 页眉页脚 ngram 去重（提供 page_count 时）+ 目录树生成 + 全文副本。
    """
    md_path = Path(markdown_path).expanduser().resolve()
    if not md_path.is_file():
        raise FileNotFoundError(f"Markdown file does not exist: {md_path}")
    return write_markdown_report_folder(
        md_path.read_text(encoding="utf-8"),
        report_name,
        page_count=page_count,
        save_dir=save_dir,
        overwrite=overwrite,
    )


# ── 步骤 4：搜索回答问题 ──────────────────────────────────────────────────


async def answer_report_question(
    report_dir: str | Path,
    question: str,
    *,
    max_steps: int = 40,
) -> str:
    """在解析好的报告目录上搜索并回答问题。

    在 ``report_dir`` 上构建受限 deep agent（只能读取该目录内的 markdown），
    以 ``question`` 驱动检索并返回最终文本答案。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    report_path = Path(report_dir).expanduser().resolve()
    if not report_path.is_dir():
        raise FileNotFoundError(f"Report directory not found: {report_path}")

    agent = create_report_fetch_agent(report_path)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=question)]},
        config={"recursion_limit": max_steps},
    )
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            return msg.content if isinstance(msg.content, str) else ""
    return (
        "AGENT_NO_ANSWER: 报告检索代理未产出最终文本答案"
        "（可能问题过于宽泛、超出检索上限，或模型未收敛）。"
        "建议把问题收窄到「公司 + 报告期 + 具体指标/章节/事件」。"
    )


# ── 全链路编排 ────────────────────────────────────────────────────────────


async def run_report_pipeline(
    *,
    pdf_path: str | Path | None = None,
    info_code: str | None = None,
    encoded_url: str | None = None,
    pdf_url: str | None = None,
    report_name: str | None = None,
    question: str | None = None,
    task_id: str | None = None,
    ocr_server_url: str | None = None,
    keep_pages: bool = True,
    page_count: int | None = None,
    save_dir: str | Path | None = None,
    overwrite: bool = True,
    max_steps: int = 40,
    verbose: bool = True,
) -> dict[str, Any]:
    """执行完整链路：下载 → OCR → 解析成文件夹结构 → （可选）搜索回答问题。

    Args:
        pdf_path: 本地 PDF 路径（与 info_code/encoded_url/pdf_url 四选一）。
        info_code / encoded_url / pdf_url: 下载来源（东方财富研报 / 直链）。
        report_name: ``reports/`` 下的报告目录名；缺省取 PDF 文件名。
        question: 可选；提供时在解析结果上搜索并回答。
        task_id: OCR 任务 id（缺省自动生成）。
        ocr_server_url: PaddleOCR-VL vLLM 服务地址（缺省读 ``PADDLE_VL_SERVER_URL``）。
        keep_pages: 是否保留每页 OCR 原始结果。
        page_count: 覆盖 OCR 返回的页数（一般无需传，自动取 OCR 结果）。
        save_dir: ``reports/`` 根目录（缺省 ``<PROJECT_ROOT>/reports``）。
        overwrite: 同名报告目录已存在时是否覆盖。
        max_steps: 问答阶段检索步数上限。
        verbose: 是否打印各步骤进度。

    Returns:
        dict：``pdf_path`` / ``task_id`` / ``markdown_path`` / ``page_count`` /
        ``report_dir`` / ``answer``（仅提供 question 时）。
    """
    steps: list[dict[str, Any]] = []

    # ── 1) 下载 ──────────────────────────────────────────────────────────
    if pdf_path is None:
        if verbose:
            print(f"[1/4] 下载 PDF（info_code={info_code}, encoded_url={encoded_url}, pdf_url={pdf_url}）")
        pdf_path = download_report_pdf(
            info_code=info_code,
            encoded_url=encoded_url,
            pdf_url=pdf_url,
        )
    pdf_path = Path(pdf_path).expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")
    steps.append({"step": "download", "pdf_path": str(pdf_path)})

    # ── 2) OCR ───────────────────────────────────────────────────────────
    if verbose:
        print(f"[2/4] OCR：{pdf_path.name}")
    ocr_result = ocr_markdown(
        pdf_path,
        task_id=task_id,
        ocr_server_url=ocr_server_url,
        keep_pages=keep_pages,
    )
    markdown_path = ocr_result["markdown_path"]
    ocr_page_count = ocr_result.get("page_count") or 0
    steps.append(
        {
            "step": "ocr",
            "task_id": ocr_result["metadata"]["task_id"],
            "markdown_path": markdown_path,
            "page_count": ocr_page_count,
        }
    )

    # ── 3) 解析成 reports/ 文件夹结构（report_parser.py） ────────────────
    resolved_report_name = (report_name or pdf_path.stem).strip()
    if verbose:
        print(f"[3/4] 解析章节 → reports/{resolved_report_name}/")
    report_dir = parse_to_report_folder(
        markdown_path,
        resolved_report_name,
        page_count=page_count or ocr_page_count,
        save_dir=save_dir,
        overwrite=overwrite,
    )
    steps.append({"step": "parse", "report_dir": str(report_dir)})

    result: dict[str, Any] = {
        "pdf_path": str(pdf_path),
        "task_id": ocr_result["metadata"]["task_id"],
        "markdown_path": markdown_path,
        "page_count": ocr_page_count,
        "report_dir": str(report_dir),
        "report_name": resolved_report_name,
        "steps": steps,
    }

    # ── 4) 搜索回答问题 ─────────────────────────────────────────────────
    if question:
        if verbose:
            print(f"[4/4] 检索问答：{question}")
        result["answer"] = await answer_report_question(
            report_dir,
            question,
            max_steps=max_steps,
        )
        if verbose:
            print(f"    ↳ {result['answer'][:300]}{'…' if len(result['answer']) > 300 else ''}")

    if verbose:
        print("完成：", json.dumps({k: v for k, v in result.items() if k != "steps"}, ensure_ascii=False))
    return result


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m alphabee.financial_report.pipeline",
        description="财报处理全链路：下载 → OCR → 解析成 reports/ 文件夹结构 → 搜索回答问题",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--info-code", help="东方财富研报 infoCode")
    source.add_argument("--encoded-url", help="东方财富研报 encodeUrl")
    source.add_argument("--pdf-url", help="PDF 直链（http/https）")
    source.add_argument("--pdf-path", help="本地 PDF 文件路径")
    parser.add_argument("--report-name", help="reports/ 下的报告目录名（缺省取 PDF 文件名）")
    parser.add_argument("--question", help="在解析结果上搜索回答的问题（可选）")
    parser.add_argument("--ocr-server-url", default=None, help="PaddleOCR-VL vLLM 服务地址")
    parser.add_argument("--no-keep-pages", action="store_true", help="不保留每页 OCR 原始结果")
    parser.add_argument("--no-overwrite", action="store_true", help="同名报告目录已存在时不覆盖")
    parser.add_argument("--save-dir", default=None, help="reports/ 根目录（默认 <PROJECT_ROOT>/reports）")
    parser.add_argument("--max-steps", type=int, default=40, help="问答阶段检索步数上限")
    parser.add_argument("--quiet", action="store_true", help="不打印进度")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not any([args.info_code, args.encoded_url, args.pdf_url, args.pdf_path]):
        print("错误：必须提供 PDF 来源之一：--info-code / --encoded-url / --pdf-url / --pdf-path", file=sys.stderr)
        sys.exit(2)

    result = asyncio.run(
        run_report_pipeline(
            pdf_path=args.pdf_path,
            info_code=args.info_code,
            encoded_url=args.encoded_url,
            pdf_url=args.pdf_url,
            report_name=args.report_name,
            question=args.question,
            ocr_server_url=args.ocr_server_url,
            keep_pages=not args.no_keep_pages,
            save_dir=args.save_dir,
            overwrite=not args.no_overwrite,
            max_steps=args.max_steps,
            verbose=not args.quiet,
        )
    )
    if args.question and "answer" in result:
        print("\n==== 问答结果 ====")
        print(result["answer"])


if __name__ == "__main__":
    main()
