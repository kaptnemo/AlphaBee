#!/usr/bin/env python3
"""报告基线统计（Q0，设计 ``REPORT_CLAIM_FIRST_AND_PARAMS_GOVERNANCE_DESIGN`` §1.1/§1.6）。

把报告"体积被什么占据"的口径固化成可复现脚本：字符构成、叙事/明细节占比、
系统问题与中期决策块占比、数字 token 密度、issue 数量与最长消息。产物是
``baseline.csv`` / ``baseline.md`` / ``baseline.json``，供 Q1-lite 的 go/no-go 对照。

两种输入（口径不同，输出与文档均显式标注）：

- ``--reports <dir>``（默认 ``data/reports``）：直接统计归档 markdown 原文；
- ``--task-records <dir>``（设计默认输入 ``data/task_records``）：反序列化
  :class:`~alphabee.task_records.models.TaskRecord`，用
  :func:`alphabee.apps.cli.task_report.build_report_markdown` 渲染后统计。
  渲染结果与归档 md 存在差异（标题/元信息行按记录字段重建、中期块按当前渲染器
  重绘），两口径的 ``total_chars`` 不可直接互比；``issue_count`` /
  ``issue_max_msg_chars`` 在任务记录口径直接取 ``record.issues``。

纪律：纯函数（切分/统计/渲染可单测）、禁 LLM、禁网络、只读（不写 ``data/``，
不改 TaskRecord）；脚本入口只做参数解析与 IO。

用法::

    python scripts/report_baseline.py --reports data/reports --format all --out tmp/report_baseline/
    python scripts/report_baseline.py --task-records data/task_records --format csv --out tmp/report_baseline/
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alphabee.apps.cli.task_report import build_report_markdown
from alphabee.task_records.models import TaskRecord

SCHEMA_VERSION = "1"

#: 默认输入/输出路径（与设计 §1.6 一致）。
DEFAULT_REPORTS_DIR = Path("data/reports")
DEFAULT_TASK_RECORDS_DIR = Path("data/task_records")
DEFAULT_OUT_DIR = Path("tmp/report_baseline")
DEFAULT_OUTPUT_STEM = "baseline"

METHOD_REPORTS = "reports"
METHOD_TASK_RECORDS = "task_records"
KNOWN_LAYOUTS = ("legacy", "memo")

#: 13 节 key（归档/legacy 渲染顺序；也是 body13 的统计集合）。
SECTION_KEYS: tuple[str, ...] = (
    "executive_summary",
    "investment_viewpoint",
    "scenario_analysis",
    "key_metrics",
    "signal_analysis",
    "anomaly_detection",
    "conflict_analysis",
    "dimension_analysis",
    "company_track",
    "review_findings",
    "falsification_conditions",
    "risks",
    "disclaimer",
)

#: 叙事节（§1.1）：写判断/结论的 7 个 section。
NARRATIVE_KEYS: tuple[str, ...] = (
    "executive_summary",
    "investment_viewpoint",
    "scenario_analysis",
    "key_metrics",
    "falsification_conditions",
    "risks",
    "disclaimer",
)

#: 明细节（§1.1）：逐条罗列引擎结果的 6 个 section。
DETAIL_KEYS: tuple[str, ...] = (
    "signal_analysis",
    "anomaly_detection",
    "conflict_analysis",
    "dimension_analysis",
    "company_track",
    "review_findings",
)

#: memo 主文六节（设计 §3.1；免责声明不计入主文，归主文尾的独立小节）。
MAIN_SECTION_KEYS: tuple[str, ...] = (
    "executive_summary",
    "investment_viewpoint",
    "scenario_analysis",
    "risks",
    "falsification_conditions",
    "key_metrics",
)

#: memo 附录 A–F 六节（设计 §3.1）。
APPENDIX_SECTION_KEYS: tuple[str, ...] = (
    "signal_analysis",
    "anomaly_detection",
    "conflict_analysis",
    "dimension_analysis",
    "company_track",
    "review_findings",
)

#: 旧（legacy）标题 → section key。与 ``task_report._SECTION_TITLES`` 对齐（此处刻意不
#: import 私有常量，避免把渲染面私有映射变成统计口径的隐式依赖）。
LEGACY_TITLE_TO_KEY: dict[str, str] = {
    "核心观点摘要": "executive_summary",
    "投资观点展开": "investment_viewpoint",
    "情景分析": "scenario_analysis",
    "核心指标": "key_metrics",
    "风险信号": "signal_analysis",
    "勾稽关系异常": "anomaly_detection",
    "数据矛盾": "conflict_analysis",
    "维度分析": "dimension_analysis",
    "公司赛道与对标组": "company_track",
    "审查发现": "review_findings",
    "可证伪条件": "falsification_conditions",
    "主要风险": "risks",
    "免责声明": "disclaimer",
}

#: memo（Q1-lite）标题 → section key。第二步会收敛到统一的布局模块
#: （``alphabee/apps/cli/report_layout.py``）；Q0 先以脚本内常量承载，便于同时统计
#: legacy 归档与 memo 新渲染两种文本。
MEMO_TITLE_TO_KEY: dict[str, str] = {
    "一页结论": "executive_summary",
    "核心主张": "investment_viewpoint",
    "情景与触发": "scenario_analysis",
    "我们可能错在哪里": "risks",
    "观察清单": "falsification_conditions",
    "关键指标": "key_metrics",
    "免责声明": "disclaimer",
}

#: memo 附录标题前缀 → section key（标题形如 ``附录 A 风险信号明细``）。
APPENDIX_PREFIX_TO_KEY: tuple[tuple[str, str], ...] = (
    ("附录 A", "signal_analysis"),
    ("附录 B", "anomaly_detection"),
    ("附录 C", "conflict_analysis"),
    ("附录 D", "dimension_analysis"),
    ("附录 E", "company_track"),
    ("附录 F", "review_findings"),
)

TITLE_TO_KEY: dict[str, str] = {**LEGACY_TITLE_TO_KEY, **MEMO_TITLE_TO_KEY}

#: 系统问题区标题（从该行起到文件尾）。
SYSTEM_SECTION_TITLE = "系统问题"
SYSTEM_SECTION_RE = re.compile(r"(?m)^## 系统问题[ \t]*$")
#: 中期决策块分隔行（渲染器实际输出 ASCII ``=``×64；设计文档写作 ``══``，两者都认）。
MIDTERM_SEPARATOR_RE = re.compile(r"(?m)^(?:═|=){3,}[ \t]*$")

#: 数字 token（§1.1 粗口径，只用于密度比较，不做语义校验）。
NUMERIC_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?%?")
#: 归档 md 的 issue 行：``- [severity] category: message``（单行解析）。
ISSUE_LINE_RE = re.compile(r"^- \[(critical|high|medium|low)\] ([^:\n]+): (.*)$")
#: 归档文件名：``300504.SZ-2026-10-30.md`` / ``兴森科技-2026-10-17.md``。
REPORT_FILENAME_RE = re.compile(r"^(?P<symbol>.+?)-(?P<date>\d{4}-\d{2}-\d{2})\.md$")
#: 标的代码（6 位数字 + 交易所后缀）；不匹配 ⇒ 文件名前缀不是代码，md 中注明。
SYMBOL_CODE_RE = re.compile(r"^\d{6}\.(?:SZ|SH|BJ)$")

#: CSV 固定列顺序（acceptance 的 14 列；memo 追加两列）。
BASE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "total_chars",
    "body13_chars",
    "narrative_chars",
    "detail_chars",
    "issue_chars",
    "midterm_chars",
    "detail_ratio",
    "nonarg_ratio",
    "numeric_tokens",
    "numeric_per_kchar",
    "issue_count",
    "issue_max_msg_chars",
)
MEMO_COLUMNS: tuple[str, ...] = ("main_chars", "appendix_chars")

_INT_COLUMNS = {
    "total_chars",
    "body13_chars",
    "narrative_chars",
    "detail_chars",
    "issue_chars",
    "midterm_chars",
    "numeric_tokens",
    "issue_count",
    "issue_max_msg_chars",
    "main_chars",
    "appendix_chars",
}
_RATIO_COLUMNS = {"detail_ratio", "nonarg_ratio"}


# ═══════════════════════════════════════════════════════════════════
# 纯函数：切分
# ═══════════════════════════════════════════════════════════════════


def title_to_key(title: str) -> str | None:
    """章节标题 → section key；未知标题（含系统问题区）返回 ``None``。"""
    normalized = title.strip()
    if normalized in TITLE_TO_KEY:
        return TITLE_TO_KEY[normalized]
    for prefix, key in APPENDIX_PREFIX_TO_KEY:
        if normalized.startswith(prefix):
            return key
    return None


def split_sections(text: str) -> dict[str, str]:
    """按 ``## `` 标题行切分正文，返回 ``{section_key: 章节正文}``。

    口径（§1.1）：正文 = 标题行之后、下一个 ``## `` 标题行之前的内容；去掉章节
    边界的空行（保留正文内部段落空行），**不含**标题行、头部元信息与页面分隔。
    未知标题（如 ``## 系统问题``）不进入结果；缺失章节直接缺席（调用方按 0 计）。
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_key, buffer
        if current_key is not None:
            body = "\n".join(buffer).strip()
            if current_key in sections:
                sections[current_key] = f"{sections[current_key]}\n\n{body}".strip()
            else:
                sections[current_key] = body
        current_key = None
        buffer = []

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            current_key = title_to_key(line[3:])
            continue
        if current_key is not None:
            buffer.append(line)
    flush()
    return sections


