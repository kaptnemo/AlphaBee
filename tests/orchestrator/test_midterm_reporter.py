"""midterm_decision_reporter 节点测试（决策层总结落日志，不进报告）。"""

from __future__ import annotations

import asyncio

from alphabee.core import Artifact, ArtifactRoleGroup, ArtifactType
from alphabee.midterm.models import (
    CognitiveState,
    CompanyStateArtifact,
    EvidenceEvent,
    ExpectedValue,
    PositionDecision,
    ScenarioOutcome,
    StateBelief,
)
from alphabee.orchestrator.nodes import midterm_reporter as node


def _company_state(symbol="600519.SH"):
    return CompanyStateArtifact(
        symbol=symbol,
        as_of_date="2025-06-30",
        state=StateBelief(
            distribution={"S1": 0.2, "S2": 0.8},
            argmax_state=CognitiveState.S2_CONFIRM.value,
            entropy=0.3,
        ),
        thesis="看多核心观点",
        thesis_confidence=0.6,
        prior_confidence=0.5,
        expected_value=ExpectedValue(
            scenarios=[
                ScenarioOutcome(scenario="bull", probability=0.3, expected_return=40.0),
                ScenarioOutcome(scenario="base", probability=0.5, expected_return=5.0),
                ScenarioOutcome(scenario="bear", probability=0.2, expected_return=-20.0),
            ],
            ev=8.5,
            risk=20.0,
            risk_adjusted_ev=0.425,
            probability_source="bayes_posterior",
        ),
        evidence_log=[
            EvidenceEvent(
                id="ev-1",
                date="2025-06-01",
                kind="expectation",
                description="一致预期上修",
                effect_on_thesis="confirming",
                confidence_delta=0.1,
            )
        ],
        position=PositionDecision(position_band="加仓", stock_weight=0.4, actual_weight=0.32),
    )


def _midterm_artifact(symbol="600519.SH"):
    return Artifact(
        id="a-midterm",
        type=ArtifactType.MIDTERM_DECISION,
        producer_step="resolve_midterm_decision",
        value=_company_state(symbol).model_dump(mode="json"),
    )


def _state(artifacts):
    return {"artifacts": artifacts, "issues": []}


def test_summary_artifact_type_and_role_group_registered():
    assert ArtifactType.MIDTERM_DECISION_SUMMARY.value == "midterm_decision_summary"
    from alphabee.core.schemas import _ARTIFACT_TYPE_TO_ROLE_GROUP

    assert _ARTIFACT_TYPE_TO_ROLE_GROUP[ArtifactType.MIDTERM_DECISION_SUMMARY] == ArtifactRoleGroup.DECISION


def test_render_summary_contains_key_sections():
    text = node.render_midterm_decision_summary(_company_state())

    assert "中期决策总结" in text
    assert "600519.SH" in text
    assert "argmax: S2" in text
    assert "看多核心观点" in text
    assert "EV=8.5" in text
    assert "加仓" in text
    assert "一致预期上修" in text


def test_node_produces_summary_artifact():
    result = asyncio.run(node.report_midterm_decision(_state([_midterm_artifact()]), {}))

    assert result["steps"][0].status.value == "succeeded"
    summaries = [a for a in result["artifacts"] if a.type == ArtifactType.MIDTERM_DECISION_SUMMARY]
    assert len(summaries) == 1
    assert "600519.SH" in summaries[0].value["text"]
    assert summaries[0].value["symbol"] == "600519.SH"


def test_node_skips_when_no_midterm_decision():
    result = asyncio.run(node.report_midterm_decision(_state([]), {}))

    assert result["steps"][0].status.value == "skipped"
    assert result.get("artifacts", []) == []
    assert result.get("issues", []) == []
