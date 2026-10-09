"""对标组 LLM 抽取与校验测试（COMPANY_TRACK Phase C，C2/C4）。"""

import json

import pytest

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
    assert group.codes == ["603296.SH"]  # A 股进基准（调用方给定候选不经 LLM 质量闸）
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
            [
                {
                    "name": "华勤技术",
                    "code": "603296.SH",
                    "reason": "LLM 命中",
                    "overlap": 0.9,
                    "dims": {"product": 0.9, "customer": 0.8, "material_tech": 0.7, "business_model": 0.9},
                    "verdict": "direct",
                }
            ],
            {"note": ""},
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group("601138.SH", fragments=["研报片段"], store=store)
    assert group.codes == ["603296.SH"]
    assert group.source == "llm"
    # 判定 C：保留者的合成 overlap 与四维**随组持久化**（供审计与后续重标定）
    # 记分 = 合成分（权重 × 四维）：0.9*0.4 + 0.8*0.3 + 0.7*0.2 + 0.9*0.1 = 0.85
    assert group.scores == {"603296.SH": pytest.approx(0.85)}
    assert group.match_dims["603296.SH"] == {
        "product": 0.9,
        "customer": 0.8,
        "material_tech": 0.7,
        "business_model": 0.9,
    }
    reloaded = store.load("601138.SH")
    assert reloaded is not None and reloaded.match_dims == group.match_dims


# ── 同行业成分股闭集择优（在线兜底） ────────────────────────────────────────


def test_build_peer_group_universe_path(tmp_path, monkeypatch):
    """无 fragments 但有同行业闭集 ⇒ 走闭集择优，source=llm。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    captured: list[list[dict]] = []

    def fake_select(symbol, segments, universe, industry="", use_llm=True):
        captured.append(universe)
        return (
            [
                {
                    "name": "北京君正",
                    "code": "300223.SZ",
                    "reason": "存储芯片设计同环节",
                    "overlap": 0.88,
                    "dims": {"product": 0.9, "customer": 0.8, "material_tech": 0.8, "business_model": 0.9},
                    "verdict": "direct",
                }
            ],
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
            [
                {
                    "name": "沪电股份",
                    "code": "002463.SZ",
                    "reason": "通信设备 PCB 同环节",
                    "overlap": 0.9,
                    "dims": {"product": 0.9, "customer": 0.9, "material_tech": 0.8, "business_model": 0.9},
                    "verdict": "direct",
                }
            ],
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
    """剔除明细进 notes（可审计），空结果标记 no_peers 终态；drop 文本来自确定性 Gate。"""
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
                        "overlap": 0.15,
                        "dims": {"product": 0.2, "customer": 0.3, "material_tech": 0.1, "business_model": 0.4},
                        "drop": "产品重叠不足",
                        "reason": "高洁净管件，终端错配",
                    }
                ],
            },
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, _ = build_peer_group("002318.SZ", business_description="工业不锈钢管", store=store)

    assert group.is_empty() and group.no_peers is True
    assert any("质量闸剔除 300260.SZ 新莱应材" in n and "产品重叠不足" in n for n in group.notes)


def test_build_peer_group_remaps_stale_bj_code_by_name(tmp_path, monkeypatch):
    """陈旧北交所代码（873593.BJ→920593.BJ）按公司名回查当前代码，不再误剔。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    monkeypatch.setattr(
        build_module,
        "infer_peer_candidates",
        lambda *a, **k: (
            [
                {
                    "name": "鼎智科技",
                    "code": "873593.BJ",
                    "exchange": "BJ",
                    "reason": "直线运动",
                    "source": "infer",
                    "overlap": 0.8,
                    "dims": {"product": 0.8, "customer": 0.8, "material_tech": 0.8, "business_model": 0.9},
                    "verdict": "direct",
                }
            ],
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
                        '[{"name": "沪电股份", "code": "002463.SZ", "exchange": "SZ", "overlap": 0.9, '
                        '"dims": {"product": 0.9, "customer": 0.8, "material_tech": 0.8, "business_model": 0.9}, '
                        '"verdict": "direct", "reason": "PCB 同环节"}, '
                        '{"name": "沪电股份", "code": "002463.SZ", "reason": "重复"}]'
                    )
                },
            )()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    candidates, _ = infer_peer_candidates("002916.SZ", [], "印制电路板与封装基板", industry="PCB")
    assert len(candidates) == 1
    assert candidates[0]["code"] == "002463.SZ"
    assert candidates[0]["source"] == "infer"
    # 判定 C（设计 §4）：生成器产出候选 + overlap + 结构化 dims（四维 JSON 文本），供 Gate 消费
    assert list(candidates[0]) == ["name", "code", "exchange", "reason", "source", "overlap", "dims"]
    assert set(json.loads(candidates[0]["dims"])) == {
        "product",
        "customer",
        "material_tech",
        "business_model",
    }


