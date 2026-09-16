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

from datetime import date, timedelta
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

# 改造 D（设计 MIDTERM_INSIGHT_INJECTION_DESIGN.md §4）EV 收益 materiality 化：
# materiality_rank 变量名中识别「下行变量」的关键词（子串匹配，显式可维护）。
# 这些变量的 critical 化会加深 bear 收益——公司特定下行，而非纯估值分位公式。
_DOWNSIDE_VARS: tuple[str, ...] = (
    "存货",  # 存货去化与减值
    "商誉",  # 商誉减值
    "汇兑",  # 汇兑持续 / 汇兑损益
    "减值",  # 各类资产减值
    "应收账款",  # 应收账款坏账
    "现金流",  # 经营/自由现金流恶化
    "杠杆",  # 杠杆 / 负债
    "质押",  # 股权质押风险
    "毛利率",  # 毛利率下滑
)
_MATERIALITY_PENALTY_PER_ITEM = 5.0  # PERCENT 每个 critical 下行变量加深 bear 5%


def _market_exposure_from_bounds(low: float | None, high: float | None) -> float | None:
    """把 ``[position_low, position_high]`` 合成单值市场暴露（中值）。

    取中值作为中性暴露（确定性）；仅一侧有值时用该侧；两侧都缺失 → ``None``。
    """
    if low is not None and high is not None:
        return (low + high) / 2.0
    if low is not None:
        return low
    return high


def _market_exposure(market: MarketFactor) -> float | None:
    """把 M 的 PositionAdvice（position_low/high）合成单值市场暴露（§7.1）。

    取 ``[position_low, position_high]`` **中值**作为中性暴露（既不激进也不保守，
    且确定性）；仅一侧有值时用该侧；两侧都缺失 → ``None``（``position.actual_weight``
    随之 ``None``，不静默回退 0）。
    """
    return _market_exposure_from_bounds(market.position_low, market.position_high)


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


def _factor_direction(scores: VariableScores) -> float | None:
    """因子方向分均值（正=看多，负=看空），供 ``scenario_probability`` 的情景极化方向。

    七个因子方向分由 ``score_engine.compress_scores`` 产出，符号已统一（正=对多头有利），
    因此可直接聚合为「多空倾向」——F 看多（营收/份额/细分）与 C 看空（拥挤）同时存在时，
    均值自然反映「好公司但贵/拥挤」的混合倾向，而非证据层 ``effect_on_thesis`` 那种
    相对 H 的确认/反驳（会因 H 偏空而 sign error）。全部缺失 → ``None``。
    """
    vals = [
        scores.f_fundamental_trend,
        scores.e_revision,
        scores.t_relative_strength,
        scores.v_valuation_percentile,
        scores.c_crowding,
        scores.r_risk,
    ]
    avail = [v for v in vals if v is not None]
    if not avail:
        return None
    return sum(avail) / len(avail)


def _filter_fresh_evidence(
    evidence: list[EvidenceEvent] | None,
    as_of_date: str,
    *,
    max_age_days: int = 365,
) -> list[EvidenceEvent]:
    """过滤过期证据（P2 时效）：``date`` 早于 ``as_of_date - max_age_days`` 的事件丢弃。

    历史业绩预告（如 2024/2025 的 +30~40% 预告）在当期决策中属于「已结算」的旧信息，
    继续参与贝叶斯置信度更新会虚增 confidence（用旧利好推高后验）。日期无法解析的
    事件保守保留（不因解析失败丢证据）。
    """
    if not evidence or not as_of_date:
        return list(evidence or [])
    try:
        cutoff = date.fromisoformat(as_of_date) - timedelta(days=max_age_days)
    except ValueError:
        return list(evidence)  # as_of_date 无法解析 → 不过滤（保守）

    fresh: list[EvidenceEvent] = []
    for e in evidence:
        try:
            if date.fromisoformat(e.date) >= cutoff:
                fresh.append(e)
        except ValueError:
            fresh.append(e)  # 事件日期无法解析 → 保留（保守）
    return fresh


def _materiality_penalty(insight_materiality: Any) -> float:
    """materiality 修正（改造 D）：critical 且命中下行变量 → 每个加深 bear 5%。

    ``insight_materiality`` 为 ``insight.materiality_rank``（``list[{variable,
    importance, reasoning}]``）；仅 ``importance=="critical"`` 且 ``variable`` 命中
    ``_DOWNSIDE_VARS`` 关键字的项计入惩罚。缺失/空/非 dict → 0（不编造）。
    """
    penalty = 0.0
    for item in insight_materiality or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("importance") or "").strip().lower() != "critical":
            continue
        variable = str(item.get("variable") or "").strip()
        if any(kw in variable for kw in _DOWNSIDE_VARS):
            penalty += _MATERIALITY_PENALTY_PER_ITEM
    return penalty


