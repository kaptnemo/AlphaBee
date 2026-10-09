"""Phase F 报告层消费测试：ReportGenerationPayload.company_track + 确定性报告章节（含对标组置信度）。"""

from pathlib import Path

import pytest

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


# ── 对标组置信度进入报告层（设计 §3.4/§3.7/§8 决策 5） ──────────────────────


def _state_with_confidence(level: str, *, taxonomy_reliable: bool) -> dict:
    state = _state_with_track()
    track = next(a for a in state["artifacts"] if a.type == ArtifactType.COMPANY_TRACK)
    track.value.update(
        {
            "peer_group_confidence": level,
            "peer_group_confidence_score": 0.15 if level == "低" else 0.85,
            "peer_group_confidence_basis": f"置信度 {level}（score=0.15；taxonomy_reliable=1.00；权重={{}}）",
            "peer_group_taxonomy_reliable": taxonomy_reliable,
        }
    )
    return state


def test_report_payload_carries_peer_confidence():
    payload = build_report_generation_payload(_state_with_confidence("中", taxonomy_reliable=True))
    assert payload.company_track is not None
    assert payload.company_track.peer_confidence == "中"
    assert payload.company_track.peer_confidence_score == pytest.approx(0.85)
    assert "置信度 中" in payload.company_track.peer_confidence_basis
    assert payload.company_track.peer_taxonomy_reliable is True
    assert payload.company_track.peer_confidence_low is False


def _track_section(report: dict) -> str:
    return report.get("sections", {}).get("company_track", "")


def test_deterministic_report_shows_confidence_and_low_hint():
    """低置信 ⇒ 报告显式提示基准参考性弱；中置信 ⇒ 不出现该提示（两种情形都钉住）。"""
    low_payload = build_report_generation_payload(_state_with_confidence("低", taxonomy_reliable=False))
    low_section = _track_section(build_deterministic_report(low_payload))
    assert "对标组置信度: 低" in low_section
    assert "同业基准参考性弱" in low_section
    assert "分类兜底，未经业务核验" in low_section

    mid_payload = build_report_generation_payload(_state_with_confidence("中", taxonomy_reliable=True))
    mid_section = _track_section(build_deterministic_report(mid_payload))
    assert "对标组置信度: 中" in mid_section
    assert "同业基准参考性弱" not in mid_section
    assert "分类兜底" not in mid_section


def test_payload_builder_maps_artifact_confidence_fields():
    """映射完整性：artifact 四个置信字段逐一进 payload（防漏字段导致报告层拿不到）。"""
    from alphabee.orchestrator.contracts import ReportCompanyTrackPayload

    payload = build_report_generation_payload(_state_with_confidence("低", taxonomy_reliable=False))
    assert isinstance(payload.company_track, ReportCompanyTrackPayload)
    assert payload.company_track.peer_confidence == "低"
    assert payload.company_track.peer_confidence_low is True
    assert payload.company_track.peer_taxonomy_reliable is False
    assert payload.company_track.peer_confidence_basis


def test_m5_missing_low_confidence_hint_is_killed():
    """M5：低置信不输出提示（去掉警示行）⇒ 必红。"""
    import importlib.util
    import sys
    import tempfile

    from alphabee import PROJECT_ROOT

    source = (PROJECT_ROOT / "alphabee" / "orchestrator" / "reporter.py").read_text(encoding="utf-8")
    old = """            if track.peer_confidence_low:
                track_lines.append(
                    "- ⚠ 对标组置信度低：同业基准参考性弱，不宜作为估值/盈利比较的主要依据（需补充候选或人工核验）"
                )"""
    assert old in source, "变异锚点失效（reporter 低置信提示实现已变）"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reporter_mutant_m5.py"
        path.write_text(
            source.replace(old, "            if False:  # M5 低置信不提示\n                pass", 1), encoding="utf-8"
        )
        spec = importlib.util.spec_from_file_location("reporter_mutant_m5", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["reporter_mutant_m5"] = module
        spec.loader.exec_module(module)
    payload = build_report_generation_payload(_state_with_confidence("低", taxonomy_reliable=False))
    section = _track_section(module.build_deterministic_report(payload))
    assert "同业基准参考性弱" not in section, "M5 未被杀死：变异体仍输出低置信提示"
    reference_section = _track_section(build_deterministic_report(payload))
    assert "同业基准参考性弱" in reference_section
