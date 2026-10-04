"""P1-4：惩罚不提升维度置信度（置信度只反映证据覆盖度，不因被罚而升高）。

背景（用户外部数据交叉核对 002916 深南电路一轮暴露）：``_apply_verified_conflict``
在扣维度分的同时执行 ``confidence = min(1.0, confidence + 0.1)``，于是**被打爆的维度
置信度最高**；而 ``_thesis_direction`` 对 ``confidence > 0`` 的维度分数取**简单均值**
⇒ 惩罚越重、越自信、越主导方向。修复：罚分只改 ``score``，不改 ``confidence``。

判别力（断言必须自证）：把 ``dim.confidence = min(1.0, dim.confidence + 0.1)`` 加回
``_apply_verified_conflict`` ⇒ 本文件 ``*_does_not_raise_confidence`` 两组断言必红。
"""

from __future__ import annotations

import pytest

from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.models import InvestmentThesis, ThesisDimension

#: 恶性/判不清的解释文本 —— 保证命中 P0-2 的「非良性」分支，使惩罚路径真正执行。
#: （良性文本会被方向投影跳过惩罚，无法用于本文件的判别力。）
_NON_BENIGN_EXPLANATIONS = ("确认操纵收入", "待进一步核验")


def _dimension(score: float = 0.5, confidence: float = 0.4) -> ThesisDimension:
    return ThesisDimension(
        id="earnings_quality",
        name="盈利质量",
        judgment="positive",
        score=score,
        confidence=confidence,
    )


def _apply(engine: ThesisEngine, dim: ThesisDimension, *, penalty: float) -> None:
    engine._apply_verified_conflict(
        dimensions={"earnings_quality": dim},
        dim_ids=["earnings_quality"],
        conflict={"id": "c1", "theme": "盈利增长但现金流恶化", "severity": "high"},
        hypothesis={"id": "h1", "explanation": "确认操纵收入"},
        summary="核验结论：确认操纵收入。",
        penalty=penalty,
    )


# ── ① 直接调用：罚分只改分数、不改置信度 ─────────────────────────────────────


@pytest.mark.parametrize("penalty", [0.55, 0.8, 0.1])
def test_penalty_lowers_score_by_exact_amount(penalty: float):
    """分数语义不变：score 恰好减去 penalty（下界夹取）。"""
    dim = _dimension(score=0.5)
    _apply(ThesisEngine(), dim, penalty=penalty)
    assert dim.score == pytest.approx(max(-1.0, 0.5 - penalty))


@pytest.mark.parametrize("penalty", [0.55, 0.8, 0.1])
def test_penalty_does_not_raise_confidence(penalty: float):
    """核心断言：罚分后置信度**不变**（原实现会 +0.1）。"""
    dim = _dimension(score=0.5, confidence=0.4)
    _apply(ThesisEngine(), dim, penalty=penalty)
    assert dim.confidence == pytest.approx(0.4)


def test_penalty_does_not_raise_confidence_at_upper_bound():
    """置信度已接近上界时也不得被抬到 1.0（原实现 min(1.0, +0.1) 会顶到上界）。"""
    dim = _dimension(score=0.5, confidence=0.95)
    _apply(ThesisEngine(), dim, penalty=0.55)
    assert dim.confidence == pytest.approx(0.95)


def test_penalty_with_evidence_still_recorded():
    """负向证据仍入 ``dim.evidence``（惩罚的可见性面不受本修复影响）。"""
    dim = _dimension()
    _apply(ThesisEngine(), dim, penalty=0.55)
    assert any(item.source_label == "verified_conflict" for item in dim.evidence)


# ── ② 端到端：有 verified 冲突 vs 无冲突，维度置信度相同、分数更低 ─────────────


def _run_engine(explanation: str | None, *, status: str = "verified", severity: str = "high") -> InvestmentThesis:
    signal_results = {
        "quality_signal": {
            "level": "high",
            "interpretation": "盈利质量信号。",
            "thesis_impact": {"earnings_quality": "positive"},
        },
        # 额外信号（映射到其他维度）：把 ``earnings_quality`` 的**覆盖度**压到 < 1.0，
        # 否则 ``confidence = min(1.0, contribs/total_signals)`` 会被钳到 1.0 ⇒
        # 「+0.1」不可观测，端到端断言将失去判别力（变异实验实测证实）。
        "growth_signal": {
            "level": "medium",
            "interpretation": "成长信号。",
            "thesis_impact": {"growth_quality": "positive"},
        },
        "cash_signal": {
            "level": "medium",
            "interpretation": "现金流信号。",
            "thesis_impact": {"financial_quality": "positive"},
        },
        "valuation_signal": {
            "level": "medium",
            "interpretation": "估值信号。",
            "thesis_impact": {"valuation_fit": "neutral"},
        },
    }
    conflict_analysis = None
    verification_results = None
    if explanation is not None:
        conflict_analysis = {
            "conflicts": [
                {
                    "id": "c1",
                    "theme": "盈利增长但现金流恶化",
                    "description": "利润增长未被现金流验证。",
                    "related_dimensions": ["earnings_quality"],
                    "severity": severity,
                    "confidence": 0.9,
                    "hypotheses": [{"id": "h1", "explanation": explanation, "status": "pending"}],
                }
            ]
        }
        verification_results = [
            {
                "hypothesis_id": "h1",
                "status": status,
                "summary": f"核验结论：{explanation}。",
                "gaps": [],
            }
        ]
    return ThesisEngine().run(
        symbol="600519.SH",
        period="2024Q4",
        signal_results=signal_results,
        conflict_analysis=conflict_analysis,
        verification_results=verification_results,
    )


def _earnings_dimension(thesis: InvestmentThesis) -> ThesisDimension:
    """``InvestmentThesis.dimensions`` 是 ``dict[str, ThesisDimension]``（键为维度 id）。"""
    return thesis.dimensions["earnings_quality"]


@pytest.mark.parametrize("explanation", _NON_BENIGN_EXPLANATIONS)
def test_end_to_end_verified_conflict_does_not_raise_confidence(explanation: str):
    """端到端：命中惩罚路径的 verified 冲突使分数下降，但置信度与无冲突基线相同。"""
    baseline = _earnings_dimension(_run_engine(None))
    penalized = _earnings_dimension(_run_engine(explanation))
    assert penalized.score < baseline.score
    assert penalized.confidence == pytest.approx(baseline.confidence)


def test_end_to_end_partial_conflict_does_not_raise_confidence():
    """partial（0.7 折减）路径同样不提升置信度。"""
    baseline = _earnings_dimension(_run_engine(None))
    penalized = _earnings_dimension(_run_engine("确认操纵收入", status="partial"))
    assert penalized.score < baseline.score
    assert penalized.confidence == pytest.approx(baseline.confidence)
