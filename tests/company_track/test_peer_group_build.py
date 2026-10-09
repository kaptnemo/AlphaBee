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


# ── 同行业成分股闭集择优（在线兜底） ────────────────────────────────────────


def test_build_peer_group_universe_path(tmp_path, monkeypatch):
    """无 fragments 但有同行业闭集 ⇒ 走闭集择优，source=llm。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    captured: list[list[dict]] = []

    def fake_select(symbol, segments, universe, industry="", use_llm=True):
        captured.append(universe)
        return (
            [{"name": "北京君正", "code": "300223.SZ", "reason": "存储芯片设计同环节"}],
            {"note": ""},
        )

    monkeypatch.setattr(build_module, "select_peer_candidates", fake_select)
    store = PeerGroupStore(root=tmp_path)
    group, _ = build_peer_group(
        "603986.SH",
        universe_codes=["300223.SZ", "688766.SH"],
        industry="半导体",
        store=store,
    )

    assert group.codes == ["300223.SZ"]
    assert group.source == "llm"
    # 闭集经 build_peer_universe 补全为 [{code, name}]，且**排除标的自身**
    assert {item["code"] for item in captured[0]} == {"300223.SZ", "688766.SH"}


def test_build_peer_group_business_description_path(tmp_path, monkeypatch):
    """有公司业务描述 ⇒ 走 LLM 推断，source=llm（优先于闭集与片段）。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        build_module,
        "infer_peer_candidates",
        lambda symbol, segments, business_description, industry="", use_llm=True: (
            [{"name": "沪电股份", "code": "002463.SZ", "reason": "通信设备 PCB 同环节"}],
            {"note": ""},
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, _ = build_peer_group(
        "002916.SZ",
        business_description="公司主营印制电路板与封装基板……",
        universe_codes=["600183.SH"],
        store=store,
    )
    assert group.codes == ["002463.SZ"]
    assert group.source == "llm"


def test_build_peer_group_notes_list_dropped_details(tmp_path, monkeypatch):
    """质量闸剔除明细进 notes（可审计），空结果标记 no_peers 终态。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    monkeypatch.setattr(
        build_module,
        "infer_peer_candidates",
        lambda *a, **k: (
            [],
            {
                "note": "质量闸剔除 1 条",
                "llm_ok": True,
                "dropped": [
                    {
                        "name": "新莱应材",
                        "code": "300260.SZ",
                        "overlap": 0.45,
                        "drop": "理由自曝实质差异",
                        "reason": "下游偏半导体、医药洁净应用",
                    }
                ],
            },
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, _ = build_peer_group("002318.SZ", business_description="工业不锈钢管", store=store)

    assert group.is_empty() and group.no_peers is True
    assert any("质量闸剔除 300260.SZ 新莱应材" in n and "下游偏半导体" in n for n in group.notes)


def test_build_peer_group_remaps_stale_bj_code_by_name(tmp_path, monkeypatch):
    """陈旧北交所代码（873593.BJ→920593.BJ）按公司名回查当前代码，不再误剔。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    monkeypatch.setattr(
        build_module,
        "infer_peer_candidates",
        lambda *a, **k: (
            [{"name": "鼎智科技", "code": "873593.BJ", "exchange": "BJ", "reason": "直线运动", "source": "infer"}],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    monkeypatch.setattr(build_module, "validate_a_share_codes", lambda codes: ([], list(codes), None))
    monkeypatch.setattr(
        build_module,
        "resolve_current_code_by_name",
        lambda name: "920593.BJ" if name == "鼎智科技" else None,
    )
    store = PeerGroupStore(root=tmp_path)
    group, _ = build_peer_group("301029.SZ", business_description="FA 零部件", store=store)

    assert group.codes == ["920593.BJ"]
    assert any("873593.BJ → 920593.BJ" in n for n in group.notes)


def test_infer_peer_candidates_source_and_dedup(monkeypatch):
    import alphabee.utils.llm as llm_module
    from alphabee.company_track import infer_peer_candidates

    class FakeModel:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {
                    "content": (
                        '[{"name": "沪电股份", "code": "002463.SZ", "exchange": "SZ", "reason": "PCB 同环节"}, '
                        '{"name": "沪电股份", "code": "002463.SZ", "reason": "重复"}]'
                    )
                },
            )()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    candidates, _ = infer_peer_candidates("002916.SZ", [], "印制电路板与封装基板", industry="PCB")
    assert len(candidates) == 1
    assert candidates[0]["code"] == "002463.SZ"
    assert candidates[0]["source"] == "infer"


def _fake_infer(monkeypatch, content: str):
    import alphabee.utils.llm as llm_module

    class FakeModel:
        def invoke(self, prompt):
            return type("R", (), {"content": content})()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())


def test_infer_peer_candidates_drops_low_overlap(monkeypatch):
    from alphabee.company_track import infer_peer_candidates

    _fake_infer(
        monkeypatch,
        '[{"name": "A", "code": "002463.SZ", "overlap": 0.9, "reason": "同环节直接竞争"}, '
        '{"name": "B", "code": "600183.SH", "overlap": 0.3, "reason": "上游覆铜板"}]',
    )
    candidates, meta = infer_peer_candidates("002916.SZ", [], "印制电路板与封装基板")
    assert [c["code"] for c in candidates] == ["002463.SZ"]
    assert meta["dropped"][0]["code"] == "600183.SH"
    assert "overlap" in meta["dropped"][0]["drop"]


def test_infer_peer_candidates_drops_self_contradicting_reason(monkeypatch):
    """理由自曝终端/材料差异 ⇒ 即便 overlap 高也剔除。"""
    from alphabee.company_track import infer_peer_candidates

    _fake_infer(
        monkeypatch,
        '[{"name": "新莱应材", "code": "300260.SZ", "overlap": 0.9, '
        '"reason": "同属不锈钢管件，但下游偏半导体、医药、食品等洁净应用"}]',
    )
    candidates, meta = infer_peer_candidates("002318.SZ", [], "工业不锈钢管")
    assert candidates == []
    assert meta["dropped"][0]["drop"] == "理由自曝实质差异"
    assert "未推断出" in meta["note"]


def test_infer_peer_candidates_accepts_percentage_overlap(monkeypatch):
    from alphabee.company_track import infer_peer_candidates

    _fake_infer(monkeypatch, '[{"name": "A", "code": "002463.SZ", "overlap": 85, "reason": "同环节"}]')
    candidates, _ = infer_peer_candidates("002916.SZ", [], "PCB")
    assert [c["code"] for c in candidates] == ["002463.SZ"]


def test_infer_peer_candidates_missing_overlap_not_dropped(monkeypatch):
    """LLM 漏给 overlap（None）不因阈值剔除，交给理由自洽兜底。"""
    from alphabee.company_track import infer_peer_candidates

    _fake_infer(monkeypatch, '[{"name": "A", "code": "002463.SZ", "reason": "同环节直接竞争"}]')
    candidates, _ = infer_peer_candidates("002916.SZ", [], "PCB")
    assert [c["code"] for c in candidates] == ["002463.SZ"]


def test_select_peer_candidates_drops_out_of_set_codes(monkeypatch):
    """LLM 返回闭集外代码 ⇒ 一律丢弃（防幻觉/代码漂移）。"""
    import alphabee.utils.llm as llm_module
    from alphabee.company_track import select_peer_candidates

    class FakeModel:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {
                    "content": (
                        '[{"code": "300223.SZ", "reason": "同环节"}, {"code": "999999.SZ", "reason": "幻觉代码"}]'
                    )
                },
            )()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    universe = [{"code": "300223.SZ", "name": "北京君正"}, {"code": "688766.SH", "name": "普冉股份"}]
    candidates, _ = select_peer_candidates("603986.SH", [], universe, industry="半导体")
    assert [c["code"] for c in candidates] == ["300223.SZ"]
    assert candidates[0]["name"] == "北京君正"
