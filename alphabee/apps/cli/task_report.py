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
本模块是**独立的 markdown 渲染面**，服务"事后重读 / 归档 / 导出"场景。章节标题映射（含 ``company_track``）与占位符跳过口径
**对齐** ``render_final_report``（13 节），但输出为纯文本 markdown；两处口径的漂移由
``tests/apps/test_task_report.py::test_build_report_markdown_full``（13 标题齐全钉）与
``test_placeholder_sections_skipped``（占位符跳过钉）共同钉住。

纪律：

- 纯函数、禁 LLM、无网络：渲染只读记录字段，绝不触发任何外部调用；
- fail-open：``report_raw`` 缺失/空 → 渲染最小骨架 + 显式占位说明，**不抛**；
- 不修改记录、不写 ``data/task_records``（落盘只发生在显式 ``output`` 路径）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from alphabee.task_records.models import TaskRecord

# 章节 key → 展示标题（与 renderer.render_final_report 的 13 节口径对齐；
# 未知 key 按原样显示，新增章节不会被静默吞掉）。
_SECTION_TITLES: dict[str, str] = {
    "executive_summary": "核心观点摘要",
    "investment_viewpoint": "投资观点展开",
    "scenario_analysis": "情景分析",
    "key_metrics": "核心指标",
    "signal_analysis": "风险信号",
    "anomaly_detection": "勾稽关系异常",
    "conflict_analysis": "数据矛盾",
    "dimension_analysis": "维度分析",
    "company_track": "公司赛道与对标组",
    "review_findings": "审查发现",
    "falsification_conditions": "可证伪条件",
    "risks": "主要风险",
    "disclaimer": "免责声明",
}

# 占位符章节（值与 render_final_report 的跳过条件逐字对齐）：值是占位文案时
# 跳过渲染（终端与 markdown 两渲染面同口径，避免把"未生成"当正文输出）。
_PLACEHOLDER_VALUES: dict[str, str] = {
    "investment_viewpoint": "未生成独立投资观点，以下为结构化数据分析。",
    "scenario_analysis": "未生成情景分析。",
    "conflict_analysis": "未检测到显著数据矛盾，多维度指标之间逻辑自洽。",
    "review_findings": "未执行审查",
    "falsification_conditions": "无可证伪条件。",
}

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
        # 状态证据 V2 design §6.9：新记录才追加 formula / 不确定 / 顶背离（旧记录保持原行，
        # 不出现空标签）。顶背离仅在 severity > 0 时展示——0.0 表示「无顶背离」，不打印噪声值。
        if record.midterm_formula:
            line += f" · formula {record.midterm_formula}"
            line += f" · 不确定 {'是' if record.midterm_uncertain else '否'}"
            if record.midterm_adverse_severity > 0.0:
                line += f" · 顶背离 {record.midterm_adverse_severity:.2f}"
        lines.append(line)
    return lines


def build_report_markdown(record: TaskRecord) -> str:
    """把单条 :class:`TaskRecord` 渲染为纯 markdown 报告（确定性、无 ANSI、禁 LLM）。

    Args:
        record: ``TaskStore.load(task_id)`` 载入的记录。

    Returns:
        markdown 字符串（末尾恰好一个换行）；``report_raw`` 缺失/空时返回
        最小骨架 + 显式占位说明（fail-open，不抛）。
    """
    raw = record.report_raw or {}
    title = str(raw.get("title") or record.query or "投资分析报告")

    parts: list[str] = [f"# {title}", ""]
    parts.extend(_meta_lines(record, raw))
    parts.append("")

    summary = str(raw.get("summary") or "").strip()
    if summary:
        parts.append("> " + summary.replace("\n", "\n> "))
        parts.append("")

    sections = raw.get("sections") or {}
    if not raw:
        parts.append(f"_{_MISSING_REPORT_NOTE}_")
        parts.append("")
    for key, value in sections.items():
        if value is None:
            continue
        text = value.strip() if isinstance(value, str) else str(value)
        if not text:
            continue
        if text == _PLACEHOLDER_VALUES.get(key):
            continue  # 占位文案不渲染（与终端渲染同口径）
        parts.append(f"## {_SECTION_TITLES.get(key, key)}")
        parts.append("")
        parts.append(text)
        parts.append("")

    issues = list(record.issues or [])
    if issues:
        parts.append("## 系统问题")
        parts.append("")
        for issue in issues:
            parts.append(f"- [{issue.severity}] {issue.category}: {issue.message}")
        parts.append("")
        parts.append(f"共 {len(issues)} 个问题")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


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
