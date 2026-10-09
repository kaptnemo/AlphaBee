"""对标组存储测试（COMPANY_TRACK Phase D / Phase C3 存储基础）。"""

import pytest

from alphabee.company_track.peer_group_store import PeerGroup, PeerGroupStore


def test_save_load_roundtrip(tmp_path):
    store = PeerGroupStore(root=tmp_path)
    group = PeerGroup(
        symbol="603986.SH",
        codes=["002415.SZ", "688396.SH", "601138.SH"],
        source="manual",
        name="AI 服务器 ODM",
    )
    path = store.save(group)
    assert path.exists()
    assert path.name == "603986.SH.json"

    loaded = store.load("603986.SH")
    assert loaded is not None
    assert loaded.symbol == "603986.SH"
    assert loaded.codes == ["002415.SZ", "688396.SH", "601138.SH"]
    assert loaded.name == "AI 服务器 ODM"
    assert loaded.updated_at  # 保存时写入时间戳


def test_load_missing_returns_none(tmp_path):
    store = PeerGroupStore(root=tmp_path)
    assert store.load("999999.SH") is None


def test_save_overwrites_latest(tmp_path):
    store = PeerGroupStore(root=tmp_path)
    store.save(PeerGroup(symbol="600519.SH", codes=["000858.SZ"]))
    store.save(PeerGroup(symbol="600519.SH", codes=["000858.SZ", "600809.SH"]))
    loaded = store.load("600519.SH")
    assert loaded.codes == ["000858.SZ", "600809.SH"]  # latest-wins


def test_path_sanitized(tmp_path):
    store = PeerGroupStore(root=tmp_path)
    path = store.path_for("../evil")
    assert path.resolve().is_relative_to(tmp_path.resolve())


def test_is_empty():
    assert PeerGroup(symbol="x").is_empty() is True
    assert PeerGroup(symbol="x", codes=["A"]).is_empty() is False


def test_no_peers_flag_roundtrip(tmp_path):
    """no_peers 终态标记持久化（旧文件无该字段 → 默认 False，向后兼容）。"""
    store = PeerGroupStore(root=tmp_path)
    store.save(PeerGroup(symbol="301029.SZ", source="llm", no_peers=True, notes=["确无对标"]))
    loaded = store.load("301029.SZ")
    assert loaded is not None
    assert loaded.no_peers is True
    # 旧格式（无 no_peers 字段）→ False
    legacy = tmp_path / "600000.SH.json"
    legacy.write_text('{"symbol": "600000.SH", "codes": [], "source": "manual"}', encoding="utf-8")
    assert store.load("600000.SH").no_peers is False


# ── 判定 C：scores / match_dims 持久化（设计 §3.4 / §8 决策 2） ─────────────


def test_scores_and_match_dims_roundtrip(tmp_path):
    """scores（code→合成 overlap）与 match_dims（code→四维）save/load 往返一致。"""
    store = PeerGroupStore(root=tmp_path)
    dims = {"product": 0.9, "customer": 0.8, "material_tech": 0.7, "business_model": 0.6}
    store.save(
        PeerGroup(
            symbol="002916.SZ",
            codes=["002463.SZ"],
            source="llm",
            scores={"002463.SZ": 0.84},
            match_dims={"002463.SZ": dims},
        )
    )
    loaded = store.load("002916.SZ")
    assert loaded is not None
    assert loaded.scores == {"002463.SZ": pytest.approx(0.84)}
    assert loaded.match_dims == {"002463.SZ": dims}


def test_legacy_file_without_new_keys_loads_defaults(tmp_path):
    """旧文件（无 scores/match_dims 两键）仍可 load，且取默认空表（append 带默认，向后兼容）。"""
    store = PeerGroupStore(root=tmp_path)
    legacy = tmp_path / "600000.SH.json"
    legacy.write_text(
        '{"symbol": "600000.SH", "codes": ["000001.SZ"], "source": "manual", "notes": [], "reason_map": {}}',
        encoding="utf-8",
    )
    loaded = store.load("600000.SH")
    assert loaded is not None
    assert loaded.codes == ["000001.SZ"]
    assert loaded.scores == {} and loaded.match_dims == {}


def test_new_keys_default_to_empty_for_fresh_group():
    """新建 PeerGroup 默认空表（不引入必需参数 ⇒ 既有构造点零改动）。"""
    group = PeerGroup(symbol="x")
    assert group.scores == {} and group.match_dims == {}
