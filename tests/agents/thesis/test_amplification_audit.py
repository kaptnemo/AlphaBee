"""F3 放大标注 + 加权边审计测试（§8.1 / §8.2 / §14.4-B）。

覆盖要点：

* **三方一致**：``INSIGHT_CONFIDENCE_WEIGHTS`` ↔ 契约文案（``AMPLIFICATION_AUDIT``）↔ 设计文档
  §14.4-A（防"文档写 0.92、代码是 0.95"的漂移）；
* **四条 WEIGHTED 边各有分支**（§8.1 表逐行），且与契约 ``AMPLIFICATION_AUDIT`` 的边集合一致；
* 方向**一致 / 不一致**双例（L1 确定性判读）；
* **真实发射点**：``review_thesis`` 节点把审计发射为 ``Decision(maker="amplification_audit")``
  （带 ``based_on`` + ``evidence_refs``，能通过 ``evidence_refs_present``），方向不一致时额外出
  D3 偏离（``category="amplification_direction_conflict"``，``amplified_by=[边 id]``）；
* 开关 ``audit_enabled=False`` → **零新增 Decision/Issue**（与 F3 之前逐字段一致）；配置缺失/异常
  → fail-open（默认开启）；
* ``use_llm=False`` **零 LLM 调用**（v1 的 L2 是显式非目标，见 ``audit_amplification`` docstring）；
* ``ThesisReview.amplification_audit`` append-only（旧 payload 仍可构造/序列化）；
* ``amplified_by`` → §10.1 代价敞口放大因子（1+n）联动；
* 契约层 ``validate_contracts() == []``。
"""

from __future__ import annotations

import re

import pytest

from alphabee.agents.thesis import reviewer as reviewer_module
from alphabee.agents.thesis.engine import INSIGHT_CONFIDENCE_WEIGHTS
from alphabee.agents.thesis.models import InvestmentThesis, ThesisDimension, ThesisReview
from alphabee.agents.thesis.reviewer import (
    AMPLIFICATION_EDGE_AUDITORS,
    AmplificationContext,
    attach_amplification_audit,
    audit_amplification,
)
from alphabee.core.schemas import ArtifactType, IssueSeverity
from alphabee.orchestrator.node_contracts import AMPLIFICATION_AUDIT, NODE_CONTRACTS, AmplificationMode

# ── 构造器 ──────────────────────────────────────────────────────────────────


def _thesis(
    *,
    overall_judgment: str = "positive",
    dimensions: dict | None = None,
    dim_judgment: str = "positive",
    dim_score: float = 0.5,
    evidence: list | None = None,
) -> InvestmentThesis:
    dims = dimensions
    if dims is None:
        dims = {
            "earnings_quality": ThesisDimension(
                id="earnings_quality",
                name="盈利质量",
                judgment=dim_judgment,
                score=dim_score,
                confidence=0.8,
                evidence=list(evidence or []),
            )
        }
    return InvestmentThesis(
        symbol="600519.SH",
        period="2024Q4",
        dimensions=dims,
        overall_judgment=overall_judgment,
        overall_score=dim_score,
    )


def _insight(
    *,
    core_view: str = "看好公司长期增长与盈利修复",
    supporting: int = 2,
    counter: int = 0,
    confidence: str = "medium",
    degraded: bool = False,
    fallback_tier: int = 0,
) -> dict:
    return {
        "core_view": core_view,
        "supporting_evidence": [{"statement": f"s{i}"} for i in range(supporting)],
        "counter_evidence": [{"statement": f"c{i}"} for i in range(counter)],
        "confidence": confidence,
        "degraded": degraded,
        "fallback_tier": fallback_tier,
    }


def _signals(impact: str = "positive", *, level: str = "high") -> dict:
    return {"results": {"revenue_quality_risk": {"level": level, "thesis_impact": {"earnings_quality": impact}}}}


def _conflicts(*, severity: str = "high", status: str = "verified", dimensions: list | None = None):
    """真实契约载荷（``ConflictAnalysisResult``）：审计必须能吃编排层的**正式**产物。"""
    from alphabee.agents.schemas import ConflictAnalysisResult, ConflictItem, HypothesisItem

    return ConflictAnalysisResult(
        conflicts=[
            ConflictItem(
                id="c1",
                theme="盈利增长但现金流恶化",
                description="利润增长没有被现金流验证。",
                related_dimensions=list(dimensions or ["earnings_quality"]),
                severity=severity,
                confidence=0.9,
                hypotheses=[
                    HypothesisItem(
                        id="h1",
                        conflict_id="c1",
                        explanation="收入确认前置，回款滞后",
                        predictions=["经营现金流/净利润持续低于1"],
                        required_evidence=["financial_facts"],
                        score=0.8,
                        status=status,
                    )
                ],
            )
        ]
    )


