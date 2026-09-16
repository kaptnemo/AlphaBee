"""resolve_midterm_decision 节点测试（方案 A + 决策点 6(a)，Phase 1）。"""

from __future__ import annotations

import asyncio

from alphabee.agents.schemas import ConflictAnalysisResult
from alphabee.core import Artifact, ArtifactRoleGroup, ArtifactType, Run, RunStatus
from alphabee.midterm.models import (
    CognitiveState,
    CompanyStateArtifact,
    EvidenceEvent,
    ExpectationGap,
    StateBelief,
    VariableScores,
)
from alphabee.orchestrator.contracts import (
    FactCollectionArtifact,
    InsightArtifact,
    ThesisArtifact,
    coerce_midterm_decision,
)
from alphabee.orchestrator.nodes import midterm as node


def _run(symbol="600519.SH"):
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.RUNNING,
        context={"symbol": symbol, "query": "分析贵州茅台"},
    )


def _state(symbol="600519.SH", artifacts=None, run=None):
    return {
        "run": run if run is not None else _run(symbol),
        "steps": [],
        "artifacts": artifacts or [],
        "issues": [],
        "decisions": [],
    }


def _insight_artifact(
    core_view="核心观点：看多",
    confidence="high",
    degraded=False,
    fallback_tier=0,
    supporting=None,
    counter=None,
    materiality=None,
):
    return Artifact(
        id="a-insight",
        type=ArtifactType.INSIGHT_ANALYSIS,
        producer_step="synthesize_insights",
        value=InsightArtifact(
            core_view=core_view,
            confidence=confidence,
            degraded=degraded,
            fallback_tier=fallback_tier,
            supporting_evidence=supporting or [],
            counter_evidence=counter or [],
            materiality_rank=materiality or [],
        ).model_dump(mode="json"),
    )


def _thesis_artifact(overall_judgment="positive", dimensions=None):
    return Artifact(
        id="a-thesis",
        type=ArtifactType.THESIS_ANALYSIS,
        producer_step="run_thesis",
        value=ThesisArtifact(
            thesis={
                "overall_judgment": overall_judgment,
                "dimensions": dimensions or {},
            }
        ).model_dump(mode="json"),
    )


def _fact_artifact(raw_response="公司经营稳健。营收同比增长。"):
    return Artifact(
        id="a-fact",
        type=ArtifactType.FACT_COLLECTION,
        producer_step="collect_raw_facts",
        value=FactCollectionArtifact(
            agent="FactCollector",
            query="分析贵州茅台",
            symbol="600519.SH",
            raw_response=raw_response,
        ).model_dump(mode="json"),
    )


def _conflict_artifact():
    return Artifact(
        id="a-conflict",
        type=ArtifactType.CONFLICTS_RESULT,
        producer_step="explore_conflicts",
        value=ConflictAnalysisResult.model_validate(
            {
                "conflicts": [
                    {
                        "id": "c1",
                        "theme": "盈利增长但现金流恶化",
                        "description": "利润增长没有被现金流验证。",
                        "related_dimensions": ["earnings_quality"],
                        "severity": "high",
                        "confidence": 0.9,
                        "hypotheses": [
                            {
                                "id": "h1",
                                "conflict_id": "c1",
                                "explanation": "收入质量不足",
                                "predictions": [],
                                "required_evidence": [],
                                "score": 0.8,
                                "status": "verified",
                            }
                        ],
                    }
                ]
            }
        ).model_dump(mode="json"),
    )


def _fake_decision(symbol="600519.SH", thesis=""):
    return CompanyStateArtifact(
        symbol=symbol,
        thesis=thesis,
        thesis_confidence=0.7,
        prior_confidence=0.7,
        state=StateBelief(
            distribution={"S1": 0.2, "S2": 0.8},
            argmax_state=CognitiveState.S2_CONFIRM.value,
            entropy=0.0,
        ),
        expectation_gap=ExpectationGap(),
        variable_scores=VariableScores(),
        evidence_log=[],
    )


def _ev(description, effect="confirming", delta=0.3, kind="expectation"):
    return EvidenceEvent(
        id=f"id-{description}",
        date="2025-01-01",
        kind=kind,
        description=description,
        effect_on_thesis=effect,
        confidence_delta=delta,
    )


