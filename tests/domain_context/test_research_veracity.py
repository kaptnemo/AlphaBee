"""收敛层数值可靠性回归（DRIVER_PROFILE_P0_FIX_PLAN §8；P0-1A/P0-1B/P0-2）。

三个真机缺陷被钉成回归 fixture（药明康德 603259.SH 的 Tier B 真实产物暴露）：

- **P0-1A**：H1「2026H1 化学业务收入 249.9 亿（占集团 86.46%）」与
  H3「2026H1 美国客户收入 140.3 亿、占集团收入 67.45%」分别隐含集团收入 289 亿 / 208 亿，
  同一 (基数, 期别) 互相矛盾却通过了收敛层；
- **P0-1B**：H3 的 2026H1 数字（140.3 亿 / 67.45%）只有一条 2025 年报 quote，C2 只查
  "ref 非空"查不出来；占比从句内的无出处数字必须降权、其余只留痕；
- **P0-2**：「生物学业务毛利率 34.92%」曾被拿去与集团毛利率 53.9% 比较，产生假冲突
  （`research_meta.issues` 里的 `driver_profile_research_fact_conflict` 即此误报）。

范式与 ``test_research.py`` 一致：**纯函数 + ``ResearchHypothesis`` 构造 + ``converge()``
增量断言**，无 LLM、无盘、无网络。每类校验的"边界/退化"用例紧随其后，防止后续修复
把期别前缀重新吞成"分部名"而静默漏检（t2/t10 审查 R1/R2）。
"""

from __future__ import annotations

from typing import Any

import pytest

from alphabee.agents.driver_profile.output import (
    DriverResearchOutput,
    ResearchEvidence,
    ResearchHypothesis,
)
from alphabee.config import DomainContextResearchSettings
from alphabee.domain_context import research
from alphabee.domain_context.contracts import DriverProfile

#: 集团毛利率基准（药明康德 2026H1，§8.3 回归 fixture 的口径）。
GROUP_MARGIN = 53.9


# ══════════════════════════════════════════════════════════════════════════
# helpers（与 test_research.py 同范式，但不跨文件 import 其私有 helper）
# ══════════════════════════════════════════════════════════════════════════


def _profile(**overrides: Any) -> DriverProfile:
    base: dict[str, Any] = {
        "symbol": "603259.SH",
        "playbook": "generic_fundamental",
        "playbook_version": 1,
        "anchor_strength": "none",
        "fallback": True,
        "provenance": "rule",
        "primary_drivers": ["毛利率（Tier A 兜底）"],
    }
    base.update(overrides)
    return DriverProfile(**base)


def _hypothesis(
    variable: str,
    *,
    company_form: str = "",
    confidence: float = 0.9,
    ref: str = "2025 年报 第三节 管理层讨论与分析",
    quote: str = "报告期内主要业务收入同比增长。",
    kind: str = "annual_report",
) -> ResearchHypothesis:
    return ResearchHypothesis(
        variable=variable,
        company_form=company_form,
        evidence=[ResearchEvidence(kind=kind, ref=ref, quote=quote)],
        confidence=confidence,
    )


def _settings() -> DomainContextResearchSettings:
    return DomainContextResearchSettings(enabled=True, max_hypotheses=8)


def _converge(
    *hypotheses: ResearchHypothesis,
    research_input: dict[str, Any] | None = None,
) -> DriverProfile:
    return research.converge(
        _profile(),
        research.DriverResearchResult(
            output=DriverResearchOutput(driver_hypotheses=list(hypotheses)),
            meta={},
        ),
        settings=_settings(),
        research_input=research_input if research_input is not None else {},
    )


def _categories(converged: DriverProfile) -> list[str]:
    return [str(issue.get("category")) for issue in converged.research_meta["issues"]]


def _messages(converged: DriverProfile, category: str) -> list[str]:
    return [
        str(issue.get("message")) for issue in converged.research_meta["issues"] if issue.get("category") == category
    ]


#: 药明康德 H1/H3 的真实 company_form（P0-1A 与 P0-1B 的共同素材）。
WUXI_H1_FORM = "2026H1 化学业务收入249.9亿（占集团86.46%）、毛利率56.55%（同比+7.48pp）"
WUXI_H3_FORM = "2026H1 美国客户收入140.3亿、占集团收入67.45%（同比+31.0%）"


