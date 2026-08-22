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