def _anomaly_evidence(*, traced: bool = True) -> list:
    return [
        {
            "signal_id": "anomaly_pattern:cashflow_divergence" if traced else "cashflow_divergence",
            "signal_name": "现金流背离",
            "level": "high",
            "impact": "negative",
            "source_type": "anomaly",
            "source_label": "anomaly_pattern",
        }
    ]


# ── ① 三方一致：常量 ↔ 契约文案 ↔ 文档 ──────────────────────────────────────

_PAIR_RE = re.compile(r"\b(high|medium|low)\b[\"']?\s*:\s*(\d+(?:\.\d+)?)")


def _weights_from_text(text: str) -> dict[str, float]:
    return {name: float(value) for name, value in _PAIR_RE.findall(text)}


def test_weight_constant_matches_contract_text():
    """契约文案（``AMPLIFICATION_AUDIT["insight->thesis"].weight``）与常量逐值一致。"""
    binding = AMPLIFICATION_AUDIT["insight->thesis"]
    assert _weights_from_text(binding.weight) == INSIGHT_CONFIDENCE_WEIGHTS
    # 文案必须点名常量，便于读者从文本直达代码（§8.2 规则 1 的"权重值必须显式"）
    assert "INSIGHT_CONFIDENCE_WEIGHTS" in binding.weight


def test_weight_constant_matches_design_doc():
    """设计文档 §14.4-A 的常量定义与代码逐值一致（防文档/代码漂移）。"""
    from alphabee import PROJECT_ROOT

    doc = (PROJECT_ROOT / "docs" / "design" / "DEVIATION_CONTROL_FRAMEWORK.md").read_text(encoding="utf-8")
    lines = [ln for ln in doc.splitlines() if "INSIGHT_CONFIDENCE_WEIGHTS" in ln and "{" in ln]
    assert lines, "设计文档 §14.4-A 未找到 INSIGHT_CONFIDENCE_WEIGHTS 的定义行"
    parsed = [_weights_from_text(ln) for ln in lines]
    assert any(candidate == INSIGHT_CONFIDENCE_WEIGHTS for candidate in parsed), (
        f"文档登记值与代码常量不一致：文档={parsed}，代码={INSIGHT_CONFIDENCE_WEIGHTS}"
    )


def test_medium_weight_is_the_collapsed_value():
    """F3 的有意收口：``medium`` 由历史内联值 0.95 → 0.92（三方已一致，此处显式钉住）。"""
    assert INSIGHT_CONFIDENCE_WEIGHTS == {"high": 1.0, "medium": 0.92, "low": 0.85}


def test_engine_uses_constant_in_both_branches():
    """引擎侧：系数与**默认分支**都必须走常量（不得再留一份内联魔数）。

    只对 **AST 里的代码**做断言（docstring 为保留变更史会提到旧值 0.95，不应被误判成代码回潮）。
    """
    import ast
    import inspect
    import textwrap

    from alphabee.agents.thesis.engine import ThesisEngine

    source = inspect.getsource(ThesisEngine._apply_insight)
    tree = ast.parse(textwrap.dedent(source))
    floats = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, float)}
    assert not (floats & {0.85, 0.92, 0.95}), f"engine 仍留内联系数魔数：{sorted(floats & {0.85, 0.92, 0.95})}"
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Dict)], "engine 仍留内联系数表"
    # 常量必须同时出现在"取值"与"默认分支"两处（默认档 = medium，不再另设魔数）
    assert source.count("INSIGHT_CONFIDENCE_WEIGHTS") >= 2


# ── ② 四条 WEIGHTED 边各有分支 ──────────────────────────────────────────────