def _fake_infer(monkeypatch, content: str):
    import alphabee.utils.llm as llm_module

    class FakeModel:
        def invoke(self, prompt):
            return type("R", (), {"content": content})()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())


def test_generator_emits_all_scored_candidates_and_gate_does_the_dropping(tmp_path, monkeypatch):
    """判定分工（设计 §4）：**生成器只产出候选**（带 overlap），**剔除由构建阶段 Gate 执行**。

    端到端剔除以仍被覆盖：低 overlap 候选在 ``build_peer_group`` 被 Gate 剔除，
    notes 逐条写明 ``质量闸剔除 {code} {name}（overlap x < y）：{reason}``。
    """
    from alphabee.company_track import infer_peer_candidates
    from alphabee.company_track.peer_group_build import build_peer_group
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _DIMS = '"dims": {"product": 0.9, "customer": 0.8, "material_tech": 0.8, "business_model": 0.9}'
    payload = (
        '[{"name": "A", "code": "002463.SZ", "overlap": 0.9, "reason": "同环节直接竞争", '
        + _DIMS
        + ', "verdict": "direct"}, '
        '{"name": "B", "code": "600183.SH", "overlap": 0.3, "reason": "上游覆铜板", '
        + _DIMS
        + ', "verdict": "direct"}]'
    )

    # ① 生成器：两条都返回（不自行按 overlap 剔除）
    _fake_infer(monkeypatch, payload)
    candidates, meta = infer_peer_candidates("002916.SZ", [], "印制电路板与封装基板")
    assert [c["code"] for c in candidates] == ["002463.SZ", "600183.SH"]
    assert meta["dropped"] == []

    # ② 构建阶段：Gate 按合成 overlap 剔除低分候选，明细进 notes（可审计）
    monkeypatch.setattr(
        "alphabee.company_track.peer_group_build.infer_peer_candidates",
        lambda *a, **k: (
            [
                {"name": "A", "code": "002463.SZ", "reason": "同环节直接竞争", "source": "infer", "overlap": 0.9},
                {"name": "B", "code": "600183.SH", "reason": "上游覆铜板", "source": "infer", "overlap": 0.3},
            ],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    monkeypatch.setattr(
        "alphabee.company_track.peer_group_build.validate_a_share_codes",
        lambda codes: (list(codes), [], None),
    )
    group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))
    assert group.codes == ["002463.SZ"]
    assert any("质量闸剔除 600183.SH B（" in n and "上游覆铜板" in n for n in warnings)
    assert "600183.SH" not in group.scores


