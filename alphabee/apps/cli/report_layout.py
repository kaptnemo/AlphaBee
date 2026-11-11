"""报告章节布局的唯一来源（legacy / memo 两布局）。

本模块集中定义渲染面（``task_report.build_report_markdown`` markdown 文件、
``renderer.render_final_report`` 终端）共用的**标题、顺序、占位符与字符口径**：

- ``legacy``：现状 13 节（归档报告 / 旧记录），顺序与标题必须与 Q0 基线逐字节一致；
- ``memo``：Q1-lite 投资备忘录，公司概况 → 摘要 → 主文六节 → 审查与数据问题 →
  附录 A–F → 附录 G（系统问题全量）/ 附录 H（中期决策明细）→ 免责声明。其中 G/H 由
  :func:`render_issue_digest` 与渲染面负责折叠，不属于 13 个 section key。

同时提供后续 G4 门禁与 A/B 指标共用的 :func:`report_shape`：从**渲染后的 markdown**
计算 ``main_chars`` / ``appendix_chars`` / ``issue_chars`` / ``total_chars`` / ``section_count``。
三个消费者（渲染、门禁、A/B）只认这一份口径，避免"门禁算主文、渲染算 13 节"的漂移。

口径说明：

- 章节正文 = ``## `` 标题行之后、下一个 ``## `` 标题行之前的内容，去掉章节边界空行
  （保留正文内部段落空行）；标题行、头部元信息、空行不计入正文字符数。
- ``legacy`` 的 ``main_chars`` = 13 节正文之和；``memo`` 的 ``main_chars`` = 公司概况 +
  主文六节之和、``appendix_chars`` = 附录 A–F 六节之和；``issue_chars`` 沿用 Q0 基线口径
  （``## 系统问题`` 行起至中期决策块之前；中期块不计入 issue）。
- **过滤职责在消费者**：:func:`ordered_blocks` 返回 ``raw_text``（原值）与 ``text``（strip
  后）双视图，但不替调用方跳过空值/占位符；markdown 面按 strip 后文本判空/比占位符，
  legacy 终端按原值判断（HEAD 语义），memo 终端按 strip 后文本判断。详见函数 docstring。
- ``scripts/report_baseline.py`` 目前保留同语义的本地常量（该脚本不在本步改动范围），
  收敛到本模块留到 W2 与 G4 门禁一起做。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

#: legacy 现状 13 节顺序（与 ``Sections`` 字段顺序 / 归档报告 JSON 顺序一致）。
LEGACY_ORDER: tuple[str, ...] = (
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

#: legacy 标题（与 Q0 基线 / 旧归档逐字一致）。
LEGACY_TITLES: dict[str, str] = {
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

#: memo 主文前的「公司概况」节（memo 专属：渲染在摘要之前，面向零基础读者）。
OVERVIEW_SECTIONS: tuple[str, ...] = ("company_overview",)

#: memo 主文六节（设计 §3.1；不含免责声明与附录）。
MAIN_SECTIONS: tuple[str, ...] = (
    "executive_summary",
    "investment_viewpoint",
    "scenario_analysis",
    "risks",
    "falsification_conditions",
    "key_metrics",
)

#: memo 附录 A–F 六节（设计 §3.1）。
APPENDIX_SECTIONS: tuple[str, ...] = (
    "signal_analysis",
    "anomaly_detection",
    "conflict_analysis",
    "dimension_analysis",
    "company_track",
    "review_findings",
)

#: memo 公司概况标题（渲染在摘要之前；legacy 无此节）。
OVERVIEW_TITLES: dict[str, str] = {
    "company_overview": "公司概况",
}

#: memo 主文标题（顺序与 ``MAIN_SECTIONS`` 一致）。
MAIN_TITLES: dict[str, str] = {
    "executive_summary": "一页结论",
    "investment_viewpoint": "核心主张",
    "scenario_analysis": "情景与触发",
    "risks": "我们可能错在哪里",
    "falsification_conditions": "观察清单",
    "key_metrics": "关键指标",
}

#: memo 附录标题（顺序与 ``APPENDIX_SECTIONS`` 一致）。
APPENDIX_TITLES: dict[str, str] = {
    "signal_analysis": "附录 A 风险信号明细",
    "anomaly_detection": "附录 B 勾稽异常明细",
    "conflict_analysis": "附录 C 数据矛盾明细",
    "dimension_analysis": "附录 D 维度评分明细",
    "company_track": "附录 E 公司赛道与对标组",
    "review_findings": "附录 F 审查发现",
}

#: 占位符文案（值与原 ``task_report._PLACEHOLDER_VALUES`` 逐字一致）：消费者命中即跳过
#: 渲染（markdown/memo 按 strip 后文本比较；legacy 终端按原值比较，HEAD 语义）。
PLACEHOLDER_VALUES: dict[str, str] = {
    "investment_viewpoint": "未生成独立投资观点，以下为结构化数据分析。",
    "scenario_analysis": "未生成情景分析。",
    "conflict_analysis": "未检测到显著数据矛盾，多维度指标之间逻辑自洽。",
    "review_findings": "未执行审查",
    "falsification_conditions": "无可证伪条件。",
}

#: memo 终端对附录的预览字符数（标题之外最多打印的正文长度；函数参数可覆盖）。
APPENDIX_PREVIEW_CHARS = 800
#: 兼容旧名（Q1-lite A 已对外暴露 ``APPENDIX_CLIP_CHARS``）。
APPENDIX_CLIP_CHARS = APPENDIX_PREVIEW_CHARS

#: memo 折叠块的标题（系统问题摘要 / 附录 G / 附录 H）。
ISSUE_DIGEST_TITLE = "审查与数据问题（摘要）"
APPENDIX_G_TITLE = "附录 G 系统问题清单"
APPENDIX_H_TITLE = "附录 H 中期决策明细"
#: 附录 G 每条 issue 的最大字符数（设计 §3.2.3：全量但单条 ≤600 字）。
ISSUE_APPENDIX_MAX_CHARS = 600
#: 附录 G 首行固定标注（设计 §3.2.3：原文保留在 TaskRecord.issues；memo markdown 与终端共用）。
ISSUE_APPENDIX_NOTE = "完整 message 见任务记录（TaskRecord.issues；附录每条按 600 字截断）。"
#: 主文系统问题摘要的总预算（设计 §2.2/§3.1：摘要 ≤800 字，优先于单条 160）。
ISSUE_DIGEST_MAX_CHARS = 800

#: 系统问题区标题（从该行起到文件尾；中期决策块单独拆出）。
SYSTEM_SECTION_TITLE = "系统问题"
_SYSTEM_SECTION_RE = re.compile(r"(?m)^## 系统问题[ \t]*$")
#: 中期决策块分隔行（渲染器实际输出 ASCII ``=``×64；设计文档写作 ``══``，两者都认）。
_MIDTERM_SEPARATOR_RE = re.compile(r"(?m)^(?:═|=){3,}[ \t]*$")

#: 标题 → key 反查（legacy + memo 主文/附录；供基线脚本等只读消费者使用）。
_TITLE_TO_KEY: dict[str, str] = {
    **{title: key for key, title in LEGACY_TITLES.items()},
    **{title: key for key, title in OVERVIEW_TITLES.items()},
    **{title: key for key, title in MAIN_TITLES.items()},
    **{title: key for key, title in APPENDIX_TITLES.items()},
    "免责声明": "disclaimer",
}

#: memo 附录标题前缀 → key（容忍标题编号后的明细名微调）。
_APPENDIX_PREFIX_TO_KEY: tuple[tuple[str, str], ...] = (
    ("附录 A", "signal_analysis"),
    ("附录 B", "anomaly_detection"),
    ("附录 C", "conflict_analysis"),
    ("附录 D", "dimension_analysis"),
    ("附录 E", "company_track"),
    ("附录 F", "review_findings"),
)

_KNOWN_SECTIONS: frozenset[str] = (
    frozenset(LEGACY_ORDER) | frozenset(OVERVIEW_SECTIONS) | frozenset(MAIN_SECTIONS) | frozenset(APPENDIX_SECTIONS)
)


class SectionBlock(NamedTuple):
    """渲染面消费的一个章节块：``key`` + 当前布局下的标题 + 正文双视图。

    ``raw_text`` 是 JSON 里的原值（``str`` 化、**未 strip**），供 legacy 终端按 HEAD 语义
    打印（原值判空 / 原值比较占位符 / 原值截断）；``text`` 是 ``raw_text`` 去掉两端空白
    后的视图，供 markdown 与 memo 终端使用。两者都由调用方决定是否过滤，本模块不替
    消费者做 strip/占位符判断。
    """

    key: str
    title: str
    text: str
    raw_text: str


def normalize_layout(layout: str | None) -> str:
    """把任意布局值归一到 ``legacy`` / ``memo``；未知值 fail-open 回 ``legacy``。"""
    return layout if layout in ("legacy", "memo") else "legacy"


def title_to_key(title: str) -> str | None:
    """章节标题 → section key；未知标题（含系统问题区）返回 ``None``。"""
    normalized = title.strip()
    key = _TITLE_TO_KEY.get(normalized)
    if key is not None:
        return key
    for prefix, prefix_key in _APPENDIX_PREFIX_TO_KEY:
        if normalized.startswith(prefix):
            return prefix_key
    return None


def section_title(key: str, layout: str = "legacy") -> str:
    """取某布局下 section 的展示标题；未知 key 回退为 key 本身（fail-open 透传）。"""
    if normalize_layout(layout) == "memo":
        return (
            OVERVIEW_TITLES.get(key) or MAIN_TITLES.get(key) or APPENDIX_TITLES.get(key) or LEGACY_TITLES.get(key, key)
        )
    return LEGACY_TITLES.get(key, key)


def _legacy_keys(sections: Mapping[str, Any]) -> list[str]:
    """legacy 顺序：已知 13 节按 ``LEGACY_ORDER`` 排，未知 key 保持其原始相对位置。

    现状 markdown 按 JSON ``sections`` 插入顺序渲染；135 条历史记录里已知 key 的相对顺序
    恰好都是 ``LEGACY_ORDER`` 的子序列，所以"已知节归一 + 未知节原位透传"能同时满足
    "legacy 逐字节不变"（含旧记录里的 ``investment_thesis`` 未知节）与"顺序唯一来源"。
    """
    known = [key for key in LEGACY_ORDER if key in sections]
    has_unknown = any(key not in _KNOWN_SECTIONS for key in sections)
    if not has_unknown:
        return known

    ordered: list[str] = []
    pointer = 0
    for key in sections:
        if key not in LEGACY_ORDER:
            ordered.append(key)
            continue
        target = known.index(key)
        while pointer <= target:
            ordered.append(known[pointer])
            pointer += 1
    while pointer < len(known):
        ordered.append(known[pointer])
        pointer += 1
    return ordered


def _memo_keys(sections: Mapping[str, Any]) -> list[str]:
    """memo 顺序：公司概况 → 主文六节 → 附录 A–F → 免责声明 → 未知 key（尾部透传）。"""
    ordered = [key for key in (*OVERVIEW_SECTIONS, *MAIN_SECTIONS, *APPENDIX_SECTIONS, "disclaimer") if key in sections]
    ordered.extend(key for key in sections if key not in _KNOWN_SECTIONS)
    return ordered


def ordered_blocks(sections: Mapping[str, Any] | None, layout: str = "legacy") -> list[SectionBlock]:
    """按布局返回可渲染章节块；**本函数不做空值/占位符过滤**，与旧实现的职责划分不同。

    旧渲染面各自做过滤，两处语义并不相同：

    - markdown（``task_report``）：先 ``strip``，空串跳过、与占位符逐字相等跳过；
    - legacy 终端（``renderer``）：按**原值**判断（``if value`` / ``value != placeholder``），
      原值带空白时照旧打印，不 strip、不额外跳过。

    因此本函数只返回 ``raw_text`` + ``text`` 双视图（``None`` 值仍跳过），过滤由各消费者按
    上述 HEAD 语义自行执行；把过滤下沉到本模块会改变 legacy 终端对 padded/空白/占位符
    输入的输出（t5 review F1）。

    Args:
        sections: 报告 JSON 的 ``sections`` 映射（key → 文本）。
        layout: ``legacy``（默认，旧报告）或 ``memo``（Q1-lite 投资备忘录）。

    Returns:
        按布局排好序的 :class:`SectionBlock` 列表；``text`` 已去两端空白，``raw_text`` 保留原值。
    """
    if not sections:
        return []
    layout = normalize_layout(layout)
    blocks: list[SectionBlock] = []
    keys = _legacy_keys(sections) if layout == "legacy" else _memo_keys(sections)
    for key in keys:
        value = sections.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            raw_text = value
            text = value.strip()  # markdown/HEAD：str 值 strip 后判空与占位符
        else:
            raw_text = str(value)
            text = raw_text  # HEAD markdown 对非 str 值不 strip，保持原样
        blocks.append(
            SectionBlock(key=key, title=section_title(key, layout), text=text, raw_text=raw_text),
        )
    return blocks


def _issue_field(issue: Any, name: str) -> str:
    """读 issue 字段：兼容 ``IssueRecord`` / ``Issue`` 模型与 ``issues`` JSON dict。"""
    if isinstance(issue, Mapping):
        return str(issue.get(name) or "")
    return str(getattr(issue, name, "") or "")


def _clip_text(text: str, limit: int) -> str:
    """按字符数截断（不追加省略号，保证结果 ≤ limit）。"""
    return text if len(text) <= limit else text[:limit]


def render_issue_digest(
    issues: Any,
    *,
    max_items: int = 5,
    max_chars: int = 160,
) -> tuple[str, str]:
    """把系统问题拆成「主文摘要 + 附录 G 全量」两份文本（纯函数、确定性、无 LLM）。

    Args:
        issues: ``IssueRecord`` / ``Issue`` 模型列表，或 issues JSON dict 列表。
        max_items: 主文摘要保留的 critical/high 条数上限（保持原顺序）。
        max_chars: 主文摘要每条 message 的最大字符数。

    Returns:
        ``(digest, appendix)``：digest = critical/high 前 N 条 + 严重度计数行，
        总长 ≤ ``ISSUE_DIGEST_MAX_CHARS``（800；超出时逐步缩短单条 message，仍 ≤ ``max_chars``）；
        appendix = 首行固定标注 :data:`ISSUE_APPENDIX_NOTE` + 全量 issue
        （每条 message ≤ ``ISSUE_APPENDIX_MAX_CHARS``，不丢条）。
        无 issue 时返回 ``("", "")``（不产生空附录/孤立标注）。
    """
    items = list(issues or [])
    if not items:
        return "", ""

    def _line(issue: Any, message_limit: int) -> str:
        severity = _issue_field(issue, "severity") or "?"
        category = _issue_field(issue, "category") or "?"
        message = _clip_text(_issue_field(issue, "message"), message_limit)
        return f"- [{severity}] {category}: {message}"

    critical_high = [issue for issue in items if _issue_field(issue, "severity") in ("critical", "high")]
    head = critical_high[:max_items]

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    other = 0
    for issue in items:
        severity = _issue_field(issue, "severity")
        if severity in counts:
            counts[severity] += 1
        else:
            other += 1
    other_part = f" / 其它 {other}" if other else ""
    count_line = (
        f"共 {len(items)} 条问题（critical {counts['critical']} / high {counts['high']} / "
        f"medium {counts['medium']} / low {counts['low']}{other_part}），完整清单见附录 G。"
    )

    def _compose(message_limit: int) -> str:
        lines = [_line(issue, message_limit) for issue in head]
        return "\n\n".join(part for part in ("\n".join(lines), count_line) if part)

    # 摘要总预算 800 字（设计 §2.2/§3.1）：超出时逐步缩短单条 message（仍 ≤ max_chars），
    # 计数行保持完整；保证 digest 恒 ≤ ISSUE_DIGEST_MAX_CHARS（构造样例与真实 34 条均成立）。
    message_limit = max_chars
    digest = _compose(message_limit)
    while len(digest) > ISSUE_DIGEST_MAX_CHARS and message_limit > 0:
        message_limit -= 1
        digest = _compose(message_limit)

    issue_lines = "\n".join(_line(issue, ISSUE_APPENDIX_MAX_CHARS) for issue in items)
    appendix = f"{ISSUE_APPENDIX_NOTE}\n\n{issue_lines}"
    return digest, appendix


def _markdown_section_bodies(markdown: str) -> dict[str, str]:
    """按 ``## `` 标题行切分渲染后的 markdown，返回 ``{section_key: 正文}``（正文已 strip）。"""
    bodies: dict[str, list[str]] = {}
    current_key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_key, buffer
        if current_key is not None:
            bodies.setdefault(current_key, []).append("\n".join(buffer).strip())
        current_key = None
        buffer = []

    for line in markdown.splitlines():
        if line.startswith("## "):
            flush()
            current_key = title_to_key(line[3:])
            continue
        if current_key is not None:
            buffer.append(line)
    flush()
    return {key: "\n\n".join(part for part in parts if part) for key, parts in bodies.items()}


