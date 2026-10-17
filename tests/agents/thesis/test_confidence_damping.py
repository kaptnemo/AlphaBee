"""D-2 P0：维度置信度归零修复（冲突折减改「乘性 ×0.85、一档封顶」）的回归钉。

设计依据：``docs/design/THESIS_CONFIDENCE_DAMPING_DESIGN.md`` §3.2 / §3.3 / §6.1 / §6.3。

根因（两轮真实 run 复现：002916 深南电路 7×``0.0`` + ``credit_risk`` ``0.03``；300760 迈瑞医疗
7×``0.0`` + ``operational_stability`` ``0.12``）：``engine._apply_conflict_analysis`` 用**绝对减法**
（verified+gap / rejected ``−0.05``、unknown/pending ``−0.1``）按「每条假设 × 每个相关维度」重复
折减 confidence，而 confidence 本身是**覆盖度比例**（``len(contribs) / total_signals``，
``total_signals = 22`` ⇒ 单维度初始量级 4.5%~27%）⇒ 一次 ``−0.1`` 即击穿；置信度归零后
被下游（reviewer Rule 1 / ``_compute_overall``）当成「无信号覆盖」。

修复（§3.2）：三处减法改为 ``damp_flags`` 登记，循环后对每个被命中维度**最多乘一次**
``DAMPING_FACTOR``（0.85，与 ``_apply_insight`` 的降级阻尼同源同值，DEVIATION §7.2「一档封顶」）；
**score 路径零改动**（罚分 / ``_remove_disputed_contributions`` 精确扣除 / ``counter_evidence`` /
``missing_evidence`` 全部原样）。reviewer 侧（§3.3）Rule 1 文案分流：证据为空才报「无信号覆盖」。

判别力（变异必红，§6.5）：
* M1 把乘性改回 ``max(0.0, conf - 0.1)`` ⇒ 本文件「乘性 / 不归零」断言必红；
* M2 去掉一档封顶（每条假设乘一次）⇒ 「一档封顶」断言必红；
* M3 删掉 reviewer 文案分流 ⇒ 文案断言必红；
* M4 误改 score 路径（如删罚分）⇒ 本文件 score 断言 + ``test_conflict_direction_r3.py`` 的
  002916 分数逐位断言 + ``test_confidence_penalty.py`` 必红。

P1 追加（§3.4 可观测性）：折减循环内产出确定性日志 ``thesis_confidence_damped``
（``dim_id`` / ``hits`` / ``damped``），命中数由 ``damp_flags: dict[str, int]`` 计数 ——
本文件末尾两例用 ``caplog`` 断言字段齐全、按维度计数、零覆盖维度不产出该行。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
import structlog

from alphabee.agents.thesis import engine as engine_module
from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.models import (
    CompanyContext,
    EvidenceItem,
    ThesisDimension,
)
from alphabee.agents.thesis.reviewer import ThesisReviewer

# ── 构造工具（按 §3.2 的补丁面：直接驱动 ``_apply_conflict_analysis``，不引入 I/O）───────

_TARGET_DIM = "earnings_quality"


def _dim(
    dim_id: str,
    confidence: float,
    *,
    score: float = 0.0,
    evidence: list[EvidenceItem] | None = None,
) -> ThesisDimension:
    return ThesisDimension(
        id=dim_id,
        name=dim_id,
        judgment="neutral",
        score=score,
        evidence=list(evidence or []),
        confidence=confidence,
    )


def _ev(impact: str = "neutral", level: str = "none") -> EvidenceItem:
    return EvidenceItem(
        signal_id=f"sig_{impact}_{level}",
        signal_name=f"sig_{impact}_{level}",
        level=level,
        impact=impact,
    )


def _conflict(
    cid: str,
    dim_ids: list[str],
    *,
    severity: str = "high",
    hypotheses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": cid,
        "theme": f"冲突{cid}",
        "description": "描述",
        "related_dimensions": list(dim_ids),
        "severity": severity,
        "confidence": 0.9,
        "hypotheses": hypotheses
        if hypotheses is not None
        else [{"id": f"{cid}_h1", "explanation": "确认操纵收入", "status": "pending"}],
    }


def _verified(hid: str, status: str, *, gaps: list[str] | None = None, summary: str = "核验结论。") -> dict[str, Any]:
    return {"hypothesis_id": hid, "status": status, "summary": summary, "gaps": list(gaps or [])}


def _apply_conflict(
    *,
    dimensions: dict[str, ThesisDimension],
    conflicts: list[dict[str, Any]],
    verification_results: list[dict[str, Any]] | None = None,
    dim_effective: dict[str, list[tuple[str, str, float]]] | None = None,
    signal_results: dict[str, dict[str, Any]] | None = None,
    anomaly_report: dict[str, Any] | None = None,
) -> None:
    ThesisEngine()._apply_conflict_analysis(
        dimensions=dimensions,
        dim_effective=dim_effective if dim_effective is not None else {dim_id: [] for dim_id in dimensions},
        conflict_analysis={"conflicts": conflicts},
        verification_results=verification_results,
        signal_results=signal_results or {},
        anomaly_report=anomaly_report,
    )


def _hits(dim_id: str, count: int, *, severity: str = "high") -> tuple[list[dict[str, Any]], list[str]]:
    """构造 ``count`` 条各自命中 ``dim_id`` 的冲突与对应的假设 id（一假设一冲突）。"""
    conflicts = [
        _conflict(
            f"c{i}",
            [dim_id],
            severity=severity,
            hypotheses=[{"id": f"h{i}", "explanation": "确认操纵收入", "status": "pending"}],
        )
        for i in range(count)
    ]
    return conflicts, [f"h{i}" for i in range(count)]


# ── §6.1 用例 1：单次命中为乘性（0.5 → 0.425）───────────────────────────────


def test_single_hit_is_multiplicative():
    """1 条 rejected 假设命中 ⇒ ``0.5 × 0.85 = 0.425``（不是 ``0.5 − 0.05``）。"""
    dims = {_TARGET_DIM: _dim(_TARGET_DIM, 0.5, score=0.5)}
    conflicts, hids = _hits(_TARGET_DIM, 1)

    _apply_conflict(
        dimensions=dims,
        conflicts=conflicts,
        verification_results=[_verified(hids[0], "rejected")],
    )

    assert dims[_TARGET_DIM].confidence == pytest.approx(0.425)
    # 变异 M1（改回绝对减法 −0.05）时该断言必红：0.5 − 0.05 = 0.45 ≠ 0.425。
    assert dims[_TARGET_DIM].confidence != pytest.approx(0.5 - 0.05)


# ── §6.1 用例 2：一档封顶（5 条假设命中仍只乘一次）─────────────────────────


def test_damping_capped_at_one_tier_for_five_hits():
    """同维度被 5 条假设命中 ⇒ 仍 ``0.425``（≠ ``0.5 × 0.85⁵``）。"""
    dims = {_TARGET_DIM: _dim(_TARGET_DIM, 0.5, score=0.5)}
    conflicts, hids = _hits(_TARGET_DIM, 5)

    _apply_conflict(
        dimensions=dims,
        conflicts=conflicts,
        verification_results=[_verified(hid, "rejected") for hid in hids],
    )

    conf = dims[_TARGET_DIM].confidence
    assert conf == pytest.approx(0.425)
    # 变异 M2（去掉一档封顶、每条假设乘一次）时该断言必红。
    assert conf != pytest.approx(0.5 * 0.85**5)


def test_damping_capped_across_mixed_status_and_severity():
    """混合 status/severity 命中同一维度 ⇒ 仍只乘一次（一档封顶与命中路径无关）。"""
    dims = {_TARGET_DIM: _dim(_TARGET_DIM, 0.5, score=0.5)}
    conflicts = [
        _conflict("c1", [_TARGET_DIM], severity="high", hypotheses=[{"id": "h1", "explanation": "确认操纵收入"}]),
        _conflict("c2", [_TARGET_DIM], severity="critical", hypotheses=[{"id": "h2", "explanation": "确认操纵收入"}]),
        _conflict("c3", [_TARGET_DIM], severity="medium", hypotheses=[{"id": "h3", "explanation": "确认操纵收入"}]),
    ]
    verifications = [
        _verified("h1", "verified", gaps=["缺口：缺少第三方重算。"]),
        _verified("h2", "rejected"),
        _verified("h3", "unknown"),
    ]

    _apply_conflict(dimensions=dims, conflicts=conflicts, verification_results=verifications)

    assert dims[_TARGET_DIM].confidence == pytest.approx(0.425)


# ── §6.1 用例 3：三种 status 同口径（verified+gap / rejected / unknown）─────


def test_three_status_paths_share_one_tier():
    """三条路径各命中一次 ⇒ 各自只乘一次 0.85（原 −0.05 / −0.05 / −0.1 的相对差异取消）。"""
    dims = {
        "earnings_quality": _dim("earnings_quality", 0.5, score=0.5),
        "growth_quality": _dim("growth_quality", 0.5, score=0.5),
        "financial_quality": _dim("financial_quality", 0.5, score=0.5),
    }
    conflicts = [
        _conflict(
            "c1", ["earnings_quality"], severity="medium", hypotheses=[{"id": "h1", "explanation": "确认操纵收入"}]
        ),
        _conflict(
            "c2", ["growth_quality"], severity="medium", hypotheses=[{"id": "h2", "explanation": "确认操纵收入"}]
        ),
        _conflict(
            "c3", ["financial_quality"], severity="medium", hypotheses=[{"id": "h3", "explanation": "确认操纵收入"}]
        ),
    ]
    verifications = [
        _verified("h1", "verified", gaps=["缺口：缺少第三方重算。"]),
        _verified("h2", "rejected"),
        _verified("h3", "unknown"),
    ]

    _apply_conflict(dimensions=dims, conflicts=conflicts, verification_results=verifications)

    for dim_id in dims:
        assert dims[dim_id].confidence == pytest.approx(0.425), dim_id


# ── §6.1 用例 4：G1 不归零不变量（任意冲突条数 × 任意假设数）────────────────


@pytest.mark.parametrize(
    ("status", "hits"),
    [
        ("rejected", 1),  # 原 −0.05 一次
        ("rejected", 32),  # 原 −0.05 三十二次（远超薄覆盖度）
        ("unknown", 1),  # 原 −0.1 一次（300760/002916 的击穿路径）
        ("unknown", 32),
        ("verified", 32),  # 原 verified+gap 的 −0.05 三十二次
    ],
)
def test_g1_covered_dimension_never_reaches_zero(status: str, hits: int):
    """薄覆盖量级（1/22 = 0.045）被任意条数冲突命中 ⇒ ``confidence > 0``。"""
    initial = 1 / 22
    dims = {_TARGET_DIM: _dim(_TARGET_DIM, initial, score=0.2)}
    conflicts, hids = _hits(_TARGET_DIM, hits, severity="critical")
    verifications = [
        _verified(hid, status, gaps=["缺口：缺少第三方重算。"] if status == "verified" else None) for hid in hids
    ]

    _apply_conflict(dimensions=dims, conflicts=conflicts, verification_results=verifications)

    conf = dims[_TARGET_DIM].confidence
    assert conf > 0.0
    assert conf == pytest.approx(round(initial * 0.85, 3))


def test_g1_covered_dimension_never_reaches_zero_end_to_end():
    """端到端（engine.run）：22 条信号 + 8 条 unknown 冲突 ⇒ 有贡献维度置信度仍 > 0。"""
    signal_results = {
        "quality_signal": {
            "level": "high",
            "interpretation": "盈利质量信号。",
            "thesis_impact": {"earnings_quality": "positive"},
        }
    }
    # 21 条计数但不映射任何维度的信号：把覆盖度压到真实的 1/22 量级。
    for i in range(21):
        signal_results[f"filler_{i:02d}"] = {"level": "none", "interpretation": "无风险发现。"}

    conflicts = [
        _conflict(
            f"c{i}",
            ["earnings_quality"],
            severity="high",
            hypotheses=[{"id": f"h{i}", "explanation": "确认操纵收入", "status": "pending"}],
        )
        for i in range(8)
    ]
    verifications = [_verified(f"h{i}", "unknown") for i in range(8)]

    thesis = ThesisEngine().run(
        symbol="300760.SZ",
        period="20260630",
        signal_results=signal_results,
        conflict_analysis={"conflicts": conflicts},
        verification_results=verifications,
    )

    dim = thesis.dimensions["earnings_quality"]
    assert 0.0 < dim.confidence == pytest.approx(round((1 / 22) * 0.85, 3))
    # 修复前：8 × (−0.1) 早已把 0.045 打成 0（G1 失守）。
    assert dim.confidence > 0.0


# ── §6.1 用例 5：零覆盖保持零（不抬升）──────────────────────────────────────


def test_zero_coverage_dimension_stays_zero_when_hit():
    """无贡献维度（覆盖度 0）被命中 ⇒ 仍 ``0.0``（阻尼只作用于「有证据但被修订过」的维度）。"""
    dims = {"growth_quality": _dim("growth_quality", 0.0, score=0.0)}
    conflicts, hids = _hits("growth_quality", 5, severity="critical")

    _apply_conflict(
        dimensions=dims,
        conflicts=conflicts,
        verification_results=[_verified(hid, "rejected") for hid in hids],
    )

    assert dims["growth_quality"].confidence == 0.0


def test_zero_coverage_dimension_stays_zero_end_to_end():
    """端到端：无信号映射的维度被冲突命中 ⇒ confidence 保持 0.0，且 conflict 证据只进 evidence。"""
    signal_results = {
        "quality_signal": {
            "level": "high",
            "interpretation": "盈利质量信号。",
            "thesis_impact": {"earnings_quality": "positive"},
        }
    }
    conflicts = [
        _conflict(
            "c1",
            ["growth_quality"],
            severity="high",
            hypotheses=[{"id": "h1", "explanation": "确认操纵收入", "status": "pending"}],
        )
    ]

    thesis = ThesisEngine().run(
        symbol="300760.SZ",
        period="20260630",
        signal_results=signal_results,
        conflict_analysis={"conflicts": conflicts},
        verification_results=[_verified("h1", "rejected")],
    )

    growth = thesis.dimensions["growth_quality"]
    assert growth.confidence == 0.0
    assert growth.evidence == []
    assert growth.counter_evidence  # rejected 的反证照常入账（score 路径不受影响）


# ── §6.1 用例 6：score 路径不受影响（回归 P1-4 / P0-③）─────────────────────


def test_penalty_is_exact_and_does_not_raise_confidence():
    """verified 恶性冲突：score 精确减 penalty，confidence **不变**（gaps 为空 ⇒ 不折减、不抬升）。"""
    dim = _dim("credit_risk", 0.5, score=0.5)
    conflicts = [
        _conflict("c1", ["credit_risk"], severity="high", hypotheses=[{"id": "h1", "explanation": "确认操纵收入"}])
    ]

    _apply_conflict(
        dimensions={"credit_risk": dim},
        conflicts=conflicts,
        verification_results=[_verified("h1", "verified")],
    )

    assert dim.score == pytest.approx(0.5 - 0.55)
    assert dim.confidence == pytest.approx(0.5)  # P1-4：罚分**不**升置信度
    assert any(item.source_label == "verified_conflict" for item in dim.evidence)


def test_gap_path_keeps_evidence_append_and_only_damps_confidence():
    """verified+gap：``missing_evidence`` 照常追加、罚分照常执行，只有 confidence 走乘性阻尼。"""
    dim = _dim("credit_risk", 0.5, score=0.5)
    gaps = ["缺口：缺少第三方重算。", "缺口：缺少币种敞口拆分。"]
    conflicts = [
        _conflict("c1", ["credit_risk"], severity="high", hypotheses=[{"id": "h1", "explanation": "确认操纵收入"}])
    ]

    _apply_conflict(
        dimensions={"credit_risk": dim},
        conflicts=conflicts,
        verification_results=[_verified("h1", "verified", gaps=gaps)],
    )

    assert dim.score == pytest.approx(0.5 - 0.55)
    assert dim.missing_evidence == gaps
    assert dim.confidence == pytest.approx(0.425)


def test_rejected_path_deducts_disputed_contribution_exactly_and_only_damps_confidence():
    """rejected：被证伪的那块贡献精确扣除（不是整维归零），反证入 ``counter_evidence``。"""
    dim = _dim("credit_risk", 0.5, score=0.4)
    dim_effective = {"credit_risk": [("sig_disputed", "signal", 0.9), ("sig_keep", "signal", -0.1)]}
    hypothesis = {
        "id": "h1",
        "explanation": "确认操纵收入",
        "status": "pending",
        "disputed_signal_ids": ["sig_disputed"],
    }
    conflicts = [_conflict("c1", ["credit_risk"], severity="high", hypotheses=[hypothesis])]

    _apply_conflict(
        dimensions={"credit_risk": dim},
        conflicts=conflicts,
        verification_results=[_verified("h1", "rejected", summary="核验结论：不构成资金占用。")],
        dim_effective=dim_effective,
    )

    assert dim_effective["credit_risk"] == [("sig_keep", "signal", -0.1)]
    assert dim.score == pytest.approx(-0.1)
    assert "核验结论：不构成资金占用。" in dim.counter_evidence
    assert dim.confidence == pytest.approx(0.425)


def test_unknown_path_keeps_missing_evidence_append_and_only_damps_confidence():
    """unknown/pending：``missing_evidence`` 照常追加（原 ``−0.1`` 改为一次 ×0.85）。"""
    dim = _dim("growth_quality", 0.4, score=0.2)
    conflicts = [
        _conflict("c1", ["growth_quality"], severity="medium", hypotheses=[{"id": "h1", "explanation": "待进一步核验"}])
    ]

    _apply_conflict(
        dimensions={"growth_quality": dim},
        conflicts=conflicts,
        verification_results=[_verified("h1", "unknown", gaps=["缺口：缺少连续两期序列。"])],
    )

    assert dim.missing_evidence == ["缺口：缺少连续两期序列。"]
    assert dim.score == pytest.approx(0.2)  # unknown 不改分数
    assert dim.confidence == pytest.approx(0.34)  # round(0.4 × 0.85, 3)


# ── §6.1 用例 7：reviewer Rule 1 文案分流 ───────────────────────────────────


def _verdict(dim: ThesisDimension) -> Any:
    return ThesisReviewer()._layer1_check(dim.id, dim, {}, CompanyContext())


def test_rule1_reports_no_coverage_only_when_evidence_is_empty():
    """evidence 为空 ⇒ 原文案「该维度无信号覆盖，判断不可靠」。"""
    for confidence in (0.0, 0.5):
        verdict = _verdict(_dim("earnings_quality", confidence))
        assert verdict.status == "insufficient"
        assert verdict.suggested_action == "needs_more_data"
        assert verdict.issues[0] == "该维度无信号覆盖，判断不可靠"


def test_rule1_reports_conflict_damping_when_confidence_zero_with_evidence():
    """``confidence == 0`` 且 evidence 非空 ⇒ 如实新文案（证据条数），不再误报「无信号覆盖」。"""
    verdict = _verdict(_dim("earnings_quality", 0.0, evidence=[_ev(), _ev(), _ev()]))

    assert verdict.status == "insufficient"
    assert verdict.suggested_action == "needs_more_data"
    joined = " ".join(verdict.issues)
    assert "该维度无信号覆盖" not in joined
    assert "该维度置信度被冲突折减归零" in joined
    assert "证据 3 条" in joined


def test_rule1_status_decision_unchanged_for_old_artifact_replay():
    """状态判定不变（旧 artifact 重放行为等价）：两条分支都是 insufficient / needs_more_data。"""
    empty_evidence = _verdict(_dim("earnings_quality", 0.0))
    zero_conf_with_evidence = _verdict(_dim("earnings_quality", 0.0, evidence=[_ev()]))

    assert (empty_evidence.status, empty_evidence.suggested_action) == ("insufficient", "needs_more_data")
    assert (zero_conf_with_evidence.status, zero_conf_with_evidence.suggested_action) == (
        "insufficient",
        "needs_more_data",
    )


def test_rule1_does_not_fire_with_evidence_and_positive_confidence():
    """有证据且 confidence > 0 ⇒ Rule 1 不触发（不得因本修复把正常维度降级）。"""
    verdict = _verdict(_dim("earnings_quality", 0.8, evidence=[_ev(), _ev()]))

    assert verdict.status == "confirmed"
    assert verdict.issues == []


# ── §6.3 300760 半重建 fixture（口径诚实披露）──────────────────────────────

# 口径披露（F-4，设计 §6.3 / 附录 A.5）：
# * **重建**：22 条信号的 ``level`` 逐条来自 record（``task-623f034dc596``，分布
#   ``none 15 / low 2 / medium 2 / missing_fact 3``）；``thesis_impact`` 未落 record
#   （``recorder._extract_signals`` 只存 id/level/interpretation），按下表由 report
#   ``dimension_analysis`` 的 evidence 清单**重建**；
# * **推断**：6 条 ``verified_conflict`` issue 的 severity 来自 record，``related_dimensions``
#   按 report 的冲突→维度叙述**推断**（record 未落该字段）；
# * 因此断言一律用**不变量**而非精确值（设计 §6.3 明文）；但重建后的**信号层分数**对
#   6/8 个维度与 record（``data/task_records/300760.SZ/2026-10-16/task-623f034dc596.json``）
#   逐位吻合（见 ``test_300760_reconstructed_signal_layer_matches_record_scores``），
#   可作为重建口径的交叉核验。

#: 信号 → level（**重建**：逐条取自 record 的 signal_results）。
_SIGNAL_LEVELS_300760: dict[str, str] = {
    "trend_break_risk": "missing_fact",
    "cashflow_quality_risk": "low",
    "cross_validation_break": "none",
    "anomaly_pattern_tax_profit_mismatch": "none",
    "expansion_risk": "medium",
    "anomaly_cluster_risk": "none",
    "debt_risk": "none",
    "anomaly_pattern_depreciation_manipulation": "none",
    "moat_erosion_risk": "low",
    "anomaly_pattern_inflated_revenue": "none",
    "anomaly_pattern_efficiency_gain": "none",
    "anomaly_pattern_high_cash_high_debt": "none",
    "anomaly_pattern_expense_capitalization": "none",
    "capital_efficiency_risk": "missing_fact",
    "revenue_quality_risk": "none",
    "growth_quality_risk": "none",
    "valuation_risk": "none",
    "anomaly_pattern_profit_without_cash": "none",
    "anomaly_pattern_cost_pressure": "none",
    "anomaly_pattern_inventory_shenanigans": "none",
    "crowding_risk": "missing_fact",
    "profitability_quality_risk": "medium",
}

#: 信号 → 维度影响（**重建**：按设计文档附录 A.5 的 report evidence 清单）。
#: ``anomaly_cluster_risk`` 对 ``financial_quality`` 取 ``positive``：该映射是唯一能让重建后的
#: ``financial_quality`` 分数（``+0.017``）与 record 逐位吻合的取法，A.5 未标注该格影响。
_SIGNAL_IMPACTS_300760: dict[str, dict[str, str]] = {
    "cashflow_quality_risk": {"capital_efficiency": "neutral"},
    "cross_validation_break": {"operational_stability": "positive"},
    "expansion_risk": {
        "capital_efficiency": "neutral",
        "credit_risk": "slightly_negative",
        "financial_quality": "slightly_negative",
    },
    "anomaly_cluster_risk": {"financial_quality": "positive", "operational_stability": "positive"},
    "debt_risk": {
        "capital_efficiency": "neutral",
        "credit_risk": "positive",
        "financial_quality": "positive",
        "operational_stability": "positive",
    },
    "moat_erosion_risk": {"competitive_moat": "neutral"},
    "revenue_quality_risk": {"competitive_moat": "positive"},
    "valuation_risk": {"valuation_fit": "positive"},
    "anomaly_pattern_high_cash_high_debt": {"credit_risk": "neutral"},
    "profitability_quality_risk": {"capital_efficiency": "neutral"},
    # earnings_quality：5 条「未命中异常模式」（A.5）
    "anomaly_pattern_tax_profit_mismatch": {"earnings_quality": "neutral"},
    "anomaly_pattern_depreciation_manipulation": {"earnings_quality": "neutral"},
    "anomaly_pattern_inflated_revenue": {"earnings_quality": "neutral"},
    "anomaly_pattern_expense_capitalization": {"earnings_quality": "neutral"},
    "anomaly_pattern_profit_without_cash": {"earnings_quality": "neutral"},
    # financial_quality：3 条 neutral 异常模式（A.5）
    "anomaly_pattern_efficiency_gain": {"financial_quality": "neutral"},
    "anomaly_pattern_cost_pressure": {"financial_quality": "neutral"},
    "anomaly_pattern_inventory_shenanigans": {"financial_quality": "neutral"},
}

#: 6 条 verified_conflict（severity 取自 record 的 issues；related_dimensions 为**推断**）。
_CONFLICTS_300760: list[tuple[str, str, list[str]]] = [
    ("c1", "high", ["earnings_quality", "growth_quality"]),  # 分产品收入口径 / 两套同比基数
    ("c2", "high", ["competitive_moat", "growth_quality"]),  # 境内 IVD 装机×集采净效应
    ("c3", "critical", ["earnings_quality", "financial_quality"]),  # 汇兑损益口径
    ("c4", "high", ["growth_quality", "earnings_quality"]),  # 新兴业务内生 vs 并表
    ("c5", "high", ["competitive_moat", "capital_efficiency"]),  # 赛道标签框架适配性
    ("c6", "high", ["earnings_quality", "financial_quality", "credit_risk"]),  # 增收不增利
]

#: 冲突 explanation / 核验缺口（**重建**：取自 record 的 verified_conflict issue 摘要与「缺口」段）。
_CONFLICT_TEXTS_300760: dict[str, tuple[str, list[str]]] = {
    "c1": (
        "收入分解列报口径变更导致两套同比基数，属数据源口径问题而非经营变化",
        ["缺口：口径判断依赖金额勾稽而非明文措辞。"],
    ),
    "c2": ("境内 IVD 装机与单产对集采降价的净效应仅部分支持，转正仅 1 期", ["缺口：缺少连续两期季度序列。"]),
    "c3": ("报表净利下滑主要由汇兑摆动解释，剔除汇兑后经营层面为正增长", ["缺口：缺少多期汇率中性口径序列。"]),
    "c4": ("新兴业务增速部分来自惠泰并表，并表机制不成立，仅部分支持", ["缺口：合并口径差异与内部交易抵销未披露。"]),
    "c5": (
        "平台型多产线装机生态框架方向成立，但续费相关性缺少可量化披露",
        ["缺口：缺少装机存量与续费收入的相关性指标。"],
    ),
    "c6": ("净利下滑主因汇兑损失而非经营性恶化，但『一次性费用』不成立", ["缺口：经营性汇兑与套保损益拆分未披露。"]),
}

#: 降级 insight（record flags 含 insight_degraded）：复现 ``operational_stability`` 0.136×0.85 → 0.12。
_DEGRADED_INSIGHT_300760: dict[str, Any] = {
    "confidence": "low",
    "degraded": True,
    "fallback_tier": 1,
    "central_tension": "增长与盈利质量的张力。",
    "counter_evidence": [],
}


def _reconstruct_300760_signals() -> dict[str, dict[str, Any]]:
    """按「重建」口径拼 22 条信号（level 来自 record，thesis_impact 来自附录 A.5）。"""
    return {
        signal_id: {
            "level": level,
            "interpretation": f"{signal_id}（重建口径）。",
            "thesis_impact": dict(_SIGNAL_IMPACTS_300760.get(signal_id, {})),
        }
        for signal_id, level in _SIGNAL_LEVELS_300760.items()
    }


def _reconstruct_300760_conflicts() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """按「推断」口径拼 6 条 verified_conflict + 对应核验结果（含缺口）。"""
    conflicts: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []
    for cid, severity, dim_ids in _CONFLICTS_300760:
        explanation, gaps = _CONFLICT_TEXTS_300760[cid]
        hid = f"{cid}_h1"
        conflicts.append(
            _conflict(
                cid,
                dim_ids,
                severity=severity,
                hypotheses=[{"id": hid, "explanation": explanation, "status": "pending"}],
            )
        )
        verifications.append(_verified(hid, "verified", gaps=gaps, summary=explanation))
    return {"conflicts": conflicts}, verifications


def test_300760_reconstructed_signal_layer_matches_record_scores():
    """重建口径交叉核验：无冲突时 6/8 维度分数与 record 逐位吻合（其余 2 维由冲突驱动）。"""
    thesis = ThesisEngine().run(
        symbol="300760.SZ",
        period="2026-10-16",
        signal_results=_reconstruct_300760_signals(),
        insight=_DEGRADED_INSIGHT_300760,
    )

    # 期望值 = record 的 thesis_dimensions.score（无冲突维度）。
    assert thesis.dimensions["capital_efficiency"].score == pytest.approx(0.0)
    assert thesis.dimensions["credit_risk"].score == pytest.approx(-0.033, abs=1e-3)
    assert thesis.dimensions["earnings_quality"].score == pytest.approx(0.0)
    assert thesis.dimensions["financial_quality"].score == pytest.approx(0.017, abs=1e-3)
    assert thesis.dimensions["operational_stability"].score == pytest.approx(0.2)
    assert thesis.dimensions["valuation_fit"].score == pytest.approx(0.2)


def test_300760_semi_reconstructed_replay_invariants():
    """§6.3 不变量断言（**重建** + **推断** 口径，见本段上方披露）。

    修复前：7 个维度 confidence = ``0.0``（绝对减法击穿）；``overall_score`` = ``0.2`` = 单维得分
    （``operational_stability``）。修复后要求：
    ① 有贡献维度 ``confidence > 0``；② ``operational_stability`` 仍为 ``0.12``（无冲突命中 ⇒ 未被改动）；
    ③ 有效维度数 ≥ 2；④ ``overall_score`` 不再等于单维得分；⑤ ``insufficient`` 维度 ≤ 1。
    """
    signal_results = _reconstruct_300760_signals()
    conflict_analysis, verifications = _reconstruct_300760_conflicts()

    thesis = ThesisEngine().run(
        symbol="300760.SZ",
        period="2026-10-16",
        signal_results=signal_results,
        conflict_analysis=conflict_analysis,
        verification_results=verifications,
        insight=_DEGRADED_INSIGHT_300760,
    )

    # ① 有贡献维度（信号层至少 1 条贡献）confidence > 0；无信号贡献的 growth_quality 保持 0。
    covered_dims = {dim for impacts in _SIGNAL_IMPACTS_300760.values() for dim in impacts}
    assert "growth_quality" not in covered_dims
    for dim_id in covered_dims:
        assert thesis.dimensions[dim_id].confidence > 0.0, dim_id
    assert thesis.dimensions["growth_quality"].confidence == 0.0

    # ② operational_stability 未被冲突命中 ⇒ 只受 insight 降级阻尼：3/22 × 0.85 = 0.116 → 0.12。
    assert thesis.dimensions["operational_stability"].confidence == pytest.approx(0.116, abs=1e-3)
    assert thesis.to_dict()["dimensions"]["operational_stability"]["confidence"] == pytest.approx(0.12)

    # ③ 有效维度数 ≥ 2（修复前只有 1 个）。
    effective = [dim for dim in thesis.dimensions.values() if dim.confidence > 0]
    assert len(effective) >= 2
    assert len(effective) == len(covered_dims)

    # ④ overall_score 不再等于单维得分（修复前 = operational_stability 的 0.2）。
    expected_overall = sum(dim.score for dim in effective) / len(effective)
    assert thesis.overall_score == pytest.approx(expected_overall)
    assert thesis.overall_score != pytest.approx(thesis.dimensions["operational_stability"].score)

    # ⑤ 审查侧：insufficient 维度 ≤ 1（修复前 7 个）。
    review = ThesisReviewer().review(thesis, signal_results, use_llm=False)
    insufficient = [v.dimension_id for v in review.dimension_verdicts.values() if v.status == "insufficient"]
    assert len(insufficient) <= 1, insufficient


# ── §3.4 可观测性（P1）：thesis_confidence_damped 确定性日志 ────────────────


def _capture_damping_events(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], None],
) -> list[dict[str, Any]]:
    """在「structlog → stdlib + JSON 渲染」下执行 ``action``，用 ``caplog`` 收折减事件。

    两处工程理由（整目录运行时实测踩到，单文件运行不会暴露）：

    * 仓库任一模块调用 ``alphabee.utils.get_logger()`` 会把 structlog 全局接到 stdlib
      （``wrap_for_formatter`` + ``cache_logger_on_first_use=True``）；而模块级 ``engine.logger``
      **一旦首次使用就被缓存**，此后 ``structlog.configure()`` 不再影响它。故这里把模块属性换成
      一个**未绑定的新代理**（同名 logger），让它在本次配置下绑定 —— 被测代码行（折减循环里的
      ``logger.info``）不变，用完由 ``monkeypatch`` 与保存的全局配置还原。
    * 收上来的 ``record.msg`` 可能是 dict（包装式）或 JSON 字符串（渲染式），统一归一化后比较。
    """
    saved_config = structlog.get_config()
    saved_logger = engine_module.logger
    structlog.reset_defaults()
    structlog.configure(
        processors=[structlog.stdlib.add_log_level, structlog.processors.JSONRenderer()],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    monkeypatch.setattr(engine_module, "logger", structlog.get_logger("alphabee.agents.thesis.engine"))
    try:
        with caplog.at_level(logging.INFO, logger="alphabee.agents.thesis.engine"):
            action()
    finally:
        structlog.configure(**saved_config)
        engine_module.logger = saved_logger

    events: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != "alphabee.agents.thesis.engine":
            continue
        payload: Any = record.msg
        if isinstance(payload, str) and payload.lstrip().startswith("{"):
            payload = json.loads(payload)
        if isinstance(payload, dict) and payload.get("event") == "thesis_confidence_damped":
            events.append(payload)
    return events


def test_damping_emits_audit_log_with_hit_count(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """§3.4：折减循环后产出 ``thesis_confidence_damped``（dim_id/hits/damped 三字段齐全）。

    一档封顶语义不变：5 条假设命中同一维度 ⇒ ``hits=5`` 但只折减一次（``damped=0.425``）。
    """
    dims = {_TARGET_DIM: _dim(_TARGET_DIM, 0.5, score=0.5)}
    conflicts, hids = _hits(_TARGET_DIM, 5)

    def _action() -> None:
        _apply_conflict(
            dimensions=dims,
            conflicts=conflicts,
            verification_results=[_verified(hid, "rejected") for hid in hids],
        )

    events = _capture_damping_events(caplog, monkeypatch, _action)

    assert [event["event"] for event in events] == ["thesis_confidence_damped"]
    assert events[0]["dim_id"] == _TARGET_DIM
    assert events[0]["hits"] == 5
    assert events[0]["damped"] == pytest.approx(0.425)
    assert events[0]["level"] == "info"  # structlog.stdlib.add_log_level
    assert dims[_TARGET_DIM].confidence == pytest.approx(0.425)  # 一档封顶语义不变


def test_damping_log_counts_per_dim_and_skips_zero_coverage(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """hits 按维度计数（每维度一行）；零覆盖维度不折减 ⇒ 不产出折减日志。"""
    dims = {
        "earnings_quality": _dim("earnings_quality", 0.5, score=0.5),
        "growth_quality": _dim("growth_quality", 0.0, score=0.0),
    }
    conflicts = [
        _conflict(
            "c1",
            ["earnings_quality"],
            hypotheses=[
                {"id": "h1", "explanation": "确认操纵收入", "status": "pending"},
                {"id": "h2", "explanation": "确认操纵收入", "status": "pending"},
            ],
        ),
        _conflict(
            "c2",
            ["growth_quality"],
            severity="critical",
            hypotheses=[{"id": "h3", "explanation": "确认操纵收入", "status": "pending"}],
        ),
    ]
    verifications = [
        _verified("h1", "rejected"),
        _verified("h2", "unknown"),
        _verified("h3", "rejected"),
    ]

    def _action() -> None:
        _apply_conflict(dimensions=dims, conflicts=conflicts, verification_results=verifications)

    events = _capture_damping_events(caplog, monkeypatch, _action)

    assert [event["dim_id"] for event in events] == ["earnings_quality"]
    assert events[0]["hits"] == 2  # rejected + unknown 各一次命中
    assert events[0]["damped"] == pytest.approx(0.425)  # 一档封顶：两次命中仍只折减一次
    # 零覆盖维度（覆盖度 0）被命中也不折减、不抬升 ⇒ 不产出折减日志。
    assert dims["growth_quality"].confidence == 0.0
