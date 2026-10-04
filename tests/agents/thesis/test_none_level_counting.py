"""P1-3：none 级证据不计入 positive 票（消除 contested→blocked 误封）。

复现根因：``reviewer._layer1_check`` 的 Rule 3 竞争壁垒计数把 none 级证据
（revenue_quality_risk / profitability_quality_risk 的 none = 无风险发现）计入
positive 票 ⇒ 「两条无风险信号 + 两条负面」命中 ``positive>=2 and negative>=2``
⇒ is_severe ⇒ contested ⇒ overall_status=blocked（"因为没发现问题而被封"）。

修复：none 级从 positive 计数中排除（strong_positive 排除 none 的既有逻辑不变，
合流为「none 级既不进 positive 也不进 strong_positive」）。

判别力承重：同结构但真正面证据（非 none）⇒ 仍触发 contested。
"""

from __future__ import annotations

from types import SimpleNamespace

from alphabee.agents.thesis.models import CompanyContext, EvidenceItem, InvestmentThesis, ThesisDimension
from alphabee.agents.thesis.reviewer import ThesisReviewer


def _ev(impact: str, level: str) -> SimpleNamespace:
    return SimpleNamespace(
        signal_name=f"sig_{impact}_{level}", signal_id=f"sig_{impact}_{level}", impact=impact, level=level
    )


def _dim(evidence, *, judgment: str = "negative", score: float = -0.4, confidence: float = 0.8) -> SimpleNamespace:
    return SimpleNamespace(name="盈利质量", evidence=evidence, confidence=confidence, judgment=judgment, score=score)


def _verdict(evidence, *, judgment: str = "negative", score: float = -0.4) -> object:
    return ThesisReviewer()._layer1_check(
        "earnings_quality", _dim(evidence, judgment=judgment, score=score), {}, CompanyContext()
    )


def test_two_none_and_two_negative_no_longer_severe():
    """复现用例：两条 none 级（无风险发现）+ 两条负面 ⇒ 不再判 is_severe ⇒ 不 contested。"""
    verdict = _verdict(
        [
            _ev("positive", "none"),
            _ev("positive", "none"),
            _ev("negative", "high"),
            _ev("negative", "medium"),
        ]
    )
    assert verdict.status != "contested"
    assert not any("信号方向冲突" in issue for issue in verdict.issues)


def test_two_none_and_one_negative_not_conflict_at_all():
    """none 排除后正面票归零：不存在方向冲突（只有负面证据），也不走 mild/qualified 路径。"""
    verdict = _verdict(
        [
            _ev("positive", "none"),
            _ev("positive", "none"),
            _ev("negative", "high"),
        ]
    )
    assert verdict.status != "contested"
    assert not any("信号方向冲突" in issue for issue in verdict.issues)


def test_real_positive_evidence_still_triggers_contested():
    """判别力：同结构但真正面证据（high/medium）⇒ 仍触发 contested（修复不得过度放宽）。

    thesis 为极端负向判断（strong_negative）+ 强正面对冲 ⇒ Rule 3b 保留 contested
    （thesis_is_extreme 且 strong_positive>=1 分支）。
    """
    verdict = _verdict(
        [
            _ev("positive", "high"),
            _ev("positive", "medium"),
            _ev("negative", "high"),
            _ev("negative", "medium"),
        ],
        judgment="strong_negative",
        score=-0.9,
    )
    assert verdict.status == "contested"
    assert verdict.suggested_action == "reconsider_with_context"
    assert any("信号方向冲突" in issue for issue in verdict.issues)


def test_none_positive_never_becomes_strong_positive():
    """none:positive 不进 strong_positive 也不进 positive：仅 none + 强负面 ⇒ 无方向冲突。"""
    verdict = _verdict(
        [
            _ev("positive", "none"),
            _ev("negative", "high"),
        ]
    )
    assert verdict.status != "contested"
    assert not any("信号方向冲突" in issue for issue in verdict.issues)


