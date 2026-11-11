"""共享报告布局模块（``alphabee/apps/cli/report_layout.py``）测试。

覆盖：legacy/memo 的顺序与标题常量、占位符、``ordered_blocks`` 的 raw/text 双视图与未知 key
透传（过滤由渲染面按 HEAD 语义执行）、``report_shape`` 两口径与系统问题区口径，以及合成记录的
markdown 冻结哈希（legacy 渲染逐字节不变）。

全部离线：只读常量与合成 TaskRecord，不读 data/、不联网、不调用模型。
"""

from __future__ import annotations

import hashlib

from alphabee.apps.cli.colors import set_color_enabled
from alphabee.apps.cli.renderer import render_final_report
from alphabee.apps.cli.report_layout import (
    APPENDIX_SECTIONS,
    APPENDIX_TITLES,
    ISSUE_APPENDIX_NOTE,
    LEGACY_ORDER,
    LEGACY_TITLES,
    MAIN_SECTIONS,
    MAIN_TITLES,
    PLACEHOLDER_VALUES,
    normalize_layout,
    ordered_blocks,
    render_issue_digest,
    report_shape,
    section_title,
    title_to_key,
)
from alphabee.apps.cli.task_report import build_report_markdown, render_report_dict
from alphabee.midterm.models import CompanyStateArtifact, StateBelief
from alphabee.task_records.models import IssueRecord, TaskRecord

#: legacy 合成记录 markdown 的冻结哈希（改动前基线，见 tmp/baseline_legacy/baseline.txt）。
SYNTHETIC_MARKDOWN_SHA256 = "5b607e810581fb14c024d234dabe3e822b201d671a50b73928bdeb2ee655c0cd"

ALL_SECTIONS = {
    "executive_summary": "【主张】毛利率中枢下移是核心矛盾。\n\n【证据】前三季毛利率 10.07%（同比 -1.76pp）。",
    "investment_viewpoint": "**主张**：核心矛盾不是收入下滑。\n**依据**：Q3 单季收入 +23.29%，毛利率约 9.63%。",
    "scenario_analysis": "基准情景：收入放量、毛利率低位企稳。\n乐观情景：定价修复。\n悲观情景：型号结构继续下移。",
    "key_metrics": "| 指标 | 数值 | 口径 |\n|---|---|---|\n| 毛利率 | 10.07% | 20260930 |",
    "signal_analysis": "- [high] signal_a: 收入与毛利率背离（20260930）",
    "anomaly_detection": "- 应收账款周转：z_score=2.4（20260930）",
    "conflict_analysis": "- 冲突 c1（high）：收入增长与毛利率下移并存",
    "dimension_analysis": "- profitability：score=0.2 confidence=0.6 判断 positive",
    "company_track": "赛道：国防军工；对标组：中航西飞、航发动力。",
    "review_findings": "- [blocking] 证据覆盖不足的维度：credit_risk",
    "falsification_conditions": "- [证伪] 2026 年报 Q4 单季毛利率 >=12%（来源：2026 年报）",
    "risks": "- 若定价继续下移，则毛利率中枢再降一档。",
    "disclaimer": "本报告基于公开财务数据和规则引擎分析生成，不构成投资建议。",
}


