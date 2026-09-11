"""洞察力注入决策级快照回归（改造 A + C，设计 MIDTERM_INSIGHT_INJECTION_DESIGN.md §4/§6）。

用沃尔核材（002130.SZ）的 fixture 快照做**决策级**回归：不依赖实时 LLM / 网络，
直接 ``evaluate(fixture_snapshot, insight_evidence)`` 验证洞察层结构化证据注入后，
中期决策层满足验证基准：

1. ``evidence_log`` 含「高速通信线+35.44%」（confirming）与「增收不增利」（refuting）；
2. ``thesis_confidence ≤ 0.95``（改造 C clamp，不再 sigmoid 饱和到 1.0）；
3. ``position_band ∈ {观察, 减仓}``（不再是「清仓」）；
4. 高熵（entropy > 1.0）+ 负 EV 场景**非清仓**（改造 C 软阈值：减仓而非压 0）。
"""

from __future__ import annotations

from alphabee.midterm.decision_model import evaluate
from alphabee.midterm.insight_evidence_adapter import adapt_insight_evidence
from alphabee.midterm.models import (
    CrowdingFactor,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
    MarketFactor,
    RiskFactor,
    TrendFactor,
    ValuationFactor,
)
from alphabee.orchestrator.contracts import InsightArtifact

# 沃尔核材 fixture：结构性亮点（高速通信线）被整体增速掩盖 + 反证（增收不增利），
# 估值偏贵 + 换手拥挤 + E 停上修 + T 走弱 → 分类到 S4（充分定价）且熵高（不确定）。
# 该 fixture 是纯确定性输入，不发起网络/LLM 调用。
_SYMBOL = "002130.SZ"
_AS_OF_DATE = "2026-06-30"


def _wol_snapshot() -> FactorSnapshot:
    return FactorSnapshot(
        symbol=_SYMBOL,
        as_of_date=_AS_OF_DATE,
        fundamental=FundamentalFactor(revenue_yoy=1.54, net_profit_yoy=-3.0, eps_growth_yoy=-3.0),
        expectation=ExpectationFactor(eps_fy1_revision_1m=0.0),  # E 停上修（e_not_up 门控）
        trend=TrendFactor(rs_stock_market_20d=-8.0),  # T 走弱
        valuation=ValuationFactor(pe_ttm_5y_percentile=0.7, pb_5y_percentile=0.7),  # V 偏贵
        crowding=CrowdingFactor(turnover_rate_percentile=0.6),  # C 换手拥挤
        risk=RiskFactor(pledge_ratio=40.0, debt_to_assets=60.0),
        market=MarketFactor(market_score=40.0, position_low=0.0, position_high=0.2),
    )


def _wol_insight() -> InsightArtifact:
    return InsightArtifact(
        core_view="核心观点",
        supporting_evidence=[
            {"statement": "高速通信线+35.44%", "source": "segment:high_speed_comm", "weight": "strong"},
        ],
        counter_evidence=[
            {"statement": "增收不增利", "source": "signal:profit_leverage", "weight": "moderate"},
        ],
        confidence="high",
    )


def test_wol_insight_injection_snapshot_regression():
    """沃尔核材决策级快照回归：证据注入 + 置信度 clamp + 软阈值高熵不清仓。"""
    insight = _wol_insight()
    # 改造 A：洞察层结构化证据 → EvidenceEvent（纯规则，零 LLM）
    evidence = adapt_insight_evidence(insight, symbol=_SYMBOL, date=_AS_OF_DATE)

    art = evaluate(_wol_snapshot(), evidence, prior_confidence=0.7, thesis=insight.core_view)

    # 1. 证据日志含结构性洞察（confirming + refuting）
    by_desc = {e.description: e for e in art.evidence_log}
    assert "高速通信线+35.44%" in by_desc
    assert by_desc["高速通信线+35.44%"].effect_on_thesis == "confirming"
    assert by_desc["高速通信线+35.44%"].confidence_delta == 0.5  # strong
    assert "增收不增利" in by_desc
    assert by_desc["增收不增利"].effect_on_thesis == "refuting"
    assert by_desc["增收不增利"].confidence_delta == 0.3  # moderate

    # 2. thesis_confidence 不再饱和到 1.0（改造 C clamp）
    assert 0.05 <= art.thesis_confidence <= 0.95

    # 3. position_band 不再是「清仓」
    assert art.position.position_band in ("观察", "减仓")

    # 4. 高熵（不确定）+ 负 EV → 软阈值减仓而非清仓
    assert art.state.argmax_state == "S4"  # 充分定价（减仓态）
    assert art.state.entropy > 1.0  # 高熵「不确定」
    assert art.expected_value.risk_adjusted_ev is not None
    assert art.expected_value.risk_adjusted_ev < 0.0  # 赔率为负
    assert art.position.position_band != "清仓"  # 高熵 → 减仓/观察，不被硬规则压 0
    assert art.position.stock_weight > 0.0
