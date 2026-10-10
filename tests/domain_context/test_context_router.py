"""ContextRouter 测试（DOMAIN_CONTEXT_ROADMAP P0 第 3 步）。"""

from alphabee.domain_context import (
    GENERIC_FALLBACK_ID,
    RouterInput,
    route,
)
from alphabee.domain_context.schemas import PlaybookSchema


def test_muyuan_routes_to_hog_cycle():
    # 牧原：track_label=生猪养殖 + sub_industry=养殖业 → hog_cycle
    result = route(
        RouterInput(
            symbol="002714.SZ",
            track_label="生猪养殖",
            industry="农林牧渔",
            sub_industry="养殖业",
        )
    )
    assert result.playbook_id == "hog_cycle"
    assert result.fallback is False
    assert result.degraded is False
    # 展开后的 primitive 集合
    contexts = {c.context for c in result.activated_contexts}
    assert contexts == {"commodity_cycle", "biological_inventory", "cost_curve", "capacity_cycle"}
    assert "track_label_match" in result.why_selected
    assert "sub_industry_match" in result.why_selected


def test_jinchengxin_routes_to_mining_services():
    result = route(
        RouterInput(
            symbol="603979.SH",
            track_label="矿业服务",
            sub_industry="采掘服务",
        )
    )
    assert result.playbook_id == "mining_services"
    assert result.fallback is False
    contexts = {c.context for c in result.activated_contexts}
    assert contexts == {"commodity_cycle", "project_delivery", "cost_curve", "capacity_cycle"}


def test_no_match_falls_back_to_generic_not_degraded():
    # 白酒：命中不了任何专用 playbook → generic_fundamental（普通无命中，不视为降级）
    result = route(
        RouterInput(
            symbol="600519.SH",
            track_label="白酒",
            industry="食品饮料",
            sub_industry="白酒Ⅱ",
            business_model="brand",
        )
    )
    assert result.playbook_id == GENERIC_FALLBACK_ID
    assert result.fallback is True
    assert result.degraded is False
    assert result.degraded_reason == ""
    contexts = {c.context for c in result.activated_contexts}
    assert contexts == {"cost_curve", "capacity_cycle", "working_capital_stress"}


def test_empty_input_is_degraded():
    result = route(RouterInput())
    assert result.playbook_id == GENERIC_FALLBACK_ID
    assert result.fallback is True
    assert result.degraded is True
    assert result.degraded_reason == "identity_signals_missing"


def test_business_model_is_low_weight_signal():
    # 注入一个仅靠 business_model 命中的 playbook，验证 archetype 参与匹配且为低权信号
    playbooks = {
        "test_bm": PlaybookSchema(
            id="test_bm",
            match_business_models=["integrator"],
            primitives=["cost_curve"],
        ),
        GENERIC_FALLBACK_ID: PlaybookSchema(id=GENERIC_FALLBACK_ID, primitives=["capacity_cycle"]),
    }
    result = route(RouterInput(business_model="integrator"), playbooks=playbooks)
    assert result.playbook_id == "test_bm"
    assert "business_model_match" in result.why_selected


def test_track_label_beats_sub_industry():
    # 一个 playbook 靠 track_label(3) 命中，另一个靠 sub_industry(2) 命中 → 前者胜
    playbooks = {
        "by_label": PlaybookSchema(
            id="by_label",
            match_track_labels=["生猪养殖"],
            primitives=["commodity_cycle"],
        ),
        "by_ind": PlaybookSchema(
            id="by_ind",
            match_sub_industries=["养殖业"],
            primitives=["capacity_cycle"],
        ),
        GENERIC_FALLBACK_ID: PlaybookSchema(id=GENERIC_FALLBACK_ID, primitives=["cost_curve"]),
    }
    result = route(
        RouterInput(track_label="生猪养殖", sub_industry="养殖业"),
        playbooks=playbooks,
    )
    assert result.playbook_id == "by_label"


def test_tie_break_is_deterministic():
    # 两个 playbook 同分 → 按 id 字典序决平（by_a 胜 by_b）
    playbooks = {
        "by_b": PlaybookSchema(id="by_b", match_sub_industries=["养殖业"], primitives=["a"]),
        "by_a": PlaybookSchema(id="by_a", match_sub_industries=["养殖业"], primitives=["b"]),
        GENERIC_FALLBACK_ID: PlaybookSchema(id=GENERIC_FALLBACK_ID, primitives=["c"]),
    }
    result = route(RouterInput(sub_industry="养殖业"), playbooks=playbooks)
    assert result.playbook_id == "by_a"


# ── 锚定强度（anchor_strength）三档 ─────────────────────────────────────


def test_sw_code_prefix_is_strong_anchor():
    result = route(RouterInput(symbol="002714.SZ", sw_code="801010.SI"))
    assert result.playbook_id == "hog_cycle"
    assert "sw_code_match" in result.why_selected
    assert result.anchor_strength == "strong"


def test_track_label_match_is_strong_anchor():
    result = route(RouterInput(track_label="生猪养殖"))
    assert result.playbook_id == "hog_cycle"
    assert result.anchor_strength == "strong"


def test_broad_l1_industry_only_is_weak_anchor():
    # 生产形状：sub_industry 恒为空，宽 L1 行业名只算弱锚（G-1）
    result = route(RouterInput(industry="农林牧渔", sub_industry=""))
    assert result.playbook_id == "hog_cycle"
    assert "sub_industry_match" in result.why_selected
    assert "track_label_match" not in result.why_selected
    assert result.anchor_strength == "weak"


