"""resolve_company_track 节点测试（COMPANY_TRACK Phase F 在线注入）。"""

import asyncio

import alphabee.company_track as ct_module
import alphabee.company_track.peer_group_store as store_module
from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.core import ArtifactType, IssueSeverity, Run, RunStatus
from alphabee.orchestrator.nodes import resolve_company_track as node


def _run(symbol="603986.SH"):
    return Run(
        id="run-1",
        goal="分析兆易创新",
        status=RunStatus.RUNNING,
        context={"symbol": symbol, "query": "分析兆易创新"},
    )


def _state(symbol="603986.SH"):
    return {"run": _run(symbol), "steps": [], "artifacts": [], "issues": [], "decisions": []}


def _track(**overrides) -> CompanyTrackArtifact:
    kwargs = dict(
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
        track_label="存储芯片",
        business_model="component",
        review_status="approved",
    )
    kwargs.update(overrides)
    return CompanyTrackArtifact(**kwargs)


def _patch_track(monkeypatch, track):
    monkeypatch.setattr(ct_module, "build_company_track", lambda *a, **k: track)


def _patch_peer_group(monkeypatch, group=None):
    class FakeStore:
        def __init__(self, *a, **k):
            pass

        def load(self, symbol):
            return group

    monkeypatch.setattr(store_module, "PeerGroupStore", FakeStore)


def _patch_derive(monkeypatch, values=None, meta=None):
    monkeypatch.setattr(
        ct_module,
        "derive_peer_benchmarks",
        lambda codes, industry="": (values or {}, meta or {}),
    )


def _run_node(monkeypatch, track, group=None, values=None, meta=None, symbol="603986.SH"):
    _patch_track(monkeypatch, track)
    _patch_peer_group(monkeypatch, group)
    if values is not None or meta is not None:
        _patch_derive(monkeypatch, values, meta)
    return asyncio.run(node.resolve_company_track(_state(symbol), {}))


def _find_company_track(result):
    for artifact in result.get("artifacts", []):
        if artifact.type == ArtifactType.COMPANY_TRACK:
            return CompanyTrackArtifact.model_validate(artifact.value)
    return None


# ── 降级分级 ───────────────────────────────────────────────────────────────


def test_no_track_degrades_with_missing_issue(monkeypatch):
    track = _track(segments=[], degraded=True, degraded_reason="双源均失败")
    result = _run_node(monkeypatch, track)

    assert result["steps"][0].status.value == "skipped"
    issues = [i for i in result["issues"] if i.category == "company_track_missing"]
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM
    assert not result.get("artifacts")


def test_track_without_peer_group_emits_low_issue(monkeypatch):
    result = _run_node(monkeypatch, _track(), group=None)

    artifact = _find_company_track(result)
    assert artifact is not None
    assert artifact.track_label == "存储芯片"
    issues = [i for i in result["issues"] if i.category == "peer_group_missing"]
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.LOW
    assert result["fact_values"] == {}


def test_peer_group_injects_values_and_full_artifact(monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroup

    group = PeerGroup(symbol="603986.SH", codes=["300223.SZ", "688766.SH"], name="存储芯片设计")
    values = {"peer_avg_roe": 0.039, "peer_avg_debt_ratio": 0.069}
    result = _run_node(monkeypatch, _track(), group=group, values=values, meta={"error": None, "peer_count": 2})

    assert result["fact_values"]["peer_avg_roe"] == 0.039
    artifact = _find_company_track(result)
    assert artifact.peer_group == ["300223.SZ", "688766.SH"]
    assert artifact.peer_benchmarks["peer_avg_roe"] == 0.039
    assert artifact.degraded is False


def test_peer_derive_failure_marks_degraded(monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroup

    group = PeerGroup(symbol="603986.SH", codes=["300223.SZ"])
    result = _run_node(monkeypatch, _track(), group=group, values={}, meta={"error": "对标组取数失败", "peer_count": 0})

    artifact = _find_company_track(result)
    assert artifact.degraded is True
    assert "失败" in artifact.degraded_reason
    issues = [i for i in result["issues"] if i.category == "peer_group_benchmarks_missing"]
    assert len(issues) == 1
    assert result["fact_values"] == {}


def test_stale_track_emits_stale_issue(monkeypatch):
    track = _track(stale_after="2000-01-01")
    result = _run_node(monkeypatch, track, group=None)

    artifact = _find_company_track(result)
    assert artifact.stale is True
    issues = [i for i in result["issues"] if i.category == "company_track_stale"]
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM


def test_no_symbol_skips(monkeypatch):
    _patch_track(monkeypatch, _track())
    _patch_peer_group(monkeypatch, None)
    result = asyncio.run(node.resolve_company_track(_state(None), {}))
    assert result["steps"][0].status.value == "skipped"
    assert "issues" not in result or result.get("issues") == []