def _sum_sections(sections: dict[str, str], keys: Sequence[str]) -> int:
    return sum(len(sections.get(key, "")) for key in keys)


def count_main_chars(text: str) -> int:
    """memo 主文字符数（六节正文之和）；后续 G4 门禁复用同一实现。"""
    return _sum_sections(split_sections(text), MAIN_SECTION_KEYS)


def count_appendix_chars(text: str) -> int:
    """memo 附录字符数（A–F 六节正文之和）。"""
    return _sum_sections(split_sections(text), APPENDIX_SECTION_KEYS)


def split_system_region(text: str) -> tuple[str, str]:
    """拆分系统问题区与中期决策块，返回 ``(issue_region, midterm_block)``。

    系统问题区 = ``## 系统问题`` 行起至 EOF；其中的中期决策块以首个分隔行
    （``═``/``=`` 连续 ≥3）为界拆出。设计 §1.2 的 issue/midterm 字符数把分隔行
    前那个换行算进中期块，故这里把边界左移 1 个字符以保持逐字复现。
    """
    heading = SYSTEM_SECTION_RE.search(text)
    search_from = heading.start() if heading is not None else 0
    separator = MIDTERM_SEPARATOR_RE.search(text[search_from:])
    if separator is None:
        return (text[heading.start() :] if heading is not None else "", "")
    start = search_from + separator.start()
    if start > 0 and text[start - 1] == "\n":
        start -= 1
    issue_region = text[heading.start() : start] if heading is not None and heading.start() < start else ""
    return issue_region, text[start:]