def test_every_weighted_edge_has_a_branch_and_a_contract_binding():
    """§8.1 四条 WEIGHTED 边：审计分支 + 契约覆盖表一一对应（不漏边、不多边）。"""
    weighted_edges = {
        edge
        for contract in NODE_CONTRACTS.values()
        for edge, mode in contract.amplification_labels.items()
        if mode is AmplificationMode.WEIGHTED
    }
    assert set(AMPLIFICATION_EDGE_AUDITORS) == weighted_edges == set(AMPLIFICATION_AUDIT)
    assert len(AMPLIFICATION_EDGE_AUDITORS) == 4


@pytest.mark.parametrize("edge", sorted(AMPLIFICATION_EDGE_AUDITORS))
def test_audit_amplification_explicit_edge_reaches_its_branch(edge: str):
    """显式 ``edge=`` 可直达任一边分支（四条边各自的 return 值都带自己的 edge key）。"""
    audit = audit_amplification(
        _thesis(evidence=_anomaly_evidence()),
        _insight(),
        _signals(),
        _conflicts(),
        edge=edge,
    )
    assert audit is not None
    assert audit.edge == edge
    assert audit.upstream_artifact
    assert isinstance(audit.direction_consistent, bool)
    assert audit.rationale


def test_unknown_edge_is_a_programming_error():
    """显式给出 §8.1 之外的边 → KeyError（不静默回落，否则审计会"看起来跑过了"）。"""
    with pytest.raises(KeyError):
        audit_amplification(_thesis(), _insight(), _signals(), None, edge="insight->nothing")


def test_no_applicable_edge_returns_none():
    """无任何可用输入 → ``None``（不构造"看起来一致"的空审计）。"""
    assert audit_amplification(None, None, None, None) is None
    assert audit_amplification(_thesis(), None, None, None) is None


# ── ③ 方向一致 / 不一致双例 ─────────────────────────────────────────────────


def test_direction_consistent_case():
    """insight 正面 + 信号正面 → 一致，权重取 medium 档 0.92。"""
    audit = audit_amplification(_thesis(), _insight(), _signals("positive"), None)
    assert audit is not None
    assert audit.edge == "insight->thesis"
    assert audit.direction_consistent is True
    assert audit.weight == 0.92


def test_direction_inconsistent_case():
    """insight 正面 + 信号负面 → 不一致（§8.1 第 1 行"加权方向 vs 证据方向"比对）。"""
    audit = audit_amplification(_thesis(), _insight(), _signals("negative"), None)
    assert audit is not None
    assert audit.direction_consistent is False
    assert "不一致" in audit.rationale


def test_degraded_insight_caps_weight_with_damping_factor():
    """F2 叠乘保持：消费降级 insight → 权重被 ``DAMPING_FACTOR`` 一档封顶。"""
    from alphabee.orchestrator.services.degradation import DAMPING_FACTOR

    audit = audit_amplification(_thesis(), _insight(degraded=True, fallback_tier=2), _signals(), None)
    assert audit is not None
    assert audit.weight == DAMPING_FACTOR  # min(0.92, 0.85)


def test_conflict_penalty_branch_flags_positive_dimension():
    """§8.1 第 3 行：已结算的 high 冲突指向仍为正向的维度 → 不一致，权重 = 该 severity 的扣分档。"""
    from alphabee.agents.thesis.engine import _CONFLICT_PENALTY

    audit = audit_amplification(
        _thesis(dim_judgment="positive"),
        None,
        None,
        _conflicts(severity="high", status="verified"),
        edge="verified_conflict->dimension_score",
    )
    assert audit is not None
    assert audit.direction_consistent is False
    assert audit.weight == _CONFLICT_PENALTY["high"]

    ok = audit_amplification(
        _thesis(dim_judgment="negative", dim_score=-0.5),
        None,
        None,
        _conflicts(severity="high", status="verified"),
        edge="verified_conflict->dimension_score",
    )
    assert ok is not None and ok.direction_consistent is True


def test_anomaly_branch_flags_missing_projection_trace():
    """§8.1 第 2 行：异常投影证据缺留痕（无法回溯 pattern id）→ 不一致（伪异常率代理 > 0）。"""
    traced = audit_amplification(
        _thesis(evidence=_anomaly_evidence(traced=True)),
        None,
        None,
        None,
        edge="anomaly->fact_values->signal",
    )
    untraced = audit_amplification(
        _thesis(evidence=_anomaly_evidence(traced=False)),
        None,
        None,
        None,
        edge="anomaly->fact_values->signal",
    )
    assert traced is not None and traced.direction_consistent is True
    assert untraced is not None and untraced.direction_consistent is False