# ══════════════════════════════════════════════════════════════════════════
# §8.1 P0-1A：跨假设隐含总量一致性
# ══════════════════════════════════════════════════════════════════════════


def test_wuxi_implied_total_inconsistency_downgrades_both() -> None:
    """回归 fixture：同一基数「集团」同期别 2026H1 的两条占比声明互相矛盾 ⇒ 双方降权。"""
    converged = _converge(
        _hypothesis("化学业务驱动", company_form=WUXI_H1_FORM, confidence=0.85),
        _hypothesis(
            "美国客户驱动",
            company_form=WUXI_H3_FORM,
            confidence=0.8,
            ref="2025 年报 第三节 管理层讨论与分析",
            quote="报告期内美国客户收入同比增长。",
        ),
    )

    assert [item.confidence for item in converged.driver_hypotheses] == [0.3, 0.3]
    assert all(item.confidence <= research._CONTRADICTION_MAX_CONFIDENCE for item in converged.driver_hypotheses)
    messages = _messages(converged, "driver_profile_research_internal_inconsistency")
    assert len(messages) == 1
    # 隐含总量：249.9 / 86.46 × 100 ≈ 289 亿；140.3 / 67.45 × 100 ≈ 208 亿
    assert "289" in messages[0]
    assert "208" in messages[0]


def test_consistent_implied_totals_no_issue() -> None:
    """同基数同期别的两条声明都隐含 200 亿 ⇒ 无该 category issue、不被降权。

    注意（t1 反馈）：P0-1B 会独立检查占比从句内的数字出处，故这里**必须**给两条声明的
    数字（100/50、120/60）提供同值同期证据，否则会被 P0-1B 合法降权——
    这正是"P0-1A 无矛盾"与"数字有出处"两件事必须分开断言的原因。
    """
    converged = _converge(
        _hypothesis(
            "甲收入驱动",
            company_form="2026H1 甲业务收入100亿（占集团50%）。",
            ref="2026年半年度报告 第三节",
            quote="甲业务收入100亿元，占集团50%。",
        ),
        _hypothesis(
            "乙收入驱动",
            company_form="2026H1 乙业务收入120亿（占集团60%）。",
            ref="2026年半年度报告 第三节",
            quote="乙业务收入120亿元，占集团60%。",
        ),
    )

    assert "driver_profile_research_internal_inconsistency" not in _categories(converged)
    assert "driver_profile_research_claim_uncovered" not in _categories(converged)
    assert [item.confidence for item in converged.driver_hypotheses] == [0.9, 0.9]
    assert research._implied_total_contradictions([]) == []


def test_different_periods_are_not_compared() -> None:
    """同一基数、一条 2026H1 一条 2025FY ⇒ 不比较（期别不同）。

    可证伪性（t14 修复）：两条声明的隐含总量必须不同（100/50% = 200 亿 vs 300/60% = 500 亿），
    否则 `_period_tags` 一旦退化成"不给期别标签"，两条会被并到同一 (基数, 期别) 组里也仍然自洽、
    测试就永远绿——取 200 vs 500 后该退化会立刻触发 internal_inconsistency。
    """
    converged = _converge(
        _hypothesis("甲收入驱动", company_form="2026H1 甲业务收入100亿（占集团50%）。"),
        _hypothesis("乙收入驱动", company_form="2025年 乙业务收入300亿（占集团60%）。"),
    )
    assert "driver_profile_research_internal_inconsistency" not in _categories(converged)


def test_ambiguous_period_sentence_is_skipped() -> None:
    """一句含多个期别（2026H1 与 2025年）⇒ 整句跳过（宁可漏检不可误报）。"""
    converged = _converge(
        _hypothesis(
            "甲收入驱动",
            company_form="2026H1 甲业务收入100亿（占集团50%）对比2025年 乙业务收入900亿（占集团90%）",
        ),
        _hypothesis("丙收入驱动", company_form="2026H1 丙业务收入120亿（占集团60%）。"),
    )
    assert "driver_profile_research_internal_inconsistency" not in _categories(converged)


