"""对标组 LLM 抽取与校验测试（COMPANY_TRACK Phase C，C2/C4）。"""

import alphabee.company_track.peer_group_build as build_module
from alphabee.company_track import (
    build_peer_group,
    extract_peer_candidates,
    normalize_peer_code,
    split_domestic_international,
)

# ── C2 LLM 抽取 ────────────────────────────────────────────────────────────


def test_extract_llm_success(monkeypatch):
    import alphabee.utils.llm as llm_module

    class FakeModel:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {
                    "content": (
                        '[{"name": "华勤技术", "code": "603296.SH", "exchange": "SH", '
                        '"reason": "同为 AI 服务器 ODM 龙头", "source": "#0"}, '
                        '{"name": "广达", "code": "2382.TW", "exchange": "TW", '
                        '"reason": "管理层点名竞对", "source": "#1"}]'
                    )
                },
            )()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    candidates, meta = extract_peer_candidates(
        "601138.SH", [], ["#0 公司管理层表示与华勤技术、广达等 ODM 厂商直接竞争"]
    )
    assert len(candidates) == 2
    assert candidates[0]["code"] == "603296.SH"
    assert candidates[1]["exchange"] == "TW"
    assert meta["raw"]


def test_extract_llm_failure_returns_empty(monkeypatch):
    import alphabee.utils.llm as llm_module

    def boom(component, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_module, "create_chat_model", boom)
    candidates, meta = extract_peer_candidates("601138.SH", [], ["片段"])
    assert candidates == []
    assert "失败" in meta["note"]


def test_extract_no_fragments_returns_empty():
    candidates, meta = extract_peer_candidates("601138.SH", [], [])
    assert candidates == []
    assert "无研报" in meta["note"]


def test_extract_disabled():
    candidates, meta = extract_peer_candidates("601138.SH", [], ["片段"], use_llm=False)
    assert candidates == []
    assert "关闭" in meta["note"]


# ── C4 代码规范化 ──────────────────────────────────────────────────────────


def test_normalize_peer_code():
    assert normalize_peer_code("002415.SZ") == ("002415.SZ", "SZ")
    assert normalize_peer_code("002415") == ("002415.SZ", "SZ")
    assert normalize_peer_code("600519") == ("600519.SH", "SH")
    assert normalize_peer_code("688396") == ("688396.SH", "SH")
    assert normalize_peer_code("430047") == ("430047.BJ", "BJ")
    assert normalize_peer_code("2382.TW") == ("2382.TW", "TW")
    assert normalize_peer_code("AAPL.O") == ("AAPL.O", "O")
    assert normalize_peer_code("") == (None, None)
    assert normalize_peer_code("2382") == (None, None)  # 4 位无后缀无法识别
    assert normalize_peer_code("abc") == (None, None)


def test_split_domestic_international():
    candidates = [
        {"name": "华勤技术", "code": "603296.SH", "reason": "同环节"},
        {"name": "广达", "code": "2382.TW", "reason": "境外竞对"},
        {"name": "未标注交易所", "code": "123456", "reason": ""},
    ]
    domestic, international, invalid = split_domestic_international(candidates)
    assert [c["code"] for c in domestic] == ["603296.SH"]
    assert [c["code"] for c in international] == ["2382.TW"]
    assert invalid == ["未标注交易所"]  # 123456 → 前缀 1 无法推断 → 剔除


# ── 端到端 build_peer_group ────────────────────────────────────────────────


def _patch_validation(monkeypatch, valid=None):
    # build_peer_group 顶部 import 绑定 → patch peer_group_build 命名空间；
    # fake 校验：返回 valid 子集 + 未通过列表
    def fake_validate(codes):
        v = valid if valid is not None else codes
        return v, [c for c in codes if c not in set(v)], None

    monkeypatch.setattr(build_module, "validate_a_share_codes", fake_validate)


def test_build_peer_group_manual_candidates(tmp_path, monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    store = PeerGroupStore(root=tmp_path)
    candidates = [
        {"name": "华勤技术", "code": "603296.SH", "reason": "AI 服务器 ODM 龙头"},
        {"name": "广达", "code": "2382.TW", "reason": "管理层点名竞对"},
        {"name": "坏代码", "code": "2382", "reason": ""},
    ]
    group, warnings = build_peer_group("601138.SH", candidates=candidates, name="AI 服务器 ODM", store=store)
    assert group.codes == ["603296.SH"]  # A 股进基准
    assert group.international == ["2382.TW"]  # 境外仅名单
    assert group.reason_map["603296.SH"] == "AI 服务器 ODM 龙头"
    assert any("无法识别" in w for w in warnings)
    assert group.source == "manual"

    # 已持久化且可读回
    loaded = store.load("601138.SH")
    assert loaded is not None
    assert loaded.codes == ["603296.SH"]
    assert loaded.international == ["2382.TW"]


def test_build_peer_group_a_share_invalid_dropped(tmp_path, monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch, valid=["603296.SH"])  # 603116 未通过
    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group(
        "601138.SH",
        candidates=[{"name": "华勤", "code": "603296.SH"}, {"name": "假代码", "code": "603116.SH"}],
        store=store,
    )
    assert group.codes == ["603296.SH"]
    assert any("未通过" in w for w in warnings)


def test_build_peer_group_no_candidates_empty(tmp_path):
    from alphabee.company_track.peer_group_store import PeerGroupStore

    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group("601138.SH", use_llm=False, store=store)
    assert group.is_empty()
    assert any("不编造" in w for w in warnings)
    assert store.load("601138.SH") is not None  # 空对标组也留痕


def test_build_peer_group_llm_candidates(tmp_path, monkeypatch):
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        build_module,
        "extract_peer_candidates",
        lambda symbol, segments, fragments, use_llm=True: (
            [{"name": "华勤技术", "code": "603296.SH", "reason": "LLM 命中"}],
            {"note": ""},
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group("601138.SH", fragments=["研报片段"], store=store)
    assert group.codes == ["603296.SH"]
    assert group.source == "llm"
