"""对标组存储测试（COMPANY_TRACK Phase D / Phase C3 存储基础）。"""

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