def test_report_mainline_branch_compares_insight_with_thesis_direction():
    """§8.1 第 4 行：insight 主线方向 vs thesis 判断方向（该边无显式系数 → weight=None）。"""
    audit = audit_amplification(
        _thesis(overall_judgment="negative", dim_judgment="negative", dim_score=-0.5),
        _insight(),
        None,
        None,
        edge="insight->report",
    )
    assert audit is not None
    assert audit.direction_consistent is False
    assert audit.weight is None


# ── ④ use_llm：零 LLM 调用 ──────────────────────────────────────────────────


def test_use_llm_false_makes_zero_llm_calls(monkeypatch):
    """``use_llm=False`` 必须**零 LLM 调用**：把模型工厂换成"一调用就炸"的桩来证明。"""

    def _boom(*args, **kwargs):  # pragma: no cover - 只要被调用就失败
        raise AssertionError("audit_amplification 不得在 use_llm=False 下创建/调用 LLM")

    monkeypatch.setattr(reviewer_module, "create_structured_model", _boom)
    audit = audit_amplification(_thesis(), _insight(), _signals(), None, use_llm=False)
    assert audit is not None and audit.direction_consistent is True


def test_use_llm_true_is_explicitly_not_implemented_in_v1(monkeypatch):
    """``use_llm=True`` 同样是零 LLM 调用（§16"不做 LLM 检测器"），但必须**显式留痕**。"""

    def _boom(*args, **kwargs):  # pragma: no cover - 只要被调用就失败
        raise AssertionError("v1 的 L2 是显式非目标：不得调用 LLM")

    monkeypatch.setattr(reviewer_module, "create_structured_model", _boom)
    audit = audit_amplification(_thesis(), _insight(), _signals(), None, use_llm=True)
    assert audit is not None
    assert "[L2 未启用" in audit.rationale
    assert audit.direction_consistent is True  # 留痕不改变 L1 结论


# ── ④b 挂钩路径：节点硬传关键字（t61 定稿，无签名探测） ──────────────────────


def test_node_hard_passes_amplification_keyword(monkeypatch):
    """★ t61 定稿：节点**硬传** ``amplification=``（不再探测签名）。

    用 spy 替身（``review(**kwargs)``）捕获真实调用参数：开关开启 → 传 ``AmplificationContext``；
    开关关闭 → 传 ``None``（review() 对 None 严格 no-op，而不是"少传一个关键字"）。
    """
    import asyncio

    import alphabee.orchestrator.agent as agent_module

    captured: list[dict] = []

    class SpyReviewer:
        def review(self, **kwargs):
            captured.append(kwargs)
            return ThesisReview(symbol="600519.SH", period="2024Q4")

    _patch_company_context(monkeypatch)
    monkeypatch.setattr(reviewer_module, "ThesisReviewer", lambda: SpyReviewer())

    asyncio.run(agent_module.review_thesis(_node_state(), {}))
    assert len(captured) == 1
    assert isinstance(captured[0]["amplification"], AmplificationContext)

    captured.clear()
    monkeypatch.setattr(agent_module, "_amplification_settings", lambda: (False, IssueSeverity.HIGH))
    asyncio.run(agent_module.review_thesis(_node_state(), {}))
    assert captured[0]["amplification"] is None, "开关关闭时必须硬传 None（no-op），不得省略关键字"


def test_no_signature_probe_residual_path():
    """★ t61：能力探测 shim 不得回潮 —— 残留它会让"实现方接口不匹配"被静默吞掉。"""
    import inspect

    from alphabee.orchestrator import agent as agent_module

    assert not hasattr(agent_module, "_reviewer_accepts_amplification")
    assert "inspect.signature" not in inspect.getsource(agent_module)
    # 两条替身已改由 ``**kwargs`` 承接硬传关键字（它们的用例在 t61 verify 里一起跑）
    assert "amplification" in inspect.getsource(agent_module.review_thesis)


