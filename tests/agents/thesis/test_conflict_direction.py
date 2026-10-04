"""P0-2：冲突假设方向投影——良性 verified/partial 冲突不再无条件投负票/扣分。

覆盖要点：

* ``conflict_direction`` 分类器：良性 / 恶性 / 判不清（unknown），否定窗口覆盖
  （「未发现操纵」「不构成资金占用」「高于正常水平」「排除操纵」等）；
* ``engine._apply_conflict_analysis``：良性已核验冲突不投负票、不扣维度分，恶性维持
  既有负贡献，partial 维持 0.7 折减，gaps 补齐与方向投影正交；
* ``reviewer._conflict_votes``：与 engine 同口径（同一方向投影规则、同源文本
  ``explanation``），良性不计负票、恶性/判不清维持负票；
* 审计边 ``verified_conflict->dimension_score``：全良性冲突不要求维度被扣分、该边不适用
  （消除「engine 已按投影跳过扣分，审计却仍报不一致」的伪 D3）；
* 判别力承重：良性仍扣分 / 恶性不扣分两个方向各有用例（变异实验见提交记录）。
"""

from __future__ import annotations

import pytest

from alphabee.agents.schemas import ConflictAnalysisResult, ConflictItem, HypothesisItem
from alphabee.agents.thesis import reviewer as reviewer_module
from alphabee.agents.thesis.engine import ThesisEngine, conflict_direction
from alphabee.agents.thesis.models import InvestmentThesis, ThesisDimension
from alphabee.agents.thesis.reviewer import audit_amplification

# ── ① 分类器：良性 / 恶性 / 判不清 + 否定窗口 ───────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("扩张期正常现象", "benign"),
        ("AI 需求驱动的良性备货", "benign"),
        ("高估值有成长性支撑", "benign"),
        ("主动安排存货", "benign"),
        ("符合行业惯例", "benign"),
        ("存货增加属正常季节性波动", "benign"),
        ("不构成资金占用", "benign"),
        ("排除操纵嫌疑", "benign"),
        ("未发现异常", "benign"),
        ("无异常", "benign"),
        ("财务风险可控", "benign"),
        ("确认操纵", "negative"),
        ("存在粉饰报表", "negative"),
        ("滞销积压", "negative"),
        ("持续恶化", "negative"),
        ("资金占用", "negative"),
        ("合理性存疑", "negative"),
        ("违背行业惯例", "negative"),
        ("待进一步核验", "unknown"),
        ("收入确认前置，回款滞后", "unknown"),
        ("未发现操纵", "unknown"),  # 否定窗口：不算恶性，也无良性标记
        ("无法排除操纵", "unknown"),  # 双重否定：恶性/良性标记均被否定覆盖
        ("高于正常水平", "unknown"),  # 「高于正常」不算良性
    ],
)
def test_conflict_direction_classifier(text: str, expected: str):
    assert conflict_direction(text) == expected


# ── ② engine：良性不投负票/不扣分，恶性维持负贡献，partial 折减，gaps 正交 ────


def _run_engine(explanation: str, *, status: str = "verified", severity: str = "high", gaps=None) -> InvestmentThesis:
    signal_results = {
        "quality_signal": {
            "level": "high",
            "interpretation": "盈利质量信号。",
            "thesis_impact": {"earnings_quality": "positive"},
        }
    }
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
            "gaps": gaps or [],
        }
    ]
    return ThesisEngine().run(
        symbol="600519.SH",
        period="2024Q4",
        signal_results=signal_results,
        conflict_analysis=conflict_analysis,
        verification_results=verification_results,
    )


def _conflict_evidence(dim: ThesisDimension) -> list:
    return [item for item in dim.evidence if item.source_type == "conflict"]


def test_benign_verified_conflict_adds_no_negative_vote_and_no_penalty():
    """良性解释的已核验冲突：不投负票（无冲突证据条目）、不扣维度分（基线 1.0 不变）。"""
    thesis = _run_engine("扩张期正常现象")
    dim = thesis.dimensions["earnings_quality"]
    assert _conflict_evidence(dim) == []
    assert dim.score == pytest.approx(1.0)


def test_malignant_verified_conflict_keeps_negative_contribution():
    """恶性解释的已核验冲突：维持既有负贡献（负向证据条目 + high 档 penalty 0.55）。"""
    thesis = _run_engine("确认操纵")
    dim = thesis.dimensions["earnings_quality"]
    assert len(_conflict_evidence(dim)) == 1
    assert _conflict_evidence(dim)[0].impact == "negative"
    assert dim.score == pytest.approx(1.0 - 0.55)


def test_partial_malignant_conflict_keeps_scaled_penalty():
    """partial 恶性冲突：维持 0.7 折减口径。"""
    thesis = _run_engine("确认粉饰", status="partial")
    dim = thesis.dimensions["earnings_quality"]
    assert len(_conflict_evidence(dim)) == 1
    assert dim.score == pytest.approx(1.0 - 0.55 * 0.7)


def test_benign_conflict_still_fills_gaps():
    """gaps 补齐与方向投影正交：良性冲突同样补齐缺失证据（仅跳过负票/扣分）。"""
    thesis = _run_engine("正常现象", gaps=["客户回款明细"])
    dim = thesis.dimensions["earnings_quality"]
    assert _conflict_evidence(dim) == []
    assert dim.score == pytest.approx(1.0)
    assert "客户回款明细" in dim.missing_evidence


