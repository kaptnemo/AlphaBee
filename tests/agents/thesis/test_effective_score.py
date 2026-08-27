"""effective_score 净得分 + 强档降级。"""

from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.models import score_to_judgment


def test_score_to_judgment_downgrades_extreme_scores_at_low_confidence():
    # 方向强度打到极端（±1.0）但证据覆盖度很低时，不能给 strong_* 档位。
    assert score_to_judgment(-1.0, 0.1) == "negative"
    assert score_to_judgment(1.0, 0.1) == "positive"


def test_score_to_judgment_keeps_strong_scores_at_adequate_confidence():
    assert score_to_judgment(-1.0, 0.5) == "strong_negative"
    assert score_to_judgment(1.0, 0.9) == "strong_positive"


def test_score_to_judgment_default_confidence_does_not_downgrade():
    assert score_to_judgment(-1.0) == "strong_negative"
    assert score_to_judgment(1.0) == "strong_positive"


def test_engine_computes_effective_score_and_downgrades_judgment():
    # 4 条信号里只有 1 条打向 credit_risk（high negative），
    # 该维度 score=-1.0 但 confidence=0.25，属于"极端结论 + 证据单薄"，
    # 应产出 effective_score=-0.25 且判断退到 negative 而非 strong_negative。
    signal_results = {
        "credit_risk_signal": {
            "level": "high",
            "interpretation": "有息负债与现金同时偏高。",
            "thesis_impact": {"credit_risk": "negative"},
        },
        "growth_signal": {
            "level": "low",
            "interpretation": "增长仍为正。",
            "thesis_impact": {"growth_quality": "positive"},
        },
        "earnings_signal": {
            "level": "low",
            "interpretation": "盈利质量尚可。",
            "thesis_impact": {"earnings_quality": "positive"},
        },
        "valuation_signal": {
            "level": "low",
            "interpretation": "估值中性。",
            "thesis_impact": {"valuation_fit": "neutral"},
        },
    }

    thesis = ThesisEngine().run(
        symbol="002130.SZ",
        period="20260630",
        signal_results=signal_results,
    )

    dim = thesis.dimensions["credit_risk"]
    assert dim.score == -1.0
    assert dim.confidence == 0.25
    assert dim.effective_score == -0.25
    assert dim.judgment == "negative"


def test_engine_keeps_strong_judgment_when_confidence_is_high():
    # 只有 1 条信号时 confidence=1.0，强档无需降级。
    signal_results = {
        "credit_risk_signal": {
            "level": "high",
            "interpretation": "有息负债与现金同时偏高。",
            "thesis_impact": {"credit_risk": "negative"},
        },
    }

    thesis = ThesisEngine().run(
        symbol="002130.SZ",
        period="20260630",
        signal_results=signal_results,
    )

    dim = thesis.dimensions["credit_risk"]
    assert dim.score == -1.0
    assert dim.confidence == 1.0
    assert dim.effective_score == -1.0
    assert dim.judgment == "strong_negative"


def test_to_dict_includes_effective_score():
    thesis = ThesisEngine().run(
        symbol="002130.SZ",
        period="20260630",
        signal_results={
            "credit_risk_signal": {
                "level": "high",
                "interpretation": "有息负债与现金同时偏高。",
                "thesis_impact": {"credit_risk": "negative"},
            },
        },
    )

    dim_dict = thesis.to_dict()["dimensions"]["credit_risk"]

    assert "effective_score" in dim_dict
    assert dim_dict["effective_score"] == -1.0
