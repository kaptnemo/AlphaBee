"""midterm_decision_reporter 节点测试（决策层总结落日志，不进报告）。

覆盖三块：

1. 既有契约（artifact 类型/role_group 注册、关键章节、节点产出、缺产物 SKIP）；
2. **P2-6 报告呈现口径**（用户外部数据交叉核对 002916 挖出的三项误读）：
   ① 头部显式区分「财报口径（20260630）」与「估值/行情口径（20260930 + close/PE_TTM/PB）」，
   ② midterm 的 regime（熊市阶段）进入正文并给出「仓位=观察」的宏观成因，
   ③ 后验 P(H|E)=0.95 与状态确信度（S4 概率质量 0.377）分开呈现；
3. 报告 prompt 侧的口径硬约束（LLM 报告头部必须分别标注两个口径）。
"""

from __future__ import annotations

import asyncio
import hashlib

from alphabee.agents.facts.models import (
    FinancialFacts,
    FinancialSnapshot,
    MarketFacts,
)
from alphabee.core import Artifact, ArtifactRoleGroup, ArtifactType
from alphabee.midterm.models import (
    AdverseDivergence,
    CognitiveState,
    CompanyStateArtifact,
    EvidenceEvent,
    ExpectedValue,
    FactorSnapshot,
    MarketFactor,
    PositionDecision,
    ScenarioOutcome,
    StateBelief,
)
from alphabee.orchestrator.nodes import midterm_reporter as node


def _company_state(symbol="600519.SH"):
    return CompanyStateArtifact(
        symbol=symbol,
        as_of_date="2025-06-30",
        state=StateBelief(
            distribution={"S1": 0.2, "S2": 0.8},
            argmax_state=CognitiveState.S2_CONFIRM.value,
            entropy=0.3,
        ),
        thesis="看多核心观点",
        thesis_confidence=0.6,
        prior_confidence=0.5,
        expected_value=ExpectedValue(
            scenarios=[
                ScenarioOutcome(scenario="bull", probability=0.3, expected_return=40.0),
                ScenarioOutcome(scenario="base", probability=0.5, expected_return=5.0),
                ScenarioOutcome(scenario="bear", probability=0.2, expected_return=-20.0),
            ],
            ev=8.5,
            risk=20.0,
            risk_adjusted_ev=0.425,
            probability_source="bayes_posterior",
        ),
        evidence_log=[
            EvidenceEvent(
                id="ev-1",
                date="2025-06-01",
                kind="expectation",
                description="一致预期上修",
                effect_on_thesis="confirming",
                confidence_delta=0.1,
            )
        ],
        position=PositionDecision(position_band="加仓", stock_weight=0.4, actual_weight=0.32),
    )


def _midterm_artifact(symbol="600519.SH"):
    return Artifact(
        id="a-midterm",
        type=ArtifactType.MIDTERM_DECISION,
        producer_step="resolve_midterm_decision",
        value=_company_state(symbol).model_dump(mode="json"),
    )


def _state(artifacts):
    return {"artifacts": artifacts, "issues": []}


def _state_with_facts(
    artifacts,
    *,
    financial_period="20260630",
    market_trade_date="20260930",
    close_price=373.01,
    pe_ttm=60.97,
    pb_ratio=14.09,
    market_facts=True,
):
    """构造带 financial_facts / market_facts 的 state（P2-6 口径时点的两个来源）。"""
    state = _state(artifacts)
    state["financial_facts"] = FinancialFacts(
        stock_code="002916.SZ",
        snapshots=[FinancialSnapshot(period=financial_period)],
    )
    if market_facts:
        state["market_facts"] = MarketFacts(
            stock_code="002916.SZ",
            trade_date=market_trade_date,
            close_price=close_price,
            pe_ttm=pe_ttm,
            pb_ratio=pb_ratio,
        )
    return state


def _summary_text(result) -> str:
    summaries = [a for a in result["artifacts"] if a.type == ArtifactType.MIDTERM_DECISION_SUMMARY]
    assert len(summaries) == 1
    return summaries[0].value["text"]


def _rendered_text(decision, state) -> str:
    """经节点渲染（payload → 总结 artifact），与真实投递路径一致。"""
    artifact = Artifact(
        id="a-midterm",
        type=ArtifactType.MIDTERM_DECISION,
        producer_step="resolve_midterm_decision",
        value=decision.model_dump(mode="json"),
    )
    return _summary_text(asyncio.run(node.report_midterm_decision(state([artifact]), {})))