def test_benign_conflict_medium_severity_still_no_penalty():
    """severity=medium 的良性已核验冲突：同样跳过（修复前 medium 本不触发 penalty，行为不变）。"""
    thesis = _run_engine("正常现象", severity="medium")
    dim = thesis.dimensions["earnings_quality"]
    assert _conflict_evidence(dim) == []
    assert dim.score == pytest.approx(1.0)


# ── ③ reviewer._conflict_votes：与 engine 同口径 ────────────────────────────


def _conflicts_payload(explanations: list[str], *, status: str = "verified", severity: str = "high"):
    """真实契约载荷（``ConflictAnalysisResult``），验证结算只回写 status（同编排层）。"""
    return ConflictAnalysisResult(
        conflicts=[
            ConflictItem(
                id="c1",
                theme="盈利增长但现金流恶化",
                description="利润增长未被现金流验证。",
                related_dimensions=["earnings_quality"],
                severity=severity,
                confidence=0.9,
                hypotheses=[
                    HypothesisItem(
                        id=f"h{i}",
                        conflict_id="c1",
                        explanation=explanation,
                        predictions=[],
                        required_evidence=[],
                        score=0.8,
                        status=status,
                    )
                    for i, explanation in enumerate(explanations, start=1)
                ],
            )
        ]
    )


def test_conflict_votes_skip_benign_settled_conflicts():
    votes, notes = reviewer_module._conflict_votes(_conflicts_payload(["扩张期正常现象"]))
    assert votes == []
    assert any("良性" in note for note in notes)


def test_conflict_votes_keep_malignant_votes():
    votes, notes = reviewer_module._conflict_votes(_conflicts_payload(["确认操纵"]))
    assert votes == pytest.approx([-0.55 / 0.8])
    assert notes


def test_conflict_votes_unknown_falls_back_to_negative_vote():
    """判不清 → 保守回落既有负贡献（行为与修复前一致）。"""
    votes, _ = reviewer_module._conflict_votes(_conflicts_payload(["待进一步核验"]))
    assert votes == pytest.approx([-0.55 / 0.8])


def test_conflict_votes_mixed_benign_and_malignant_casts_vote():
    votes, _ = reviewer_module._conflict_votes(_conflicts_payload(["正常现象", "确认操纵"]))
    assert votes == pytest.approx([-0.55 / 0.8])


def test_conflict_votes_mixed_benign_and_unknown_casts_vote():
    """全良性才不计票；掺入判不清的假设 → 保守维持负票。"""
    votes, _ = reviewer_module._conflict_votes(_conflicts_payload(["正常现象", "待进一步核验"]))
    assert votes == pytest.approx([-0.55 / 0.8])


def test_engine_and_reviewer_share_the_same_projection_rule():
    """同一 explanation 在两个消费点给出同一方向结论（口径一致的机读证据）。"""
    for text in ("扩张期正常现象", "确认操纵", "待进一步核验"):
        thesis = _run_engine(text)
        dim = thesis.dimensions["earnings_quality"]
        votes, _ = reviewer_module._conflict_votes(_conflicts_payload([text]))
        engine_skipped = _conflict_evidence(dim) == []
        reviewer_skipped = votes == []
        assert engine_skipped == reviewer_skipped, f"两处口径不一致：{text!r}"


# ── ④ 审计边：全良性冲突不要求扣分、该边不适用（消除伪 D3） ─────────────────


def _thesis(*, dim_judgment: str = "positive", dim_score: float = 0.5) -> InvestmentThesis:
    return InvestmentThesis(
        symbol="600519.SH",
        period="2024Q4",
        dimensions={
            "earnings_quality": ThesisDimension(
                id="earnings_quality",
                name="盈利质量",
                judgment=dim_judgment,
                score=dim_score,
                confidence=0.8,
            )
        },
        overall_judgment=dim_judgment,
        overall_score=dim_score,
    )


def test_audit_skips_benign_conflicts_and_does_not_require_penalty():
    """engine 按投影跳过扣分后，正向维度 + 全良性冲突不得再被判「扣分未落地」。"""
    audit = audit_amplification(
        _thesis(dim_judgment="positive"),
        None,
        None,
        _conflicts_payload(["扩张期正常现象"]),
        edge="verified_conflict->dimension_score",
    )
    assert audit is not None
    assert audit.direction_consistent is True
    assert audit.weight is None
    assert "核查 0 个" in audit.rationale


def test_audit_flags_malignant_conflicts_on_positive_dimensions():
    audit = audit_amplification(
        _thesis(dim_judgment="positive"),
        None,
        None,
        _conflicts_payload(["确认操纵"]),
        edge="verified_conflict->dimension_score",
    )
    assert audit is not None
    assert audit.direction_consistent is False
    assert audit.weight == 0.55


def test_audit_mixed_benign_and_malignant_still_flags():
    audit = audit_amplification(
        _thesis(dim_judgment="positive"),
        None,
        None,
        _conflicts_payload(["正常现象", "确认操纵"]),
        edge="verified_conflict->dimension_score",
    )
    assert audit is not None and audit.direction_consistent is False


def test_edge_inapplicable_when_only_benign_settled_conflicts():
    """insight/signals 均缺且冲突全良性 ⇒ 该边无审计输入 → 不构造"看起来一致"的空审计。"""
    assert audit_amplification(_thesis(), None, None, _conflicts_payload(["正常现象"])) is None


def test_edge_still_applicable_for_unknown_direction():
    """判不清的已核验冲突维持强制扣分面（保守回落），该边仍适用。"""
    audit = audit_amplification(_thesis(dim_judgment="positive"), None, None, _conflicts_payload(["待进一步核验"]))
    assert audit is not None
    assert audit.edge == "verified_conflict->dimension_score"
    assert audit.direction_consistent is False
