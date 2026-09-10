"""decision_model.py 的单元测试：四引擎编排、artifact 组装、无 dead-end、get_decision、导出。

全部用合成 ``FactorSnapshot`` / ``EvidenceEvent``，get_decision 用 monkeypatch 替换数据源。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.decision_model import collect_evidence, evaluate, get_decision, get_decision_with_evidence
from alphabee.midterm.models import (
    AuditSnapshot,
    CrowdingFactor,
    EvidenceEvent,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
    MarketFactor,
    RiskFactor,
    TrendFactor,
    ValuationFactor,
)


def _snapshot(**overrides) -> FactorSnapshot:
    """构建带全量非空值的快照；``overrides`` 里的键为因子名，值为字段 dict。"""
    defaults: dict = {
        "fundamental": FundamentalFactor(revenue_yoy=10.0, net_profit_yoy=20.0, eps_growth_yoy=15.0),
        "expectation": ExpectationFactor(eps_fy1_revision_1m=3.0, revision_breadth=0.6),
        "trend": TrendFactor(rs_stock_market_20d=10.0, rs_stock_market_60d=5.0),
        "valuation": ValuationFactor(pe_ttm_5y_percentile=0.3, pb_5y_percentile=0.4),
        "crowding": CrowdingFactor(holder_count_change=5.0, hot_rank=80, turnover_rate_percentile=0.4),
        "risk": RiskFactor(pledge_ratio=10.0, debt_to_assets=40.0, audit=AuditSnapshot(audit_opinion="标准无保留意见")),
        "market": MarketFactor(market_score=60.0, position_high=0.8),
    }
    defaults.update(overrides)
    return FactorSnapshot(symbol="600519.SH", **defaults)


def _ev(effect: str, delta: float) -> EvidenceEvent:
    return EvidenceEvent(
        id="e1", date="2024-01-01", kind="expectation", description="", effect_on_thesis=effect, confidence_delta=delta
    )


# ─────────────────────────────────────────────────────────────────────────────
# 编排入口：四引擎串接 + artifact 组装
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluate_assembles_artifact():
    art = evaluate(_snapshot(), [_ev("confirming", 0.5)], prior_confidence=0.5)
    assert art.symbol == "600519.SH"
    assert art.state is not None  # StateBelief
    assert art.state.argmax_state in ("S0", "S1", "S2", "S3", "S4", "S5")
    assert art.variable_scores.f_fundamental_trend is not None
    assert art.factor_snapshot is not None
    assert art.position is not None
    assert art.expected_value is not None
    assert art.expected_value.ev is not None  # 有证据 + 估值存在 → EV 可算


def test_evaluate_no_evidence_ev_degraded():
    # 零证据 → EV 显式降级（ev=None），不静默由 state_prior 给激进 EV
    art = evaluate(_snapshot())
    assert art.expected_value is not None
    assert art.expected_value.ev is None
    assert art.expected_value.probability_source == "state_prior"
    assert "零证据" in art.expected_value.note


def test_evaluate_with_evidence_updates_confidence():
    art = evaluate(_snapshot(), [_ev("confirming", 0.5)], prior_confidence=0.5)
    assert art.thesis_confidence == pytest.approx(0.75)
    assert art.expected_value.probability_source == "bayes_posterior"


def test_evaluate_no_evidence_no_prior():
    art = evaluate(_snapshot(), evidence=None, prior_confidence=None)
    assert art.thesis_confidence == 0.0  # 无证据无先验 → 0（字段非空，无法 None）
    assert art.expected_value.probability_source == "state_prior"


def test_evaluate_writes_back_state_and_confidence():
    # 无证据无先验：confidence 缺失 → 按模型非空字段约定回退 0.0（与 thesis_confidence 同口径）
    art = evaluate(_snapshot())
    snap = art.factor_snapshot
    assert snap is not None
    assert snap.state == art.state.argmax_state  # 顶层 state 与快照 state 一致（不再残留 S0）
    assert snap.confidence == 0.0

    # 有证据 + 先验：后验回写进快照
    art2 = evaluate(_snapshot(), [_ev("confirming", 0.5)], prior_confidence=0.5)
    assert art2.factor_snapshot.confidence == pytest.approx(0.75)
    assert art2.factor_snapshot.state == art2.state.argmax_state


def test_evaluate_writes_back_direction_scores():
    """方向分回写：子模型 direction/score 与 VariableScores 不再两套不一致。"""
    art = evaluate(_snapshot())
    snap = art.factor_snapshot
    scores = art.variable_scores

    # F=0.5 / T=0.375 → improving；E≈0.25（log1p 反饱和后）→ neutral；V=0.3 恰在阈值 → fair
    assert snap.fundamental.direction == "improving"
    assert snap.fundamental.score == pytest.approx(50.0 + 50.0 * scores.f_fundamental_trend)
    assert snap.expectation.direction == "neutral"
    assert snap.expectation.score == pytest.approx(50.0 + 50.0 * scores.e_revision)
    assert snap.trend.direction == "improving"
    assert snap.trend.score == pytest.approx(50.0 + 50.0 * scores.t_relative_strength)
    assert snap.valuation.direction == "fair"
    assert snap.valuation.score == pytest.approx(50.0 + 50.0 * scores.v_valuation_percentile)
    assert snap.crowding.direction == "normal"
    assert snap.crowding.score == pytest.approx(50.0 + 50.0 * scores.c_crowding)
    assert snap.risk.direction == "neutral"
    assert snap.risk.score == pytest.approx(50.0 + 50.0 * scores.r_risk)


def test_writeback_sign_convention_valuation_crowding_risk():
    """C/R 反向（越拥挤/越风险越负）+ V 用估值分位（低分位=便宜=正）。"""
    snap = _snapshot(
        valuation=ValuationFactor(pe_ttm_5y_percentile=0.05, pb_5y_percentile=0.05),
        crowding=CrowdingFactor(holder_count_change=-10.0, hot_rank=1, turnover_rate_percentile=0.9),
        risk=RiskFactor(pledge_ratio=80.0, debt_to_assets=80.0),
    )
    art = evaluate(snap)
    s = art.factor_snapshot
    assert s.valuation.direction == "cheap"
    assert s.valuation.score > 50.0
    assert s.crowding.direction == "overheated"
    assert s.crowding.score < 50.0
    assert s.risk.direction == "risk_rising"
    assert s.risk.score < 50.0


def test_writeback_missing_direction_keeps_default():
    """空快照 → 方向分缺失 → 子模型 score 保持 None、direction 保持模型默认（不制造方向）。"""
    empty = FactorSnapshot(symbol="000001.SZ")
    art = evaluate(empty)
    snap = art.factor_snapshot
    assert snap.fundamental.direction == "stable"
    assert snap.fundamental.score is None
    assert snap.expectation.direction == "neutral"
    assert snap.expectation.score is None
    assert snap.trend.direction == "neutral"
    assert snap.trend.score is None
    assert snap.valuation.direction == "neutral"
    assert snap.valuation.score is None
    assert snap.crowding.direction == "neutral"
    assert snap.crowding.score is None
    assert snap.risk.direction == "neutral"
    assert snap.risk.score is None


def test_evaluate_missing_valuation_ev_none():
    art = evaluate(_snapshot(valuation=ValuationFactor()), [_ev("confirming", 0.5)], prior_confidence=0.5)
    assert art.expected_value is not None
    assert art.expected_value.ev is None  # 估值缺失 → EV 显式 None，不静默回退


def test_market_exposure_midpoint():
    snap = _snapshot(market=MarketFactor(market_score=60.0, position_low=0.5, position_high=0.8))
    art = evaluate(snap)
    assert art.position.portfolio_exposure == pytest.approx(0.65)
    assert art.position.actual_weight == pytest.approx(0.65 * art.position.stock_weight)
    assert any("取中值" in r for r in art.position.rationale)


def test_market_exposure_one_sided():
    snap = _snapshot(market=MarketFactor(market_score=60.0, position_low=0.5))
    assert evaluate(snap).position.portfolio_exposure == pytest.approx(0.5)


def test_market_exposure_missing_is_none():
    snap = _snapshot(market=MarketFactor())
    art = evaluate(snap)
    assert art.position.portfolio_exposure is None
    assert art.position.actual_weight is None


def test_uncertain_adds_research_to_next_evidence():
    # 空快照 → 全方向分缺失 → 熵高 uncertain → 研究任务落入 next_evidence_to_watch
    empty = FactorSnapshot(symbol="000001.SZ")
    art = evaluate(empty)
    assert len(art.next_evidence_to_watch) > 0


def test_degraded_passthrough():
    snap = _snapshot()
    snap.degraded = True
    snap.degraded_reason = "upstream missing"
    art = evaluate(snap)
    assert art.degraded is True
    assert art.degraded_reason == "upstream missing"


def test_evidence_log_passthrough():
    evs = [_ev("confirming", 0.3), _ev("refuting", 0.1)]
    art = evaluate(_snapshot(), evs)
    assert len(art.evidence_log) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 便捷入口 get_decision（monkeypatch 数据源）
# ─────────────────────────────────────────────────────────────────────────────


def test_get_decision_calls_snapshot_then_evaluate(monkeypatch):
    snap = _snapshot()
    import alphabee.midterm.factors as factors_mod

    monkeypatch.setattr(factors_mod, "get_factor_snapshot", lambda symbol, include_market=True: snap)
    art = get_decision("600519.SH")
    assert art.symbol == "600519.SH"
    assert art.state is not None


# ─────────────────────────────────────────────────────────────────────────────
# E4 便捷入口：get_decision_with_evidence / collect_evidence（monkeypatch 抽取层）
# ─────────────────────────────────────────────────────────────────────────────


def test_get_decision_with_evidence_llm_failure_degrades(monkeypatch):
    """LLM 抽取全挂 → evidence=[] → 模型照常出 state_prior 保守版（§8 只降级不中断）。"""
    import alphabee.midterm.decision_model as dm

    snap = _snapshot()
    monkeypatch.setattr("alphabee.midterm.factors.get_factor_snapshot", lambda symbol, include_market=True: snap)
    monkeypatch.setattr(dm, "collect_evidence", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM down")))

    art = get_decision_with_evidence("600519.SH", thesis="H", window_texts=["文本"])
    assert art.expected_value is not None
    assert art.expected_value.probability_source == "state_prior"
    assert art.evidence_log == []
    assert art.thesis_confidence == 0.0  # 无证据无先验 → 保守


def test_get_decision_with_evidence_enriches_bayes_posterior(monkeypatch):
    """evidence 非空 → bayes_posterior + evidence_log 留痕。"""
    import alphabee.midterm.decision_model as dm

    snap = _snapshot()
    evs = [_ev("confirming", 0.3)]
    monkeypatch.setattr("alphabee.midterm.factors.get_factor_snapshot", lambda symbol, include_market=True: snap)
    monkeypatch.setattr(dm, "collect_evidence", lambda *a, **k: evs)

    art = get_decision_with_evidence("600519.SH", thesis="H", window_texts=["文本"], prior_confidence=0.5)
    assert art.expected_value.probability_source == "bayes_posterior"
    assert art.evidence_log == evs
    assert art.thesis_confidence == pytest.approx(0.65)  # 0.5 + confirming 0.3 → log-odds 更新


def test_collect_evidence_degrades_when_sources_fail(monkeypatch):
    """数值类数据源 + 定性 LLM 全失败 → collect_evidence 返回 []（§8/§11 不中断）。"""
    import alphabee.midterm.evidence_extractor as ex

    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(ex, "extract_facts", _boom)  # 定性 Stage A 失败
    # 数值类阶段内部懒导入 get_expectation_fact 触发 tushare token 写入，在沙箱内
    # 因 /home 不可写而失败，被 collect_evidence 的 try/except 捕获（不中断）。
    assert collect_evidence("600519.SH", thesis="H", window_texts=["文本"]) == []


def test_evidence_log_feeds_diff_attribution():
    """evidence_log 留痕 → diff 的 ConfidenceDelta.evidence_ids 归因（打通 §5）。"""
    from alphabee.midterm.diff import diff

    prev = evaluate(_snapshot(as_of_date="2024-01-01"), None)
    evs = [
        EvidenceEvent(
            id="e1",
            date="2024-01-05",
            kind="expectation",
            description="x",
            effect_on_thesis="confirming",
            confidence_delta=0.3,
        ),
        EvidenceEvent(
            id="e2",
            date="2024-01-05",
            kind="expectation",
            description="y",
            effect_on_thesis="refuting",
            confidence_delta=0.1,
        ),
    ]
    curr = evaluate(_snapshot(as_of_date="2024-01-05"), evs, prior_confidence=0.5)
    d = diff(prev, curr)
    assert d.confidence.evidence_ids == ["e1", "e2"]
    assert [e.id for e in d.new_evidence] == ["e1", "e2"]


# ─────────────────────────────────────────────────────────────────────────────
# __init__ 导出四引擎关键入口
# ─────────────────────────────────────────────────────────────────────────────


def test_init_exports_engines():
    import alphabee.midterm as midterm

    for name in (
        "evaluate",
        "get_decision",
        "get_decision_with_evidence",
        "collect_evidence",
        "compress_scores",
        "classify_state",
        "update_confidence",
        "scenario_probability",
        "build_position",
    ):
        assert hasattr(midterm, name), name


def test_init_exports_evidence_functions():
    import alphabee.midterm as midterm

    for name in (
        "build_numeric_evidence",
        "adapt_verification",
        "extract_facts",
        "judge_facts",
        "assemble_events",
        "dedupe_events",
        "FactEventCache",
    ):
        assert hasattr(midterm, name), name


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数 / 确定性
# ─────────────────────────────────────────────────────────────────────────────


def test_deterministic():
    snap = _snapshot()
    a = evaluate(snap, [_ev("confirming", 0.2)], prior_confidence=0.5)
    b = evaluate(snap, [_ev("confirming", 0.2)], prior_confidence=0.5)
    assert a.model_dump() == b.model_dump()
