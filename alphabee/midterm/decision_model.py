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

from typing import Any

from alphabee.midterm.bayes import ScenarioProbability, scenario_probability, update_confidence
from alphabee.midterm.classifier import classify_state
from alphabee.midterm.models import (
    CompanyStateArtifact,
    EvidenceEvent,
    ExpectedValue,
    FactorSnapshot,
    MarketFactor,
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


def _market_exposure(market: MarketFactor) -> float | None:
    """把 M 的 PositionAdvice（position_low/high）合成单值市场暴露（§7.1）。

    取 ``[position_low, position_high]`` **中值**作为中性暴露（既不激进也不保守，
    且确定性）；仅一侧有值时用该侧；两侧都缺失 → ``None``（``position.actual_weight``
    随之 ``None``，不静默回退 0）。
    """
    low = market.position_low
    high = market.position_high
    if low is not None and high is not None:
        return (low + high) / 2.0
    if low is not None:
        return low
    return high


# 方向分 → 子模型 direction 标签的阈值（与 classifier._UP_T / _DOWN_T 同口径，±0.3）
_DIRECTION_UP = 0.3
_DIRECTION_DOWN = -0.3


def _score_0_100(direction_score: float | None) -> float | None:
    """``[-1, 1]`` 方向分 → ``0-100`` 分数（50=中性，100=最有利，0=最不利）。

    缺失方向分（``None``）→ ``None``（缺失显式 None，不静默回退 50）。
    """
    if direction_score is None:
        return None
    return 50.0 + 50.0 * direction_score


def _writeback_directions(snapshot: FactorSnapshot, scores: VariableScores) -> None:
    """把 ``compress_scores`` 的方向分回写进各因子子模型的 ``direction`` / ``score``。

    消除「``VariableScores`` 已有方向分，但子模型仍停在 ``direction=neutral`` /
    ``score=None``」的两套不一致（dead 字段）。映射（方向分均为 ``[-1, 1]``，
    正 = 对多头有利）：

    - F/E/T：直接用方向分；``> +0.3`` → improving，``< -0.3`` → deteriorating，
      否则 F 为 stable、E/T 为 neutral；
    - V：方向分正 = 估值分位低（便宜）→ cheap；负 → expensive；中间 → fair；
    - C：方向分正 = 不拥挤（冷）→ cold；负 → overheated；中间 → normal；
    - R：方向分正 = 风险下降 → risk_declining；负 → risk_rising；中间 → neutral。

    ``score`` 统一映射为 0-100（50=中性），与「正=有利」约定一致。方向分缺失时
    保持子模型默认（``score=None``，``direction`` 维持原默认值），不制造虚假方向。
    """

    def _label(score: float, up: str, down: str, neutral: str) -> str:
        if score > _DIRECTION_UP:
            return up
        if score < _DIRECTION_DOWN:
            return down
        return neutral

    def _apply(factor: Any, score: float | None, up: str, down: str, neutral: str) -> None:
        if score is None:
            return  # 缺失：保持模型默认（direction 默认值 + score=None）
        factor.direction = _label(score, up, down, neutral)
        factor.score = _score_0_100(score)

    _apply(snapshot.fundamental, scores.f_fundamental_trend, "improving", "deteriorating", "stable")
    _apply(snapshot.expectation, scores.e_revision, "improving", "deteriorating", "neutral")
    _apply(snapshot.trend, scores.t_relative_strength, "improving", "deteriorating", "neutral")
    _apply(snapshot.valuation, scores.v_valuation_percentile, "cheap", "expensive", "fair")
    _apply(snapshot.crowding, scores.c_crowding, "cold", "overheated", "normal")
    _apply(snapshot.risk, scores.r_risk, "risk_declining", "risk_rising", "neutral")


def _estimate_expected_value(
    snapshot: FactorSnapshot,
    scenario_probs: ScenarioProbability,
    scores: VariableScores,
    *,
    has_evidence: bool = True,
) -> ExpectedValue:
    """由 ScenarioProbability + 估值分位 + EPS 修订估计 ExpectedValue（§5.2）。

    R 分解为 ``earnings_contribution``（E 修订驱动）与 ``valuation_contribution``
    （估值分位驱动，低分位=便宜=上行空间）。估值分位缺失时 R/EV 显式 ``None``
    （不静默回退）；E 修订缺失时 earnings 贡献显式 ``None``，仅估值贡献 R。

    零证据（``has_evidence=False``）时 EV 显式降级：保守 state_prior 不支撑可信 EV，
    ``ev=None`` 并注明降级原因，不静默给激进 EV。
    """
    if not has_evidence:
        return ExpectedValue(
            scenarios=scenario_probs.as_scenarios(),
            ev=None,
            risk=None,
            risk_adjusted_ev=None,
            probability_source=scenario_probs.probability_source,
            as_of_date=snapshot.as_of_date,
            note="零证据：EV 无 EvidenceEvent 支撑，由保守 state_prior 推导，显式降级（ev=None）",
        )

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

    bull_earn: float | None
    base_earn: float | None
    bear_earn: float | None
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

    # 回写快照：消除顶层 StateBelief（argmax=Sx）与 factor_snapshot.state 残留 S0、
    # confidence 残留默认值的自相矛盾，并把方向分写进各因子子模型（消除
    # direction/score 与 VariableScores 两套不一致的 dead 字段）。
    snapshot.state = cls.state.argmax_state
    # confidence 缺失（无证据无先验）时按模型非空字段约定回退 0.0（与 thesis_confidence 同口径）
    snapshot.confidence = confidence if confidence is not None else 0.0
    _writeback_directions(snapshot, scores)

    has_evidence = bool(evidence)
    scenario_probs = scenario_probability(cls.state.argmax_state, confidence=confidence, has_evidence=has_evidence)
    ev = _estimate_expected_value(snapshot, scenario_probs, scores, has_evidence=has_evidence)

    position = build_position(
        cls.state,
        confidence=confidence,
        risk_adjusted_ev=ev.risk_adjusted_ev,
        market_exposure=_market_exposure(snapshot.market),
        risk_adjustment=scores.r_risk,
        portfolio_adjustment=portfolio_adjustment,
        single_stock_cap=single_stock_cap,
    )
    position.rationale.append(
        f"市场暴露：PositionAdvice=[{snapshot.market.position_low}, "
        f"{snapshot.market.position_high}] → 取中值 {_market_exposure(snapshot.market)}"
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


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="AlphaBee Midterm Decision Model")
    parser.add_argument("symbol", type=str, help="股票代码（如 600519）")
    parser.add_argument("--evidence", type=str, default=None, help="证据日志 JSON 文件路径")
    parser.add_argument("--prior_confidence", type=float, default=None, help="先验 P(H)")
    parser.add_argument("--thesis", type=str, default="", help="核心假设 H")
    args = parser.parse_args()

    evidence = None
    if args.evidence:
        with open(args.evidence, encoding="utf-8") as f:
            evidence = [EvidenceEvent(**item) for item in json.load(f)]

    artifact = get_decision(
        symbol=args.symbol,
        evidence=evidence,
        prior_confidence=args.prior_confidence,
        thesis=args.thesis,
    )

    print(artifact.model_dump_json(indent=2, ensure_ascii=False))
