"""F1：rejected 假设未精确溯源时，维度分数 ×0.5 折减（一档封顶）的回归钉。

设计依据：``docs/design/THESIS_CONTEXT_MISMATCH_FIX_PLAN.md`` §3.1 / §5.2 / §6.2。

F1 只动 **score 通道**：rejected 假设未溯源（无 ``disputed_*`` 且兜底未命中）→ 相关维度
分数在循环尾部统一 ×``_REJECTED_SCORE_DAMP``（0.5）。语义：一档封顶（集合登记）、
方向不变（×正数）、``score==0`` 跳过、``context_notes`` 留痕 + ``thesis_score_damped``
日志。置信度通道（``damp_flags``）、verified/unknown 路径、traced 精确扣除路径均零改动。

末尾含 §6.2 的 603986 半重建重放（重建/推断口径见该段 docstring）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import structlog

from alphabee.agents.thesis import engine as engine_module
from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.models import ThesisDimension

# ── 直接驱动 _apply_conflict_analysis 的构造工具（同 test_confidence_damping.py）───


def _dim(dim_id: str, confidence: float, *, score: float = 0.0) -> ThesisDimension:
    return ThesisDimension(id=dim_id, name=dim_id, judgment="neutral", score=score, confidence=confidence)


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
        else [{"id": f"{cid}_h1", "explanation": "公司账务处理符合会计准则。", "status": "pending"}],
    }


def _verified(hid: str, status: str, *, summary: str = "核验结论。") -> dict[str, Any]:
    return {"hypothesis_id": hid, "status": status, "summary": summary, "gaps": []}


def _apply_conflict(
    *,
    dimensions: dict[str, ThesisDimension],
    conflicts: list[dict[str, Any]],
    verification_results: list[dict[str, Any]],
    dim_effective: dict[str, list[tuple[str, str, float]]] | None = None,
) -> None:
    ThesisEngine()._apply_conflict_analysis(
        dimensions=dimensions,
        dim_effective=dim_effective if dim_effective is not None else {dim_id: [] for dim_id in dimensions},
        conflict_analysis={"conflicts": conflicts},
        verification_results=verification_results,
        signal_results={},
        anomaly_report=None,
    )


# ── §5.2 用例 1：未溯源 rejected → score ×0.5 + 留痕 ─────────────────────────


def test_untraced_rejected_damps_score_by_half():
    dim = _dim("credit_risk", 0.5, score=-1.0)
    _apply_conflict(
        dimensions={"credit_risk": dim},
        conflicts=[_conflict("c1", ["credit_risk"])],
        verification_results=[_verified("c1_h1", "rejected", summary="核验结论：不构成资金占用。")],
    )

    assert dim.score == pytest.approx(-0.5)
    assert dim.score < 0  # 方向不变
    assert any("折减" in note for note in dim.context_notes)
    assert "核验结论：不构成资金占用。" in dim.counter_evidence  # 反证照常入账


# ── §5.2 用例 2：多假设命中一档封顶（只折减一次）────────────────────────────


def test_multiple_untraced_hits_damp_once():
    dim = _dim("credit_risk", 0.5, score=-0.8)
    conflicts = [
        _conflict("c1", ["credit_risk"], hypotheses=[{"id": "h1", "explanation": "无模式名。"}]),
        _conflict("c2", ["credit_risk"], hypotheses=[{"id": "h2", "explanation": "无模式名。"}]),
    ]
    _apply_conflict(
        dimensions={"credit_risk": dim},
        conflicts=conflicts,
        verification_results=[_verified("h1", "rejected"), _verified("h2", "rejected")],
    )

    assert dim.score == pytest.approx(-0.4)  # -0.8 × 0.5 一次，不是 ×0.25
    assert dim.score != pytest.approx(-0.2)


# ── §5.2 用例 3：正向维度对称折减（0.9 → 0.45）──────────────────────────────


def test_positive_dimension_symmetric_damping():
    dim = _dim("earnings_quality", 0.5, score=0.9)
    _apply_conflict(
        dimensions={"earnings_quality": dim},
        conflicts=[_conflict("c1", ["earnings_quality"])],
        verification_results=[_verified("c1_h1", "rejected")],
    )

    assert dim.score == pytest.approx(0.45)
    assert dim.score > 0  # 方向不变


# ── §5.2 用例 4：score==0 跳过（中性维度折减是无操作）───────────────────────


def test_zero_score_skipped():
    dim = _dim("credit_risk", 0.5, score=0.0)
    _apply_conflict(
        dimensions={"credit_risk": dim},
        conflicts=[_conflict("c1", ["credit_risk"])],
        verification_results=[_verified("c1_h1", "rejected")],
    )

    assert dim.score == 0.0
    assert dim.context_notes == []  # 无折减留痕
    assert dim.confidence == pytest.approx(0.425)  # 置信度通道照旧独立折减


# ── §5.2 用例 5：traced rejected 精确扣除 → 无折减 ───────────────────────────


def test_traced_rejected_no_score_damping():
    dim = _dim("credit_risk", 0.5, score=0.4)
    dim_effective = {"credit_risk": [("sig_disputed", "signal", 0.9), ("sig_keep", "signal", -0.1)]}
    hypothesis = {
        "id": "h1",
        "explanation": "确认操纵收入",
        "status": "pending",
        "disputed_signal_ids": ["sig_disputed"],
    }
    _apply_conflict(
        dimensions={"credit_risk": dim},
        conflicts=[_conflict("c1", ["credit_risk"], hypotheses=[hypothesis])],
        verification_results=[_verified("h1", "rejected", summary="核验结论：不构成资金占用。")],
        dim_effective=dim_effective,
    )

    assert dim.score == pytest.approx(-0.1)  # 精确扣除后 -0.1，未被再 ×0.5
    assert dim.context_notes == []  # 无折减留痕
    assert dim.confidence == pytest.approx(0.425)  # 置信度通道照旧


# ── §5.2 用例 6：thesis_score_damped 日志三字段齐全 ──────────────────────────


def _capture_score_events(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    action: Any,
) -> list[dict[str, Any]]:
    """同 test_confidence_damping 的日志收口：只收 ``thesis_score_damped`` 事件。"""
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
        if isinstance(payload, dict) and payload.get("event") == "thesis_score_damped":
            events.append(payload)
    return events


def test_score_damping_emits_audit_log(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch):
    dim = _dim("credit_risk", 0.5, score=-1.0)

    def _action() -> None:
        _apply_conflict(
            dimensions={"credit_risk": dim},
            conflicts=[_conflict("c1", ["credit_risk"])],
            verification_results=[_verified("c1_h1", "rejected")],
        )

    events = _capture_score_events(caplog, monkeypatch, _action)

    assert len(events) == 1
    assert events[0]["event"] == "thesis_score_damped"
    assert events[0]["dim_id"] == "credit_risk"
    assert events[0]["damped_score"] == pytest.approx(-0.5)
    assert events[0]["level"] == "info"  # structlog.stdlib.add_log_level


# ── §6.2 603986 半重建重放（口径披露）────────────────────────────────────────
#
# 口径披露（与 test_confidence_damping 的 300760 半重建同纪律）：
# * **重建**：22 条信号的 ``level`` 逐条来自 record ``data/task_records/603986.SH/2026-10-17/
#   task-6e9476f66ac8.json`` 的 ``signal_results``；``thesis_impact`` 由信号 YAML 规则
#   （``alphabee/agents/signal/rules/*.yaml`` 的 ``thesis_impact`` 表）按 level 逐条重建
#   （record 未落该字段，同 recorder 只存 id/level/interpretation 的契约）；二阶异常
#   ``depreciation_manipulation`` 的 ``severity=medium`` 取自 ``patterns.yaml``。
# * **推断**：各冲突的 ``related_dimensions`` 按 record ``report_raw.conflict_analysis`` 的
#   冲突→维度叙述与 ``dimension_analysis`` 的归因**推断**（record 未落该字段）；hypothesis
#   的 ``explanation`` 取自 record 的 ``verified_conflict`` issue 原文与 ``assumption_invalidated``
#   的已证伪假设清单，``status`` 取自 ``conflict_analysis`` 的假设状态小结。
# * 因此断言只承诺**不变量**（三个 blocking 维度不再 strong_negative、分数 ×0.5 且方向不变、
#   折减留痕可审计），不承诺其余维度与 record 逐位一致。

_SIGNAL_LEVELS_603986: dict[str, str] = {
    "trend_break_risk": "missing_fact",
    "cashflow_quality_risk": "low",
    "cross_validation_break": "high",
    "anomaly_pattern_tax_profit_mismatch": "none",
    "expansion_risk": "none",
    "anomaly_cluster_risk": "medium",
    "debt_risk": "none",
    "anomaly_pattern_depreciation_manipulation": "medium",
    "moat_erosion_risk": "none",
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
    "profitability_quality_risk": "none",
}

#: 信号 → 维度影响（**重建**：signal YAML 的 thesis_impact 表按 record level 取值）。
_SIGNAL_IMPACTS_603986: dict[str, dict[str, str]] = {
    "cashflow_quality_risk": {
        "capital_efficiency": "neutral",
        "financial_quality": "neutral",
        "earnings_quality": "slightly_negative",
    },
    "cross_validation_break": {
        "operational_stability": "negative",
        "financial_quality": "negative",
        "earnings_quality": "negative",
    },
    "anomaly_pattern_tax_profit_mismatch": {"earnings_quality": "neutral"},
    "expansion_risk": {
        "growth_quality": "positive",
        "capital_efficiency": "neutral",
        "financial_quality": "positive",
        "credit_risk": "positive",
    },
    "anomaly_cluster_risk": {"operational_stability": "slightly_negative", "financial_quality": "slightly_negative"},
    "debt_risk": {
        "operational_stability": "positive",
        "capital_efficiency": "neutral",
        "financial_quality": "positive",
        "credit_risk": "positive",
    },
    "anomaly_pattern_depreciation_manipulation": {"earnings_quality": "slightly_negative"},
    "moat_erosion_risk": {"competitive_moat": "positive", "earnings_quality": "neutral"},
    "anomaly_pattern_inflated_revenue": {"earnings_quality": "neutral"},
    "anomaly_pattern_efficiency_gain": {"financial_quality": "neutral"},
    "anomaly_pattern_high_cash_high_debt": {"credit_risk": "neutral"},
    "anomaly_pattern_expense_capitalization": {"earnings_quality": "neutral"},
    "revenue_quality_risk": {
        "competitive_moat": "positive",
        "growth_quality": "neutral",
        "financial_quality": "positive",
        "earnings_quality": "positive",
    },
    "growth_quality_risk": {
        "growth_quality": "positive",
        "financial_quality": "positive",
        "earnings_quality": "positive",
    },
    "valuation_risk": {"valuation_fit": "positive"},
    "anomaly_pattern_profit_without_cash": {"earnings_quality": "neutral"},
    "anomaly_pattern_cost_pressure": {"financial_quality": "neutral"},
    "anomaly_pattern_inventory_shenanigans": {"financial_quality": "neutral"},
    "profitability_quality_risk": {
        "competitive_moat": "positive",
        "capital_efficiency": "neutral",
        "financial_quality": "positive",
        "earnings_quality": "positive",
    },
}

#: 冲突（cid, theme, severity, related_dimensions[推断], [(hid, explanation, status, summary), ...]）。
#: explanation 取自 record 的 verified_conflict issue 原文 / assumption_invalidated 已证伪假设清单。
_CONFLICTS_603986: list[tuple[str, str, str, list[str], list[tuple[str, str, str, str]]]] = [
    (
        "c1",
        "利基DRAM/SLC NAND供给缺口可持续性 vs 海外大厂回流风险",
        "critical",
        ["growth_quality", "competitive_moat", "earnings_quality"],
        [
            (
                "c1_h1",
                "供给缺口可持续：海外大厂聚焦HBM/高端DRAM，结构性退出利基DRAM与2D NAND，短期无回流计划，利基存储量价齐升延续，毛利率与增速可持续。",
                "partial",
                "供给缺口延续但长鑫DDR4扩产构成中期反证。",
            ),
            (
                "c1_h2",
                "供给缺口逆转：大厂回流利基市场或AI需求放缓，量价齐升逻辑见顶，毛利率与收入增速将回落，当前高增长不可持续。",
                "rejected",
                "供给缺口逆转被证伪。",
            ),
        ],
    ),
    (
        "c2",
        "存储芯片整体高增但分产品量价贡献不可拆解",
        "high",
        ["earnings_quality", "growth_quality"],
        [
            (
                "c2_h1",
                "利润弹性主要来自利基DRAM量价齐升（核心驱动与框架假设一致），分产品数据可验证该贡献。",
                "partial",
                "量价齐升确认但分产品明细缺失。",
            ),
            ("c2_h2", "利润来源不可拆解，无法确认量价贡献。", "partial", "来源不明。"),
        ],
    ),
    (
        "c3",
        "非经常性损益掩盖主业盈利质量",
        "high",
        ["earnings_quality"],
        [
            (
                "c3_h1",
                "非经常性收益为证券投资公允价值浮盈，标的高度集中且存在回撤风险，主业扣非盈利质量被高利润增速掩盖。",
                "verified",
                "非经常性收益确为证券投资浮盈，主业盈利质量被高增速掩盖的判断成立。",
            ),
            (
                "c3_h2",
                "非经常性收益虽大但主业扣非盈利同样强劲且可持续，非经常性损益只是周期红利外的附加收益。",
                "rejected",
                "主业同样强劲被证伪。",
            ),
        ],
    ),
    (
        "c4",
        "Fabless模式下晶圆/封测产能能否支撑量增上限",
        "high",
        ["growth_quality"],
        [
            (
                "c4_h1",
                "产能充足：公司已锁定长期晶圆/封测产能协议，原材料与在产品备货充足，量增上限不构成约束。",
                "partial",
                "备货充足但长期协议缺量化。",
            ),
            (
                "c4_h2",
                "产能瓶颈：代工产能分配不足或存货快速消化反映备货不足，量增受限，收入兑现度低于预期。",
                "rejected",
                "产能瓶颈被证伪。",
            ),
        ],
    ),
    (
        "c5",
        "单一generic_fundamental框架是否足以刻画存储芯片周期属性",
        "high",
        ["valuation_fit", "competitive_moat"],
        [
            (
                "c5_h1",
                "周期框架(cyclical)：当前盈利为供给缺口的周期顶部，PE折价是市场对盈利均值回归的合理定价，『低估值』不构成安全边际。",
                "partial",
                "周期框架部分成立。",
            ),
            ("c5_h2", "成长框架：供给缺口延续，当前低估值是成长安全边际。", "partial", "成长框架待验证。"),
        ],
    ),
    (
        "c6",
        "三表勾稽关系断裂：财务数据自洽性存疑 vs 多数派生指标健康",
        "critical",
        ["earnings_quality", "operational_stability", "financial_quality"],
        [
            (
                "c6_h1",
                "正常经营变化：周期复苏导致收入/存货/应收等科目剧烈波动，勾稽关系偏离是业务结构剧变而非操纵，数据自洽性可解释。",
                "verified",
                "勾稽偏离可由周期复苏解释，未发现操纵迹象。",
            ),
            ("c6_h2", "会计操纵，财务数据自洽性存疑。", "rejected", "会计操纵被证伪。"),
        ],
    ),
    (
        "c7",
        "折旧摊销/固定资产比率显著下降：正常资产结构变化 vs 调节利润嫌疑",
        "high",
        ["earnings_quality"],
        [
            (
                "c7_h1",
                "正常结构变化：新产能（在建工程转固）导致固定资产基数快速扩大但折旧尚未同步，或折旧政策/年限正常变更，非刻意调节利润。",
                "partial",
                "正常结构变化获支持。",
            ),
            ("c7_h2", "通过延长折旧年限或变更折旧方法调节利润。", "rejected", "调节利润被证伪。"),
        ],
    ),
    (
        "c8",
        "估值与基本面背离：低估值 vs 高ROE/高成长",
        "high",
        ["valuation_fit"],
        [
            (
                "c8_h1",
                "合理定价：市场将当前盈利视为供给缺口的周期顶部，低估值是对盈利均值回归与可持续性不足的定价，非低估。",
                "partial",
                "合理定价获支持。",
            ),
            ("c8_h2", "市场低估，当前低估值构成安全边际。", "rejected", "市场低估被证伪。"),
        ],
    ),
    (
        "c9",
        "市场份额增长 vs 对标组增速落后：相对申万行业与相对同行的结论背离",
        "high",
        ["competitive_moat"],
        [
            (
                "c9_h1",
                "产品结构差异：对标组收入增速高或含并购/低基数/不同子产品结构，公司主业内生份额（相对申万行业）仍在提升，非竞争优势弱化。",
                "partial",
                "产品结构差异可解释。",
            ),
            ("c9_h2", "竞争优势弱化。", "rejected", "竞争优势弱化被证伪。"),
        ],
    ),
]


def _reconstruct_603986() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    signal_results = {
        sid: {
            "level": lvl,
            "interpretation": f"{sid}（重建口径）。",
            "thesis_impact": dict(_SIGNAL_IMPACTS_603986.get(sid, {})),
        }
        for sid, lvl in _SIGNAL_LEVELS_603986.items()
    }
    anomaly_report = {
        "pattern_matches": [
            {
                "pattern_id": "depreciation_manipulation",
                "pattern_name": "折旧调节利润嫌疑",
                "severity": "medium",
                "risk_dimension": "earnings_quality",
                "explanation": "折旧摊销/固定资产比率显著下降：可能通过延长折旧年限或变更折旧方法来减少当期费用、调节利润。",
            }
        ]
    }
    conflicts: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []
    for cid, theme, severity, dim_ids, hypotheses in _CONFLICTS_603986:
        conflicts.append(
            {
                "id": cid,
                "theme": theme,
                "description": "描述",
                "related_dimensions": list(dim_ids),
                "severity": severity,
                "confidence": 0.9,
                "hypotheses": [
                    {"id": hid, "explanation": explanation, "status": "pending"}
                    for hid, explanation, _s, _sum in hypotheses
                ],
            }
        )
        verifications.extend(
            {"hypothesis_id": hid, "status": status, "summary": summary, "gaps": []}
            for hid, _explanation, status, summary in hypotheses
        )
    return signal_results, anomaly_report, {"conflicts": conflicts}, verifications


def test_603986_replay_three_blocking_dims_damped():
    """§6.2 不变量断言：三个 blocking 维度的 strong_negative 闭合为 negative、分数 ×0.5、留痕可审计。

    修复前（record）：竞争壁垒 -1.0/negative（score 与标签矛盾）、盈利质量 -1.0/strong_negative、
    成长质量 -0.85/strong_negative。F1 后：未溯源 rejected 的维度分数 ×0.5 ⇒ 三个维度均不再
    strong_negative，且 ``context_notes`` 含折减留痕（``thesis_score_damped`` 日志由用例 6 钉住）。
    """
    signal_results, anomaly_report, conflict_analysis, verification_results = _reconstruct_603986()
    thesis = ThesisEngine().run(
        symbol="603986.SH",
        period="20260630",
        signal_results=signal_results,
        anomaly_report=anomaly_report,
        conflict_analysis=conflict_analysis,
        verification_results=verification_results,
    )

    moat = thesis.dimensions["competitive_moat"]
    earnings = thesis.dimensions["earnings_quality"]
    growth = thesis.dimensions["growth_quality"]

    # 竞争壁垒：-1.0 → -0.5，judgment 保持 negative，score 与标签矛盾消除（§3.1.4）。
    assert moat.score == pytest.approx(-0.5)
    assert moat.judgment == "negative"
    # 盈利质量 / 成长质量：strong_negative 闭合为 negative（方向不变、至少降一档）。
    for dim in (earnings, growth):
        assert dim.judgment == "negative"
        assert dim.score < 0 and dim.score >= -0.5
    # 折减留痕可审计：三条维度均有 context_notes 折减记录。
    for dim in (moat, earnings, growth):
        assert any("折减" in note for note in dim.context_notes)