def test_summary_artifact_type_and_role_group_registered():
    assert ArtifactType.MIDTERM_DECISION_SUMMARY.value == "midterm_decision_summary"
    from alphabee.core.schemas import _ARTIFACT_TYPE_TO_ROLE_GROUP

    assert _ARTIFACT_TYPE_TO_ROLE_GROUP[ArtifactType.MIDTERM_DECISION_SUMMARY] == ArtifactRoleGroup.DECISION


def test_render_summary_contains_key_sections():
    text = node.render_midterm_decision_summary(_company_state())

    assert "中期决策总结" in text
    assert "600519.SH" in text
    assert "argmax: S2" in text
    assert "看多核心观点" in text
    assert "EV=8.5" in text
    assert "加仓" in text
    assert "一致预期上修" in text


def test_node_produces_summary_artifact():
    result = asyncio.run(node.report_midterm_decision(_state([_midterm_artifact()]), {}))

    assert result["steps"][0].status.value == "succeeded"
    summaries = [a for a in result["artifacts"] if a.type == ArtifactType.MIDTERM_DECISION_SUMMARY]
    assert len(summaries) == 1
    assert "600519.SH" in summaries[0].value["text"]
    assert summaries[0].value["symbol"] == "600519.SH"


def test_node_skips_when_no_midterm_decision():
    result = asyncio.run(node.report_midterm_decision(_state([]), {}))

    assert result["steps"][0].status.value == "skipped"
    assert result.get("artifacts", []) == []
    assert result.get("issues", []) == []


# ── P2-6 ① 头部两个时点分开标注 ──────────────────────────────────────────────


def test_report_header_separates_financial_and_valuation_time_points():
    """头部必须把财报口径与估值/行情口径分行标注，且两类日期不得互换/合并。"""
    decision = _company_state("002916.SZ")
    text = _rendered_text(decision, lambda arts: _state_with_facts(arts))

    assert "财报口径（财务数据报告期）: 20260630" in text
    assert "估值/行情口径（行情截止日）: 20260930" in text
    # 估值数字随行情口径一起出现（读者不会再把它当财报期末估值）
    assert "收盘价 373.01" in text
    assert "PE_TTM 60.97" in text
    assert "PB 14.09" in text
    assert "属估值/行情口径，不得按财报期末口径解读" in text
    # 合并/互换即失败：两个口径各自只允许自己的日期
    assert "财报口径（财务数据报告期）: 20260930" not in text
    assert "估值/行情口径（行情截止日）: 20260630" not in text


def test_valuation_time_point_not_backfilled_from_decision_date():
    """行情截止日缺失时必须显式「（未提供）」——不得用决策日/采集日顶替。"""
    decision = _company_state("600519.SH")  # as_of_date = 2025-06-30
    text = _rendered_text(decision, lambda arts: _state_with_facts(arts, market_facts=False))

    assert "财报口径（财务数据报告期）: 20260630" in text
    assert "估值/行情口径（行情截止日）: （未提供）" in text
    valuation_line = next(line for line in text.splitlines() if "估值/行情口径（行情截止日）" in line)
    assert "2025-06-30" not in valuation_line


def test_financial_period_missing_is_not_silently_omitted():
    decision = _company_state("600519.SH")
    text = _rendered_text(decision, lambda arts: _state_with_facts(arts, financial_period=""))

    assert "财报口径（财务数据报告期）: （未提供）" in text
    assert "估值/行情口径（行情截止日）: 20260930" in text


# ── P2-6 ② regime 进入正文 + 仓位成因 ───────────────────────────────────────


def _regime_decision(symbol="002916.SZ", band="观察"):
    return _company_state(symbol).model_copy(
        update={
            "state": StateBelief(
                distribution={"S3": 0.3, "S4": 0.377, "S5": 0.323},
                argmax_state="S4",
                entropy=1.05,
            ),
            "thesis_confidence": 0.95,
            "factor_snapshot": FactorSnapshot(
                symbol=symbol,
                as_of_date="2026-09-30",
                state="S4",
                confidence=0.95,
                market=MarketFactor(
                    regime="熊市阶段",
                    market_score=13.65,
                    position_low=0.0,
                    position_high=0.2,
                    hs300_close=4358.0,
                    hs300_pe_ttm=14.06,
                    hs300_pb=1.478,
                ),
            ),
            "position": PositionDecision(
                position_band=band,
                portfolio_exposure=0.1,
                stock_weight=0.0,
                actual_weight=0.0,
            ),
        }
    )