def test_hook_path_and_attach_path_produce_identical_audits():
    """两条路径（``review(amplification=...)`` / 节点侧 ``attach_amplification_audit``）
    产出的审计**逐字段一致** —— 否则"兼容窄签名"就会变成"两套口径"。"""
    from dataclasses import asdict

    from alphabee.agents.thesis.reviewer import ThesisReviewer

    ctx = AmplificationContext(insight=_insight(), signals=_signals("negative"), conflicts=_conflicts())
    via_hook = (
        ThesisReviewer()
        .review(thesis=_thesis(), signal_results=_signals("negative")["results"], amplification=ctx)
        .amplification_audit
    )
    via_attach = attach_amplification_audit(ThesisReview(symbol="X", period="P"), _thesis(), ctx)
    assert via_hook is not None and via_attach is not None
    assert asdict(via_hook) == asdict(via_attach)
    assert via_hook.edge == "insight->thesis" and via_hook.direction_consistent is False


# ── ⑤ 载体字段 append-only ──────────────────────────────────────────────────


def test_thesis_review_amplification_field_is_append_only():
    """旧 payload（无 ``amplification_audit`` 键）仍可构造；新字段默认 ``None`` 且进 ``to_dict()``。"""
    legacy = ThesisReview(symbol="600519.SH", period="2024Q4")
    assert legacy.amplification_audit is None
    payload = legacy.to_dict()
    assert payload["amplification_audit"] is None

    fresh = ThesisReview(symbol="600519.SH", period="2024Q4")
    audit = attach_amplification_audit(fresh, _thesis(), AmplificationContext(insight=_insight(), signals=_signals()))
    assert audit is not None and fresh.amplification_audit is audit
    serialized = fresh.to_dict()["amplification_audit"]
    assert serialized is not None
    assert set(serialized) == {"edge", "upstream_artifact", "weight", "direction_consistent", "rationale"}
    assert serialized["edge"] == "insight->thesis"


def test_attach_is_a_strict_noop_without_context():
    """``context=None`` 与"空 context"都 → 严格 no-op（审计关闭时的逐字段一致，不产生字段噪声）。"""
    review = ThesisReview(symbol="X", period="P")
    assert attach_amplification_audit(review, _thesis(), None) is None
    assert review.amplification_audit is None
    empty = AmplificationContext()
    assert empty.is_empty() is True
    assert attach_amplification_audit(review, _thesis(), empty) is None
    assert review.amplification_audit is None