def _patch_decision(monkeypatch):
    captured = {}

    def fake_collect_split(symbol, thesis="", window_texts=None, model=None):
        captured["symbol"] = symbol
        captured["thesis"] = thesis
        captured["window_texts"] = window_texts
        return [], []  # 数值/定性证据在节点单测中固定为空，只测映射与接线

    def fake_get_decision(
        symbol,
        evidence=None,
        *,
        include_market=True,
        prior_confidence=None,
        thesis="",
        insight_materiality=None,
    ):
        captured["symbol"] = symbol
        captured["evidence"] = evidence
        captured["include_market"] = include_market
        captured["prior_confidence"] = prior_confidence
        captured["thesis"] = thesis
        captured["insight_materiality"] = insight_materiality
        return _fake_decision(symbol=symbol, thesis=thesis)

    monkeypatch.setattr(node, "collect_evidence_split", fake_collect_split)
    monkeypatch.setattr(node, "get_decision", fake_get_decision)
    return captured


def _find_midterm(result):
    for artifact in result.get("artifacts", []):
        if artifact.type == ArtifactType.MIDTERM_DECISION:
            return artifact
    return None


# ── artifact 类型登记 ───────────────────────────────────────────────────────


def test_midterm_decision_artifact_type_and_role_group_registered():
    assert ArtifactType.MIDTERM_DECISION.value == "midterm_decision"
    from alphabee.core.schemas import _ARTIFACT_TYPE_TO_ROLE_GROUP

    assert _ARTIFACT_TYPE_TO_ROLE_GROUP[ArtifactType.MIDTERM_DECISION] == ArtifactRoleGroup.DECISION


def test_coerce_midterm_decision_roundtrips():
    decision = _fake_decision(thesis="H")
    coerced = coerce_midterm_decision(decision.model_dump(mode="json"))
    assert isinstance(coerced, CompanyStateArtifact)
    assert coerced.symbol == "600519.SH"
    assert coerce_midterm_decision(None) is None
    assert coerce_midterm_decision("not-a-dict") is None
    assert coerce_midterm_decision(decision) is decision


# ── 映射（H / prior_confidence / window_texts）──────────────────────────────


def test_maps_insight_core_view_and_confidence(monkeypatch):
    captured = _patch_decision(monkeypatch)
    result = asyncio.run(
        node.resolve_midterm_decision(
            _state(
                artifacts=[
                    _insight_artifact(core_view="看多核心观点", confidence="high"),
                    _thesis_artifact(overall_judgment="positive"),
                ]
            ),
            {},
        )
    )

    assert captured["thesis"] == "看多核心观点"
    assert captured["prior_confidence"] == 0.7
    assert captured["include_market"] is True
    assert _find_midterm(result) is not None


def test_insight_missing_falls_back_to_thesis_overall_judgment(monkeypatch):
    captured = _patch_decision(monkeypatch)
    result = asyncio.run(
        node.resolve_midterm_decision(
            _state(artifacts=[_thesis_artifact(overall_judgment="positive")]),
            {},
        )
    )

    assert "看多" in captured["thesis"]
    assert captured["prior_confidence"] is None  # thesis 无维度置信度 → 无先验，保守退化
    assert _find_midterm(result) is not None


def test_degraded_insight_downgrades_to_thesis_and_damps_prior(monkeypatch):
    captured = _patch_decision(monkeypatch)
    asyncio.run(
        node.resolve_midterm_decision(
            _state(
                artifacts=[
                    _insight_artifact(core_view="旧观点", confidence="high", degraded=True, fallback_tier=2),
                    _thesis_artifact(overall_judgment="negative"),
                ]
            ),
            {},
        )
    )

    assert "看空" in captured["thesis"]  # degraded 时忽略 core_view，用 thesis 判断
    assert captured["prior_confidence"] == 0.7 * 0.7  # high→0.7，tier2 阻尼 ×0.7


def test_confidence_string_mapping(monkeypatch):
    for label, expected in (("low", 0.3), ("medium", 0.5), ("high", 0.7)):
        captured = _patch_decision(monkeypatch)
        asyncio.run(node.resolve_midterm_decision(_state(artifacts=[_insight_artifact(confidence=label)]), {}))
        assert captured["prior_confidence"] == expected


