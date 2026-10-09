"""对标组 LLM 抽取与校验测试（COMPANY_TRACK Phase C，C2/C4）。"""

import json

import pytest

import alphabee.company_track.peer_group_build as build_module
from alphabee import PROJECT_ROOT
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


@pytest.fixture(autouse=True)
def _taxonomy_cache_isolation():
    """每个用例前后清空分类学快照缓存（合成快照的用例不得污染后续用例的缓存）。"""
    import alphabee.company_track.peer_taxonomy as taxo

    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()
    yield
    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()


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
    """有公司业务描述 ⇒ 走 LLM 推断，source=llm（优先于闭集与片段）。

    judge 显式关闭：本用例考察**来源优先级**（判定 D 的接线另有专测见文件末段）。
    """
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_off(monkeypatch)
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
    """陈旧北交所代码（873593.BJ→920593.BJ）按公司名回查当前代码，不再误剔。

    judge 显式关闭：本用例考察 A 股存在性校验与按名回查，judge 接线另有专测。
    """
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _judge_off(monkeypatch)
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

    judge 显式关闭：本用例的判据是「生成器不自行剔除 + Gate 执行剔除」，
    judge 生效态的消费口径见文件末段 ``test_judge_applied_*``。
    """
    from alphabee.company_track import infer_peer_candidates
    from alphabee.company_track.peer_group_build import build_peer_group
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _judge_off(monkeypatch)
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
    （judge 显式关闭：本条钉的是生成器口径下的终态；judge 生效态的终态见
    ``test_judge_ok_with_empty_kept_sets_no_peers`` 与 ``test_judge_degraded_never_sets_no_peers``。）
    """
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_off(monkeypatch)
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
    _taxonomy_off(monkeypatch)
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
    _judge_off(monkeypatch)
    _taxonomy_off(monkeypatch)
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


# ── 判定 D：独立 batch judge 接线（生成/判定解耦 + fail-open，设计 §3.2/§3.6/§4） ──


def _judge_off(monkeypatch) -> None:
    """把 ``company_track.peer_quality.judge_enabled`` 置 False（判据与 judge 无关的用例用）。"""
    from alphabee import config as config_module

    settings = config_module.get_settings().model_copy(deep=True)
    settings.company_track.peer_quality.judge_enabled = False
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    build_module._PEER_QUALITY_CACHE.clear()


def _judge_on(monkeypatch, *, batch_size: int = 20) -> None:
    """显式打开 judge（并把 batch_size 写进配置），供批量参数断言用。"""
    from alphabee import config as config_module

    settings = config_module.get_settings().model_copy(deep=True)
    settings.company_track.peer_quality.judge_enabled = True
    settings.company_track.peer_quality.judge_batch_size = batch_size
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    build_module._PEER_QUALITY_CACHE.clear()


def _taxonomy_off(monkeypatch) -> None:
    """把 ``company_track.peer_quality.taxonomy_enabled`` 置 False（判据与判定 E 无关的用例用）。

    分类学召回池会把「同 L3/L2 成分」并入候选池并逐条注入特征，凡以判定 C/D 为判据的用例
    都关掉它以免被池规模影响；判定 E 有专测（见文件末段）。
    """
    from alphabee import config as config_module

    settings = config_module.get_settings().model_copy(deep=True)
    settings.company_track.peer_quality.taxonomy_enabled = False
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    build_module._PEER_QUALITY_CACHE.clear()


def _patch_infer(monkeypatch, candidates: list[dict], *, llm_ok: bool = True) -> None:
    monkeypatch.setattr(
        build_module,
        "infer_peer_candidates",
        lambda *a, **k: (list(candidates), {"note": "", "llm_ok": llm_ok, "dropped": []}),
    )


def _patch_judge(monkeypatch, *, rows: dict[str, dict] | None = None, ok: bool = True) -> list[dict]:
    """patch 生产 judge 入口（**不调真 LLM**），返回逐次调用记录。"""
    from alphabee.company_track import peer_judge

    calls: list[dict] = []

    def fake(description, candidates, *, batch_size=peer_judge.JUDGE_BATCH_SIZE_DEFAULT, model=None):
        calls.append({"description": description, "pool": [dict(c) for c in candidates], "batch_size": batch_size})
        if ok:
            return peer_judge.JudgeReport(
                results=dict(rows or {}),
                batches=1,
                failed_batches=0,
                missing_codes=(),
                out_of_set_codes=(),
                errors=(),
                called=True,
            )
        missing = tuple(str(c.get("code")) for c in candidates)
        return peer_judge.JudgeReport(
            results={},
            batches=1,
            failed_batches=1,
            missing_codes=missing,
            errors=("batch#0: RuntimeError: llm down",),
            called=True,
        )

    monkeypatch.setattr(peer_judge, "judge_peer_candidates_batched", fake)
    return calls


