"""resolve_company_track 节点测试（COMPANY_TRACK Phase F 在线注入）。"""

import asyncio

import alphabee.company_track as ct_module
import alphabee.company_track.peer_group_store as store_module
import alphabee.company_track.peer_report as report_module
from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.core import Artifact, ArtifactType, IssueSeverity, Run, RunStatus
from alphabee.orchestrator.contracts import IndustryContextArtifact
from alphabee.orchestrator.nodes import resolve_company_track as node


def _run(symbol="603986.SH"):
    return Run(
        id="run-1",
        goal="分析兆易创新",
        status=RunStatus.RUNNING,
        context={"symbol": symbol, "query": "分析兆易创新"},
    )


def _state(symbol="603986.SH", universe=None):
    artifacts: list[Artifact] = []
    if universe is not None:
        artifacts.append(
            Artifact(
                id="art-industry",
                type=ArtifactType.INDUSTRY_CONTEXT,
                producer_step="resolve_industry_context",
                value=IndustryContextArtifact(
                    industry="半导体",
                    sw_code="850811.SI",
                    peer_universe=list(universe),
                ).model_dump(mode="json"),
            )
        )
    return {"run": _run(symbol), "steps": [], "artifacts": artifacts, "issues": [], "decisions": []}


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


def _patch_build(monkeypatch, group, captured=None):
    """在线兜底 build_peer_group 打桩：返回给定对标组（不落盘），可选捕获调用 kwargs。"""

    def _fake(symbol, **kwargs):
        if captured is not None:
            captured.append(kwargs)
        return group, []

    monkeypatch.setattr(ct_module, "build_peer_group", _fake)


def _patch_report_fragments(monkeypatch, fragments=None):
    """本地财报片段打桩（默认无片段 ⇒ 退闭集路径，隔离真实 reports/）。"""
    monkeypatch.setattr(
        report_module,
        "fetch_local_report_fragments",
        lambda symbol, **kwargs: (fragments or [], {"note": "无本地报告"}),
    )


def _run_node(
    monkeypatch,
    track,
    group=None,
    values=None,
    meta=None,
    symbol="603986.SH",
    universe=None,
    fragments=None,
    built=None,
    captured=None,
):
    _patch_track(monkeypatch, track)
    _patch_peer_group(monkeypatch, group)
    _patch_report_fragments(monkeypatch, fragments)
    if built is not None:
        _patch_build(monkeypatch, built, captured)
    if values is not None or meta is not None:
        _patch_derive(monkeypatch, values, meta)
    return asyncio.run(node.resolve_company_track(_state(symbol, universe), {}))


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


def test_track_without_peer_group_online_fallback_builds(monkeypatch):
    """存储未命中 + 有同行业闭集 ⇒ 闭集择优构建对标组并注入，不再报 peer_group_missing。"""
    from alphabee.company_track.peer_group_store import PeerGroup

    built = PeerGroup(
        symbol="603986.SH",
        codes=["300223.SZ", "688766.SH"],
        name="存储芯片设计",
        source="llm",
    )
    values = {"peer_avg_roe": 0.041}
    captured: list[dict] = []
    result = _run_node(
        monkeypatch,
        _track(),
        group=None,
        universe=["300223.SZ", "688766.SH"],
        built=built,
        values=values,
        meta={"error": None, "peer_count": 2},
        captured=captured,
    )

    assert result["fact_values"]["peer_avg_roe"] == 0.041
    artifact = _find_company_track(result)
    assert artifact.peer_group == ["300223.SZ", "688766.SH"]
    assert artifact.peer_group_source == "llm"
    assert not [i for i in result["issues"] if i.category == "peer_group_missing"]
    # 闭集来自 IndustryContextArtifact.peer_universe，原样透传给 build_peer_group
    assert captured[0]["universe_codes"] == ["300223.SZ", "688766.SH"]


def test_track_without_peer_group_local_report_fragments_path(monkeypatch):
    """有本地财报片段 ⇒ 透传给 build_peer_group（优先于闭集），并注入结果。"""
    from alphabee.company_track.peer_group_store import PeerGroup

    built = PeerGroup(symbol="603986.SH", codes=["300223.SZ", "688766.SH"], source="llm")
    captured: list[dict] = []
    section = "第三节 管理层讨论与分析：公司主营存储芯片……"
    result = _run_node(
        monkeypatch,
        _track(),
        group=None,
        universe=["688766.SH"],
        fragments=[section],
        built=built,
        values={"peer_avg_roe": 0.05},
        meta={"error": None, "peer_count": 2},
        captured=captured,
    )

    assert captured[0]["business_description"] == section
    assert captured[0]["universe_codes"] == ["688766.SH"]  # 兜底闭集仍透传
    assert result["fact_values"]["peer_avg_roe"] == 0.05
    assert not [i for i in result["issues"] if i.category == "peer_group_missing"]


