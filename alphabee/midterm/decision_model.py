"""Layer 3 编排入口：把四引擎串成完整决策流水线（设计文档 §6 / §9）。

``evaluate(snapshot, evidence) -> CompanyStateArtifact`` 是确定性主函数，数据单向流动：

```text
FactorSnapshot
  → score_engine.compress_scores  → VariableScores
  → classifier.classify_state     → StateBelief + StateTransition + 一致性
  → bayes.update_confidence       → Confidence（P(H|E)）
  → bayes.scenario_probability    → ScenarioProbability（P_bull/base/bear）
  → 情景估值 / EV                  → ExpectedValue（EV = Σ P·R）
  → position.build_position       → PositionDecision（三轴 × M × 组合）
  → 组装 CompanyStateArtifact
```

核心纪律：

- **确定性纯函数**：``evaluate`` 只编排四个确定性引擎，不调 LLM。
- **无 dead-end**（``alphabee-pipeline-contract-steward``）：classifier 的
  ``research_tasks`` 落入 ``CompanyStateArtifact.next_evidence_to_watch``；
  bayes 的 ``ScenarioProbability`` 落入 ``ExpectedValue.scenarios``；四引擎产出
  全部组装进 artifact。
- **缺失显式 ``None``**（``alphabee-schema-steward``）：估值缺失时情景收益 R 与 EV
  显式 ``None``（不静默回退）；``snapshot.degraded`` 透传。
"""

from __future__ import annotations

from alphabee.midterm.bayes import ScenarioProbability, scenario_probability, update_confidence
from alphabee.midterm.classifier import classify_state
from alphabee.midterm.models import (
    CompanyStateArtifact,
    EvidenceEvent,
    ExpectedValue,
    FactorSnapshot,
    ScenarioOutcome,
    VariableScores,
)
from alphabee.midterm.position import build_position
from alphabee.midterm.score_engine import compress_scores

# ─────────────────────────────────────────────────────────────────────────────
# 情景收益估计参数（结构性示意，§6.3 应回测）
# ─────────────────────────────────────────────────────────────────────────────

_BULL_SCALE = 40.0  # PERCENT 估值分位 0 → bull 估值扩张贡献 +40%
_BEAR_SCALE = 30.0  # PERCENT 估值分位 1 → bear 估值压缩额外 -30%
_BEAR_BASE = 10.0  # PERCENT bear 基础下行 -10%（即使分位 0）
_EARN_SCALE = 10.0  # PERCENT E 修订 +1 → 收益 earnings 贡献 +10%


def _estimate_expected_value(
    snapshot: FactorSnapshot,
    scenario_probs: ScenarioProbability,
    scores: VariableScores,
) -> ExpectedValue:
    """由 ScenarioProbability + 估值分位 + EPS 修订估计 ExpectedValue（§5.2）。

    R 分解为 ``earnings_contribution``（E 修订驱动）与 ``valuation_contribution``
    （估值分位驱动，低分位=便宜=上行空间）。估值分位缺失时 R/EV 显式 ``None``
    （不静默回退）；E 修订缺失时 earnings 贡献显式 ``None``，仅估值贡献 R。
    """
    percentile = snapshot.valuation.percentile
    if percentile is None:
        percentile = snapshot.valuation.pe_ttm_5y_percentile

    e_rev = scores.e_revision

    if percentile is None:
        scenarios = [
            ScenarioOutcome(scenario="bull", probability=scenario_probs.p_bull),
            ScenarioOutcome(scenario="base", probability=scenario_probs.p_base),
            ScenarioOutcome(scenario="bear", probability=scenario_probs.p_bear),
        ]
        return ExpectedValue(
            scenarios=scenarios,
            ev=None,
            risk=None,
            risk_adjusted_ev=None,
            probability_source=scenario_probs.probability_source,
            as_of_date=snapshot.as_of_date,
            note="估值分位缺失，无法估计情景收益 R",
        )

    bull_val = (1.0 - percentile) * _BULL_SCALE
    bear_val = -(percentile * _BEAR_SCALE + _BEAR_BASE)
    base_val = 0.0

    if e_rev is not None:
        bull_earn = e_rev * _EARN_SCALE
        base_earn = e_rev * _EARN_SCALE * 0.5
        bear_earn = e_rev * _EARN_SCALE * 0.5
    else:
        bull_earn = base_earn = bear_earn = None

    def _ret(val: float, earn: float | None) -> float:
        return val if earn is None else val + earn

    scenarios = [
        ScenarioOutcome(
            scenario="bull",
            probability=scenario_probs.p_bull,
            expected_return=_ret(bull_val, bull_earn),
            earnings_contribution=bull_earn,
            valuation_contribution=bull_val,
        ),
        ScenarioOutcome(
            scenario="base",
            probability=scenario_probs.p_base,
            expected_return=_ret(base_val, base_earn),
            earnings_contribution=base_earn,
            valuation_contribution=base_val,
        ),
        ScenarioOutcome(
            scenario="bear",
            probability=scenario_probs.p_bear,
            expected_return=_ret(bear_val, bear_earn),
            earnings_contribution=bear_earn,
            valuation_contribution=bear_val,
        ),
    ]

    ev = sum(s.probability * (s.expected_return or 0.0) for s in scenarios)
    risk = abs(bear_val + (bear_earn or 0.0))  # 风险度量 = 下行幅度（示意）
    risk_adjusted_ev = ev / risk if risk > 0.0 else None

    return ExpectedValue(
        scenarios=scenarios,
        ev=ev,
        risk=risk,
        risk_adjusted_ev=risk_adjusted_ev,
        probability_source=scenario_probs.probability_source,
        as_of_date=snapshot.as_of_date,
        note="结构性示意：EV=ΣP·R，R 由估值分位 + EPS 修订估计",
    )


