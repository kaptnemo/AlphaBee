"""财报处理全链路：（可选）获取链接 → 下载 → OCR → 解析成文件夹结构 → 搜索回答问题。

把「（可选）按公司获取财报/研报下载链接 → 下载 PDF → OCR 提取 Markdown →
按章节解析成 ``reports/`` 文件夹结构（复用 :mod:`alphabee.financial_report.report_parser`）
→ 在解析结果上搜索回答问题」串成一条可直接调用的管线，并提供 CLI。

典型用法（CLI）::

    # 按公司获取最新财报下载链接 → 下载 + OCR + 解析 + 问答
    python -m alphabee.financial_report.pipeline \\
        --company-code 300750 --company-name 宁德时代 \\
        --link-kind financial --report-type semiannual \\
        --question "宁德时代 2026 年上半年营业收入和净利润分别是多少？"

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

    results = await run_report_pipeline(
        company_code="300750",
        company_name="宁德时代",
        link_kind="financial",
        report_type="semiannual",
        question="海外业务的最新进展如何？",
    )
    for r in results:
        print(r["report_name"], "→", r["answer"])

管线各步产物：
- 获取链接：``get_report_links``（财报走巨潮、研报走东方财富，仅产出 URL 不落地）
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
from alphabee.financial_report.links import (
    get_financial_report_links,
    get_research_report_links,
)
from alphabee.financial_report.report_parser import reports_root, write_markdown_report_folder
from alphabee.loader.pdf_ocr_loader import DEFAULT_OCR_SERVER_URL
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


# ── 步骤 0：获取下载链接（links.py） ──────────────────────────────────────


def get_report_links(
    *,
    kind: str = "financial",
    code: str | None = None,
    name: str | None = None,
    report_type: str | list[str] | None = "all",
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int = 20,
    page_num: int = 1,
    timeout: int = 20,
) -> dict[str, Any]:
    """获取公司财报或研报的下载链接（只产出链接，不落地文件）。

    统一的链接获取入口，按 ``kind`` 分发到 :func:`get_financial_report_links`
    （财报/定期报告，巨潮）或 :func:`get_research_report_links`（券商研报，东方财富）。

    Args:
        kind: ``"financial"``（财报/定期报告公告）或 ``"research"``（券商研报）。
        code: 股票代码（``300750`` 或 ``300750.SZ``）；研报必须提供 code，财报可用 name 替代。
        name: 公司简称（如 ``宁德时代``）；仅 ``kind="financial"`` 生效。
        report_type: 财报报告类型（``all``/``annual``/``semiannual``/``q1``/``q3``
            及别名）；仅 ``kind="financial"`` 生效。
        start_date / end_date: 披露/发布日期范围（``YYYY-MM-DD``，可选）。
        page_size / page_num: 分页（研报用）。
        timeout: 单次请求超时（秒）。

    Returns:
        dict：``source`` / ``code`` / ``name``（财报）或 ``has_next``（研报）/
        ``count`` / ``reports``；``reports`` 每条含 ``title`` / ``date`` /
        ``download_url`` 等。
    """
    if kind == "research":
        if not code:
            raise ValueError("研报链接查询需提供 code。")
        return get_research_report_links(
            code,
            start_date=start_date,
            end_date=end_date,
            page_size=page_size,
            page_num=page_num,
            timeout=timeout,
        )
    return get_financial_report_links(
        code=code,
        name=name,
        report_type=report_type,
        start_date=start_date,
        end_date=end_date,
        page_size=page_size,
        timeout=timeout,
    )


def pick_report_links(links: dict[str, Any]) -> list[dict[str, Any]]:
    """从 ``get_report_links`` 的结果里挑出可下载的完整报告链接（按日期降序）。

    过滤掉无 ``download_url`` 或标题含「摘要」的条目，返回全部符合条件的链接。
    """
    reports = links.get("reports") or []
    full_reports = [r for r in reports if r.get("download_url") and "摘要" not in r.get("title", "")]
    if not full_reports:
        raise ValueError("未获取到任何可下载的完整报告链接（可能无该类型报告，或日期范围过窄）。")
    return full_reports


# ── 已处理记录（去重：记录已处理的报告，重跑时跳过） ─────────────────────

PROCESSED_RECORD_FILENAME = ".processed_reports.json"


def _processed_record_path(save_dir: str | Path | None) -> Path:
    return reports_root(save_dir) / PROCESSED_RECORD_FILENAME


def _load_processed(save_dir: str | Path | None) -> set[str]:
    """读取已处理报告记录（以 ``download_url`` 为键），文件缺失/损坏时返回空集。"""
    path = _processed_record_path(save_dir)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, dict):
        return {str(k) for k in data}
    if isinstance(data, list):
        return {str(k) for k in data}
    return set()


def _save_processed(save_dir: str | Path | None, processed: set[str]) -> None:
    """把已处理报告记录写回磁盘（download_url 列表）。"""
    path = _processed_record_path(save_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sorted(processed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
    result: dict[str, Any] = _ocr_pdf_to_markdown_tool(
        pdf_path=str(Path(pdf_path).expanduser().resolve()),
        task_id=task_id or f"pipeline-{uuid.uuid4().hex[:12]}",
        ocr_server_url=ocr_server_url or DEFAULT_OCR_SERVER_URL,
        keep_pages=keep_pages,
        include_content=include_content,
    ).model_dump(mode="json")
    return result


# ── 步骤 3：解析成 reports/ 文件夹结构（report_parser.py） ────────────────


def parse_to_report_folder(
    markdown_path: str | Path,
    report_name: str,
    *,
    company_name: str | None = None,
    company_code: str | None = None,
    page_count: int | None = None,
    save_dir: str | Path | None = None,
    overwrite: bool = True,
) -> Path:
    """把清洗后的 Markdown 按章节解析成报告文件夹结构。

    复用 :func:`alphabee.financial_report.report_parser.write_markdown_report_folder`：
    章节切分 + 页眉页脚 ngram 去重（提供 page_count 时）+ 目录树生成 + 全文副本。

    提供 ``company_name`` 时输出新嵌套结构
    ``<save_dir>/<公司名>(<代码>)/财报/<报告期+类型>/``，否则输出旧平铺结构
    ``<save_dir>/<报告名>/``（向后兼容）。
    """
    md_path = Path(markdown_path).expanduser().resolve()
    if not md_path.is_file():
        raise FileNotFoundError(f"Markdown file does not exist: {md_path}")
    return write_markdown_report_folder(
        md_path.read_text(encoding="utf-8"),
        report_name,
        company_name=company_name,
        company_code=company_code,
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
    company_name: str | None = None,
    company_code: str | None = None,
    link_kind: str = "financial",
    report_type: str | list[str] | None = "all",
    link_start_date: str | None = None,
    link_end_date: str | None = None,
    question: str | None = None,
    task_id: str | None = None,
    ocr_server_url: str | None = None,
    keep_pages: bool = True,
    page_count: int | None = None,
    save_dir: str | Path | None = None,
    overwrite: bool = True,
    max_steps: int = 40,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """执行完整链路：（可选）获取链接 → 下载 → OCR → 解析成文件夹结构 → （可选）问答。

    下载来源优先级：

    1. ``pdf_path``：本地 PDF，直接跳过下载。
    2. ``info_code`` / ``encoded_url`` / ``pdf_url``：显式下载来源（单条）。
    3. ``company_code`` / ``company_name``：未给显式 PDF 来源时，先按 ``link_kind``
       获取下载链接，再逐个处理全部符合条件、且未处理过的链接。

    链接模式下会记录已处理的报告（以 ``download_url`` 为键，持久化在
    ``<save_dir>/.processed_reports.json``），重跑时自动跳过已处理过的链接。

    Args:
        pdf_path: 本地 PDF 路径（与其它下载来源二选一）。
        info_code / encoded_url / pdf_url: 下载来源（东方财富研报 / 直链）。
        report_name: ``reports/`` 下的报告目录名；缺省取 PDF 文件名（链接模式下取链接标题）。
        company_name / company_code: 提供 ``company_name`` 时报告目录落入
            ``<save_dir>/<公司名>(<代码>)/财报/<报告期>/`` 嵌套结构；无显式 PDF 来源时
            也用于获取下载链接。
        link_kind: 链接类型 ``"financial"``（财报/巨潮）或 ``"research"``（研报/东方财富）。
        report_type: 财报报告类型（仅 ``link_kind="financial"`` 生效）。
        link_start_date / link_end_date: 链接披露/发布日期范围（``YYYY-MM-DD``）。
        question: 可选；提供时在每份解析结果上分别搜索并回答。
        task_id: OCR 任务 id（缺省自动生成；多条任务时忽略传入值，各自自动生成）。
        ocr_server_url: PaddleOCR-VL vLLM 服务地址（缺省读 ``PADDLE_VL_SERVER_URL``）。
        keep_pages: 是否保留每页 OCR 原始结果。
        page_count: 覆盖 OCR 返回的页数（一般无需传，自动取 OCR 结果）。
        save_dir: ``reports/`` 根目录（缺省 ``<PROJECT_ROOT>/reports``）。
        overwrite: 同名报告目录已存在时是否覆盖。
        max_steps: 问答阶段检索步数上限。
        verbose: 是否打印各步骤进度。

    Returns:
        list[dict]：每个报告一条结果，每条含 ``report_name`` / ``pdf_path`` /
        ``task_id`` / ``markdown_path`` / ``page_count`` / ``report_dir`` /
        ``steps`` / ``answer``（仅提供 question 时）/ ``link``（链接模式时）。
    """
    # ── 构造待处理任务列表 ─────────────────────────────────────────────
    tasks: list[dict[str, Any]] = []
    processed: set[str] = set()

    if pdf_path is not None:
        tasks = [{"kind": "local", "pdf_path": str(pdf_path), "title": report_name or Path(pdf_path).stem}]
    elif info_code or encoded_url or pdf_url:
        tasks = [
            {
                "kind": "explicit",
                "info_code": info_code,
                "encoded_url": encoded_url,
                "pdf_url": pdf_url,
                "title": report_name or "",
            }
        ]
    elif company_code or company_name:
        if verbose:
            print(f"[链接] 获取下载链接（kind={link_kind}，code={company_code}，name={company_name}）")
        links = get_report_links(
            kind=link_kind,
            code=company_code,
            name=company_name,
            report_type=report_type,
            start_date=link_start_date,
            end_date=link_end_date,
        )
        all_links = pick_report_links(links)
        processed = _load_processed(save_dir)
        picked_links = [lnk for lnk in all_links if (lnk.get("download_url") or "") not in processed]
        if verbose:
            skipped = len(all_links) - len(picked_links)
            print(f"       共 {len(all_links)} 条可下载报告，已处理 {skipped} 条，待处理 {len(picked_links)} 条")
        for link in picked_links:
            tasks.append(
                {
                    "kind": "link",
                    "download_url": link.get("download_url"),
                    "title": link.get("title") or "",
                    "link": link,
                }
            )
    else:
        raise ValueError(
            "必须提供 PDF 来源之一（pdf_path / info_code / encoded_url / pdf_url）"
            "或公司信息（company_code / company_name，用于获取下载链接）。"
        )

    # ── 逐报告处理：下载 → OCR → 解析 → （问答） ───────────────────────
    results: list[dict[str, Any]] = []
    for i, task in enumerate(tasks):
        if verbose:
            print(f"[{i + 1}/{len(tasks)}] 处理报告：{task['title'] or '(未命名)'}")

        steps: list[dict[str, Any]] = []

        # 1) 下载
        if task["kind"] == "local":
            pdf_file = Path(task["pdf_path"]).expanduser().resolve()
        elif task["kind"] == "link":
            download_url = task["download_url"]
            if not download_url:
                raise ValueError(f"选中的链接无 download_url：{task['title']}")
            if verbose:
                print(f"    下载 PDF：{download_url}")
            pdf_file = download_report_pdf(pdf_url=download_url)
        else:  # explicit
            if verbose:
                print(
                    f"    下载 PDF（info_code={task['info_code']}, encoded_url={task['encoded_url']}, pdf_url={task['pdf_url']}）"
                )
            pdf_file = download_report_pdf(
                info_code=task["info_code"],
                encoded_url=task["encoded_url"],
                pdf_url=task["pdf_url"],
            )
        pdf_file = Path(pdf_file).expanduser().resolve()
        if not pdf_file.is_file():
            raise FileNotFoundError(f"PDF file does not exist: {pdf_file}")
        steps.append({"step": "download", "pdf_path": str(pdf_file)})

        # 2) OCR（多条任务时各自自动生成独立 task_id，避免互相覆盖）
        if verbose:
            print(f"    OCR：{pdf_file.name}")
        ocr_task_id = task_id if len(tasks) == 1 else None
        ocr_result = ocr_markdown(
            pdf_file,
            task_id=ocr_task_id,
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

        # 3) 解析
        resolved_report_name = (task["title"] or report_name or pdf_file.stem).strip()
        if verbose:
            print(f"    解析章节 → reports/{resolved_report_name}/")
        report_dir = parse_to_report_folder(
            markdown_path,
            resolved_report_name,
            company_name=company_name,
            company_code=company_code,
            page_count=page_count or ocr_page_count,
            save_dir=save_dir,
            overwrite=overwrite,
        )
        steps.append({"step": "parse", "report_dir": str(report_dir)})

        # 链接模式：解析成功后记录为已处理，避免重跑时重复处理
        if task["kind"] == "link":
            processed.add(task["download_url"])
            _save_processed(save_dir, processed)

        result: dict[str, Any] = {
            "report_name": resolved_report_name,
            "pdf_path": str(pdf_file),
            "task_id": ocr_result["metadata"]["task_id"],
            "markdown_path": markdown_path,
            "page_count": ocr_page_count,
            "report_dir": str(report_dir),
            "steps": steps,
        }
        if task["kind"] == "link":
            result["link"] = task["link"]

        # 4) 问答
        if question:
            if verbose:
                print(f"    检索问答：{question}")
            result["answer"] = await answer_report_question(
                report_dir,
                question,
                max_steps=max_steps,
            )
            if verbose:
                print(f"    ↳ {result['answer'][:300]}{'…' if len(result['answer']) > 300 else ''}")

        results.append(result)

    if verbose:
        print(
            "完成：",
            json.dumps([{k: v for k, v in r.items() if k != "steps"} for r in results], ensure_ascii=False),
        )
    return results


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m alphabee.financial_report.pipeline",
        description="财报处理全链路：（可选）获取链接 → 下载 → OCR → 解析成 reports/ 文件夹结构 → 搜索回答问题",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--info-code", help="东方财富研报 infoCode")
    source.add_argument("--encoded-url", help="东方财富研报 encodeUrl")
    source.add_argument("--pdf-url", help="PDF 直链（http/https）")
    source.add_argument("--pdf-path", help="本地 PDF 文件路径")
    parser.add_argument("--report-name", help="reports/ 下的报告目录名（缺省取 PDF 文件名/链接标题）")
    parser.add_argument(
        "--company-name",
        help="公司中文简称（提供时报告目录落入 <公司名>(<代码>)/财报/ 嵌套结构；无 PDF 来源时用于获取链接）",
    )
    parser.add_argument("--company-code", help="6 位股票代码，如 300750（无 PDF 来源时用于获取链接）")
    parser.add_argument(
        "--link-kind",
        choices=["financial", "research"],
        default="financial",
        help="链接类型：financial=财报（巨潮）、research=研报（东方财富）",
    )
    parser.add_argument(
        "--report-type", default="all", help="财报报告类型：all/annual/semiannual/q1/q3（仅 financial 生效）"
    )
    parser.add_argument("--link-start-date", default=None, help="链接披露/发布日期范围起（YYYY-MM-DD）")
    parser.add_argument("--link-end-date", default=None, help="链接披露/发布日期范围止（YYYY-MM-DD）")
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
    if not any(
        [
            args.info_code,
            args.encoded_url,
            args.pdf_url,
            args.pdf_path,
            args.company_code,
            args.company_name,
        ]
    ):
        print(
            "错误：必须提供 PDF 来源之一（--info-code / --encoded-url / --pdf-url / --pdf-path）"
            "或公司信息（--company-code / --company-name，用于获取下载链接）",
            file=sys.stderr,
        )
        sys.exit(2)

    results = asyncio.run(
        run_report_pipeline(
            pdf_path=args.pdf_path,
            info_code=args.info_code,
            encoded_url=args.encoded_url,
            pdf_url=args.pdf_url,
            report_name=args.report_name,
            company_name=args.company_name,
            company_code=args.company_code,
            link_kind=args.link_kind,
            report_type=args.report_type,
            link_start_date=args.link_start_date,
            link_end_date=args.link_end_date,
            question=args.question,
            ocr_server_url=args.ocr_server_url,
            keep_pages=not args.no_keep_pages,
            save_dir=args.save_dir,
            overwrite=not args.no_overwrite,
            max_steps=args.max_steps,
            verbose=not args.quiet,
        )
    )
    if args.question:
        print("\n==== 问答结果 ====")
        for r in results:
            print(f"\n【{r['report_name']}】\n{r['answer']}")
    else:
        for r in results:
            print(f"\n报告目录：{r['report_dir']}")


if __name__ == "__main__":
    main()
