"""F2：insight 反证按 related_dimensions 定向注入（不再全维广播错位）的回归钉。

设计依据：``docs/implementation/thesis/THESIS_CONTEXT_MISMATCH_FIX_PLAN.md`` §3.2 / §5.2。

行为：``engine._apply_insight`` 对每条 ``counter_evidence`` 读 ``related_dimensions``——
带合法维度 id 时只注入目标维度；无字段 / 空数组时保持全维广播（旧产物兼容）；非法 id
按 ``dimensions`` 键过滤丢弃（不报错）。反证注入只动 counter_evidence，不动置信度阻尼与分数通道。
"""

from __future__ import annotations

from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.models import ThesisDimension

_THESIS_DIMS = (
    "financial_quality",
    "earnings_quality",
    "growth_quality",
    "competitive_moat",
    "credit_risk",
    "operational_stability",
    "capital_efficiency",
    "valuation_fit",
)


def _dims() -> dict[str, ThesisDimension]:
    return {dim_id: ThesisDimension(id=dim_id, name=dim_id, judgment="neutral", score=0.0) for dim_id in _THESIS_DIMS}


def _apply_insight(dims: dict[str, ThesisDimension], insight: dict) -> None:
    ThesisEngine()._apply_insight(dimensions=dims, insight=insight)


def _text(statement: str) -> str:
    return f"[洞察反证] {statement} (来源: insight)"


def test_related_dimensions_scopes_counter_evidence_to_target():
    """带合法 related_dimensions 的反证只进目标维度，其它维度不收到。"""
    dims = _dims()
    _apply_insight(
        dims,
        {"counter_evidence": [{"statement": "反证X", "source": "insight", "related_dimensions": ["earnings_quality"]}]},
    )

    text = _text("反证X")
    assert text in dims["earnings_quality"].counter_evidence
    for dim_id, dim in dims.items():
        if dim_id != "earnings_quality":
            assert text not in dim.counter_evidence


def test_missing_related_dimensions_broadcasts_to_all():
    """无 related_dimensions 字段的旧产物 → 全维广播（行为兼容）。"""
    dims = _dims()
    _apply_insight(dims, {"counter_evidence": [{"statement": "反证X", "source": "insight"}]})

    text = _text("反证X")
    for dim in dims.values():
        assert text in dim.counter_evidence


def test_illegal_related_dimension_id_is_dropped_without_error():
    """非法维度 id 被丢弃（不报错），仅合法目标维度收到反证。"""
    dims = _dims()
    _apply_insight(
        dims,
        {
            "counter_evidence": [
                {"statement": "反证X", "source": "insight", "related_dimensions": ["bogus_dim", "earnings_quality"]}
            ]
        },
    )

    text = _text("反证X")
    assert text in dims["earnings_quality"].counter_evidence
    for dim_id, dim in dims.items():
        if dim_id != "earnings_quality":
            assert text not in dim.counter_evidence


def test_dedup_coexists_with_conflict_counter_evidence():
    """insight 的反证去重与 _apply_conflict_analysis 的 counter_evidence 共存、各留一份。"""
    engine = ThesisEngine()
    dims = _dims()
    engine._apply_conflict_analysis(
        dimensions=dims,
        dim_effective={dim_id: [] for dim_id in dims},
        conflict_analysis={
            "conflicts": [
                {
                    "id": "c1",
                    "theme": "冲突",
                    "description": "描述",
                    "related_dimensions": ["credit_risk"],
                    "severity": "high",
                    "confidence": 0.9,
                    "hypotheses": [{"id": "h1", "explanation": "大存大贷", "status": "pending"}],
                }
            ]
        },
        verification_results=[{"hypothesis_id": "h1", "status": "rejected", "summary": "核验结论：再融资。"}],
        signal_results={},
        anomaly_report=None,
    )
    # 全维广播注入两次同一反证：去重后各维度只留一份。
    insight = {"counter_evidence": [{"statement": "反证X", "source": "insight"}]}
    _apply_insight(dims, insight)
    _apply_insight(dims, insight)

    credit = dims["credit_risk"]
    # conflict 写入的反证与 insight 注入的反证共存，各一份（无重复）。
    assert credit.counter_evidence.count("核验结论：再融资。") == 1
    assert credit.counter_evidence.count(_text("反证X")) == 1