def _gen_candidate(code: str, name: str, product: float) -> dict:
    """生成器候选：故意让**生成器维分**与 judge 维分不同，用于证明 Gate 消费的是哪一侧。"""
    return {
        "name": name,
        "code": code,
        "reason": f"{name} 同环节",
        "source": "infer",
        "overlap": 0.9,
        "dims": {"product": product, "customer": product, "material_tech": product, "business_model": product},
    }


def _judge_row(verdict: str, product: float, customer: float = 0.9) -> dict:
    return {
        "verdict": verdict,
        "dims": {"product": product, "customer": customer, "material_tech": 0.9, "business_model": 0.9},
        "reason": "匹配点：同环节；不匹配点：无",
    }


def test_judge_applied_gate_consumes_judge_verdict_and_dims(tmp_path, monkeypatch):
    """judge 生效：Gate 采纳 **judge 的 verdict 与 dims**（不是生成器自评分）。

    构造三只候选，使结论只可能来自 judge 侧：
    - ``KEEP``：生成器给 product=0.05（本会被产品下限剔），judge 给 0.9/direct ⇒ 必须保留；
    - ``REJECT``：生成器给 0.95（本会保留），judge 给 reject（dims 全 1.0）⇒ 必须按 ``judge reject`` 剔；
    - ``FLOOR``：生成器 0.95，judge 给 ``product=0.05``（direct）⇒ 必须按“产品重叠不足”剔。
    """
    from alphabee.company_track import peer_judge
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    _patch_infer(
        monkeypatch,
        [
            _gen_candidate("002463.SZ", "KEEP", 0.05),
            _gen_candidate("300476.SZ", "REJECT", 0.95),
            _gen_candidate("600183.SH", "FLOOR", 0.95),
        ],
    )
    calls = _patch_judge(
        monkeypatch,
        rows={
            "002463.SZ": _judge_row("direct", 0.9),
            "300476.SZ": _judge_row("reject", 1.0),
            "600183.SH": _judge_row("direct", 0.05),
        },
    )
    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=store)

    # ① judge 被调用一次，入参是**生成器候选池**（闭集），且不含生成器分数
    assert len(calls) == 1
    assert [item["code"] for item in calls[0]["pool"]] == ["002463.SZ", "300476.SZ", "600183.SH"]
    assert all(set(item) == {"code", "name"} for item in calls[0]["pool"])

    # ② 保留集与剔除以 judge 为准
    assert group.codes == ["002463.SZ"]
    drops = {note.split()[1]: note for note in warnings if "质量闸剔除" in note}
    assert "judge reject" in drops["300476.SZ"]
    assert "产品重叠不足" in drops["600183.SH"]
    # ③ 持久化的维度 = judge 的四维（而非生成器 0.05）
    assert group.match_dims["002463.SZ"] == peer_judge.normalize_dims(_judge_row("direct", 0.9)["dims"])
    assert group.scores["002463.SZ"] == pytest.approx(0.9, abs=1e-6)


def test_judge_degraded_falls_back_to_generator_and_records_note(tmp_path, monkeypatch):
    """fail-open（设计 §3.6）：judge 降级 ⇒ **回退生成器分**继续走 Gate，notes 记 ``judge_degraded``。

    判别力（M2）：把 ``_run_judge`` 里的降级 note 去掉 ⇒ 本用例的 notes 断言必红（回退证据消失）。
    """
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    # 生成器认为 KEEP 高分 ⇒ 回退后仍应保留（若误把 judge 的空结果当"全剔"就会清空成空组）
    _patch_infer(monkeypatch, [_gen_candidate("002463.SZ", "KEEP", 0.9)])
    _patch_judge(monkeypatch, ok=False)
    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=store)

    assert group.codes == ["002463.SZ"], "judge 降级时未回退生成器分"
    degraded = [note for note in warnings if note.startswith("judge 降级")]
    assert degraded, "judge 降级未在 notes 记账（回退证据丢失）"
    assert "回退生成器分" in degraded[0] and "不置 no_peers" in degraded[0]
    assert "002463.SZ" in degraded[0]  # 未取得可用判定的候选被列名
    assert group.scores["002463.SZ"] == pytest.approx(0.9, abs=1e-6)  # 生成器维分（product=0.9 ⇒ 合成 0.9）