def _estimate_expected_value(
    snapshot: FactorSnapshot,
    scenario_probs: ScenarioProbability,
    scores: VariableScores,
    *,
    has_evidence: bool = True,
    insight_materiality: Any = None,
) -> ExpectedValue:
    """由 ScenarioProbability + 估值分位 + EPS 修订估计 ExpectedValue（§5.2）。

    R 分解为 ``earnings_contribution``（E 修订驱动）与 ``valuation_contribution``
    （估值分位驱动，低分位=便宜=上行空间）。估值分位缺失时 R/EV 显式 ``None``
    （不静默回退）；E 修订缺失时 earnings 贡献显式 ``None``，仅估值贡献 R。

    改造 D（EV 收益 materiality 化）：``insight_materiality``（``materiality_rank``）
    里 critical 的下行变量（``_DOWNSIDE_VARS``）会加深 bear 收益（每个 -5%），
    使 bear 下行幅度部分来自公司真实下行路径，而非纯估值分位公式。

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
    base_bear = -(percentile * _BEAR_SCALE + _BEAR_BASE)
    bear_val = base_bear - _materiality_penalty(insight_materiality)  # 改造 D：critical 下行变量加深 bear
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
    insight_materiality: Any = None,
) -> CompanyStateArtifact:
    """串起四引擎，把 FactorSnapshot 决策为 CompanyStateArtifact（§6 三轴 + §9）。

    Args:
        snapshot: 七因子快照（``alphabee.midterm.factors.get_factor_snapshot`` 产物）。
        evidence: 证据日志（``EvidenceEvent`` 列表）；``None``/空表示无证据。
        prior_confidence: 先验 P(H)（0-1）；``None`` 且无证据时后验为 ``None``。
        thesis: 核心假设 H（文本，透传进 artifact）。
        portfolio_adjustment: 组合层相关性/集中度调整乘数（默认 1.0 无调整）。
        single_stock_cap: 单股上限（RATIO），超限置 ``position.restricted``。
        insight_materiality: ``insight.materiality_rank``（``list[{variable, importance,
            reasoning}]``，改造 D）；critical 下行变量会加深 bear 收益。``None`` 无修正。

    Returns:
        :class:`CompanyStateArtifact`：state=StateBelief、thesis_confidence、
        variable_scores、factor_snapshot、expected_value、position 等，无 dead-end。
    """
    # P2 时效：过滤超过 1 年的旧证据（历史业绩预告已结算，不虚增当期置信度）
    evidence = _filter_fresh_evidence(evidence, snapshot.as_of_date)

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
    # 情景概率方向用「因子方向分」（score_engine 正确符号化），而非证据层的
    # effect_on_thesis（相对于 H 的确认/反驳，H 偏空时 confirming=看空会 sign error）。
    # 证据继续只通过 update_confidence 影响置信度（后验），不直接充当多空方向。
    scenario_probs = scenario_probability(
        cls.state.argmax_state,
        confidence=confidence,
        has_evidence=has_evidence,
        factor_direction=_factor_direction(scores),
    )
    ev = _estimate_expected_value(
        snapshot, scenario_probs, scores, has_evidence=has_evidence, insight_materiality=insight_materiality
    )

    # 改造 E 接线（P1E-1）：market_exposure 取 scores.m 里 regime 软约束上浮后的
    # position_low/high（E↑ + segment divergence → 上下限上浮），而非 snapshot.market 原始暴露。
    m_summary = scores.m or {}
    adj_low = m_summary.get("position_low")
    adj_high = m_summary.get("position_high")
    market_exposure = _market_exposure_from_bounds(adj_low, adj_high)

    position = build_position(
        cls.state,
        confidence=confidence,
        risk_adjusted_ev=ev.risk_adjusted_ev,
        market_exposure=market_exposure,
        risk_adjustment=scores.r_risk,
        portfolio_adjustment=portfolio_adjustment,
        single_stock_cap=single_stock_cap,
    )
    position.rationale.append(
        f"市场暴露：PositionAdvice=[{snapshot.market.position_low}, {snapshot.market.position_high}]"
        f" → regime 软约束后 [{adj_low}, {adj_high}] → 取中值 {market_exposure}"
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
    insight_materiality: Any = None,
) -> CompanyStateArtifact:
    """便捷入口：``get_factor_snapshot(symbol)`` 后再 ``evaluate``。

    Args:
        symbol: 股票代码（``"600519"`` / ``"600519.SH"`` / ``"300750.SZ"``）。
        evidence: 证据日志；``None``/空表示无证据。
        include_market: 是否采集 M（market_regime，开销较大）。
        prior_confidence / thesis / insight_materiality: 透传 ``evaluate``。

    Returns:
        :class:`CompanyStateArtifact`。
    """
    from alphabee.midterm.factors import get_factor_snapshot

    snapshot = get_factor_snapshot(symbol, include_market=include_market)
    return evaluate(
        snapshot,
        evidence,
        prior_confidence=prior_confidence,
        thesis=thesis,
        insight_materiality=insight_materiality,
    )


def collect_evidence_split(
    symbol: str,
    thesis: str = "",
    window_texts: str | list[str] | None = None,
    *,
    model: Any = None,
    as_of_date: str = "",
) -> tuple[list[EvidenceEvent], list[EvidenceEvent]]:
    """收集 EvidenceEvent，分通道返回 ``(numeric_evidence, qualitative_evidence)``。

    - 数值类（§6）：``build_numeric_evidence``（forecast/express/revision，纯规则禁 LLM）；
    - 定性文本（§4）：Stage A/B（LLM，失败降级）；
    - 任一环节失败（网络 / LLM / 解析）→ 该通道 ``[]``（§8/§11 只降级不中断）。

    分通道动机（A2 先验-似然同源解耦）：调用方（``resolve_midterm_decision``）
    需要区分 LLM 判断通道（Stage B 定性）与数据规则通道（数值），以便与
    insight 证据二选一入账——避免同一个 LLM 观点既当先验又当似然双重计数。
    ``collect_evidence`` 保持合并返回（向后兼容）。
    """
    from alphabee.midterm.evidence_extractor import dedupe_events

    numeric: list[EvidenceEvent] = []
    # 1. 数值类规则（§6 纯规则禁 LLM）
    try:
        from alphabee.agents.facts.tools.expectation_fact import get_expectation_fact
        from alphabee.collectors.consensus.eastmoney import build_consensus
        from alphabee.midterm.evidence_rules import build_numeric_evidence

        exp_data = get_expectation_fact(symbol)
        consensus = build_consensus(symbol)
        numeric.extend(build_numeric_evidence(exp_data, consensus, symbol=symbol, as_of_date=as_of_date))
    except Exception:
        pass  # 数值类获取失败 → 无数值证据（不中断）

    # 2. 定性文本 Stage A/B（LLM 必需，失败降级）
    qualitative: list[EvidenceEvent] = []
    if window_texts:
        try:
            from alphabee.midterm.evidence_extractor import assemble_events, extract_facts, judge_facts

            facts = extract_facts(window_texts, symbol=symbol, model=model)
            judgments = judge_facts(facts, thesis=thesis, model=model)
            qualitative.extend(assemble_events(facts, judgments))
        except Exception:
            pass  # LLM 失败 → 无定性证据（§11 只降级不中断）

    return dedupe_events(numeric), dedupe_events(qualitative)


def collect_evidence(
    symbol: str,
    thesis: str = "",
    window_texts: str | list[str] | None = None,
    *,
    model: Any = None,
    as_of_date: str = "",
) -> list[EvidenceEvent]:
    """收集 EvidenceEvent（合并通道）：数值类规则（纯规则）+ Stage A/B（LLM，失败降级）。

    ``collect_evidence_split`` 的合并便捷入口（向后兼容）。需要区分 LLM 判断
    通道与数据规则通道（A2 同源解耦）的调用方请直接用 ``collect_evidence_split``。

    Returns:
        去重后的 EvidenceEvent[]；全部失败 → []。
    """
    from alphabee.midterm.evidence_extractor import dedupe_events

    numeric, qualitative = collect_evidence_split(symbol, thesis, window_texts, model=model, as_of_date=as_of_date)
    return dedupe_events([*numeric, *qualitative])


def get_decision_with_evidence(
    symbol: str,
    thesis: str = "",
    window_texts: str | list[str] | None = None,
    *,
    include_market: bool = True,
    prior_confidence: float | None = None,
    model: Any = None,
    insight_materiality: Any = None,
) -> CompanyStateArtifact:
    """便捷入口：先 evidence 抽取（数值规则 + Stage A/B）再 get_decision（§8 两遍）。

    两遍（§8）：
    1. 收集 EvidenceEvent（数值类纯规则 + 定性 Stage A/B，LLM）；
    2. ``get_decision(symbol, evidence=evidence)``：evidence 非空 → bayes_posterior，
       空/None → state_prior 保守版。

    LLM / 网络失败 → ``evidence=[]`` → 模型照常出 state_prior 保守版（只降级不中断，
    不破坏确定性核心）；抽取的 EvidenceEvent 写入 ``evidence_log``，供 diff 的
    ``ConfidenceDelta.evidence_ids`` 归因（MIDTERM_STATE_DIFF_DESIGN.md §5）。

    Args:
        insight_materiality: ``insight.materiality_rank``（改造 D）透传 ``get_decision``。

    Returns:
        :class:`CompanyStateArtifact`。
    """
    try:
        evidence = collect_evidence(symbol, thesis=thesis, window_texts=window_texts, model=model)
    except Exception:
        evidence = []  # 兜底：抽取全挂 → state_prior 保守版（§8）
    return get_decision(
        symbol,
        evidence=evidence,
        include_market=include_market,
        prior_confidence=prior_confidence,
        thesis=thesis,
        insight_materiality=insight_materiality,
    )


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
