"""F4：review_thesis 节点把 COMPANY_TRACK 同行基准组装为 peer_context 显式传入 reviewer。"""

import asyncio

from alphabee.agents.thesis.models import CompanyContext, ThesisReview
from alphabee.company_track.contracts import CompanyTrackArtifact
from alphabee.core import Artifact, ArtifactType, Run, RunStatus
from alphabee.orchestrator import agent as agent_module


def _base_run() -> Run:
    return Run(
        id="run-1",
        goal="分析",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "query": "分析"},
    )


def _thesis_dict() -> dict:
    return {
        "symbol": "600519.SH",
        "period": "2024Q4",
        "overall_judgment": "positive",
        "dimensions": {
            "earnings_quality": {
                "id": "earnings_quality",
                "name": "盈利质量",
                "judgment": "positive",
                "score": 0.4,
                "confidence": 0.8,
                "evidence": [],
            }
        },
    }


def _fact_artifact() -> Artifact:
    return Artifact(
        id="a-fact",
        type=ArtifactType.FACT_COLLECTION,
        producer_step="collect_raw_facts",
        value={"agent": "FactCollector", "query": "q", "symbol": "600519.SH", "raw_response": ""},
    )


def _thesis_artifact() -> Artifact:
    return Artifact(
        id="a-thesis",
        type=ArtifactType.THESIS_ANALYSIS,
        producer_step="run_thesis",
        value={"thesis": _thesis_dict()},
    )


def _state(artifacts: list[Artifact]) -> dict:
    return {
        "run": _base_run(),
        "steps": [],
        "artifacts": artifacts,
        "issues": [],
        "decisions": [],
        "financial_facts": None,
        "market_facts": None,
        "llm_review": False,
    }


def _capture_reviewer(monkeypatch) -> dict:
    import alphabee.agents.thesis.reviewer as reviewer_module

    captured: dict = {}

    class FakeReviewer:
        def review(self, thesis, signal_results, company_context, use_llm=False, **kwargs):
            captured["peer_context"] = kwargs.get("peer_context", "NOT_PASSED")
            return ThesisReview(
                symbol=thesis.symbol,
                period=thesis.period,
                dimension_verdicts={},
                overall_status="passed",
            )

    monkeypatch.setattr(reviewer_module, "ThesisReviewer", lambda: FakeReviewer())
    monkeypatch.setattr(agent_module, "build_company_context", lambda **kwargs: CompanyContext(symbol="600519.SH"))
    return captured


def test_review_thesis_passes_peer_context_when_track_present(monkeypatch):
    captured = _capture_reviewer(monkeypatch)
    track = CompanyTrackArtifact(
        track_label="存储芯片",
        peer_group=["北京君正", "普冉股份"],
        peer_benchmarks={"roe": 0.24},
    )
    artifacts = [
        _fact_artifact(),
        _thesis_artifact(),
        Artifact(
            id="a-track",
            type=ArtifactType.COMPANY_TRACK,
            producer_step="resolve_company_track",
            value=track.model_dump(mode="json"),
        ),
    ]

    asyncio.run(agent_module.review_thesis(_state(artifacts), {}))

    assert captured["peer_context"] == {
        "track_label": "存储芯片",
        "peer_group": ["北京君正", "普冉股份"],
        "peer_benchmarks": {"roe": 0.24},
    }


def test_review_thesis_passes_none_without_track(monkeypatch):
    captured = _capture_reviewer(monkeypatch)
    artifacts = [_fact_artifact(), _thesis_artifact()]

    asyncio.run(agent_module.review_thesis(_state(artifacts), {}))

    assert captured["peer_context"] is None