def test_judge_degraded_never_sets_no_peers(tmp_path, monkeypatch):
    """fail-open 硬要求（M1）：judge 失败 ⇒ **绝不置** ``no_peers``（可重试），即使 Gate 清空。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    # 生成器分低 ⇒ Gate 全剔；但 judge 降级 ⇒ 不得置终态
    _patch_infer(monkeypatch, [_gen_candidate("300260.SZ", "低分", 0.05)])
    _patch_judge(monkeypatch, ok=False)
    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group("002318.SZ", business_description="工业不锈钢管", store=store)

    assert group.is_empty()
    assert group.no_peers is False, "judge 降级却置 no_peers ⇒ 该标的永不重试（M1）"
    assert any(note.startswith("judge 降级") for note in warnings)


def test_judge_ok_with_empty_kept_sets_no_peers(tmp_path, monkeypatch):
    """双有效 + 无保留 ⇒ ``no_peers=True``；judge 未接线（配置关闭）时沿用生成器口径。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("300260.SZ", "低分", 0.05)])
    _patch_judge(monkeypatch, rows={"300260.SZ": _judge_row("reject", 0.05)}, ok=True)
    group, _ = build_peer_group("002318.SZ", business_description="工业不锈钢管", store=PeerGroupStore(root=tmp_path))
    assert group.is_empty() and group.no_peers is True

    # 对照：judge 关闭（回滚口径）⇒ 生成器有效响应且无保留即终态
    _judge_off(monkeypatch)
    group2, _ = build_peer_group("002318.SZ", business_description="工业不锈钢管", store=PeerGroupStore(root=tmp_path))
    assert group2.is_empty() and group2.no_peers is True


def test_judge_not_called_on_non_infer_paths(tmp_path, monkeypatch):
    """非生成器推断路径（研报片段抽取 / 闭集择优 / 人工直传）**不调 judge**，行为保持。"""
    from alphabee.company_track import peer_judge
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    calls = _patch_judge(monkeypatch, rows={})
    monkeypatch.setattr(
        build_module,
        "extract_peer_candidates",
        lambda *a, **k: (
            [{"name": "华勤技术", "code": "603296.SH", "reason": "管理层点名", "source": "#0"}],
            {"note": "", "llm_ok": True},
        ),
    )
    group, _ = build_peer_group("601138.SH", fragments=["片段"], store=PeerGroupStore(root=tmp_path))
    assert group.codes == ["603296.SH"]
    assert calls == [], "非推断路径不应调用独立 judge"
    assert peer_judge.JUDGE_BATCH_SIZE_DEFAULT == 20


def test_judge_batch_size_comes_from_config(tmp_path, monkeypatch):
    """批量上限取配置 ``judge_batch_size``（生产入口按批切分的入口参数）。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch, batch_size=2)
    _taxonomy_off(monkeypatch)
    _patch_infer(
        monkeypatch,
        [_gen_candidate("002463.SZ", "A", 0.9), _gen_candidate("300476.SZ", "B", 0.9)],
    )
    calls = _patch_judge(
        monkeypatch,
        rows={"002463.SZ": _judge_row("direct", 0.9), "300476.SZ": _judge_row("direct", 0.9)},
    )
    group, _ = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))
    assert len(calls) == 1 and calls[0]["batch_size"] == 2
    assert group.codes == ["002463.SZ", "300476.SZ"]


def test_judge_enabled_false_is_verbatim_rollback(tmp_path, monkeypatch):
    """回滚口径：``judge_enabled=false`` ⇒ 不调 judge、无降级 note、判定回到生成器口径。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_off(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("002463.SZ", "KEEP", 0.9)])
    calls = _patch_judge(monkeypatch, rows={"002463.SZ": _judge_row("reject", 1.0)})
    group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))
    assert calls == []
    assert group.codes == ["002463.SZ"], "judge 关闭时不应消费 judge 的 reject"
    assert not [note for note in warnings if "judge" in note]