def test_track_no_peers_terminal_skips_online_fallback(monkeypatch):
    """空对标组且已判定 no_peers ⇒ 不再重试在线兜底（避免每次重复调用 LLM）。"""
    from alphabee.company_track.peer_group_store import PeerGroup

    terminal = PeerGroup(symbol="603986.SH", source="llm", no_peers=True, notes=["确无对标"])
    called: list[object] = []
    monkeypatch.setattr(ct_module, "build_peer_group", lambda *a, **k: called.append(True) or (None, []))

    result = _run_node(monkeypatch, _track(), group=terminal, universe=["300223.SZ"], fragments=["业务描述"])

    assert called == []
    issues = [i for i in result["issues"] if i.category == "peer_group_missing"]
    assert len(issues) == 1


def test_track_without_peer_group_no_universe_skips_build(monkeypatch):
    """无同行业闭集 ⇒ 不触发构建（不编造），仍走 peer_group_missing 降级。"""
    called: list[object] = []
    monkeypatch.setattr(ct_module, "build_peer_group", lambda *a, **k: called.append(True) or (None, []))

    result = _run_node(monkeypatch, _track(), group=None, universe=None)

    assert called == []
    issues = [i for i in result["issues"] if i.category == "peer_group_missing"]
    assert len(issues) == 1
    assert result["fact_values"] == {}


def test_track_without_peer_group_online_build_empty_falls_back(monkeypatch):
    """有闭集但择优为空对标组 ⇒ 不注入，仍走 peer_group_missing 降级。"""
    from alphabee.company_track.peer_group_store import PeerGroup

    empty = PeerGroup(symbol="603986.SH", source="llm")
    result = _run_node(monkeypatch, _track(), group=None, universe=["300223.SZ", "688766.SH"], built=empty)

    assert result["fact_values"] == {}
    issues = [i for i in result["issues"] if i.category == "peer_group_missing"]
    assert len(issues) == 1


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


def test_min_peers_gate_blocks_single_candidate(monkeypatch):
    """保留数 < min_peers(2) ⇒ 不注入任何 peer_*、记 peer_group_missing、notes 写明『中位数不可比』。"""
    from alphabee.company_track.peer_group_store import PeerGroup

    group = PeerGroup(symbol="603986.SH", codes=["300223.SZ"], source="llm")
    result = _run_node(
        monkeypatch,
        _track(),
        group=group,
        values={"peer_avg_roe": 0.05},
        meta={"error": None, "peer_count": 1},
    )

    assert result["fact_values"] == {}
    issues = [i for i in result["issues"] if i.category == "peer_group_missing"]
    assert len(issues) == 1
    assert "对标组不足" in issues[0].message and "中位数不可比" in issues[0].message
    artifact = _find_company_track(result)
    assert artifact is not None
    assert artifact.peer_group == []  # 不注入：中位数不可比
    assert any("对标组不足" in note for note in group.notes)


def test_min_peers_gate_allows_two_candidates(monkeypatch):
    """保留数 == min_peers(2) ⇒ 正常注入（边界不误杀）。"""
    from alphabee.company_track.peer_group_store import PeerGroup

    group = PeerGroup(symbol="603986.SH", codes=["300223.SZ", "688766.SH"], source="llm")
    result = _run_node(
        monkeypatch,
        _track(),
        group=group,
        values={"peer_avg_roe": 0.05},
        meta={"error": None, "peer_count": 2},
    )

    assert result["fact_values"]["peer_avg_roe"] == 0.05
    assert not [i for i in result["issues"] if i.category == "peer_group_missing"]


def test_min_peers_gate_reads_config(monkeypatch):
    """min_peers 由 company_track.peer_quality 配置驱动：调到 3 时 2 只候选也回退。"""
    from alphabee.company_track.peer_group_store import PeerGroup

    monkeypatch.setattr(node, "_min_peers", lambda: 3)
    group = PeerGroup(symbol="603986.SH", codes=["300223.SZ", "688766.SH"], source="llm")
    result = _run_node(
        monkeypatch,
        _track(),
        group=group,
        values={"peer_avg_roe": 0.05},
        meta={"error": None, "peer_count": 2},
    )

    assert result["fact_values"] == {}
    assert [i for i in result["issues"] if i.category == "peer_group_missing"]


def test_peer_derive_failure_marks_degraded(monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroup

    group = PeerGroup(symbol="603986.SH", codes=["300223.SZ", "688766.SH"])
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