def _synthetic_record() -> TaskRecord:
    """与 tmp/baseline_legacy/capture.py 的合成记录逐字段一致（含 midterm + issues）。"""
    midterm = CompanyStateArtifact(
        symbol="600760.SH",
        state=StateBelief(distribution={"S0": 0.05, "S5": 0.9}, argmax_state="S5", entropy=0.52),
        thesis="交付节拍错配可解释毛利率下移",
        thesis_confidence=0.61,
        as_of_date="2026-11-01",
        evidence_count=16,
        classifier_order="tef",
        classifier_formula="v2",
        uncertain=False,
        adverse_divergence={"severity": 0.85},
    )
    return TaskRecord(
        task_id="task-baseline-synthetic",
        query="分析中航沈飞",
        symbol="600760.SH",
        timestamp="2026-11-01T10:00:00",
        overall_confidence="medium",
        risk_count={"high": 1, "medium": 2, "low": 0, "blocked": 0},
        midterm_state="S5",
        midterm_confidence=0.61,
        midterm_evidence_count=16,
        midterm_classifier_order="tef",
        midterm_formula_revision="v2",
        midterm_uncertain=False,
        midterm_adverse_severity=0.85,
        financial_period="20260930",
        valuation_as_of="20261030",
        valuation_close=48.5,
        valuation_pe_ttm=52.0,
        valuation_pb=6.4,
        report_raw={
            "title": "600760.SH 中航沈飞 投资分析报告 — 财报口径 20260930 / 行情口径 20261030",
            "layout": "legacy",
            "summary": "核心矛盾是毛利率中枢下移，收入下滑不是主因。",
            "overall_confidence": "medium",
            "risk_count": {"high": 1, "medium": 2, "low": 0, "blocked": 0},
            "sections": dict(ALL_SECTIONS),
        },
        issues=[
            IssueRecord(
                severity="critical",
                category="evidence_gap",
                message="毛利率口径缺 Q3 单季拆分，证据强度不足，建议补财报原文。",
            ),
            IssueRecord(
                severity="high",
                category="numeric_inconsistency",
                message="20260930 营收同比 23.29% 与 20260331 口径不一致，需核对。",
            ),
            IssueRecord(severity="medium", category="missing_data", message="北向资金数据缺失。"),
        ],
        midterm=midterm,
    )


# ═══════════════════════════════════════════════════════════════════
# 常量：顺序 / 标题 / 占位符
# ═══════════════════════════════════════════════════════════════════