def test_judge_is_off_by_default(tmp_path, monkeypatch):
    """**默认不启用**（DoD 负结果口径）：未改配置时生成器推断路径**不调用** judge，判定走 C 口径。

    这是负结果路径的落脚点：接线与配置保留（置 ``judge_enabled=true`` 即启用，见上一条用例），
    但生产缺省行为 = 判定 C（关键词表已删除的「结构化维度 + 确定性 Gate」口径）。
    """
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    build_module._PEER_QUALITY_CACHE.clear()  # 不 patch 配置：读真实缺省（judge_enabled=False）
    _patch_infer(monkeypatch, [_gen_candidate("002463.SZ", "KEEP", 0.9)])
    calls = _patch_judge(monkeypatch, rows={"002463.SZ": _judge_row("reject", 0.1)})
    group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))

    assert calls == [], "缺省配置下不应调用独立 judge"
    assert group.codes == ["002463.SZ"], "缺省口径应完全按生成器 dims 判定（C）"
    assert not [note for note in warnings if "judge" in note]


def _mutate_build(old: str, new: str, name: str, monkeypatch) -> None:
    """把 peer_group_build 源码变异后加载为模块（并把依赖换成 fake，保持密闭）。"""
    import importlib.util
    import sys
    import tempfile
    from pathlib import Path

    source = build_module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert text.count(old) == 1, f"变异锚点不唯一：{old!r}"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.py"
        path.write_text(text.replace(old, new), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        mutant = importlib.util.module_from_spec(spec)
        sys.modules[name] = mutant
        spec.loader.exec_module(mutant)
    monkeypatch.setattr(mutant, "validate_a_share_codes", lambda codes: (list(codes), [], None))


def test_m1_judge_failure_mis_setting_no_peers_is_killed(tmp_path, monkeypatch):
    """M1：judge 降级时误置 ``no_peers``（丢失"可重试"语义）⇒ 必红。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("300260.SZ", "低分", 0.05)])
    _patch_judge(monkeypatch, ok=False)
    _mutate_build(
        '        no_peers = bool(use_llm and meta.get("llm_ok") and (judge_ok if judge_called else True))',
        '        no_peers = bool(use_llm and meta.get("llm_ok"))  # M1 忽略 judge 降级',
        "pbg_mutant_m1",
        monkeypatch,
    )
    # 变异体：降级也置终态 ⇒ 用同一夹具跑变异模块，断言其 no_peers=True（说明判据能观察到该行为）
    import sys as _sys

    mutant = _sys.modules["pbg_mutant_m1"]
    monkeypatch.setattr(
        mutant,
        "infer_peer_candidates",
        lambda *a, **k: (
            [_gen_candidate("300260.SZ", "低分", 0.05)],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    group_mutant, _ = mutant.build_peer_group(
        "002318.SZ", business_description="工业不锈钢管", store=PeerGroupStore(root=tmp_path)
    )
    assert group_mutant.no_peers is True, "M1 未被杀死：变异体仍不置 no_peers"
    # 参照实现：同一输入下必须为 False
    group_ref, _ = build_module.build_peer_group(
        "002318.SZ", business_description="工业不锈钢管", store=PeerGroupStore(root=tmp_path)
    )
    assert group_ref.no_peers is False


def test_m2_dropping_judge_degraded_note_is_killed(tmp_path, monkeypatch):
    """M2：judge 降级时**丢掉回退证据**（不记 ``judge_degraded`` note）⇒ 必红。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("002463.SZ", "KEEP", 0.9)])
    _patch_judge(monkeypatch, ok=False)
    _mutate_build(
        "    if not report.ok:\n        note = report.note()\n        if note:\n            warnings.append(note)",
        "    if not report.ok:\n        # M2 丢弃降级证据（静默采纳生成分）",
        "pbg_mutant_m2",
        monkeypatch,
    )
    import sys as _sys

    mutant = _sys.modules["pbg_mutant_m2"]
    monkeypatch.setattr(
        mutant,
        "infer_peer_candidates",
        lambda *a, **k: (
            [_gen_candidate("002463.SZ", "KEEP", 0.9)],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    _group, warnings_mutant = mutant.build_peer_group(
        "002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path)
    )
    assert not [note for note in warnings_mutant if note.startswith("judge 降级")], "M2 未被杀死：变异体仍留有降级证据"
    _group_ref, warnings_ref = build_module.build_peer_group(
        "002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path)
    )
    assert any(note.startswith("judge 降级") for note in warnings_ref)


def test_m3_ignoring_judge_reject_is_killed(tmp_path, monkeypatch):
    """M3：Gate 忽略 ``verdict=="reject"``（只按 dims 判）⇒ 必红（未变异的实现必须剔）。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("300476.SZ", "REJECT", 0.95)])
    _patch_judge(monkeypatch, rows={"300476.SZ": _judge_row("reject", 1.0)})
    _mutate_build(
        '        elif verdict == "reject":\n            drop = DROP_JUDGE_REJECT',
        "        elif False:  # M3 忽略 judge reject\n            drop = DROP_JUDGE_REJECT",
        "pbg_mutant_m3",
        monkeypatch,
    )
    import sys as _sys

    mutant = _sys.modules["pbg_mutant_m3"]
    monkeypatch.setattr(
        mutant,
        "infer_peer_candidates",
        lambda *a, **k: (
            [_gen_candidate("300476.SZ", "REJECT", 0.95)],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    group_mutant, _ = mutant.build_peer_group(
        "002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path)
    )
    assert group_mutant.codes == ["300476.SZ"], "M3 未被杀死：忽略 reject 后候选仍被保留"
    group_ref, warnings_ref = build_module.build_peer_group(
        "002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path)
    )
    assert group_ref.is_empty() and any("judge reject" in note for note in warnings_ref)


# ── 判定 E：分类学召回池并入 + 特征注入 + 绝不硬闸（设计 §3.1/§3.3/§6 Step 3） ──

_TAXO_HEADER = [
    "stock_code",
    "symbol",
    "company_name",
    "area",
    "industry",
    "cnspell",
    "market",
    "list_date",
    "act_name",
    "act_ent_type",
    "sw_l1_code",
    "sw_l1_name",
    "sw_l2_code",
    "sw_l2_name",
    "sw_l3_code",
    "sw_l3_name",
]


def _taxo_row(code: str, name: str, l1: tuple[str, str], l2: tuple[str, str], l3: tuple[str, str]) -> dict[str, str]:
    return {
        "stock_code": code,
        "symbol": code.split(".")[0],
        "company_name": name,
        "area": "广东",
        "industry": "元器件",
        "cnspell": "X",
        "market": "主板",
        "list_date": "20100101",
        "act_name": "某",
        "act_ent_type": "自然人",
        "sw_l1_code": l1[0],
        "sw_l1_name": l1[1],
        "sw_l2_code": l2[0],
        "sw_l2_name": l2[1],
        "sw_l3_code": l3[0],
        "sw_l3_name": l3[1],
    }


_PCB_L1 = ("801080.SI", "电子")
_PCB_L2 = ("801083.SI", "元件")
_PCB_L3 = ("850822.SI", "印制电路板")
_OTHER_L3 = ("850751.SI", "其他专用设备")
_OTHER_L2 = ("801072.SI", "专用设备")


def _patch_snapshot(monkeypatch, tmp_path, rows: list[dict[str, str]]) -> None:
    """把分类学快照指向合成 CSV（密闭：不读真实数据、不联网）。"""
    import csv

    import alphabee.company_track.peer_taxonomy as taxo

    path = tmp_path / "all_stocks.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_TAXO_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    monkeypatch.setattr(taxo, "TAXONOMY_CSV", path)
    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()


def _pcb_rows(count: int = 20) -> list[dict[str, str]]:
    """可信 L3（印制电路板）：count 只，含标的 002916.SZ 与生成器候选 002463.SZ。"""
    codes = ["002916.SZ", "002463.SZ"] + [f"8{index:05d}.SZ" for index in range(count - 2)]
    return [_taxo_row(code, f"PCB{index}", _PCB_L1, _PCB_L2, _PCB_L3) for index, code in enumerate(codes)]


def test_recall_pool_merged_dedup_excludes_self_and_is_order_stable(tmp_path, monkeypatch):
    """召回池并入：去重、排除标的自身、保序（生成器候选在前）、受 ``build_peer_universe`` 上限约束。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, _pcb_rows(20))
    _patch_infer(
        monkeypatch,
        [
            _gen_candidate("002463.SZ", "已在池内", 0.9),
            _gen_candidate("600584.SH", "不在快照", 0.9),
        ],
    )
    calls = _patch_judge(monkeypatch, ok=False)  # judge 关闭口径：只考察召回池并入
    _judge_off(monkeypatch)
    group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))

    merged = [item["code"] for item in calls[0]["pool"]] if calls else []
    assert merged == []  # judge 未启用 ⇒ 不调 judge
    assert group.codes == ["002463.SZ", "600584.SH"], "召回池不得改变既有候选的采纳结果（精度不放宽）"
    merge_notes = [note for note in warnings if note.startswith("分类学召回池并入")]
    assert len(merge_notes) == 1 and "同 L3 成分" in merge_notes[0]
    # 20 只成分 − 标的自身 1 只 − 已在候选池内 1 只 = 18 只并入
    assert "并入 18 只" in merge_notes[0]
    # 未判分的并入项聚合一行（不逐条刷 notes）
    unscored = [note for note in warnings if note.startswith("分类学召回池 18 只未经判分")]
    assert len(unscored) == 1
    assert not [note for note in warnings if "质量闸剔除 8000" in note]