def test_infer_peer_candidates_keeps_reason_variants(monkeypatch):
    """**措辞不是判据**：理由里含旧否决词表措辞（下游偏/而非/部分重叠）仍按 overlap 决定去留。"""
    from alphabee.company_track import infer_peer_candidates

    _fake_infer(
        monkeypatch,
        '[{"name": "武进不锈", "code": "603878.SH", "overlap": 0.85, '
        '"reason": "同属不锈钢管，部分重叠于电站锅炉领域，而非石化"}]',
    )
    candidates, meta = infer_peer_candidates("002318.SZ", [], "工业不锈钢管")
    assert [c["code"] for c in candidates] == ["603878.SH"]
    assert meta["dropped"] == []

    # 低 overlap 也不再由生成器剔除（判定移交构建阶段 Gate，设计 §4）
    _fake_infer(
        monkeypatch,
        '[{"name": "新莱应材", "code": "300260.SZ", "overlap": 0.3, '
        '"reason": "同属不锈钢管件，但下游偏半导体，而非石化"}]',
    )
    candidates, meta = infer_peer_candidates("002318.SZ", [], "工业不锈钢管")
    assert [c["code"] for c in candidates] == ["300260.SZ"]
    assert meta["dropped"] == []


def test_infer_peer_candidates_accepts_percentage_overlap(monkeypatch):
    from alphabee.company_track import infer_peer_candidates

    _fake_infer(
        monkeypatch,
        '[{"name": "A", "code": "002463.SZ", "overlap": 85, "reason": "同环节", "dims": {"product": 0.9, '
        '"customer": 0.8, "material_tech": 0.8, "business_model": 0.9}, "verdict": "direct"}]',
    )
    candidates, _ = infer_peer_candidates("002916.SZ", [], "PCB")
    assert [c["code"] for c in candidates] == ["002463.SZ"]


def test_infer_peer_candidates_missing_overlap_not_dropped(monkeypatch):
    """LLM 漏给 overlap（None）不因阈值剔除，交给理由自洽兜底。"""
    from alphabee.company_track import infer_peer_candidates

    _fake_infer(
        monkeypatch,
        '[{"name": "A", "code": "002463.SZ", "reason": "同环节直接竞争", "dims": {"product": 0.9, '
        '"customer": 0.8, "material_tech": 0.8, "business_model": 0.9}, "verdict": "direct"}]',
    )
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


# ── Gate 清空 ⇒ 空组必须置 no_peers 终态（设计 §3.6） ──────────────────────


def test_gate_emptied_group_sets_no_peers_terminal(tmp_path, monkeypatch):
    """Gate 把生成器给出的候选**全部剔除** ⇒ 空组必须置 ``no_peers`` 终态。

    与「生成器零候选」分支同属终态空组（设计 §3.6：生成器与 judge 均有效响应且无保留）；
    未置位会让 ``resolve_company_track`` 每次分析都重复走在线兜底并重复调用 LLM。

    判别力：去掉该分支的 ``no_peers=...`` ⇒ 本用例必红。
    """
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        build_module,
        "infer_peer_candidates",
        lambda *a, **k: (
            [
                {
                    "name": "新莱应材",
                    "code": "300260.SZ",
                    "reason": "高洁净管件，终端错配",
                    "overlap": 0.15,
                    "dims": {"product": 0.2, "customer": 0.3, "material_tech": 0.1, "business_model": 0.4},
                    "verdict": "adjacent",
                }
            ],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, _warnings = build_peer_group("002318.SZ", business_description="工业不锈钢管", store=store)

    assert group.is_empty()
    assert group.no_peers is True, "Gate 清空后未置 no_peers 终态 ⇒ 每次分析重复调用 LLM"
    assert store.load("002318.SZ").no_peers is True, "落盘终态须同样携带 no_peers"
    assert any("质量闸后无保留候选" in note for note in group.notes)


def test_gate_emptied_but_llm_failed_does_not_set_no_peers(tmp_path, monkeypatch):
    """对照：LLM 未有效响应（``llm_ok=False``）时**不得**置 ``no_peers``，以便下次重试。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        build_module,
        "infer_peer_candidates",
        lambda *a, **k: (
            [{"name": "新莱应材", "code": "300260.SZ", "reason": "x", "overlap": 0.15, "verdict": "adjacent"}],
            {"note": "LLM 推断失败: 超时", "llm_ok": False, "dropped": []},
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, _warnings = build_peer_group("002318.SZ", business_description="工业不锈钢管", store=store)

    assert group.is_empty() and group.no_peers is False