def test_attach_is_fail_open(monkeypatch):
    """审计内部异常 → 只 warning + 不挂值，绝不打断 review / run。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(reviewer_module, "audit_amplification", _boom)
    review = ThesisReview(symbol="X", period="P")
    assert attach_amplification_audit(review, _thesis(), AmplificationContext(insight=_insight())) is None
    assert review.amplification_audit is None


def test_reviewer_hook_attaches_audit_through_review():
    """挂钩点：``ThesisReviewer.review(amplification=...)`` 直接产出带审计的 ThesisReview。"""
    from alphabee.agents.thesis.reviewer import ThesisReviewer

    review = ThesisReviewer().review(
        thesis=_thesis(),
        signal_results=_signals()["results"],
        amplification=AmplificationContext(insight=_insight(), signals=_signals()),
    )
    assert review.amplification_audit is not None
    assert review.amplification_audit.edge == "insight->thesis"
    # 缺省（不传 amplification）→ 不产字段值：既有调用方零感知
    plain = ThesisReviewer().review(thesis=_thesis(), signal_results=_signals()["results"])
    assert plain.amplification_audit is None


# ── ⑥ 节点发射：Decision / D3 偏离 / 开关 ───────────────────────────────────


def _node_artifacts(
    *,
    signal_impact: str = "positive",
    with_insight: bool = True,
    severity: str = "high",
    with_conflicts: bool = True,
    dim_judgment: str = "positive",
    dim_score: float = 0.4,
):
    """节点级状态：artifact ``value`` 必须是**JSON dict**（编排层的真实形态）。

    ``find_artifact_model`` 只对 dict 载荷做 ``model_validate``，因此这里显式 ``model_dump(mode="json")``
    —— 用 pydantic 对象当 value 会让契约查找**静默返回 None**（本文件曾踩过，已修正）。
    """
    from alphabee.core.schemas import Artifact

    thesis_dict = {
        "symbol": "600519.SH",
        "period": "2024Q4",
        "overall_judgment": "positive" if dim_judgment.startswith("positive") else "negative",
        "dimensions": {
            "earnings_quality": {
                "id": "earnings_quality",
                "name": "盈利质量",
                "judgment": dim_judgment,
                "score": dim_score,
                "confidence": 0.8,
                "evidence": [],
            }
        },
    }
    artifacts = [
        Artifact(
            id="a-fact",
            type=ArtifactType.FACT_COLLECTION,
            producer_step="collect_raw_facts",
            value={"agent": "FactCollector", "query": "q", "symbol": "600519.SH", "raw_response": ""},
        ),
        Artifact(
            id="a-thesis", type=ArtifactType.THESIS_ANALYSIS, producer_step="run_thesis", value={"thesis": thesis_dict}
        ),
        Artifact(
            id="a-signal",
            type=ArtifactType.SIGNAL_ANALYSIS,
            producer_step="run_analysis_engines",
            value=_signals(signal_impact),
        ),
    ]
    if with_insight:
        artifacts.append(
            Artifact(
                id="a-insight",
                type=ArtifactType.INSIGHT_ANALYSIS,
                producer_step="synthesize_insights",
                value=_insight(),
            )
        )
    if with_conflicts:
        artifacts.append(
            Artifact(
                id="a-conflicts",
                type=ArtifactType.CONFLICTS_RESULT,
                producer_step="explore_conflicts",
                value=_conflicts(severity=severity).model_dump(mode="json"),
            )
        )
    return artifacts


def _node_state(
    *,
    signal_impact: str = "positive",
    with_insight: bool = True,
    severity: str = "high",
    with_conflicts: bool = True,
    dim_judgment: str = "positive",
    dim_score: float = 0.4,
):
    from alphabee.core.schemas import Run, RunStatus

    return {
        "run": Run(id="run-1", goal="分析贵州茅台", status=RunStatus.RUNNING, context={"symbol": "600519.SH"}),
        "steps": [],
        "artifacts": _node_artifacts(
            signal_impact=signal_impact,
            with_insight=with_insight,
            severity=severity,
            with_conflicts=with_conflicts,
            dim_judgment=dim_judgment,
            dim_score=dim_score,
        ),
        "issues": [],
        "decisions": [],
        "financial_facts": None,
        "market_facts": None,
        "llm_review": False,
    }


def _patch_company_context(monkeypatch):
    from alphabee.agents.thesis.models import CompanyContext
    from alphabee.orchestrator import agent as agent_module

    monkeypatch.setattr(agent_module, "build_company_context", lambda **kwargs: CompanyContext(symbol="600519.SH"))


def _run_node(monkeypatch, **state_kwargs):
    import asyncio

    from alphabee.orchestrator import agent as agent_module

    return asyncio.run(agent_module.review_thesis(_node_state(**state_kwargs), {}))


def test_node_emits_amplification_decision_with_evidence_refs(monkeypatch):
    """★ 真实发射点：``Decision(maker="amplification_audit")`` 带 based_on + evidence_refs。

    并且它必须能通过本节点自己的 ``evidence_refs_present`` 检测器（否则 F3 一上线就自产 D3）。
    """
    _patch_company_context(monkeypatch)
    result = _run_node(monkeypatch, severity="medium")
    audits = [d for d in result["decisions"] if d.maker == "amplification_audit"]
    assert len(audits) == 1, "审计结论必须落库为 Decision（死端 = F3 未落地）"
    decision = audits[0]
    assert decision.based_on, "amplification_audit Decision 必须带 based_on"
    assert decision.evidence_refs, "amplification_audit Decision 必须带 evidence_refs"
    assert {"a-thesis", "a-insight", "a-signal", "a-conflicts"} <= set(decision.based_on)
    assert "[放大审计|insight->thesis]" in decision.rationale

    from alphabee.orchestrator.detectors import NodeContext as DetectorContext
    from alphabee.orchestrator.detectors import evidence_refs_present

    ctx = DetectorContext(node_id="review_thesis", step=None, new_decisions=result["decisions"])
    assert evidence_refs_present(ctx).passed is True, "自产的无证据 verdict 会让 F3 一上线就报 D3"


def test_node_emits_no_amplification_deviation_when_consistent(monkeypatch):
    """全边一致 → 仍落 Decision（"被审计过"的证据），但**无** D3 偏离。

    构造（severity=medium ⇒ §8.1 第 3 行的"已结算且 severity≥high"前提不成立，该边不适用）：
    insight 正面、signal 正面、thesis 判断正面 ⇒ 可用边（边 1 / 边 4）全部一致。
    """
    _patch_company_context(monkeypatch)
    result = _run_node(monkeypatch, severity="medium")
    audits = [d for d in result["decisions"] if d.maker == "amplification_audit"]
    assert len(audits) == 1, "一致也必须留 Decision（§8.2 规则 2 的『检查结果写 Decision』）"
    assert "**不一致**" not in audits[0].rationale, "全边一致时不得判为不一致"
    categories = {issue.category for issue in result["issues"]}
    assert "amplification_direction_conflict" not in categories


def test_node_audits_conflict_penalty_edge_when_insight_edge_is_consistent(monkeypatch):
    """§8.1 第 3 行在生产路径可达：边 1 一致时自动选边继续下探到 ``verified_conflict->dimension_score``。

    构造：insight 正面 vs signal 正面（边 1 一致），但已验证 high 冲突指向的维度仍为**正向**判断
    ⇒ 边 3 不一致 ⇒ 报 D3，且 ``amplified_by`` 指向**该边**（证明 review_thesis 契约第 3 条
    postcondition"审计 verified conflict → 维度扣分一致性"不是空话）。
    """
    _patch_company_context(monkeypatch)
    result = _run_node(monkeypatch, dim_judgment="positive", dim_score=0.4, severity="high")
    deviations = [issue for issue in result["issues"] if issue.category == "amplification_direction_conflict"]
    assert len(deviations) == 1
    assert deviations[0].amplified_by == ["verified_conflict->dimension_score"]
    audits = [d for d in result["decisions"] if d.maker == "amplification_audit"]
    assert len(audits) == 1 and "[放大审计|verified_conflict->dimension_score]" in audits[0].rationale


def test_node_emits_d3_deviation_with_amplified_by_when_inconsistent(monkeypatch):
    """★ 方向不一致 → D3 偏离：severity=high、category 已登记、``amplified_by=[边 id]``。"""
    _patch_company_context(monkeypatch)

    import asyncio

    from alphabee.orchestrator import agent as agent_module

    state = _node_state(signal_impact="negative")
    result = asyncio.run(agent_module.review_thesis(state, {}))
    deviations = [issue for issue in result["issues"] if issue.category == "amplification_direction_conflict"]
    assert len(deviations) == 1
    issue = deviations[0]
    assert issue.deviation_class.value == "d3_argument"
    assert issue.severity is IssueSeverity.HIGH
    assert issue.amplified_by == ["insight->thesis"]
    assert issue.detected_at_step == "review_thesis"
    assert issue.related_step == "run_thesis"
    assert issue.deviation_class == agent_module.DeviationClass.D3_ARGUMENT
    # 分类可在**不依赖显式字段**的情况下由 category 惰性解析出来（F0 口径）
    from alphabee.orchestrator.services.deviation import CLASS_BY_CATEGORY, detection_latency

    assert CLASS_BY_CATEGORY["amplification_direction_conflict"] == agent_module.DeviationClass.D3_ARGUMENT
    assert detection_latency(issue) == 1  # run_thesis(8) → review_thesis(9)


def test_amplified_by_raises_cost_exposure(monkeypatch):
    """§10.1 联动：``amplified_by`` 非空 ⇒ 放大因子 1+n 进入代价敞口（不是装饰性字段）。"""
    from alphabee.orchestrator.recovery import cost_exposure
    from alphabee.orchestrator.services.deviation import record_deviation

    plain = record_deviation(
        agent_module_class(),
        IssueSeverity.HIGH,
        "msg",
        detected_at_step="review_thesis",
        related_step="run_thesis",
        category="amplification_direction_conflict",
    )
    amplified = record_deviation(
        agent_module_class(),
        IssueSeverity.HIGH,
        "msg",
        detected_at_step="review_thesis",
        related_step="run_thesis",
        category="amplification_direction_conflict",
        amplified_by=["insight->thesis"],
    )
    assert cost_exposure(plain) == 10  # high 档权重 ×1
    assert cost_exposure(amplified) == 20  # ×(1+1)


def agent_module_class():
    from alphabee.core.schemas import DeviationClass

    return DeviationClass.D3_ARGUMENT


def test_node_audit_is_disabled_by_config_switch(monkeypatch):
    """★ 开关：``audit_enabled=False`` → **零新增 Decision/Issue**（审计整体关闭）。"""
    _patch_company_context(monkeypatch)

    import alphabee.orchestrator.agent as agent_module

    monkeypatch.setattr(agent_module, "_amplification_settings", lambda: (False, IssueSeverity.HIGH))
    result = _run_node(monkeypatch)
    assert [d for d in result["decisions"] if d.maker == "amplification_audit"] == []
    assert [i for i in result["issues"] if i.category == "amplification_direction_conflict"] == []


def test_node_audit_switch_is_a_pure_increment(monkeypatch):
    """开关是**纯增量**：开 ⇄ 关 的差异**只**是那一条 audit Decision（一致态下无 D3）。

    这是"关闭开关 = 纯回滚"的机读证据（逐字段对照，而非只看"少了个 maker"）。
    """
    _patch_company_context(monkeypatch)

    import asyncio

    import alphabee.orchestrator.agent as agent_module

    state = _node_state(severity="medium")  # 全边一致：增量只有 Decision

    monkeypatch.setattr(agent_module, "_amplification_settings", lambda: (False, IssueSeverity.HIGH))
    off = asyncio.run(agent_module.review_thesis(state, {}))
    monkeypatch.setattr(agent_module, "_amplification_settings", lambda: (True, IssueSeverity.HIGH))
    on = asyncio.run(agent_module.review_thesis(state, {}))

    assert [d.maker for d in on["decisions"]] == [*[d.maker for d in off["decisions"]], "amplification_audit"]
    assert {i.category for i in on["issues"]} == {i.category for i in off["issues"]}
    assert [s.id for s in on["steps"]] == [s.id for s in off["steps"]]
    assert [a.type for a in on["artifacts"]] == [a.type for a in off["artifacts"]]
    # 关闭态：审计整体 no-op（无 Decision、无偏离），且 review artifact 里字段为 None
    assert [d for d in off["decisions"] if d.maker == "amplification_audit"] == []
    assert off["artifacts"][0].value["amplification_audit"] is None
    assert on["artifacts"][0].value["amplification_audit"]["edge"] == "insight->thesis"


def test_node_audit_switch_off_suppresses_only_the_deviation(monkeypatch):
    """不一致态下关掉开关 → D3 消失，**其余** issue 集合逐一不变（不是"少了一批问题"）。"""
    _patch_company_context(monkeypatch)

    import asyncio

    import alphabee.orchestrator.agent as agent_module

    state = _node_state(signal_impact="negative")
    monkeypatch.setattr(agent_module, "_amplification_settings", lambda: (True, IssueSeverity.HIGH))
    on = asyncio.run(agent_module.review_thesis(state, {}))
    monkeypatch.setattr(agent_module, "_amplification_settings", lambda: (False, IssueSeverity.HIGH))
    off = asyncio.run(agent_module.review_thesis(state, {}))

    assert "amplification_direction_conflict" in {i.category for i in on["issues"]}
    assert "amplification_direction_conflict" not in {i.category for i in off["issues"]}
    assert {i.category for i in on["issues"]} - {"amplification_direction_conflict"} == {
        i.category for i in off["issues"]
    }


def test_amplification_settings_fail_open(monkeypatch):
    """配置缺失 / 读取异常 → ``(True, HIGH)``（fail-open，且不在 import 期读配置）。"""
    import alphabee.orchestrator.agent as agent_module

    monkeypatch.setattr(agent_module, "get_settings", None, raising=False)

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("alphabee.config.get_settings", _boom)
    assert agent_module._amplification_settings() == (True, IssueSeverity.HIGH)


def test_amplification_settings_reads_config_values():
    """正常路径：读 ``deviation.amplification`` 的 ``audit_enabled`` / ``overturn_severity``。"""
    import alphabee.orchestrator.agent as agent_module

    class _Amp:
        audit_enabled = False
        overturn_severity = "critical"

    class _Deviation:
        amplification = _Amp()

    class _Settings:
        deviation = _Deviation()

    import alphabee.config as config_module

    original = config_module.get_settings
    config_module.get_settings = lambda: _Settings()
    try:
        assert agent_module._amplification_settings() == (False, IssueSeverity.CRITICAL)
    finally:
        config_module.get_settings = original


# ── ⑦ 契约层无违规 ──────────────────────────────────────────────────────────


def test_contracts_have_no_violations():
    """``validate_contracts() == []``：F3 的登记不得引入契约违规（含放大覆盖表断言）。"""
    from alphabee.orchestrator.node_contracts import validate_contracts

    assert validate_contracts() == []