def evaluate(
    snapshot: FactorSnapshot,
    evidence: list[EvidenceEvent] | None = None,
    *,
    prior_confidence: float | None = None,
    thesis: str = "",
    portfolio_adjustment: float = 1.0,
    single_stock_cap: float | None = None,
) -> CompanyStateArtifact:
    """串起四引擎，把 FactorSnapshot 决策为 CompanyStateArtifact（§6 三轴 + §9）。

    Args:
        snapshot: 七因子快照（``alphabee.midterm.factors.get_factor_snapshot`` 产物）。
        evidence: 证据日志（``EvidenceEvent`` 列表）；``None``/空表示无证据。
        prior_confidence: 先验 P(H)（0-1）；``None`` 且无证据时后验为 ``None``。
        thesis: 核心假设 H（文本，透传进 artifact）。
        portfolio_adjustment: 组合层相关性/集中度调整乘数（默认 1.0 无调整）。
        single_stock_cap: 单股上限（RATIO），超限置 ``position.restricted``。

    Returns:
        :class:`CompanyStateArtifact`：state=StateBelief、thesis_confidence、
        variable_scores、factor_snapshot、expected_value、position 等，无 dead-end。
    """
    scores = compress_scores(snapshot)
    cls = classify_state(scores)

    confidence = update_confidence(evidence, prior=prior_confidence)
    scenario_probs = scenario_probability(cls.state.argmax_state, confidence=confidence)
    ev = _estimate_expected_value(snapshot, scenario_probs, scores)

    position = build_position(
        cls.state,
        confidence=confidence,
        risk_adjusted_ev=ev.risk_adjusted_ev,
        market_exposure=snapshot.market.position_high,
        risk_adjustment=scores.r_risk,
        portfolio_adjustment=portfolio_adjustment,
        single_stock_cap=single_stock_cap,
    )

    prior = prior_confidence if prior_confidence is not None else 0.0
    thesis_confidence = confidence if confidence is not None else prior

    return CompanyStateArtifact(
        symbol=snapshot.symbol,
        state=cls.state,
        thesis=thesis,
        thesis_confidence=thesis_confidence,
        prior_confidence=prior,
        variable_scores=scores,
        factor_snapshot=snapshot,
        expected_value=ev,
        evidence_log=list(evidence or []),
        next_evidence_to_watch=[t.unknown for t in cls.research_tasks],
        position=position,
        as_of_date=snapshot.as_of_date,
        degraded=snapshot.degraded,
        degraded_reason=snapshot.degraded_reason,
    )


def get_decision(
    symbol: str,
    evidence: list[EvidenceEvent] | None = None,
    *,
    include_market: bool = True,
    prior_confidence: float | None = None,
    thesis: str = "",
) -> CompanyStateArtifact:
    """便捷入口：``get_factor_snapshot(symbol)`` 后再 ``evaluate``。

    Args:
        symbol: 股票代码（``"600519"`` / ``"600519.SH"`` / ``"300750.SZ"``）。
        evidence: 证据日志；``None``/空表示无证据。
        include_market: 是否采集 M（market_regime，开销较大）。
        prior_confidence / thesis: 透传 ``evaluate``。

    Returns:
        :class:`CompanyStateArtifact`。
    """
    from alphabee.midterm.factors import get_factor_snapshot

    snapshot = get_factor_snapshot(symbol, include_market=include_market)
    return evaluate(snapshot, evidence, prior_confidence=prior_confidence, thesis=thesis)