def test_window_texts_only_verified_conflict_no_narrative(monkeypatch):
    # raw_response 是叙事摘要而非财报/公告/研报原文，不得进入 window_texts；
    # 只有 spec 明确要求的已验证冲突 explanation 才作为窗口文本。
    captured = _patch_decision(monkeypatch)
    asyncio.run(
        node.resolve_midterm_decision(
            _state(
                artifacts=[
                    _insight_artifact(),
                    _fact_artifact("叙事摘要：公司经营稳健，营收同比增长"),
                    _conflict_artifact(),
                ]
            ),
            {},
        )
    )

    assert len(captured["window_texts"]) == 1
    assert "叙事摘要" not in captured["window_texts"][0]
    assert "盈利增长但现金流恶化" in captured["window_texts"][0]
    assert "收入质量不足" in captured["window_texts"][0]


def test_window_texts_none_when_no_raw_text(monkeypatch):
    captured = _patch_decision(monkeypatch)
    asyncio.run(node.resolve_midterm_decision(_state(artifacts=[_insight_artifact()]), {}))

    assert captured["window_texts"] is None


# ── 改造 A：insight 正反证据注入 EvidenceEvent ───────────────────────────────


def test_insight_evidence_injected_into_decision(monkeypatch):
    captured = _patch_decision(monkeypatch)
    asyncio.run(
        node.resolve_midterm_decision(
            _state(
                artifacts=[
                    _insight_artifact(
                        supporting=[
                            {"statement": "高速通信线+35.44%", "source": "segment:high_speed_comm", "weight": "strong"}
                        ],
                        counter=[{"statement": "增收不增利", "source": "signal:profit_leverage", "weight": "moderate"}],
                    ),
                ]
            ),
            {},
        )
    )

    events = captured["evidence"]
    assert events is not None
    by_desc = {e.description: e for e in events}
    assert "高速通信线+35.44%" in by_desc
    assert by_desc["高速通信线+35.44%"].effect_on_thesis == "confirming"
    assert by_desc["高速通信线+35.44%"].confidence_delta == 0.5  # strong
    assert "增收不增利" in by_desc
    assert by_desc["增收不增利"].effect_on_thesis == "refuting"
    assert by_desc["增收不增利"].confidence_delta == 0.3  # moderate


def test_no_insight_evidence_when_insight_missing(monkeypatch):
    captured = _patch_decision(monkeypatch)
    asyncio.run(node.resolve_midterm_decision(_state(artifacts=[_thesis_artifact(overall_judgment="positive")]), {}))
    assert captured["evidence"] == []


# ── 改造 A2：先验-似然同源解耦（insight 证据与 Stage B 证据二选一入账）──────────


def test_stage_b_evidence_displaces_insight_evidence(monkeypatch):
    """Stage B 定性证据存在 → insight 证据整体丢弃（二选一，选 Stage B）。"""
    captured = _patch_decision(monkeypatch)

    def fake_split(symbol, thesis="", window_texts=None, model=None):
        return [], [_ev("事实判定证据", effect="confirming", delta=0.3, kind="fundamental")]

    monkeypatch.setattr(node, "collect_evidence_split", fake_split)
    asyncio.run(
        node.resolve_midterm_decision(
            _state(
                artifacts=[
                    _insight_artifact(
                        supporting=[{"statement": "高速通信线+35.44%", "source": "s", "weight": "strong"}],
                    ),
                ]
            ),
            {},
        )
    )

    by_desc = {e.description: e for e in captured["evidence"]}
    assert "事实判定证据" in by_desc
    assert "高速通信线+35.44%" not in by_desc  # 同源 insight 证据被丢弃


def test_insight_evidence_fallback_when_stage_b_empty(monkeypatch):
    """Stage B 为空 → insight 证据兜底入账（结构性洞察作为唯一 LLM 证据通道）。"""
    captured = _patch_decision(monkeypatch)
    asyncio.run(
        node.resolve_midterm_decision(
            _state(
                artifacts=[
                    _insight_artifact(
                        supporting=[{"statement": "高速通信线+35.44%", "source": "s", "weight": "strong"}],
                    ),
                ]
            ),
            {},
        )
    )

    by_desc = {e.description: e for e in captured["evidence"]}
    assert "高速通信线+35.44%" in by_desc  # 兜底保留