def test_share_claim_without_amount_is_ignored() -> None:
    """「占集团 50%」同句找不到「X 亿」⇒ 不参与隐含总量校验（交 P0-1B 兜底）。"""
    converged = _converge(
        _hypothesis("美国客户驱动", company_form="2026H1 美国客户占比提升，占集团50%（无金额口径）。"),
        _hypothesis("化学业务驱动", company_form="2026H1 化学业务占比提升，占集团80%（无金额口径）。"),
    )
    assert "driver_profile_research_internal_inconsistency" not in _categories(converged)


def test_zhan_base_stop_words_are_ignored() -> None:
    """「占比86.46%」形态的 base 会误抓成「比」⇒ 被 `_ZHAN_BASE_STOP` 过滤，不参与校验。

    可证伪性（t14 修复）：分子与「占」必须在**同一句**（不能用逗号/顿号分隔——
    `_SENTENCE_SPLIT_RE` 会把它们切成两句，配对不上就等于没测到），且两条声明的隐含总量
    必须不同（100/86.46 ≈ 115.6 亿 vs 120/30 = 400 亿）：一旦 `_ZHAN_BASE_STOP` 被去掉，
    base="比" 的两条声明就会同期别成群并因相对差 > 5% 触发 internal_inconsistency。
    """
    converged = _converge(
        _hypothesis("甲驱动", company_form="2026H1甲业务收入100亿占比86.46%。"),
        _hypothesis("乙驱动", company_form="2026H1乙业务收入120亿占比30%。"),
    )
    assert "driver_profile_research_internal_inconsistency" not in _categories(converged)


# ══════════════════════════════════════════════════════════════════════════
# §8.2 P0-1B：数字出处强度（C2.1）
# ══════════════════════════════════════════════════════════════════════════


def test_wuxi_uncovered_share_claim_downgrades() -> None:
    """回归 fixture：H3 的 2026H1 数字只有一条 2025 年报 quote ⇒ 占比从句内数字降权。"""
    converged = _converge(
        _hypothesis(
            "美国客户驱动",
            company_form=WUXI_H3_FORM,
            confidence=0.9,
            ref="2025 年报 第三节 管理层讨论与分析",
            quote="报告期内美国客户收入同比增长。",
        )
    )

    assert "driver_profile_research_claim_uncovered" in _categories(converged)
    # 67.45% 位于「占集团收入67.45%」从句内部 ⇒ 升级为降权
    assert converged.driver_hypotheses[0].confidence <= research._CONTRADICTION_MAX_CONFIDENCE


def test_claim_covered_by_evidence_same_period_no_issue() -> None:
    """声明「2026H1…149.9亿」+ 同值同期 evidence（ref=2026年半年度报告）⇒ 无 issue。"""
    converged = _converge(
        _hypothesis(
            "化学业务驱动",
            company_form="2026H1 化学业务收入149.9亿。",
            ref="2026年半年度报告 第三节 管理层讨论与分析",
            quote="化学业务收入149.9亿元，同比增长。",
        )
    )
    assert "driver_profile_research_claim_uncovered" not in _categories(converged)
    assert converged.driver_hypotheses[0].confidence == 0.9


def test_claim_covered_by_unitless_tushare_ref() -> None:
    """tushare ref 的值是无单位形态（netprofit_margin=42.53）⇒ 裸数值抽取必须能覆盖。

    这一条钉死 §4 的实现坑：`_NUMBER_RE` 要求单位，ref 里没有单位，只能用 `_RAW_NUMBER_RE`。
    """
    converged = _converge(
        _hypothesis(
            "净利率驱动",
            company_form="2025年 净利率42.53%。",
            kind="tushare",
            ref="fina_indicator 603259.SH end_date=20251231 netprofit_margin=42.53",
            quote="",
        )
    )
    assert "driver_profile_research_claim_uncovered" not in _categories(converged)