def parse_issue_lines(region: str) -> list[tuple[str, str, str]]:
    """从系统问题区解析 ``- [severity] category: message`` 行。

    返回 ``[(severity, category, message), ...]``。只解析单行：跨行 message 的归档
    md 会把续行留在同一段文本里，Q0 不做多行拼接（避免把 "共 N 个问题" 摘要行
    误并入最后一条消息）；该差异在 md 输出中注明。
    """
    parsed: list[tuple[str, str, str]] = []
    for line in region.splitlines():
        match = ISSUE_LINE_RE.match(line)
        if match is not None:
            parsed.append((match.group(1), match.group(2).strip(), match.group(3)))
    return parsed


def parse_report_identity(filename: str) -> tuple[str, str, bool]:
    """从归档文件名推断 ``(symbol, date, symbol_is_code)``。

    ``300504.SZ-2026-10-30.md`` → ``("300504.SZ", "2026-10-30", True)``；
    ``兴森科技-2026-10-17.md`` → ``("兴森科技", "2026-10-17", False)``（非代码，md 中注明）。
    不匹配日期模式时退化为 ``(stem, "", False)``，不抛。
    """
    match = REPORT_FILENAME_RE.match(filename)
    if match is None:
        return (Path(filename).stem, "", False)
    symbol = match.group("symbol")
    return (symbol, match.group("date"), bool(SYMBOL_CODE_RE.match(symbol)))


