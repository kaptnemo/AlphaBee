"""F3：审查层闭环（一致性进 issues + 双条件确定性重校）的回归钉。

设计依据：``docs/design/THESIS_CONTEXT_MISMATCH_FIX_PLAN.md`` §3.3 / §5.2。

行为：
* ``_merge_llm``：``signals_consistent=false`` 且 status != contested → issues 追加「信号一致性存疑：…」
  （rationale 为空用默认文案）；status==confirmed 升 contested；conflict_description 写入保留。
* ``_apply_recalibration``：双条件（LLM 层标志 ∧ 反证存在）→ strong_* 降一档；无反证/非强档不降；
  不升档不翻向；``original_judgment`` 保持原值。
"""

from __future__ import annotations

import pytest

from alphabee.agents.thesis.models import DimensionVerdict, InvestmentThesis, ThesisDimension, ThesisReview
from alphabee.agents.thesis.reviewer import ThesisReviewer


def _thesis(
    *, dim_id: str = "earnings_quality", judgment: str = "strong_negative", counter_evidence=("反证X",)
) -> InvestmentThesis:
    dim = ThesisDimension(
        id=dim_id,
        name=dim_id,
        judgment=judgment,
        score=-1.0 if judgment.endswith("negative") else 1.0,
        counter_evidence=list(counter_evidence),
    )
    return InvestmentThesis(symbol="600519.SH", period="2024Q4", dimensions={dim_id: dim})


def _verdict(
    dim_id: str = "earnings_quality",
    *,
    status: str = "qualified",
    original_judgment: str = "strong_negative",
    issues: list[str] | None = None,
    conflict_description: str = "",
) -> DimensionVerdict:
    return DimensionVerdict(
        dimension_id=dim_id,
        dimension_name=dim_id,
        status=status,
        evidence_count=1,
        original_judgment=original_judgment,
        original_score=-1.0 if original_judgment.endswith("negative") else 1.0,
        issues=list(issues or []),
        conflict_description=conflict_description,
    )


@pytest.mark.parametrize(
    ("status", "issues", "conflict_description"),
    [
        ("contested", [], ""),  # L1 contested 标志
        ("qualified", ["语境不适配：需结合行业背景校准"], ""),  # LLM 语境不适配 issue 标志
        ("qualified", [], "信号方向冲突"),  # LLM 信号一致性存疑（conflict_description）
    ],
)
def test_recalibrates_strong_judgment_when_flagged_and_counter_present(status, issues, conflict_description):
    """双条件（标志 ∧ 反证存在）→ strong_negative 降一档；original_judgment 保持原值。"""
    reviewer = ThesisReviewer()
    verdicts = {"earnings_quality": _verdict(status=status, issues=issues, conflict_description=conflict_description)}
    thesis = _thesis(counter_evidence=("反证X",))

    reviewer._apply_recalibration(verdicts, thesis)

    v = verdicts["earnings_quality"]
    assert v.recalibrated_judgment == "negative"
    assert v.original_judgment == "strong_negative"
    assert v.suggested_action == "reconsider_with_context"  # suggested_action 为空时补


def test_no_recalibration_without_counter_evidence():
    """有标志但无反证 → 不降。"""
    reviewer = ThesisReviewer()
    verdicts = {"earnings_quality": _verdict(status="contested")}
    thesis = _thesis(counter_evidence=())

    reviewer._apply_recalibration(verdicts, thesis)

    assert verdicts["earnings_quality"].recalibrated_judgment == ""


def test_no_recalibration_for_non_strong_judgment():
    """非强档（negative）即使有标志 + 反证 → 不降。"""
    reviewer = ThesisReviewer()
    verdicts = {"earnings_quality": _verdict(original_judgment="negative", status="contested")}
    thesis = _thesis(judgment="negative", counter_evidence=("反证X",))

    reviewer._apply_recalibration(verdicts, thesis)

    assert verdicts["earnings_quality"].recalibrated_judgment == ""


def test_recalibration_never_upgrades_or_flips_direction():
    """strong_positive→positive、strong_negative→negative（降一档、方向不变、不升档）。"""
    reviewer = ThesisReviewer()
    dims = {
        "earnings_quality": ThesisDimension(
            id="earnings_quality", name="盈利质量", judgment="strong_positive", score=1.0, counter_evidence=["反证X"]
        ),
        "credit_risk": ThesisDimension(
            id="credit_risk", name="信用风险", judgment="strong_negative", score=-1.0, counter_evidence=["反证Y"]
        ),
    }
    thesis = InvestmentThesis(symbol="600519.SH", period="2024Q4", dimensions=dims)
    verdicts = {
        "earnings_quality": _verdict(
            dim_id="earnings_quality", original_judgment="strong_positive", status="contested"
        ),
        "credit_risk": _verdict(dim_id="credit_risk", original_judgment="strong_negative", status="contested"),
    }

    reviewer._apply_recalibration(verdicts, thesis)

    assert verdicts["earnings_quality"].recalibrated_judgment == "positive"
    assert verdicts["credit_risk"].recalibrated_judgment == "negative"
    assert verdicts["earnings_quality"].original_judgment == "strong_positive"
    assert verdicts["credit_risk"].original_judgment == "strong_negative"


def test_to_dict_includes_recalibrated_judgment():
    v = _verdict(status="contested")
    v.recalibrated_judgment = "negative"
    review = ThesisReview(symbol="600519.SH", period="2024Q4", dimension_verdicts={"earnings_quality": v})

    d = review.to_dict()

    assert d["dimension_verdicts"]["earnings_quality"]["recalibrated_judgment"] == "negative"
    assert d["dimension_verdicts"]["earnings_quality"]["original_judgment"] == "strong_negative"
    # 既有键不破坏
    assert d["dimension_verdicts"]["earnings_quality"]["status"] == "contested"
    assert d["symbol"] == "600519.SH"


def test_merge_llm_signals_inconsistent_closes_the_loop():
    """signals_consistent=false：qualified 追加 issue、confirmed 升 contested、conflict_description 保留。"""
    reviewer = ThesisReviewer()
    verdicts = {
        "earnings_quality": _verdict(status="qualified"),
        "financial_quality": _verdict(dim_id="financial_quality", status="qualified"),
        "growth_quality": _verdict(dim_id="growth_quality", status="confirmed"),
    }
    llm_data = {
        "dimension_reviews": {
            "earnings_quality": {"signals_consistent": False, "consistency_rationale": "正负信号冲突"},
            "financial_quality": {"signals_consistent": False, "consistency_rationale": ""},
            "growth_quality": {"signals_consistent": False, "consistency_rationale": ""},
        }
    }

    result = reviewer._merge_llm(verdicts, llm_data)

    eq = result["earnings_quality"]
    assert any("信号一致性存疑" in issue and "正负信号冲突" in issue for issue in eq.issues)
    assert eq.conflict_description == "正负信号冲突"
    assert eq.status == "qualified"  # 不升 contested（Layer 1 已 qualified）

    fq = result["financial_quality"]
    assert any("多条信号方向冲突" in issue for issue in fq.issues)  # rationale 为空用默认文案

    gq = result["growth_quality"]
    assert gq.status == "contested"  # confirmed 升 contested
    assert not any("信号一致性存疑" in issue for issue in gq.issues)
