"""task record → 最终报告渲染（CLI ``--task-report``）。

从 ``data/task_records`` 持久化的 :class:`~alphabee.task_records.models.TaskRecord`
**直接重建最终投资分析报告**，不重跑流水线：

- **数据面**：``TaskRecord.report_raw`` 即最终报告的完整 JSON（13 章节 markdown），
  配合 ``issues`` / ``midterm_*`` 元信息，重渲染所需的材料全部在记录里；
- **渲染面**：:func:`build_report_markdown` 产**纯 markdown**（无 ANSI），
  终端输出与落盘文件共用同一字符串（字节一致，可测）；
- **落盘面**：:func:`render_task_record_report` 在给定 ``output`` 时写 ``.md`` 文件
  （父目录自动创建）。

与 :func:`alphabee.apps.cli.renderer.render_final_report`（运行时流式终端渲染）的关系：
本模块是**独立的 markdown 渲染面**，服务"事后重读 / 归档 / 导出"场景。章节标题/顺序/
占位符口径**统一由** :mod:`alphabee.apps.cli.report_layout` 提供（legacy 13 节 / memo
主文六节 + 附录 A–F），两个渲染面不再各自维护映射；两处口径由
``tests/apps/test_task_report.py``（13 标题齐全 / 占位符跳过 / legacy 字节不变）与
``tests/apps/test_report_layout.py``（布局顺序与冻结哈希）共同钉住。

纪律：

- 纯函数、禁 LLM、无网络：渲染只读记录字段，绝不触发任何外部调用；
- fail-open：``report_raw`` 缺失/空 → 渲染最小骨架 + 显式占位说明，**不抛**；
- 不修改记录、不写 ``data/task_records``（落盘只发生在显式 ``output`` 路径）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from alphabee.apps.cli.report_layout import (
    APPENDIX_G_TITLE,
    APPENDIX_H_TITLE,
    APPENDIX_SECTIONS,
    ISSUE_DIGEST_TITLE,
    MAIN_SECTIONS,
    OVERVIEW_SECTIONS,
    PLACEHOLDER_VALUES,
    normalize_layout,
    ordered_blocks,
    render_issue_digest,
)
from alphabee.task_records.models import TaskRecord

_MISSING_REPORT_NOTE = "（该记录未包含最终报告：report_raw 为空）"


def _meta_lines(record: TaskRecord, raw: dict[str, Any]) -> list[str]:
    """元信息行：置信度 / 风险分布 / 任务 / 标的 / 时间 / 中期决策。"""
    lines: list[str] = []
    confidence = str(raw.get("overall_confidence") or record.overall_confidence or "unknown")
    lines.append(f"**整体置信度**: {confidence}")

    risk = raw.get("risk_count") or {}
    risk_parts = [
        f"{label} {risk[key]}"
        for label, key in (("高风险", "high"), ("中风险", "medium"), ("低风险", "low"), ("阻塞", "blocked"))
        if risk.get(key)
    ]
    if risk_parts:
        lines.append("**风险分布**: " + " ".join(risk_parts))

    lines.append(f"**任务**: `{record.task_id}`")
    if record.symbol:
        lines.append(f"**标的**: {record.symbol}")
    lines.append(f"**时间**: {record.timestamp}")

    if record.midterm_state or record.midterm_confidence or record.midterm_evidence_count:
        line = (
            f"**中期决策**: 状态 {record.midterm_state or '—'} "
            f"置信度 {record.midterm_confidence:.2f} "
            f"证据 {record.midterm_evidence_count} 条"
        )
        # 状态证据 V2 design §6.9：新记录才追加计算口径 / 不确定 / 顶背离（旧记录保持原行，
        # 不出现空标签）。口径 = 顺序 + 公式版本，成对展示（二者共同决定分布，不可只显示其一）。
        # 顶背离仅在 severity > 0 时展示——0.0 表示「无顶背离」，不打印噪声值。
        provenance = " · ".join(
            part
            for part in (
                f"顺序 {record.midterm_classifier_order}" if record.midterm_classifier_order else "",
                f"formula {record.midterm_formula_revision}" if record.midterm_formula_revision else "",
            )
            if part
        )
        if provenance:
            line += f" · {provenance}"
            line += f" · 不确定 {'是' if record.midterm_uncertain else '否'}"
            if record.midterm_adverse_severity > 0.0:
                line += f" · 顶背离 {record.midterm_adverse_severity:.2f}"
        lines.append(line)
    return lines


def _issue_field(issue: Any, name: str, default: str = "") -> str:
    """读 issue 字段：兼容 ``IssueRecord`` / ``Issue`` 模型与 issues JSON dict。"""
    if isinstance(issue, Mapping):
        return str(issue.get(name) or default)
    return str(getattr(issue, name, None) or default)


def _append_section(parts: list[str], title: str, text: str) -> None:
    parts.append(f"## {title}")
    parts.append("")
    parts.append(text)
    parts.append("")


def _append_legacy_sections(
    parts: list[str],
    sections: Any,
    *,
    issues: Any,
    midterm: Any,
    midterm_time_points: Any,
) -> None:
    """legacy 渲染（与 Q1-lite 前逐字节一致）：13 节 → 系统问题全量 → 中期块。"""
    for block in ordered_blocks(sections, "legacy"):
        # markdown 面按 strip 后文本判空/比占位符（HEAD 语义；过滤职责在消费者）。
        if not block.text:
            continue
        if block.text == PLACEHOLDER_VALUES.get(block.key):
            continue
        _append_section(parts, block.title, block.text)

    issue_list = list(issues or [])
    if issue_list:
        parts.append("## 系统问题")
        parts.append("")
        for issue in issue_list:
            parts.append(
                f"- [{_issue_field(issue, 'severity', '?')}] "
                f"{_issue_field(issue, 'category', '?')}: {_issue_field(issue, 'message', '')}"
            )
        parts.append("")
        parts.append(f"共 {len(issue_list)} 个问题")
        parts.append("")

    if midterm:
        from alphabee.orchestrator.nodes.midterm_reporter import (
            ReportTimePoints,
            render_midterm_decision_summary,
        )

        time_points = midterm_time_points if midterm_time_points is not None else ReportTimePoints()
        parts.append(render_midterm_decision_summary(midterm, time_points=time_points))


def _append_memo_sections(
    parts: list[str],
    sections: Any,
    *,
    issues: Any,
    midterm: Any,
    midterm_time_points: Any,
) -> None:
    """memo 渲染：主文六节 → 审查摘要 → 附录 A–F → **附录 G/H** → 免责声明。

    折叠顺序（设计 §3.2.3/§3.2.4）：系统问题主文只留 critical/high 前 5 条摘要 + 计数行，
    全量进附录 G；中期决策完整控制台块进附录 H；免责声明始终最后。未知 section key
    保持原位（附录 A–F 之后、附录 G 之前）透传，fail-open 不丢内容。公司概况由
    :func:`render_report_dict` 在摘要之前单独渲染，这里跳过。
    """
    blocks = ordered_blocks(sections, "memo")
    main_blocks = [block for block in blocks if block.key in MAIN_SECTIONS]
    appendix_blocks = [block for block in blocks if block.key in APPENDIX_SECTIONS]
    unknown_blocks = [
        block
        for block in blocks
        if block.key not in MAIN_SECTIONS
        and block.key not in APPENDIX_SECTIONS
        and block.key not in OVERVIEW_SECTIONS
        and block.key != "disclaimer"
    ]
    disclaimer_block = next((block for block in blocks if block.key == "disclaimer"), None)

    def _iter_renderable(candidates: list[Any]) -> list[Any]:
        renderable = []
        for block in candidates:
            if not block.text:
                continue
            if block.text == PLACEHOLDER_VALUES.get(block.key):
                continue
            renderable.append(block)
        return renderable

    for block in _iter_renderable(main_blocks):
        _append_section(parts, block.title, block.text)

    issue_list = list(issues or [])
    digest, appendix_issues = render_issue_digest(issue_list)
    if digest:
        _append_section(parts, ISSUE_DIGEST_TITLE, digest)

    for block in _iter_renderable(appendix_blocks):
        _append_section(parts, block.title, block.text)
    for block in _iter_renderable(unknown_blocks):
        _append_section(parts, block.title, block.text)

    if appendix_issues:
        _append_section(parts, APPENDIX_G_TITLE, appendix_issues)

    if midterm:
        from alphabee.orchestrator.nodes.midterm_reporter import (
            ReportTimePoints,
            render_midterm_decision_summary,
        )

        time_points = midterm_time_points if midterm_time_points is not None else ReportTimePoints()
        _append_section(
            parts,
            APPENDIX_H_TITLE,
            render_midterm_decision_summary(midterm, time_points=time_points),
        )

    if disclaimer_block is not None and disclaimer_block.text:
        _append_section(parts, disclaimer_block.title, disclaimer_block.text)


def render_report_dict(
    report_raw: dict[str, Any] | None,
    *,
    issues: Any = None,
    midterm: Any = None,
    meta_lines: Any = (),
    midterm_time_points: Any = None,
    fallback_title: str = "投资分析报告",
) -> str:
    """按报告 dict 渲染纯 markdown（纯函数、确定性、禁 LLM/网络）。

    ``build_report_markdown`` 与 A/B 脚本共用该入口，避免复制标题/顺序/占位符逻辑：
    ``layout`` 从 ``report_raw["layout"]`` 读取；legacy 输出与 Q1-lite 前逐字节一致；
    memo 按「主文六节 → 审查摘要 → 附录 A–F → 附录 G/H → 免责声明」输出折叠块。

    Args:
        report_raw: 报告 JSON（含 title/summary/sections/layout）。
        issues: issue 模型/dict 列表（memo 走 :func:`render_issue_digest`；legacy 全量输出）。
        midterm: 中期决策 artifact（``CompanyStateArtifact``，可为 None）。
        meta_lines: 头部元信息行（``build_report_markdown`` 传入记录级元信息；A/B 脚本为空）。
        midterm_time_points: 中期块头部时点（缺省 ``ReportTimePoints()``）。
        fallback_title: ``report_raw`` 无 title 时的标题兜底。
    """
    raw = dict(report_raw or {})
    title = str(raw.get("title") or fallback_title)
    parts: list[str] = [f"# {title}", ""]
    parts.extend(meta_lines)
    parts.append("")

    sections = raw.get("sections") or {}
    layout = normalize_layout(raw.get("layout"))
    if layout == "memo":
        # memo 专属：公司概况放在摘要之前，供不了解公司的读者建立背景。
        overview = next(
            (block for block in ordered_blocks(sections, "memo") if block.key in OVERVIEW_SECTIONS),
            None,
        )
        if overview is not None and overview.text and overview.text != PLACEHOLDER_VALUES.get(overview.key):
            _append_section(parts, overview.title, overview.text)

    summary = str(raw.get("summary") or "").strip()
    if summary:
        parts.append("> " + summary.replace("\n", "\n> "))
        parts.append("")

    if not raw:
        parts.append(f"_{_MISSING_REPORT_NOTE}_")
        parts.append("")
    if layout == "memo":
        _append_memo_sections(
            parts,
            sections,
            issues=issues,
            midterm=midterm,
            midterm_time_points=midterm_time_points,
        )
    else:
        _append_legacy_sections(
            parts,
            sections,
            issues=issues,
            midterm=midterm,
            midterm_time_points=midterm_time_points,
        )
    return "\n".join(parts).rstrip() + "\n"


def build_report_markdown(record: TaskRecord) -> str:
    """把单条 :class:`TaskRecord` 渲染为纯 markdown 报告（确定性、无 ANSI、禁 LLM）。

    Args:
        record: ``TaskStore.load(task_id)`` 载入的记录。

    Returns:
        markdown 字符串（末尾恰好一个换行）；``report_raw`` 缺失/空时返回
        最小骨架 + 显式占位说明（fail-open，不抛）。
    """
    raw = record.report_raw or {}
    time_points = None
    if record.midterm:
        from alphabee.orchestrator.nodes.midterm_reporter import ReportTimePoints

        # P2-6：两个口径的时点随记录落库，这里与运行期渲染复用同一结构；旧记录为空 ⇒
        # 渲染「（未提供）」，不做决策日 / 采集日兜底。
        time_points = ReportTimePoints(
            financial_period=record.financial_period,
            valuation_as_of=record.valuation_as_of,
            valuation_close=record.valuation_close,
            valuation_pe_ttm=record.valuation_pe_ttm,
            valuation_pb=record.valuation_pb,
        )
    return render_report_dict(
        raw,
        issues=record.issues or [],
        midterm=record.midterm,
        meta_lines=_meta_lines(record, raw),
        midterm_time_points=time_points,
        fallback_title=str(record.query or "投资分析报告"),
    )


def default_report_output(record: TaskRecord) -> Path:
    """自动落盘路径：``<data_root>/reports/<标的或任务>-<日期>.md``。"""
    from alphabee.utils.storage import get_data_root, normalize_symbol

    label = normalize_symbol(record.symbol or record.task_id)
    date = (record.timestamp or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    return get_data_root() / "reports" / f"{label}-{date}.md"


def render_task_record_report(record: TaskRecord, *, output: str | Path | None = None) -> str:
    """渲染入口：``--task-report`` 用。

    ``output`` 非空 → 写 ``.md`` 文件（父目录自动创建）并打印落盘路径；
    缺省 → 只返回字符串，由调用方打印到终端。终端与文件共用同一字符串
    （字节一致，不因目标面而变）。
    """
    markdown = build_report_markdown(record)
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    return markdown