def test_recall_pool_falls_back_to_l2_for_residual_bucket(tmp_path, monkeypatch):
    """残差桶（L3 名为「其他*」）⇒ 召回降级用 **L2**，并标注「分类兜底，未经业务核验」。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    rows = [_taxo_row("920025.BJ", "目标", ("801890.SI", "机械设备"), _OTHER_L2, _OTHER_L3)]
    rows += [
        _taxo_row(f"9{index:05d}.SZ", f"OTH{index}", ("801890.SI", "机械设备"), _OTHER_L2, _OTHER_L3)
        for index in range(19)
    ]
    # L2 里另加 5 只其它 L3（分类降级后也应进入召回池，证明用的是 L2 而非 L3）
    rows += [
        _taxo_row(
            f"7{index:05d}.SZ",
            f"L2ONLY{index}",
            ("801890.SI", "机械设备"),
            _OTHER_L2,
            ("850752.SI", "冶金矿采化工设备"),
        )
        for index in range(5)
    ]
    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, rows)
    _patch_infer(monkeypatch, [_gen_candidate("920001.BJ", "候选", 0.9)])
    _judge_off(monkeypatch)
    _group, warnings = build_peer_group(
        "920025.BJ", business_description="专用设备", store=PeerGroupStore(root=tmp_path)
    )

    merge_notes = [note for note in warnings if note.startswith("分类学召回池并入")]
    assert len(merge_notes) == 1 and "同 L2 成分" in merge_notes[0]
    # L2 共 25 只 − 标的自身 1 只 = 24 只并入（若用 L3 只会是 20-1=19 只）
    assert "并入 24 只" in merge_notes[0]
    fallback = [note for note in warnings if note.startswith("分类兜底，未经业务核验")]
    assert fallback and "残差桶" in fallback[0] and "召回改用 L2" in fallback[0]


def test_recall_pool_respects_universe_limit(tmp_path, monkeypatch):
    """召回池受 ``build_peer_universe`` 上限约束（上限生效 ⇒ 并入数被截断）。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, _pcb_rows(40))
    _patch_infer(monkeypatch, [_gen_candidate("600584.SH", "候选", 0.9)])
    _judge_off(monkeypatch)
    monkeypatch.setattr(build_module, "TAXONOMY_RECALL_LIMIT_DEFAULT", 10, raising=False)
    _group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))
    merge_notes = [note for note in warnings if note.startswith("分类学召回池并入")]
    # 40 只成分 − 标的自身 1 只 = 39 只，受上限 10 约束 ⇒ 并入 10 只
    assert merge_notes and "并入 10 只" in merge_notes[0] and "上限 10" in merge_notes[0]