# ═══════════════════════════════════════════════════════════════════
# 纯函数：统计
# ═══════════════════════════════════════════════════════════════════


def _ratio(numerator: float, denominator: float) -> float:
    """比例；分母为 0 时记 0.0（不抛、不留 NaN）。"""
    if not denominator:
        return 0.0
    return round(numerator / denominator, 4)


def compute_metrics(
    text: str,
    *,
    layout: str = "legacy",
    issue_count: int | None = None,
    issue_max_msg_chars: int | None = None,
) -> dict[str, Any]:
    """对单份报告文本计算全部基线指标（纯函数）。

    Args:
        text: 报告全文（markdown 或渲染后的 markdown）。
        layout: ``legacy`` 时 ``main_chars=body13_chars``、``appendix_chars=0``；
            ``memo`` 时 main/appendix 分别按六节求和（设计 §3.1）。
        issue_count: 显式提供时直接用（task-record 口径取 ``record.issues``）；
            ``None`` 时从系统问题区 ``- [severity] category: message`` 行解析
            （归档 md 口径），解析不到 ⇒ 0/0 且 ``issue_parse_available=False``。
        issue_max_msg_chars: 与 ``issue_count`` 配套；缺省 0。
    """
    if layout not in KNOWN_LAYOUTS:
        raise ValueError(f"未知 layout: {layout!r}（可选 {KNOWN_LAYOUTS}）")

    sections = split_sections(text)
    body13_chars = _sum_sections(sections, SECTION_KEYS)
    total_chars = len(text)
    issue_region, midterm_block = split_system_region(text)
    issue_chars = len(issue_region)
    midterm_chars = len(midterm_block)

    if issue_count is None:
        parsed = parse_issue_lines(issue_region)
        issue_count = len(parsed)
        issue_max = max((len(message) for _, _, message in parsed), default=0)
        parse_available = bool(parsed)
    else:
        issue_max = issue_max_msg_chars or 0
        parse_available = True

    numeric_tokens = len(NUMERIC_TOKEN_RE.findall(text))
    detail_chars = _sum_sections(sections, DETAIL_KEYS)
    metrics: dict[str, Any] = {
        "total_chars": total_chars,
        "body13_chars": body13_chars,
        "narrative_chars": _sum_sections(sections, NARRATIVE_KEYS),
        "detail_chars": detail_chars,
        "issue_chars": issue_chars,
        "midterm_chars": midterm_chars,
        "detail_ratio": _ratio(detail_chars, body13_chars),
        "nonarg_ratio": _ratio(issue_chars + midterm_chars, total_chars),
        "numeric_tokens": numeric_tokens,
        "numeric_per_kchar": round(numeric_tokens / total_chars * 1000, 2) if total_chars else 0.0,
        "issue_count": issue_count,
        "issue_max_msg_chars": issue_max,
        # 内部元信息（不进 CSV）：口径该行的 issue_count 是否真的解析到了（0 行也能跑）。
        "issue_parse_available": parse_available,
    }
    if layout == "memo":
        metrics["main_chars"] = _sum_sections(sections, MAIN_SECTION_KEYS)
        metrics["appendix_chars"] = _sum_sections(sections, APPENDIX_SECTION_KEYS)
    else:
        metrics["main_chars"] = body13_chars
        metrics["appendix_chars"] = 0
    return metrics


def build_report_row(path: Path, *, layout: str = "legacy") -> dict[str, Any]:
    """归档 md → 一行基线指标（纯读取，不写盘）。"""
    text = path.read_text(encoding="utf-8")
    symbol, date, symbol_is_code = parse_report_identity(path.name)
    row: dict[str, Any] = {"symbol": symbol, "date": date, **compute_metrics(text, layout=layout)}
    row["source"] = path.name
    row["symbol_is_code"] = symbol_is_code
    return row


def collect_reports(directory: Path, *, layout: str = "legacy") -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """遍历归档 markdown 目录；单文件读失败记入 skipped，不抛。"""
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(directory.rglob("*.md")):
        try:
            rows.append(build_report_row(path, layout=layout))
        except (OSError, UnicodeDecodeError) as exc:  # fail-open：坏文件跳过并计数
            skipped.append({"source": path.name, "reason": f"{type(exc).__name__}: {exc}"})
    return rows, skipped