def test_legacy_order_matches_current_13_sections() -> None:
    assert LEGACY_ORDER == (
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
    assert [LEGACY_TITLES[key] for key in LEGACY_ORDER] == [
        "核心观点摘要",
        "投资观点展开",
        "情景分析",
        "核心指标",
        "风险信号",
        "勾稽关系异常",
        "数据矛盾",
        "维度分析",
        "公司赛道与对标组",
        "审查发现",
        "可证伪条件",
        "主要风险",
        "免责声明",
    ]


def test_memo_sections_and_titles() -> None:
    assert MAIN_SECTIONS == (
        "executive_summary",
        "investment_viewpoint",
        "scenario_analysis",
        "risks",
        "falsification_conditions",
        "key_metrics",
    )
    assert APPENDIX_SECTIONS == (
        "signal_analysis",
        "anomaly_detection",
        "conflict_analysis",
        "dimension_analysis",
        "company_track",
        "review_findings",
    )
    assert [MAIN_TITLES[key] for key in MAIN_SECTIONS] == [
        "一页结论",
        "核心主张",
        "情景与触发",
        "我们可能错在哪里",
        "观察清单",
        "关键指标",
    ]
    assert [APPENDIX_TITLES[key] for key in APPENDIX_SECTIONS] == [
        "附录 A 风险信号明细",
        "附录 B 勾稽异常明细",
        "附录 C 数据矛盾明细",
        "附录 D 维度评分明细",
        "附录 E 公司赛道与对标组",
        "附录 F 审查发现",
    ]
    assert section_title("disclaimer", "memo") == "免责声明"
    assert section_title("unknown_section", "legacy") == "unknown_section"


def test_placeholder_values_unchanged() -> None:
    assert PLACEHOLDER_VALUES == {
        "investment_viewpoint": "未生成独立投资观点，以下为结构化数据分析。",
        "scenario_analysis": "未生成情景分析。",
        "conflict_analysis": "未检测到显著数据矛盾，多维度指标之间逻辑自洽。",
        "review_findings": "未执行审查",
        "falsification_conditions": "无可证伪条件。",
    }


def test_normalize_layout_and_title_to_key() -> None:
    assert normalize_layout("memo") == "memo"
    assert normalize_layout("legacy") == "legacy"
    assert normalize_layout(None) == "legacy"
    assert normalize_layout("memo_claims") == "legacy"  # 未知布局 fail-open

    assert title_to_key("核心观点摘要") == "executive_summary"
    assert title_to_key("一页结论") == "executive_summary"
    assert title_to_key("附录 A 风险信号明细") == "signal_analysis"
    assert title_to_key("附录 E 公司赛道与对标组（补）") == "company_track"
    assert title_to_key("未知标题") is None


# ═══════════════════════════════════════════════════════════════════
# ordered_blocks
# ═══════════════════════════════════════════════════════════════════


def test_ordered_blocks_returns_raw_and_stripped_views() -> None:
    """过滤职责在消费者：本函数只给 raw_text / text 双视图，不替渲染面跳过。"""
    sections: dict[str, object] = {
        "executive_summary": "核心观点",
        "investment_viewpoint": PLACEHOLDER_VALUES["investment_viewpoint"],
        "scenario_analysis": "   ",
        "key_metrics": None,
        "signal_analysis": "  信号  ",
        "risks": "\n风险\n",
    }
    blocks = ordered_blocks(sections, "legacy")
    assert [block.key for block in blocks] == [
        "executive_summary",
        "investment_viewpoint",
        "scenario_analysis",
        "signal_analysis",
        "risks",
    ]
    by_key = {block.key: block for block in blocks}
    assert by_key["executive_summary"].raw_text == "核心观点"
    assert by_key["investment_viewpoint"].text == PLACEHOLDER_VALUES["investment_viewpoint"]
    # 纯空白：text 为空，但 raw_text 保留（legacy 终端按原值打印）
    assert by_key["scenario_analysis"].raw_text == "   "
    assert by_key["scenario_analysis"].text == ""
    # padded：text 已 strip，raw_text 保留前后空白
    assert by_key["signal_analysis"].raw_text == "  信号  "
    assert by_key["signal_analysis"].text == "信号"
    assert by_key["risks"].raw_text == "\n风险\n"
    assert by_key["risks"].text == "风险"


def test_ordered_blocks_legacy_unknown_key_keeps_relative_position() -> None:
    sections = {
        "executive_summary": "a",
        "key_metrics": "b",
        "investment_thesis": "旧章节",
        "review_findings": "c",
        "risks": "d",
    }
    assert [block.key for block in ordered_blocks(sections, "legacy")] == [
        "executive_summary",
        "key_metrics",
        "investment_thesis",
        "review_findings",
        "risks",
    ]
    assert ordered_blocks(sections, "legacy")[2].title == "investment_thesis"


def test_ordered_blocks_memo_order_and_unknown_tail() -> None:
    sections = dict(ALL_SECTIONS)
    sections["investment_thesis"] = "旧章节"
    blocks = ordered_blocks(sections, "memo")
    assert [block.key for block in blocks] == [
        *MAIN_SECTIONS,
        *APPENDIX_SECTIONS,
        "disclaimer",
        "investment_thesis",
    ]
    assert blocks[0].title == "一页结论"
    assert blocks[6].title == "附录 A 风险信号明细"
    assert blocks[-2].title == "免责声明"


def test_ordered_blocks_empty_input() -> None:
    assert ordered_blocks(None, "legacy") == []
    assert ordered_blocks({}, "memo") == []


def test_memo_company_overview_is_first_and_titled() -> None:
    """memo 独有：公司概况排在主文之前，legacy 无该标题（未知 key 原样透传）。"""
    sections = {**dict(ALL_SECTIONS), "company_overview": "公司做什么、卖给谁。"}
    blocks = ordered_blocks(sections, "memo")
    assert blocks[0].key == "company_overview"
    assert blocks[0].title == "公司概况"
    assert [block.key for block in blocks[1:7]] == list(MAIN_SECTIONS)
    assert section_title("company_overview", "memo") == "公司概况"
    assert section_title("company_overview", "legacy") == "company_overview"
    assert title_to_key("公司概况") == "company_overview"
    # 未知 key 尾部透传的判定不把公司概况当未知
    assert [block.key for block in blocks].count("company_overview") == 1


def test_report_shape_memo_counts_overview_as_main_text() -> None:
    markdown, bodies = _markdown_with_all_sections()
    markdown = markdown.replace(
        "## 核心观点摘要",
        "## 公司概况\n\n概况正文\n\n## 核心观点摘要",
        1,
    )
    shape = report_shape(markdown, "memo")
    main_expected = len("概况正文") + sum(len(bodies[key]) for key in MAIN_SECTIONS)
    assert shape["main_chars"] == main_expected
    assert shape["section_count"] == len(LEGACY_ORDER) + 1


# ═══════════════════════════════════════════════════════════════════
# report_shape
# ═══════════════════════════════════════════════════════════════════


def _markdown_with_all_sections() -> tuple[str, dict[str, str]]:
    bodies = {key: f"{key}正文" for key in LEGACY_ORDER}
    parts: list[str] = ["# 标题", "", "**整体置信度**: high", ""]
    for key in LEGACY_ORDER:
        parts.extend([f"## {LEGACY_TITLES[key]}", "", bodies[key], ""])
    return "\n".join(parts), bodies


def test_report_shape_legacy_and_memo() -> None:
    markdown, bodies = _markdown_with_all_sections()
    legacy = report_shape(markdown, "legacy")
    memo = report_shape(markdown, "memo")
    assert legacy["main_chars"] == sum(len(text) for text in bodies.values())
    assert legacy["appendix_chars"] == 0
    assert memo["main_chars"] == sum(len(bodies[key]) for key in MAIN_SECTIONS)
    assert memo["appendix_chars"] == sum(len(bodies[key]) for key in APPENDIX_SECTIONS)
    assert memo["main_chars"] + memo["appendix_chars"] == legacy["main_chars"] - len(bodies["disclaimer"])
    assert legacy["total_chars"] == memo["total_chars"] == len(markdown)
    assert legacy["issue_chars"] == memo["issue_chars"] == 0


def test_report_shape_issue_chars_excludes_midterm_block() -> None:
    markdown, _ = _markdown_with_all_sections()
    separator = "=" * 64
    markdown += f"\n## 系统问题\n\n- [high] demo: 消息\n\n{separator}\n中期决策块\n"
    heading_start = markdown.index("## 系统问题")
    separator_start = markdown.index(separator)
    boundary = separator_start - 1 if markdown[separator_start - 1] == "\n" else separator_start
    shape = report_shape(markdown, "legacy")
    assert shape["issue_chars"] == boundary - heading_start
    assert shape["issue_chars"] < len(markdown) - heading_start  # 中期块未被计入


def test_report_shape_unknown_layout_falls_back_to_legacy() -> None:
    markdown, _ = _markdown_with_all_sections()
    assert report_shape(markdown, "memo_claims") == report_shape(markdown, "legacy")


# ═══════════════════════════════════════════════════════════════════
# 冻结哈希：legacy 合成记录 markdown 逐字节不变
# ═══════════════════════════════════════════════════════════════════


def test_synthetic_record_markdown_frozen_hash() -> None:
    markdown = build_report_markdown(_synthetic_record())
    assert hashlib.sha256(markdown.encode("utf-8")).hexdigest() == SYNTHETIC_MARKDOWN_SHA256


def test_legacy_record_without_layout_field_renders_identical() -> None:
    record = _synthetic_record()
    without_layout = record.model_copy(
        update={"report_raw": {key: value for key, value in record.report_raw.items() if key != "layout"}}
    )
    assert build_report_markdown(without_layout) == build_report_markdown(record)


# ── Q1-lite B：系统问题折叠（render_issue_digest）────────────────────────────


def _digest_issues() -> list[dict[str, str]]:
    """34 条 issue 样例：2 critical / 8 high / 23 medium / 1 low（message 100 字）。"""
    issues = [{"severity": "critical", "category": f"critical_{i}", "message": "C" * 100} for i in range(2)]
    issues += [{"severity": "high", "category": f"high_{i}", "message": "H" * 100} for i in range(8)]
    issues += [{"severity": "medium", "category": f"medium_{i}", "message": "M" * 100} for i in range(23)]
    issues += [{"severity": "low", "category": "low_0", "message": "L" * 100}]
    return issues


def test_render_issue_digest_34_issues() -> None:
    issues = _digest_issues()
    digest, appendix = render_issue_digest(issues)
    # 主文摘要 ≤800 字、只含 critical/high 前 5 条 + 计数行
    assert len(digest) <= 800
    assert digest.count("- [critical]") == 2
    assert digest.count("- [high]") == 3
    assert "[medium]" not in digest and "[low]" not in digest
    assert "共 34 条问题（critical 2 / high 8 / medium 23 / low 1），完整清单见附录 G。" in digest
    # 附录首行为固定标注；全量 34 条不丢条；每条 message ≤600 字
    assert appendix.splitlines()[0] == ISSUE_APPENDIX_NOTE
    assert "完整 message 见任务记录" in ISSUE_APPENDIX_NOTE
    appendix_lines = [line for line in appendix.splitlines() if line.startswith("- [")]
    assert len(appendix_lines) == 34
    long_issues = [{"severity": "high", "category": "long", "message": "X" * 1000}]
    _, long_appendix = render_issue_digest(long_issues)
    long_line = next(line for line in long_appendix.splitlines() if line.startswith("- ["))
    assert len(long_line.split(": ", 1)[1]) == 600


def test_render_issue_digest_unknown_severity_and_empty() -> None:
    assert render_issue_digest([]) == ("", "")
    issues = [
        {"severity": "critical", "category": "c", "message": "m1"},
        {"severity": "weird", "category": "w", "message": "m2"},
    ]
    digest, appendix = render_issue_digest(issues)
    assert "其它 1" in digest
    assert "共 2 条问题（critical 1 / high 0 / medium 0 / low 0 / 其它 1）" in digest
    assert appendix.splitlines()[0] == ISSUE_APPENDIX_NOTE
    assert len([line for line in appendix.splitlines() if line.startswith("- [")]) == 2
    # 空列表不产生孤立标注
    assert render_issue_digest([]) == ("", "")


def test_render_issue_digest_respects_max_items_and_max_chars() -> None:
    issues = [{"severity": "high", "category": f"h{i}", "message": "Y" * 50} for i in range(7)]
    digest, _appendix = render_issue_digest(issues, max_items=3, max_chars=10)
    detail_lines = [line for line in digest.splitlines() if line.startswith("- [")]
    assert len(detail_lines) == 3
    assert all(len(line.split(": ", 1)[1]) == 10 for line in detail_lines)
    assert "共 7 条问题" in digest


def test_report_shape_reports_section_count() -> None:
    markdown, _bodies = _markdown_with_all_sections()
    shape = report_shape(markdown, "legacy")
    assert shape["section_count"] == len(LEGACY_ORDER)
    assert shape["section_count"] == 13


def test_render_issue_digest_total_budget_800_with_long_messages() -> None:
    """摘要总预算 800 优先于单条 160：5 条 200 字 message 也必须 ≤800，计数行完整。"""
    issues = [{"severity": "critical", "category": f"c{i}", "message": "Z" * 200} for i in range(5)]
    digest, _appendix = render_issue_digest(issues)
    assert len(digest) <= 800
    assert "共 5 条问题（critical 5 / high 0 / medium 0 / low 0），完整清单见附录 G。" in digest
    detail_lines = [line for line in digest.splitlines() if line.startswith("- [")]
    assert len(detail_lines) == 5
    assert all(len(line.split(": ", 1)[1]) <= 160 for line in detail_lines)


def test_render_issue_digest_appendix_note_single_source() -> None:
    """标注只由 render_issue_digest 产出：附录首行、恰好一次。"""
    _digest, appendix = render_issue_digest(_digest_issues())
    assert appendix.count(ISSUE_APPENDIX_NOTE) == 1
    assert appendix.splitlines()[0] == ISSUE_APPENDIX_NOTE


def _all_sections() -> dict[str, str]:
    return {key: f"{key}正文" for key in LEGACY_ORDER}


def test_memo_appendix_g_note_shared_by_markdown_and_terminal(capsys) -> None:
    """memo markdown 与终端附录 G 共用同一标注；legacy 不出现该标注。"""
    issues = [
        {"severity": "critical", "category": "critical_0", "message": "m1"},
        {"severity": "low", "category": "low_0", "message": "m2"},
    ]
    sections = _all_sections()
    memo_markdown = render_report_dict({"title": "T", "layout": "memo", "sections": sections}, issues=issues)
    assert "## 附录 G 系统问题清单" in memo_markdown
    g_section = memo_markdown[memo_markdown.index("## 附录 G 系统问题清单") :]
    assert ISSUE_APPENDIX_NOTE in g_section

    legacy_markdown = render_report_dict({"title": "T", "layout": "legacy", "sections": sections}, issues=issues)
    assert "## 系统问题" in legacy_markdown
    assert ISSUE_APPENDIX_NOTE not in legacy_markdown  # legacy 路径不消费该文案

    set_color_enabled(False)
    try:
        render_final_report(
            {
                "final_report": {
                    "title": "T",
                    "layout": "memo",
                    "sections": sections,
                    "summary": "S",
                    "overall_confidence": "high",
                },
                "issues": issues,
            }
        )
    finally:
        set_color_enabled(True)
    terminal_out = capsys.readouterr().out
    assert "附录 G 系统问题清单" in terminal_out
    assert ISSUE_APPENDIX_NOTE in terminal_out