def test_numeric_evidence_always_included_with_stage_b(monkeypatch):
    """数值规则证据与 LLM 观点无关：Stage B 存在时与 Stage B 一起恒入账。"""
    captured = _patch_decision(monkeypatch)

    def fake_split(symbol, thesis="", window_texts=None, model=None):
        return (
            [_ev("业绩快报 beat", effect="confirming", delta=0.5, kind="expectation")],
            [_ev("事实判定证据", effect="refuting", delta=0.3, kind="fundamental")],
        )

    monkeypatch.setattr(node, "collect_evidence_split", fake_split)
    asyncio.run(
        node.resolve_midterm_decision(
            _state(
                artifacts=[
                    _insight_artifact(
                        supporting=[{"statement": "高速通信线+35.44%", "source": "s", "weight": "strong"}],
                    ),
                ]
            ),
            {},
        )
    )

    by_desc = {e.description: e for e in captured["evidence"]}
    assert "业绩快报 beat" in by_desc
    assert "事实判定证据" in by_desc
    assert "高速通信线+35.44%" not in by_desc


def test_decouple_evidence_pure_policy():
    """_decouple_evidence 纯策略：Stage B 存在 → 弃 insight；Stage B 为空 → insight 兜底。"""
    n = [_ev("n", kind="expectation")]
    q = [_ev("q", kind="fundamental")]
    i = [_ev("i", kind="thesis")]

    out = node._decouple_evidence(n, q, i)
    assert {e.description for e in out} == {"n", "q"}

    out = node._decouple_evidence(n, [], i)
    assert {e.description for e in out} == {"n", "i"}


# ── 改造 D：insight.materiality_rank 传入 get_decision ─────────────────────────


def test_materiality_rank_passed_to_get_decision(monkeypatch):
    captured = _patch_decision(monkeypatch)
    materiality = [
        {"variable": "商誉减值", "importance": "critical", "reasoning": ""},
        {"variable": "存货去化与减值", "importance": "critical", "reasoning": ""},
    ]
    asyncio.run(
        node.resolve_midterm_decision(
            _state(artifacts=[_insight_artifact(materiality=materiality)]),
            {},
        )
    )
    assert captured["insight_materiality"] == materiality


def test_materiality_rank_empty_when_insight_missing(monkeypatch):
    captured = _patch_decision(monkeypatch)
    asyncio.run(node.resolve_midterm_decision(_state(artifacts=[_thesis_artifact(overall_judgment="positive")]), {}))
    assert captured["insight_materiality"] == []


# ── 降级：失败记 Issue，报告照常 ─────────────────────────────────────────────


def test_decision_failure_emits_issue_and_no_artifact(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("LLM 抽取失败")

    monkeypatch.setattr(node, "get_decision", boom)
    result = asyncio.run(node.resolve_midterm_decision(_state(artifacts=[_insight_artifact()]), {}))

    assert _find_midterm(result) is None
    issues = [i for i in result["issues"] if i.category == "midterm_decision_failed"]
    assert len(issues) == 1
    assert "LLM 抽取失败" in issues[0].message
    # 报告路径照常：节点仍返回完成态步骤（有 issue 无 artifact → failed），
    # 但不向上抛出异常中断主链
    assert result["steps"][0].status.value == "failed"


def test_no_symbol_skips(monkeypatch):
    captured = _patch_decision(monkeypatch)
    result = asyncio.run(node.resolve_midterm_decision(_state(symbol=None), {}))
    assert result["steps"][0].status.value == "skipped"
    assert "artifacts" not in result or result.get("artifacts") == []
    assert not captured


def test_invalid_upstream_artifact_does_not_raise(monkeypatch):
    # 上游 artifact value 无法通过 InsightArtifact.model_validate 时，
    # 读取+映射+调用整段被同一 try/except 兜住：记 Issue 正常返回，不向上抛异常。
    captured = _patch_decision(monkeypatch)
    bad_insight = Artifact(
        id="a-bad-insight",
        type=ArtifactType.INSIGHT_ANALYSIS,
        producer_step="synthesize_insights",
        value={"confidence": 12345},  # InsightArtifact.confidence 应为 str，触发 ValidationError
    )
    result = asyncio.run(node.resolve_midterm_decision(_state(artifacts=[bad_insight]), {}))

    assert _find_midterm(result) is None
    assert not captured  # model_validate 在调用决策模型前就失败，不应走到 get_decision_with_evidence
    issues = [i for i in result["issues"] if i.category == "midterm_decision_failed"]
    assert len(issues) == 1
    assert result["steps"][0].status.value == "failed"