def _record_symbol(record: TaskRecord, path: Path) -> str:
    """任务记录标的：优先 ``record.symbol``，退化到 ``<dir>/<symbol>/<date>/x.json`` 目录名。"""
    if record.symbol:
        return record.symbol
    if path.parent.parent != path.parent:
        return path.parent.parent.name
    return path.stem


def _record_date(record: TaskRecord, path: Path) -> str:
    """任务记录日期：优先 ``timestamp`` 前 10 位，退化到日期目录名。"""
    if record.timestamp:
        return record.timestamp[:10]
    return path.parent.name


def build_task_record_row(path: Path, *, layout: str = "legacy") -> dict[str, Any]:
    """TaskRecord JSON → 一行基线指标（渲染后统计；issue 元信息取 record.issues）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = TaskRecord.model_validate(payload)
    markdown = build_report_markdown(record)
    issues = list(record.issues or [])
    metrics = compute_metrics(
        markdown,
        layout=layout,
        issue_count=len(issues),
        issue_max_msg_chars=max((len(issue.message) for issue in issues), default=0),
    )
    row: dict[str, Any] = {
        "symbol": _record_symbol(record, path),
        "date": _record_date(record, path),
        **metrics,
    }
    row["source"] = path.name
    row["symbol_is_code"] = bool(SYMBOL_CODE_RE.match(row["symbol"]))
    return row


def collect_task_records(
    directory: Path, *, layout: str = "legacy"
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """遍历任务记录 JSON；坏记录（读失败/JSON 坏/反序列化失败）跳过并计数，不抛。"""
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(directory.rglob("*.json")):
        try:
            rows.append(build_task_record_row(path, layout=layout))
        except Exception as exc:  # noqa: BLE001 - fail-open：任何坏记录都跳过并计数
            source = path.relative_to(directory).as_posix() if path.is_relative_to(directory) else str(path)
            skipped.append({"source": source, "reason": f"{type(exc).__name__}: {exc}"})
    return rows, skipped


# ═══════════════════════════════════════════════════════════════════
# 纯函数：渲染
# ═══════════════════════════════════════════════════════════════════


def columns_for(layout: str) -> tuple[str, ...]:
    """CSV/表头列顺序：legacy 14 列；memo 追加 main_chars/appendix_chars。"""
    return BASE_COLUMNS + MEMO_COLUMNS if layout == "memo" else BASE_COLUMNS


def _csv_row(row: dict[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    return {column: row.get(column) for column in columns}


def _format_cell(column: str, value: Any) -> str:
    if column in _INT_COLUMNS:
        return f"{int(value or 0):,}"
    if column in _RATIO_COLUMNS:
        return f"{float(value or 0.0) * 100:.1f}%"
    if column == "numeric_per_kchar":
        return f"{float(value or 0.0):.1f}"
    return str(value)


def render_markdown(
    rows: Sequence[dict[str, Any]],
    *,
    method: str,
    source: str,
    layout: str,
    skipped: Sequence[dict[str, str]] = (),
) -> str:
    """渲染人读版基线表 + 口径说明（纯函数，无时间戳，输出可复现）。"""
    columns = columns_for(layout)
    method_label = "归档 markdown 原文" if method == METHOD_REPORTS else "TaskRecord 渲染后统计"
    lines: list[str] = [
        "# 报告基线统计（Q0）",
        "",
        f"- 输入口径：`{method}`（{method_label}）",
        f"- 数据源：`{source}`",
        f"- 布局：`{layout}`（legacy = 13 节口径；memo = 主文六节 + 附录 A–F 口径）",
        f"- 报告数：{len(rows)}；跳过：{len(skipped)}",
        "",
    ]

    if rows:
        lines.append("| " + " | ".join(columns) + " |")
        lines.append("|" + "|".join("---" for _ in columns) + "|")
        for row in rows:
            lines.append("| " + " | ".join(_format_cell(column, row.get(column)) for column in columns) + " |")
    else:
        lines.append("_（该目录下没有可统计的报告）_")
    lines.append("")

    if skipped:
        lines.extend(["## 跳过的文件", ""])
        for item in skipped:
            lines.append(f"- `{item['source']}`：{item['reason']}")
        lines.append("")

    lines.extend(
        [
            "## 口径说明",
            "",
            "1. `total_chars` = Python `len(text)`（中文按 1 字符计），基于 UTF-8 文本。",
            "2. `body13_chars` = 13 个 `## ` 章节正文之和；正文不含 `## ` 标题行、头部元信息与"
            "章节边界空行（保留正文内部段落空行），即设计 §1.1 的“13 节正文”口径。",
            "3. `narrative_chars` = executive_summary / investment_viewpoint / scenario_analysis / "
            "key_metrics / falsification_conditions / risks / disclaimer 七节之和。",
            "4. `detail_chars` = signal_analysis / anomaly_detection / conflict_analysis / "
            "dimension_analysis / company_track / review_findings 六节之和；故 "
            "`narrative_chars + detail_chars = body13_chars`（章节齐全时）。",
            "5. `issue_chars` = `## 系统问题` 行起、到中期决策块之前；`midterm_chars` = "
            "中期决策块（分隔行到 EOF，含分隔行前换行）。",
            "6. `numeric_tokens` = 正则 `\\d+(?:\\.\\d+)?%?` 的匹配数；`numeric_per_kchar` = 每千字符数字 token 数。",
            "7. `detail_ratio` = `detail_chars / body13_chars`；"
            "`nonarg_ratio` = `(issue_chars + midterm_chars) / total_chars`；分母为 0 时记 0.0（0.0%）。",
            "8. `issue_count` / `issue_max_msg_chars`：归档口径从系统问题区 "
            "`- [severity] category: message` 行解析（只解析单行）；任务记录口径直接取 "
            "`record.issues` 条数与 `message` 最长字符数。",
            "",
            "## 输入口径差异（`--reports` vs `--task-records`）",
            "",
            "- `--reports`：统计归档 markdown 原文，包含落盘时的标题、元信息行与渲染细节；"
            "`symbol`/`date` 由文件名推断，无代码的文件名（如 `兴森科技-2026-10-17.md`）以前缀作 "
            "`symbol`，并在下方标注“非代码”。",
            "- `--task-records`：先用 `build_report_markdown(record)` 渲染再统计（`report_raw` "
            "为空的记录也会渲染最小骨架，不抛）；渲染结果与归档 md 存在差异——标题取自 "
            "`report_raw.title`/`query`、元信息行按记录字段重建、中期块按当前渲染器重绘，"
            "因此两口径的 `total_chars` 不可直接互比，跨口径比较只用占比（`detail_ratio`/"
            "`nonarg_ratio`）。",
            "- 归档 md 的 issue 行按 `- [severity] category: message` 解析、`issue_max_msg_chars` "
            "只计 message 部分（不含前缀）；设计 §1.3 的 1,213/12,808 是整行长度，故两处数值不可"
            "直接对照（600760：message 最长 1,166，整行最长 1,213）。",
            "- 设计 §1.2 的 13 节正文/叙事/明细值把 `## ` 标题行与章节边界空行也计入（600760 "
            "合计 154 字符），本脚本按 §1.1 的“正文”口径剔除，故 `body13_chars` 系统性略小、"
            "`detail_ratio` 相应略有差异（600760：56.8% vs 设计表 56.7%）；"
            "`total_chars`/`numeric_tokens`/`issue_chars`/`midterm_chars` 与设计 §1.2 逐字一致。",
        ]
    )

    non_code = [row for row in rows if not row.get("symbol_is_code", True)]
    if non_code:
        lines.extend(["", "### 非代码标的（文件名前缀）", ""])
        lines.extend(
            f"- `{row['symbol']}`（{row['date'] or '日期未知'}）：无证券代码，`symbol` 取文件名前缀" for row in non_code
        )

    if method == METHOD_REPORTS:
        unparsed = [row for row in rows if not row.get("issue_parse_available", False)]
        if unparsed:
            lines.extend(["", "### issue 口径不可用的报告", ""])
            lines.append(
                "以下报告的系统问题区没有解析到 `- [severity] category: message` 行，"
                "`issue_count`/`issue_max_msg_chars` 记为 0/0（口径不可用）："
            )
            lines.extend(f"- `{row['source']}`" for row in unparsed)

    return "\n".join(lines).rstrip() + "\n"


def build_json_payload(
    rows: Sequence[dict[str, Any]],
    *,
    method: str,
    source: str,
    layout: str,
    skipped: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    """JSON 产物：meta + 固定列顺序 + 全部行（含 source / 代码标记等溯源字段）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "method": method,
        "source": source,
        "layout": layout,
        "columns": list(columns_for(layout)),
        "count": len(rows),
        "skipped": list(skipped),
        "rows": [dict(row) for row in rows],
    }