def test_summary_presents_market_regime_and_position_cause():
    """regime（熊市阶段）必须进入正文，并显式给出「仓位=观察」的宏观成因。"""
    text = node.render_midterm_decision_summary(_regime_decision())

    assert "市场环境（regime，宏观约束）" in text
    assert "市场阶段: 熊市阶段" in text
    assert "建议暴露带: 0–0.2" in text
    assert "沪深300: close=4358  PE_TTM=14.06  PB=1.478" in text
    assert "仓位成因: 仓位档位「观察」" in text
    cause_line = next(line for line in text.splitlines() if "仓位成因:" in line)
    assert "熊市阶段" in cause_line
    assert "0–0.2" in cause_line
    assert "不是个股基本面判断的降级" in cause_line


def test_regime_section_reads_variable_scores_fallback():
    """factor_snapshot.market 缺失时退回 variable_scores.m（同一 M 因子摘要）。"""
    decision = _regime_decision().model_copy(
        update={
            "factor_snapshot": None,
            "variable_scores": _regime_decision().variable_scores.model_copy(
                update={
                    "m": {
                        "regime": "熊市阶段",
                        "market_score": 13.65,
                        "position_low": 0.0,
                        "position_high": 0.2,
                        "hs300_close": 4358.0,
                        "hs300_pe_ttm": 14.06,
                        "hs300_pb": 1.478,
                    }
                }
            ),
        }
    )
    text = node.render_midterm_decision_summary(decision)

    assert "市场阶段: 熊市阶段" in text
    assert "仓位成因: 仓位档位「观察」" in text


def test_regime_section_is_explicit_when_market_missing():
    """M 因子未采集时不得静默省略该章节：必须显式记账「未采集」。"""
    text = node.render_midterm_decision_summary(_company_state())

    assert "市场环境（regime，宏观约束）" in text
    assert "缺失：market_regime 未采集" in text


# ── P2-6 ③ 后验与状态确信度分开呈现 ─────────────────────────────────────────


def test_posterior_and_state_confidence_are_separated():
    """0.95 是假设 H 的后验，S4=0.377 是状态确信度：必须各自独立成行、不得并列。"""
    decision = _regime_decision()
    text = node.render_midterm_decision_summary(decision)

    assert "状态确信度: S4=0.377（argmax 状态的概率质量，不是假设 H 的后验）" in text
    assert "后验 P(H|E): 0.95" in text
    assert "0.95 是假设 H 的贝叶斯后验 P(H|E)，不是状态确信度" in text
    assert "状态确信度见「认知状态」节（S4 概率质量 0.377）" in text
    # 并列误导的原始形态不得出现
    assert "状态 S4 置信度 0.95" not in text
    assert "S4 置信度 0.95" not in text
    # 后验行与状态确信度行是两条独立行（不共行）
    posterior_line = next(line for line in text.splitlines() if "后验 P(H|E)" in line)
    confidence_line = next(line for line in text.splitlines() if "状态确信度:" in line)
    assert posterior_line != confidence_line
    assert "S4=0.377" not in posterior_line


def test_state_confidence_phrase_handles_missing_state():
    decision = _company_state().model_copy(update={"state": None})
    text = node.render_midterm_decision_summary(decision)

    assert "（缺失：state=None）" in text
    assert "state 缺失，无状态确信度可比" in text


# ── P2-6：LLM 报告侧的口径硬约束（prompt 契约） ─────────────────────────────


def test_report_prompt_requires_two_time_point_disclosure():
    from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT

    assert "财报口径" in REPORT_GENERATOR_PROMPT
    assert "估值/行情口径" in REPORT_GENERATOR_PROMPT
    assert "禁止把 PE_TTM / PB / 收盘价表述为" in REPORT_GENERATOR_PROMPT
    assert "估值/行情时点未在输入中提供" in REPORT_GENERATOR_PROMPT


# ── 状态证据 V2（design §6.6）：v2 专属行 / v1 帧渲染零变化 / 顶背离节 ──────────

_V2_DISTRIBUTION = {"S0": 0.097, "S1": 0.025, "S2": 0.034, "S3": 0.025, "S4": 0.025, "S5": 0.795}
_V2_STATE_EVIDENCE = {"S0": 0.3, "S1": 0.0, "S2": 0.0692, "S3": 0.0, "S4": 0.0, "S5": 0.7626}
# 改造前（v2 接线前）本文件 _company_state() 的渲染全文 sha256：v1 帧必须逐字不变。
_V1_REPORT_SHA256 = "61092601239ade2192e8a795d64f811ffb45eac234a4b22247fa141bd2ad4cbb"