def test_judge_pool_carries_taxonomy_features(tmp_path, monkeypatch):
    """E 作为**特征**注入 judge prompt：``same_l3`` / ``same_l2``；未知代码 ⇒ 不注入（None）。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    rows = _pcb_rows(20) + [
        _taxo_row("603986.SH", "跨 L3", ("801080.SI", "电子"), ("801081.SI", "半导体"), ("850814.SI", "数字芯片设计"))
    ]
    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, rows)
    _judge_on(monkeypatch)
    _patch_infer(
        monkeypatch,
        [
            _gen_candidate("002463.SZ", "同 L3", 0.9),
            _gen_candidate("603986.SH", "跨 L3", 0.9),
            _gen_candidate("600584.SH", "不在快照", 0.9),
        ],
    )
    pool_codes = ["002463.SZ", "603986.SH", "600584.SH"]
    calls = _patch_judge(
        monkeypatch,
        rows={code: _judge_row("direct", 0.9) for code in pool_codes},
    )
    build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))

    assert len(calls) == 1
    pool = {item["code"]: item for item in calls[0]["pool"]}
    assert pool["002463.SZ"]["same_l3"] is True and pool["002463.SZ"]["same_l2"] is True
    assert pool["603986.SH"]["same_l3"] is False and pool["603986.SH"]["same_l2"] is False
    assert "same_l3" not in pool["600584.SH"] and "same_l2" not in pool["600584.SH"]  # 未知 ⇒ 不注入


def test_taxonomy_features_downgraded_for_residual_target(tmp_path, monkeypatch):
    """残差桶 ⇒ 特征降权：judge 入参只带 ``same_l2``（``same_l3`` 不注入）。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    rows = [
        _taxo_row("920025.BJ", "目标", ("801890.SI", "机械设备"), _OTHER_L2, _OTHER_L3),
        _taxo_row("920001.BJ", "同 L2 同 L3", ("801890.SI", "机械设备"), _OTHER_L2, _OTHER_L3),
    ]
    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, rows)
    _judge_on(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("920001.BJ", "同 L2 同 L3", 0.9)])
    calls = _patch_judge(monkeypatch, rows={"920001.BJ": _judge_row("direct", 0.9)})
    build_peer_group("920025.BJ", business_description="专用设备", store=PeerGroupStore(root=tmp_path))

    item = calls[0]["pool"][0]
    assert "same_l3" not in item and item.get("same_l2") is True


