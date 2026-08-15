"""industry_fact 行业匹配单元测试（申万 L1/L2/L3 匹配策略）。

修复背景：旧的 ``industry[:2]`` 前缀 contains 匹配对子行业名恒失败
（"半导体" 是申万 L2，L1 只有 "电子"），导致 sw_code 解析失败、行业基准链路整体降级。
"""

import pandas as pd

from alphabee.agents.facts.tools.industry_fact import _SW_LEVELS, match_sw_industry


def _frame(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["industry_name", "sw_code"])


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "L3": _frame(
            [
                ("半导体材料", "801113.SI"),
                ("集成电路封测", "801112.SI"),
            ]
        ),
        "L2": _frame(
            [
                ("半导体", "801081.SI"),
                ("白酒Ⅱ", "801125.SI"),
                ("非白酒", "801126.SI"),
                ("电池", "801737.SI"),
                ("汽车整车", "801881.SI"),
            ]
        ),
        "L1": _frame(
            [
                ("电子", "801080.SI"),
                ("食品饮料", "801120.SI"),
                ("银行", "801780.SI"),
                ("电力设备", "801730.SI"),
                ("汽车", "801880.SI"),
            ]
        ),
    }


# ── 精确匹配，层级由细到粗 ────────────────────────────────────────────────


def test_l2_exact_match():
    # "半导体" 是申万 L2（修复前的核心失败场景）
    code, level = match_sw_industry("半导体", _frames())
    assert (code, level) == ("801081.SI", "L2")


def test_l1_exact_match():
    # "银行" 只有 L1 精确命中（L2 是 国有大型银行Ⅱ 等，不含裸"银行"）
    code, level = match_sw_industry("银行", _frames())
    assert (code, level) == ("801780.SI", "L1")


def test_l3_exact_match_preferred():
    code, level = match_sw_industry("半导体材料", _frames())
    assert (code, level) == ("801113.SI", "L3")


# ── 前缀匹配（申万二级带 Ⅱ 后缀）──────────────────────────────────────────


def test_l2_prefix_match_with_suffix():
    # "白酒" → "白酒Ⅱ"（前缀命中；不会误中 "非白酒"）
    code, level = match_sw_industry("白酒", _frames())
    assert (code, level) == ("801125.SI", "L2")


def test_short_prefix_does_not_overmatch():
    # "非白酒" 精确/前缀都只命中 "非白酒"
    code, level = match_sw_industry("非白酒", _frames())
    assert code == "801126.SI"


# ── 无法匹配 → 降级 ────────────────────────────────────────────────────────


def test_unknown_industry_returns_none():
    assert match_sw_industry("量子计算", _frames()) == (None, None)


def test_empty_industry_returns_none():
    assert match_sw_industry("", _frames()) == (None, None)
    assert match_sw_industry(None, _frames()) == (None, None)


def test_empty_frames_returns_none():
    assert match_sw_industry("半导体", {}) == (None, None)


def test_levels_ordering():
    assert _SW_LEVELS == ("L3", "L2", "L1")