def test_agriculture_l1_plus_sw_code_upgrades_to_strong():
    # G-1 回归钉：宽 L1「农林牧渔」本身不是强锚；只有申万代码精确前缀命中才是
    result = route(RouterInput(industry="农林牧渔", sub_industry="", sw_code="801010.SI"))
    assert result.playbook_id == "hog_cycle"
    assert result.anchor_strength == "strong"


def test_sub_industry_only_is_weak_anchor():
    result = route(RouterInput(sub_industry="养殖业"))
    assert result.playbook_id == "hog_cycle"
    assert result.anchor_strength == "weak"


def test_business_model_only_is_weak_anchor():
    playbooks = {
        "test_bm": PlaybookSchema(
            id="test_bm",
            match_business_models=["integrator"],
            primitives=["cost_curve"],
        ),
        GENERIC_FALLBACK_ID: PlaybookSchema(id=GENERIC_FALLBACK_ID, primitives=["capacity_cycle"]),
    }
    result = route(RouterInput(business_model="integrator"), playbooks=playbooks)
    assert result.playbook_id == "test_bm"
    assert result.anchor_strength == "weak"


def test_no_match_is_none_anchor():
    result = route(RouterInput(track_label="白酒", industry="食品饮料", business_model="brand"))
    assert result.playbook_id == GENERIC_FALLBACK_ID
    assert result.fallback is True
    assert result.anchor_strength == "none"


# ── 分部结构 / 财务结构匹配 ─────────────────────────────────────────────


def test_segment_match_is_scored_and_recorded():
    playbooks = {
        "ai_pb": PlaybookSchema(
            id="ai_pb",
            match_segments=["服务器"],
            primitives=["capacity_cycle"],
        ),
        GENERIC_FALLBACK_ID: PlaybookSchema(id=GENERIC_FALLBACK_ID, primitives=["cost_curve"]),
    }
    result = route(
        RouterInput(
            business_model_summary="",
            dominant_segment="服务器",
            dominant_share=42.0,
            segment_summary=["服务器 42%(+58%)"],
        ),
        playbooks=playbooks,
    )
    assert result.playbook_id == "ai_pb"
    assert "segment_match" in result.why_selected
    # 结构信号本身不是强锚判据（无 track_label/sw_code 命中）→ weak
    assert result.anchor_strength == "weak"


def test_financial_structure_threshold_hit_and_miss():
    playbooks = {
        "inv_pb": PlaybookSchema(
            id="inv_pb",
            match_track_labels=["白酒"],
            match_financial_structures={"inventory_ratio": {"gt": 0.25}},
            primitives=["working_capital_stress"],
        ),
        GENERIC_FALLBACK_ID: PlaybookSchema(id=GENERIC_FALLBACK_ID, primitives=["cost_curve"]),
    }
    hit = route(
        RouterInput(track_label="白酒", financial_structure={"inventory_ratio": 0.4}),
        playbooks=playbooks,
    )
    assert "financial_structure_match" in hit.why_selected
    assert hit.anchor_strength == "strong"

    miss = route(
        RouterInput(track_label="白酒", financial_structure={"inventory_ratio": 0.1}),
        playbooks=playbooks,
    )
    assert "financial_structure_match" not in miss.why_selected
    # 声明了财务结构但公司数据不达标 → 强信号被推翻，降级为弱锚
    assert miss.playbook_id == "inv_pb"
    assert miss.anchor_strength == "weak"


def test_missing_financial_field_is_not_a_hit():
    from alphabee.domain_context.context_router import _threshold_hit

    assert _threshold_hit({"inventory_ratio": 0.4}, {"inventory_ratio": {"gt": 0.25}}) is True
    assert _threshold_hit({}, {"inventory_ratio": {"gt": 0.25}}) is False
    assert _threshold_hit({"inventory_ratio": 0.25}, {"inventory_ratio": {"gt": 0.25}}) is False
    assert _threshold_hit({"x": 1.0}, {"x": {"lt": 2.0}}) is True
    assert _threshold_hit({"x": 1.0}, {}) is False


# ── D0-c：兜底框架的结构性驱动 ─────────────────────────────────────────


def test_fallback_primary_drivers_from_dominant_segment():
    result = route(RouterInput(track_label="白酒", dominant_segment="高端白酒", dominant_share=42.0))
    assert result.playbook_id == GENERIC_FALLBACK_ID
    assert result.primary_drivers == ["主力分部 高端白酒 收入占比 42%"]


def test_fallback_primary_drivers_from_fastest_segment():
    result = route(RouterInput(track_label="白酒", fastest_segment="次高端", fastest_yoy=58.0))
    assert result.primary_drivers == ["次高端 同比 58%（快于主力）"]


def test_fallback_primary_drivers_from_margin_gap():
    result = route(
        RouterInput(
            track_label="白酒",
            financial_structure={"gross_margin": 35.0, "peer_gross_margin": 30.0},
        )
    )
    assert result.primary_drivers == ["毛利率 35.0%（行业基准 30.0%，差 +5.0pp）"]


def test_fallback_primary_drivers_without_structure_facts_stay_empty():
    # 无任何结构事实 → 与现状一致（不编造）
    result = route(RouterInput(track_label="白酒"))
    assert result.fallback is True
    assert result.primary_drivers == []
