"""Phase F 报告层消费测试：ReportGenerationPayload.company_track + 确定性报告章节。"""

from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.core import Artifact, ArtifactType, Run, RunStatus
from alphabee.orchestrator.reporter import build_deterministic_report
from alphabee.orchestrator.services.payload_builders import build_report_generation_payload


def _state_with_track():
    track = CompanyTrackArtifact(
        symbol="603986.SH",
        as_of_date="20251231",
        stale_after="2026-03-31",
        segments=[
            SegmentSnapshot(
                report_date="20251231",
                segment_name="存储芯片",
                category="按产品分类",
                revenue_share=71.3,
                revenue_yoy=26.4,
                source="em",
            )
        ],
        dominant_segment="存储芯片",
        track_label="存储芯片设计龙头",
        business_model="component",
        peer_group=["300223.SZ", "688766.SH"],
        peer_benchmarks={"peer_avg_roe": 0.039, "peer_avg_debt_ratio": 0.069},
    )
    artifact = Artifact(
        id="a-track",
        type=ArtifactType.COMPANY_TRACK,
        producer_step="resolve_company_track",
        value=track.model_dump(mode="json"),
    )
    run = Run(id="r", goal="x", status=RunStatus.RUNNING, context={"symbol": "603986.SH"})
    return {"run": run, "artifacts": [artifact], "issues": [], "steps": []}


def test_report_payload_populates_company_track():
    payload = build_report_generation_payload(_state_with_track())
    assert payload.company_track is not None
    assert payload.company_track.track_label == "存储芯片设计龙头"
    assert payload.company_track.business_model == "component"
    assert payload.company_track.peer_group == ["300223.SZ", "688766.SH"]
    assert payload.company_track.peer_benchmarks["peer_avg_roe"] == 0.039


def test_report_payload_without_track_is_none():
    run = Run(id="r", goal="x", status=RunStatus.RUNNING, context={"symbol": "603986.SH"})
    payload = build_report_generation_payload({"run": run, "artifacts": [], "issues": [], "steps": []})
    assert payload.company_track is None


def test_deterministic_report_includes_track_section():
    payload = build_report_generation_payload(_state_with_track())
    report = build_deterministic_report(payload)
    track_section = report.get("sections", {}).get("company_track", "")
    assert "存储芯片设计龙头" in track_section
    assert "对标组基准" in track_section
    assert "300223.SZ" in track_section


def test_deterministic_report_stale_notice():
    track = CompanyTrackArtifact(
        symbol="603986.SH",
        as_of_date="20241231",
        stale_after="2025-01-01",
        stale=True,
        segments=[
            SegmentSnapshot(
                report_date="20241231", segment_name="存储芯片", category="按产品分类", revenue_share=70.0, source="em"
            )
        ],
        track_label="存储芯片",
    )
    run = Run(id="r", goal="x", status=RunStatus.RUNNING, context={"symbol": "603986.SH"})
    state = {
        "run": run,
        "artifacts": [
            Artifact(id="a", type=ArtifactType.COMPANY_TRACK, producer_step="x", value=track.model_dump(mode="json"))
        ],
        "issues": [],
        "steps": [],
    }
    payload = build_report_generation_payload(state)
    report = build_deterministic_report(payload)
    assert "可能过期" in report["sections"]["company_track"]