def test_claim_covered_by_input_pack_no_issue() -> None:
    """占比数字无同值证据，但输入包有同值权威值（容差 2pp 内）⇒ 覆盖，无 issue。"""
    converged = _converge(
        _hypothesis("化学业务驱动", company_form="2026H1 化学业务占集团86.46%。"),
        research_input={"latest_segments": [{"segment_name": "化学业务", "revenue_share": 86.5}]},
    )
    assert "driver_profile_research_claim_uncovered" not in _categories(converged)


def test_untagged_claim_covered_by_number_match() -> None:
    """未标注期别的声明：只看同值（任一 quote 含同值即算覆盖）。"""
    converged = _converge(
        _hypothesis(
            "化学业务驱动",
            company_form="化学业务收入149.9亿。",
            ref="资料索引",
            quote="化学业务收入149.9亿元。",
        )
    )
    assert "driver_profile_research_claim_uncovered" not in _categories(converged)


def test_operational_units_ignored() -> None:
    """运营性单位（kL/倍/个）不是财务声明 ⇒ 不检查。"""
    converged = _converge(_hypothesis("产能驱动", company_form="2026H1 反应釜总体积>4,000kL。"))
    assert "driver_profile_research_claim_uncovered" not in _categories(converged)


def test_uncovered_non_share_claim_issue_without_downgrade() -> None:
    """非占比从句内的无出处数字 ⇒ 只留痕（issue）不降权（避免同句连坐整条假设）。"""
    converged = _converge(
        _hypothesis("毛利率结构", company_form="2026H1 毛利率56.55%（同比+7.48pp）。", confidence=0.9)
    )
    assert "driver_profile_research_claim_uncovered" in _categories(converged)
    assert converged.driver_hypotheses[0].confidence == 0.9


# ══════════════════════════════════════════════════════════════════════════
# §8.3 P0-2：毛利率口径感知
# ══════════════════════════════════════════════════════════════════════════

#: P0-2 回归 fixture 的输入包：分部基准（生物学业务真实值）+ 集团值。
WUXI_SEGMENTS = {
    "latest_segments": [
        {"segment_name": "化学业务", "gross_margin": 56.55},
        {"segment_name": "生物学业务", "gross_margin": 34.9},
    ],
    "financials": {"gross_margin": GROUP_MARGIN},
}
WUXI_FX_FORM = "2026H1 生物学业务毛利率34.92%，同比-0.12pp，公司归因于汇率波动。"


def test_wuxi_segment_margin_no_false_conflict() -> None:
    """回归 fixture：生物学业务分部毛利率只与同名分部基准比 ⇒ 与集团 53.9% 不再假冲突。"""
    converged = _converge(_hypothesis("汇率假设", company_form=WUXI_FX_FORM), research_input=WUXI_SEGMENTS)

    assert "driver_profile_research_fact_conflict" not in _categories(converged)
    assert converged.driver_hypotheses[0].confidence == 0.9


def test_segment_margin_conflict_when_really_deviates() -> None:
    """同名分部基准与声明值差超容差 ⇒ 仍要报 fact_conflict + 降权。"""
    research_input = {
        "latest_segments": [{"segment_name": "生物学业务", "gross_margin": 60.0}],
        "financials": {"gross_margin": GROUP_MARGIN},
    }
    converged = _converge(_hypothesis("汇率假设", company_form=WUXI_FX_FORM), research_input=research_input)

    assert "driver_profile_research_fact_conflict" in _categories(converged)
    assert converged.driver_hypotheses[0].confidence <= research._CONTRADICTION_MAX_CONFIDENCE


def test_unknown_segment_margin_skipped() -> None:
    """无同名分部基准（TIDES 不在输入包）⇒ 跳过，而不是拿去比集团值（P0-2 防误报）。

    文档 §8.3 行取的是 45%（与集团 53.9 只差 8.9pp ≤ 15pp 容差，**不足以证伪**——即使跳过分支
    被删掉、回退去比集团值也仍然不报冲突），故与 §8.3 无名毛利率 40%→30% 的算术笔误同类处理：
    保留 45% 的 converge 断言（对齐文档行），并追加一条**超容差探针**（20%，与集团差 33.9pp）——
    跳过分支一旦被删，它会立刻报出「TIDES毛利率: 20.0% vs 实测 53.9%」。
    """
    converged = _converge(
        _hypothesis("TIDES 驱动", company_form="2026H1 TIDES业务毛利率45%。"),
        research_input=WUXI_SEGMENTS,
    )
    assert "driver_profile_research_fact_conflict" not in _categories(converged)
    # 超容差探针：无同名分部基准 ⇒ 即使与集团值差 33.9pp 也必须跳过（helper 层直接钉死）
    assert research._margin_contradictions(["TIDES业务毛利率20%"], {"gross_margin": GROUP_MARGIN}, {}) == []


