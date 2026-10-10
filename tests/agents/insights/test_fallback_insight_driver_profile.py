"""Tier 2 兜底洞察接驱动画像（G-4）用例。

业务动机：驱动画像已经把「这家公司的盈利由什么驱动」确定性推导出来，但 Tier 2 兜底
此前只看 derived_facts / signals，导致降级路径下 ``main_driver`` 与画像的
``primary_drivers`` 不一致（甚至为空）。画像有主驱动时以它为准，没有才回退旧逻辑。
"""

from __future__ import annotations

from alphabee.agents.insights.rescue import _pick_main_driver_from_profile, build_fallback_insight


def _context(**overrides) -> dict:
    """带一条高风险衍生指标的上下文（满足"有数据"守卫），再按需覆盖。"""
    context = {
        "key_signals": [],
        "key_derived_facts": {"operating_cashflow_ratio": {"level": "high"}},
        "anomaly": {},
        "conflicts": [],
        "company": {},
        "latest_snapshot": {},
    }
    context.update(overrides)
    return context


def test_main_driver_takes_profile_primary_driver():
    output = build_fallback_insight(
        _context(driver_profile={"primary_drivers": ["猪价", "完全成本"]}),
        "002714.SZ",
    )
    assert output.main_driver == "猪价"
    assert output.confidence == "low"


def test_main_driver_falls_back_when_profile_missing():
    output = build_fallback_insight(_context(), "600519.SH")
    assert output.main_driver == "operating_cashflow_ratio"


def test_main_driver_falls_back_when_profile_has_no_driver():
    output = build_fallback_insight(_context(driver_profile={"primary_drivers": []}), "600519.SH")
    assert output.main_driver == "operating_cashflow_ratio"


def test_main_driver_falls_back_when_profile_driver_is_blank():
    # 画像的驱动变量为空串（占位）时不能当成主驱动，必须回退
    output = build_fallback_insight(_context(driver_profile={"primary_drivers": ["", "猪价"]}), "600519.SH")
    assert output.main_driver == "operating_cashflow_ratio"


def test_pick_main_driver_from_profile_handles_missing_shapes():
    assert _pick_main_driver_from_profile({}) == ""
    assert _pick_main_driver_from_profile({"primary_drivers": []}) == ""
    assert _pick_main_driver_from_profile({"primary_drivers": ["猪价"]}) == "猪价"
    assert _pick_main_driver_from_profile({"primary_drivers": [None]}) == ""


def test_profile_only_context_keeps_minimal_skeleton():
    """画像单独存在（无任何信号/衍生指标/快照）时仍输出空骨架。

    守卫语义：数据缺失 ≠ 数据健康——只有画像而没有任何信号事实时，连"未检出高风险信号"
    这类断言也不该输出。G-4 的修复只在"画像 + 其它结构化事实"的真实兜底场景生效。
    """
    output = build_fallback_insight({"driver_profile": {"primary_drivers": ["猪价"]}}, "002714.SZ")
    assert output.core_view == ""
    assert output.main_driver == ""