def _issue_chars(markdown: str) -> int:
    """``## 系统问题`` 行起至中期决策块之前的字符数（与 Q0 基线脚本同口径）。"""
    heading = _SYSTEM_SECTION_RE.search(markdown)
    if heading is None:
        return 0
    separator = _MIDTERM_SEPARATOR_RE.search(markdown, heading.start())
    if separator is None:
        return len(markdown) - heading.start()
    start = separator.start()
    if start > 0 and markdown[start - 1] == "\n":
        start -= 1
    return max(start - heading.start(), 0)


def report_shape(markdown: str, layout: str = "legacy") -> dict[str, int]:
    """从渲染后的 markdown 计算主文/附录/系统问题/总字符数与章节数（G4/A-B 共用口径）。

    Args:
        markdown: 渲染后的报告全文。
        layout: ``legacy`` 时 ``main_chars`` = 13 节正文之和；``memo`` 时
            ``main_chars`` = 主文六节之和、``appendix_chars`` = 附录 A–F 六节之和。

    Returns:
        ``{"main_chars", "appendix_chars", "issue_chars", "total_chars", "section_count"}``。
        ``section_count`` = 渲染文本中可识别的章节标题数（用于 report_ab 的指标输出）。
    """
    normalized = normalize_layout(layout)
    sections = _markdown_section_bodies(markdown)
    if normalized == "memo":
        main_keys: tuple[str, ...] = (*OVERVIEW_SECTIONS, *MAIN_SECTIONS)
        appendix_keys: tuple[str, ...] = APPENDIX_SECTIONS
    else:
        main_keys = LEGACY_ORDER
        appendix_keys = ()
    return {
        "main_chars": sum(len(sections.get(key, "")) for key in main_keys),
        "appendix_chars": sum(len(sections.get(key, "")) for key in appendix_keys),
        "issue_chars": _issue_chars(markdown),
        "total_chars": len(markdown),
        "section_count": len(sections),
    }