def test_group_scoped_margin_still_checked() -> None:
    """集团/当年口径仍与集团值比：47.64%（差 6.26pp）不报；20%（差 33.9pp）报。"""
    assert research._margin_contradictions(["当年毛利率47.64%"], {"gross_margin": GROUP_MARGIN}, {}) == []
    assert len(research._margin_contradictions(["集团毛利率20%"], {"gross_margin": GROUP_MARGIN}, {})) == 1


def test_bare_margin_behavior_unchanged() -> None:
    """无名毛利率保留原行为（30% vs 53.9 差 23.9pp > 15pp 报冲突；55% 不报）。

    文档 §8.3 表中写的是 40%，但 53.9 − 40 = 13.9pp ≤ 15pp 容差、与旧实现一致不冲突
    （算术笔误），故按"超容差"语义取 30%。
    """
    assert len(research._margin_contradictions(["毛利率 30%"], {"gross_margin": GROUP_MARGIN})) == 1
    assert research._margin_contradictions(["毛利率 55%"], {"gross_margin": GROUP_MARGIN}) == []


# ── t2 审查 R1 与 t10 审查 R2 的期别前缀回归（防止再被吞成"分部名"而静默漏检）──

_PERIOD_PREFIX_MARGIN_FORMS = [
    "2025年",
    "上半年",
    "三季度",
    "第三季度",
    "Q3",
    "H1",
    "2025年报",
    "半年报",
    "一季报",
    "中报",
    "本月",
    "本季",
]


@pytest.mark.parametrize("prefix", _PERIOD_PREFIX_MARGIN_FORMS)
def test_period_prefixed_margin_reports_conflict(prefix: str) -> None:
    """期别/语境前缀不得被当成分部名吞掉：`<前缀>毛利率30%` + 集团 53.9 ⇒ 必须报冲突。"""
    assert research._strip_period_tokens(prefix) == ""
    form = f"{prefix}毛利率30%。"
    converged = _converge(
        _hypothesis("期别口径毛利率", company_form=form),
        research_input={"financials": {"gross_margin": GROUP_MARGIN}},
    )

    assert "driver_profile_research_fact_conflict" in _categories(converged)
    assert converged.driver_hypotheses[0].confidence <= research._CONTRADICTION_MAX_CONFIDENCE


def test_glued_period_prefix_compares_with_same_named_segment() -> None:
    """期别 token 与分部名粘连（2026H1化学业务）⇒ 剥前缀后按同名分部基准比对。"""
    research_input = {
        "latest_segments": [{"segment_name": "化学业务", "gross_margin": 60.0}],
        "financials": {"gross_margin": GROUP_MARGIN},
    }
    converged = _converge(
        _hypothesis("化学业务毛利驱动", company_form="2026H1化学业务毛利率20%。"),
        research_input=research_input,
    )

    messages = _messages(converged, "driver_profile_research_fact_conflict")
    assert len(messages) == 1
    # 文案带上被消费的「业务」后缀（R1 的 (c)：不再输出被截断的「化学毛利率」）
    assert "化学业务毛利率" in messages[0]
    assert converged.driver_hypotheses[0].confidence <= research._CONTRADICTION_MAX_CONFIDENCE


# ══════════════════════════════════════════════════════════════════════════
# §8.4 _period_tags：期别归一
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026年半年度报告", {"2026H1"}),
        ("2025年年度报告", {"2025FY"}),
        ("end_date=20260630", {"2026H1"}),
        ("end_date=20251231 / 20260630", {"2025FY", "2026H1"}),
        ("2026年收入指引", {"2026FY"}),
    ],
)
def test_period_tags_normalization(text: str, expected: set[str]) -> None:
    assert research._period_tags(text) == expected