# ═══════════════════════════════════════════════════════════════════
# IO（脚本入口专用）
# ═══════════════════════════════════════════════════════════════════


def write_csv(path: Path, rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row, columns))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_outputs(
    rows: Sequence[dict[str, Any]],
    *,
    method: str,
    source: str,
    layout: str,
    skipped: Sequence[dict[str, str]],
    out_dir: Path,
    formats: Sequence[str],
) -> list[Path]:
    """按 ``--format`` 写 csv/md/json（``all`` 写三份），返回已写路径。"""
    written: list[Path] = []
    if "csv" in formats:
        path = out_dir / f"{DEFAULT_OUTPUT_STEM}.csv"
        write_csv(path, rows, columns_for(layout))
        written.append(path)
    if "md" in formats:
        path = out_dir / f"{DEFAULT_OUTPUT_STEM}.md"
        write_text(path, render_markdown(rows, method=method, source=source, layout=layout, skipped=skipped))
        written.append(path)
    if "json" in formats:
        path = out_dir / f"{DEFAULT_OUTPUT_STEM}.json"
        write_json(
            path,
            build_json_payload(rows, method=method, source=source, layout=layout, skipped=skipped),
        )
        written.append(path)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="报告基线统计（Q0）：字符构成 / 非论证占比 / 数字密度 / issue 规模",
        epilog=(
            "两个输入入口二选一：--reports 读归档 markdown；--task-records 读 TaskRecord 并渲染后统计。"
            "都不传时按设计默认读 data/task_records。"
        ),
    )
    parser.add_argument(
        "--reports",
        nargs="?",
        const=str(DEFAULT_REPORTS_DIR),
        default=None,
        metavar="DIR",
        help=f"归档 markdown 目录（默认 {DEFAULT_REPORTS_DIR}）",
    )
    parser.add_argument(
        "--task-records",
        nargs="?",
        const=str(DEFAULT_TASK_RECORDS_DIR),
        default=None,
        metavar="DIR",
        help=f"任务记录目录（设计默认输入；默认 {DEFAULT_TASK_RECORDS_DIR}）",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        metavar="DIR",
        help=f"输出目录（默认 {DEFAULT_OUT_DIR}）",
    )
    parser.add_argument(
        "--format",
        choices=("csv", "md", "json", "all"),
        default="all",
        help="输出格式（默认 all）",
    )
    parser.add_argument(
        "--layout",
        choices=KNOWN_LAYOUTS,
        default="legacy",
        help="统计口径布局（默认 legacy；memo 追加 main_chars/appendix_chars）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.reports is not None and args.task_records is not None:
        parser.error("--reports 与 --task-records 只能二选一")

    if args.reports is not None:
        method = METHOD_REPORTS
        source = Path(args.reports)
    else:
        method = METHOD_TASK_RECORDS
        source = Path(args.task_records or str(DEFAULT_TASK_RECORDS_DIR))
    if not source.is_dir():
        print(f"错误：输入目录不存在：{source}", file=sys.stderr)
        return 1

    if method == METHOD_REPORTS:
        rows, skipped = collect_reports(source, layout=args.layout)
    else:
        rows, skipped = collect_task_records(source, layout=args.layout)

    formats = ("csv", "md", "json") if args.format == "all" else (args.format,)
    written = write_outputs(
        rows,
        method=method,
        source=str(source),
        layout=args.layout,
        skipped=skipped,
        out_dir=Path(args.out),
        formats=formats,
    )
    print(f"报告基线已生成：{', '.join(str(path) for path in written)}（{len(rows)} 份报告，跳过 {len(skipped)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