def test_real_high_positive_with_strong_negative_keeps_severe_path():
    """判别力：真 high:positive + strong 负面仍走 severe 路径（strong 对 strong 保留）。"""
    verdict = _verdict(
        [_ev("positive", "high"), _ev("negative", "high")],
        judgment="strong_negative",
        score=-0.9,
    )
    assert verdict.status == "contested"


def test_single_none_and_single_negative_not_severe():
    """1 条 none + 1 条负面：修复前 positive=1/negative=1（mild qualified），修复后正面票归零。"""
    verdict = _verdict([_ev("positive", "none"), _ev("negative", "high")])
    assert verdict.status != "contested"
    assert not any("信号方向冲突" in issue for issue in verdict.issues)


def test_none_exclusion_does_not_affect_negative_counting():
    """负面计数不受影响：2 条 none + 2 条 strong 负面 ⇒ 无正面票 ⇒ 无方向冲突（不误读为多空对撞）。"""
    verdict = _verdict(
        [
            _ev("positive", "none"),
            _ev("positive", "none"),
            _ev("negative", "high"),
            _ev("negative", "high"),
        ]
    )
    assert verdict.status != "contested"
    assert not any("信号方向冲突" in issue for issue in verdict.issues)


def test_end_to_end_two_none_and_two_negative_not_blocked():
    """端到端：两条 none + 两条负面 ⇒ overall_status 不再 blocked（误封消除）。"""
    evidence = [
        EvidenceItem(
            signal_id="revenue_quality_risk",
            signal_name="收入质量风险",
            level="none",
            impact="positive",
            interpretation="未发现显著风险信号。",
        ),
        EvidenceItem(
            signal_id="profitability_quality_risk",
            signal_name="盈利质量风险",
            level="none",
            impact="positive",
            interpretation="未发现显著风险信号。",
        ),
        EvidenceItem(signal_id="risk_a", signal_name="风险A", level="high", impact="negative", interpretation="负面"),
        EvidenceItem(signal_id="risk_b", signal_name="风险B", level="medium", impact="negative", interpretation="负面"),
    ]
    dim = ThesisDimension(
        id="earnings_quality",
        name="盈利质量",
        judgment="negative",
        score=-0.5,
        confidence=0.8,
        evidence=evidence,
    )
    thesis = InvestmentThesis(
        symbol="002916.SZ",
        period="2026Q2",
        dimensions={"earnings_quality": dim},
        overall_judgment="negative",
        overall_score=-0.5,
    )
    review = ThesisReviewer().review(thesis=thesis, signal_results={})
    assert review.overall_status != "blocked"
    verdict = review.dimension_verdicts["earnings_quality"]
    assert verdict.status != "contested"
    assert not any("信号方向冲突" in issue for issue in verdict.issues)


def test_end_to_end_real_positive_still_blocked():
    """判别力端到端：同结构但真正面证据 ⇒ overall_status 仍 blocked（contested 路径保留）。"""
    evidence = [
        EvidenceItem(signal_id="p1", signal_name="正面1", level="high", impact="positive", interpretation="正面"),
        EvidenceItem(signal_id="p2", signal_name="正面2", level="medium", impact="positive", interpretation="正面"),
        EvidenceItem(signal_id="n1", signal_name="风险1", level="high", impact="negative", interpretation="负面"),
        EvidenceItem(signal_id="n2", signal_name="风险2", level="medium", impact="negative", interpretation="负面"),
    ]
    dim = ThesisDimension(
        id="earnings_quality",
        name="盈利质量",
        judgment="strong_negative",
        score=-0.9,
        confidence=0.8,
        evidence=evidence,
    )
    thesis = InvestmentThesis(
        symbol="002916.SZ",
        period="2026Q2",
        dimensions={"earnings_quality": dim},
        overall_judgment="strong_negative",
        overall_score=-0.9,
    )
    review = ThesisReviewer().review(thesis=thesis, signal_results={})
    assert review.overall_status == "blocked"
    assert review.dimension_verdicts["earnings_quality"].status == "contested"