def _v2_decision(**overrides):
    """v2 帧：600760 实帧口径（S5 / P_top 0.795 / H_norm 0.447 / 顶背离 severity 0.76）。"""
    updates = {
        "classifier_formula": "v2",
        "classifier_order": "tef",
        "entropy_normalized": 0.447,
        "state": StateBelief(distribution=dict(_V2_DISTRIBUTION), argmax_state="S5", entropy=0.7993),
        "uncertain": False,
        "factor_consistency": "divergent",
        "divergence_reason": "基本面与预期同时实质恶化而价格仍强（顶背离）",
        "adverse_divergence": AdverseDivergence(
            severity=0.7626,
            fundamental_severity=1.0,
            expectation_severity=0.7626,
            price_strength=1.0,
            description="基本面与预期同时实质恶化而价格仍强（顶背离）",
        ),
        "state_evidence": dict(_V2_STATE_EVIDENCE),
        "factor_direction": -0.5826,
    }
    updates.update(overrides)
    return _company_state().model_copy(update=updates)


def test_v1_frame_render_is_byte_identical_to_pre_change():
    """§6.6 第 5 条：v1 帧（formula 缺省 "v1" 且 state_evidence 为空）渲染保持改造前逐字不变。"""
    text = node.render_midterm_decision_summary(_company_state())
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _V1_REPORT_SHA256
    for marker in ("归一化证据", "口径: formula=", "不确定:", "factor_direction", "formula=", "顶背离"):
        assert marker not in text


def test_v2_frame_renders_normalized_entropy_evidence_and_caliber():
    text = node.render_midterm_decision_summary(_v2_decision())
    assert "熵: 0.7993（归一化 0.447）" in text
    assert "归一化证据: S0=0.3  S1=0  S2=0.0692  S3=0  S4=0  S5=0.7626" in text
    assert "口径: formula=v2 / order=tef；P 为归一化证据的 softmax 权重，非频率概率" in text
    assert "一致性: divergent（基本面与预期同时实质恶化而价格仍强（顶背离））" in text
    assert "不确定: False（P_top=0.795 ≥ 0.55）" in text
    assert "状态确信度: S5=0.795" in text


def test_v2_frame_without_state_evidence_falls_back_to_v1_rendering():
    """「state_evidence 为空」同样按 v1 渲染（§6.6 第 5 条的或条件），不输出 v2 专属行。"""
    text = node.render_midterm_decision_summary(_v2_decision(state_evidence={}))
    for marker in ("归一化证据", "口径: formula=", "不确定:", "factor_direction", "formula="):
        assert marker not in text


def test_v1_formula_with_stray_evidence_still_renders_as_v1():
    """§6.6 的「或」条件另一分支：formula=="v1" 时即使 state_evidence 非空也按 v1 渲染。"""
    text = node.render_midterm_decision_summary(_v2_decision(classifier_formula="v1", adverse_divergence=None))
    for marker in ("归一化证据", "口径: formula=", "不确定:", "factor_direction", "formula=", "顶背离"):
        assert marker not in text


def test_v2_frame_without_normalized_entropy_keeps_plain_entropy_line():
    text = node.render_midterm_decision_summary(_v2_decision(entropy_normalized=None))
    entropy_line = next(line for line in text.splitlines() if "argmax: S5" in line)
    assert "归一化" not in entropy_line


def test_adverse_divergence_section_renders_severity_and_three_components():
    text = node.render_midterm_decision_summary(_v2_decision())
    assert "顶背离（结构性反证）" in text
    assert "kind: fundamental_adverse_vs_price_strength    severity: 0.7626" in text
    assert "分解: fundamental_severity=1 × expectation_severity=0.7626 × price_strength=1" in text
    assert "文案: 基本面与预期同时实质恶化而价格仍强（顶背离）" in text
    assert "不作为加仓证据" in text


def test_adverse_divergence_section_absent_when_none():
    text = node.render_midterm_decision_summary(
        _v2_decision(adverse_divergence=None, factor_consistency="independent", divergence_reason="")
    )
    assert "顶背离（结构性反证）" not in text
    assert "不作为加仓证据" not in text
    assert "一致性: independent" in text


def test_variable_scores_factor_direction_line_and_meta_formula():
    text = node.render_midterm_decision_summary(_v2_decision())
    assert "情景方向 factor_direction: -0.5826（v2 = 0.7×mean(F,E) + 0.3×mean(F,E,T)；v1 为六因子均值）" in text
    assert "formula=v2" in text.split("── 元信息")[1]


def test_factor_direction_missing_is_explicit():
    text = node.render_midterm_decision_summary(_v2_decision(factor_direction=None))
    assert "情景方向 factor_direction: —（缺失）" in text


def test_v2_frame_survives_node_payload_roundtrip():
    """接线路径（payload → 节点 → summary artifact）同样带 v2 行。"""
    text = _rendered_text(_v2_decision(), _state)
    assert "归一化证据" in text and "formula=v2" in text
