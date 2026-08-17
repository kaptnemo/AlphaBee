"""resolve_company_track 节点测试（COMPANY_TRACK Phase D3 在线注入）。"""

import asyncio

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


def _patch_peer_group(monkeypatch, group=None):
    import alphabee.company_track.peer_group_store as store_module

    class FakeStore:
        def __init__(self, *a, **k):
            pass

        def load(self, symbol):
            return group

    monkeypatch.setattr(store_module, "PeerGroupStore", FakeStore)


def _patch_derive(monkeypatch, values=None, meta=None):
    import alphabee.company_track.peer as peer_module

    monkeypatch.setattr(
        peer_module,
        "derive_peer_benchmarks",
        lambda codes, industry="": (values or {}, meta or {}),
    )


def _run_node(monkeypatch, group=None, values=None, meta=None, symbol="603986.SH"):
    _patch_peer_group(monkeypatch, group)
    if values is not None or meta is not None:
        _patch_derive(monkeypatch, values, meta)
    return asyncio.run(node.resolve_company_track(_state(symbol), {}))


# ── 无对标组 → 显式降级留痕 ────────────────────────────────────────────────


def test_no_peer_group_emits_missing_issue(monkeypatch):
    result = _run_node(monkeypatch, group=None)

    step = result["steps"][0]
    assert step.status.value == "skipped"  # 与 resolve_industry_context 的 unknown 路径一致
    issues = [i for i in result["issues"] if i.category == "company_track_missing"]
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM
    # 不发 artifact、不注入 peer_*（回退申万基线）
    assert not result.get("artifacts")
    assert not result.get("fact_values")


def test_no_symbol_skips_silently(monkeypatch):
    result = _run_node(monkeypatch, group=None, symbol=None)
    assert result["steps"][0].status.value == "skipped"
    assert "issues" not in result or result.get("issues") == []


# ── 有对标组 → 基准注入 ─────────────────────────────────────────────────────


def test_peer_group_injects_values_and_artifact(monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroup

    group = PeerGroup(symbol="603986.SH", codes=["002415.SZ", "688396.SH"], name="AI 服务器 ODM")
    values = {"peer_avg_roe": 0.15, "peer_avg_debt_ratio": 0.50}
    meta = {"peer_count": 2, "fetched_codes": ["002415.SZ"], "source_refs": ["peer_group:manual(2)"], "error": None}

    result = _run_node(monkeypatch, group=group, values=values, meta=meta)

    # peer_* 注入 fact_values（供 derived facts / signals 引用）
    assert result["fact_values"]["peer_avg_roe"] == 0.15
    assert result["fact_values"]["peer_avg_debt_ratio"] == 0.50

    # COMPANY_TRACK artifact 落 artifacts
    artifacts = result["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0].type == ArtifactType.COMPANY_TRACK
    payload = artifacts[0].value
    assert payload["peer_group"] == ["002415.SZ", "688396.SH"]
    assert payload["peer_benchmarks"]["peer_avg_roe"] == 0.15
    assert payload["degraded"] is False
    assert not [i for i in result["issues"] if "peer" in i.category]


def test_peer_derive_failure_marks_degraded(monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroup

    group = PeerGroup(symbol="603986.SH", codes=["002415.SZ"])
    result = _run_node(monkeypatch, group=group, values={}, meta={"error": "对标组财务指标均取数失败", "peer_count": 0})

    artifacts = result["artifacts"]
    assert artifacts[0].value["degraded"] is True
    assert "失败" in artifacts[0].value["degraded_reason"]
    issues = [i for i in result["issues"] if i.category == "peer_group_benchmarks_missing"]
    assert len(issues) == 1
    # 无基准可注入
    assert result["fact_values"] == {}