def test_taxonomy_disabled_rollback(tmp_path, monkeypatch):
    """回滚口径：``taxonomy_enabled=false`` ⇒ 不读快照、不并入召回池、不注入特征。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, _pcb_rows(20))
    _judge_on(monkeypatch)
    _taxonomy_off(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("002463.SZ", "同 L3", 0.9)])
    calls = _patch_judge(monkeypatch, rows={"002463.SZ": _judge_row("direct", 0.9)})
    _group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))

    assert not [note for note in warnings if "分类学" in note or "分类兜底" in note]
    assert calls and "same_l3" not in calls[0]["pool"][0]


def test_peer_confidence_for_group_and_missing_signals(tmp_path, monkeypatch):
    """置信度按 symbol + 已落盘对标组复算：分类学可信度 + overlap 均值；无分数 ⇒ 该信号缺失。"""
    from alphabee.company_track import peer_confidence_for_group
    from alphabee.company_track.peer_group_store import PeerGroup

    _patch_snapshot(monkeypatch, tmp_path, _pcb_rows(20))
    reliable = peer_confidence_for_group(
        "002916.SZ", PeerGroup(symbol="002916.SZ", codes=["002463.SZ"], scores={"002463.SZ": 0.9})
    )
    assert reliable.signals["taxonomy_reliable"] == pytest.approx(1.0)
    assert reliable.signals["mean_overlap"] == pytest.approx(0.9)
    assert reliable.signals["judge_direct_ratio"] is None  # 持久化层无 verdict ⇒ 缺失（按 0 参与）
    # 残差桶 ⇒ 分类学信号 0
    residual = peer_confidence_for_group(
        "002916.SZ",
        PeerGroup(symbol="002916.SZ", codes=["002463.SZ"], scores={}),
    )
    assert residual.signals["mean_overlap"] is None
    empty_scores = peer_confidence_for_group("002916.SZ", PeerGroup(symbol="002916.SZ", codes=[]))
    assert empty_scores.signals["mean_overlap"] is None


def test_m1_taxonomy_as_hard_gate_is_killed(tmp_path, monkeypatch):
    """M1：把 ``same_l3`` 变成硬闸（``same_l3 is False`` ⇒ 剔）⇒ 必红。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, _pcb_rows(20))
    _judge_off(monkeypatch)
    # 跨 L3 但生成器高分：正确实现应保留（E 绝不硬闸）
    _patch_infer(monkeypatch, [_gen_candidate("603986.SH", "跨 L3 但同环节", 0.9)])
    _patch_snapshot(
        monkeypatch,
        tmp_path,
        _pcb_rows(20)
        + [
            _taxo_row(
                "603986.SH", "跨 L3", ("801080.SI", "电子"), ("801081.SI", "半导体"), ("850814.SI", "数字芯片设计")
            )
        ],
    )
    group_ref, _ = build_peer_group("002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path))
    assert group_ref.codes == ["603986.SH"], "未变异实现必须保留跨 L3 候选（否则判据不灵敏）"

    _mutate_build(
        '        elif verdict == "reject":',
        '        elif str(cand.get("same_l3")).lower() == "false":  # M1 分类学硬闸\n'
        '            drop = "分类学硬闸（M1）"\n'
        '        elif verdict == "reject":',
        "pbg_mutant_m1_taxo",
        monkeypatch,
    )
    import sys as _sys

    mutant = _sys.modules["pbg_mutant_m1_taxo"]
    monkeypatch.setattr(
        mutant,
        "infer_peer_candidates",
        lambda *a, **k: (
            [
                {
                    "name": "跨 L3 但同环节",
                    "code": "603986.SH",
                    "reason": "同环节",
                    "source": "infer",
                    "overlap": 0.9,
                    "dims": {"product": 0.9, "customer": 0.9, "material_tech": 0.9, "business_model": 0.9},
                    "same_l3": False,
                    "same_l2": False,
                }
            ],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    group_mutant, _ = mutant.build_peer_group(
        "002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path)
    )
    assert group_mutant.is_empty(), "M1 未被杀死：分类学硬闸没有剔除跨 L3 候选"


def test_m2_reliability_always_true_is_killed(tmp_path, monkeypatch):
    """M2：残差判定恒 True（不再降级）⇒ 残差桶误判为可信 ⇒ 必红。"""
    import alphabee.company_track.peer_taxonomy as taxo

    _patch_snapshot(monkeypatch, tmp_path, _pcb_rows(20))
    residual_rows = [
        _taxo_row("920025.BJ", "目标", ("801890.SI", "机械设备"), _OTHER_L2, _OTHER_L3),
        _taxo_row("920001.BJ", "同 L2", ("801890.SI", "机械设备"), _OTHER_L2, _OTHER_L3),
    ]
    _patch_snapshot(monkeypatch, tmp_path, residual_rows)
    assert taxo.assess_reliability("920025.BJ").reliable is False  # 参照实现

    mutant = _mutant_module(
        PROJECT_ROOT / "alphabee" / "company_track" / "peer_taxonomy.py",
        "    if entry.is_residual_l3_name:",
        "    if False:  # M2 残差判定恒 False（永不降级）",
        "taxo_mutant_m2",
    )
    assert mutant.assess_reliability("920025.BJ").reliable is True, "M2 未被杀死：残差桶仍判可信"
    assert mutant.recall_pool("920025.BJ")[1] == "l3"


def test_m4_recall_pool_not_merged_is_killed(tmp_path, monkeypatch):
    """M4：召回池不并入（``_merge_taxonomy_recall_pool`` 原样返回）⇒ 必红。"""
    from alphabee.company_track.peer_group_store import PeerGroupStore

    _patch_validation(monkeypatch)
    _patch_snapshot(monkeypatch, tmp_path, _pcb_rows(20))
    _judge_off(monkeypatch)
    _patch_infer(monkeypatch, [_gen_candidate("600584.SH", "候选", 0.9)])

    _mutate_build(
        "    pool_codes, level = peer_taxonomy.recall_pool(symbol, min_constituents=min_constituents)",
        "    return candidates  # M4 不并入召回池\n    pool_codes, level = peer_taxonomy.recall_pool(symbol, min_constituents=min_constituents)",
        "pbg_mutant_m4_recall",
        monkeypatch,
    )
    import sys as _sys

    mutant = _sys.modules["pbg_mutant_m4_recall"]
    monkeypatch.setattr(
        mutant,
        "infer_peer_candidates",
        lambda *a, **k: (
            [_gen_candidate("600584.SH", "候选", 0.9)],
            {"note": "", "llm_ok": True, "dropped": []},
        ),
    )
    _group_mutant, warnings_mutant = mutant.build_peer_group(
        "002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path)
    )
    assert not [note for note in warnings_mutant if note.startswith("分类学召回池并入")], "M4 未被杀死"
    _group_ref, warnings_ref = build_peer_group(
        "002916.SZ", business_description="PCB", store=PeerGroupStore(root=tmp_path)
    )
    assert any(note.startswith("分类学召回池并入") for note in warnings_ref)


def _mutant_module(path, old: str, new: str, name: str):
    import importlib.util
    import sys
    import tempfile
    from pathlib import Path

    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"变异锚点不唯一：{old!r}"
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / f"{name}.py"
        target.write_text(text.replace(old, new), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, target)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module
