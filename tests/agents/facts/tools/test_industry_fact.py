"""industry_fact 行业匹配单元测试（申万 L1/L2/L3 匹配策略）。

修复背景：旧的 ``industry[:2]`` 前缀 contains 匹配对子行业名恒失败
（"半导体" 是申万 L2，L1 只有 "电子"），导致 sw_code 解析失败、行业基准链路整体降级。
"""

import pandas as pd

from alphabee.agents.facts.tools.industry_fact import _SW_LEVELS, match_sw_industry
from alphabee.industry.classification import extract_sw_member


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


# ── 成分表精确归属（extract_sw_member，index_member_all 输出列契约）───────────


def _member(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=[
            "l1_code",
            "l1_name",
            "l2_code",
            "l2_name",
            "l3_code",
            "l3_name",
            "ts_code",
            "name",
            "in_date",
            "out_date",
            "is_new",
        ],
    )


def _member_row(l3=True, in_date="20210101", out_date=None, is_new="Y") -> tuple:
    return (
        "801730.SI",
        "电力设备",
        "801738.SI",
        "电网设备",
        "857344.SI" if l3 else None,
        "线缆部件及其他" if l3 else None,
        "600577.SH",
        "精达股份",
        in_date,
        out_date,
        is_new,
    )


def test_member_l3_preferred():
    # 精达股份场景：index_member_all 直接给出 L1/L2/L3 归属，应取最细 L3
    members = _member([_member_row()])
    assert extract_sw_member(members) == {
        "l1_code": "801730.SI",
        "l1_name": "电力设备",
        "l2_code": "801738.SI",
        "l2_name": "电网设备",
        "l3_code": "857344.SI",
        "l3_name": "线缆部件及其他",
        "sw_code": "857344.SI",
        "sw_level": "L3",
        "industry_name": "线缆部件及其他",
    }


def test_member_l2_when_no_l3():
    members = _member([_member_row(l3=False)])
    result = extract_sw_member(members)
    assert result["sw_code"] == "801738.SI"
    assert result["sw_level"] == "L2"
    assert result["industry_name"] == "电网设备"
    assert result["l3_code"] == ""  # 无 L3 归属时该层留空


def test_member_skips_inactive_rows():
    # 第一条已退出（out_date 非空）应被跳过，取当前有效行
    members = _member([_member_row(in_date="20200101", out_date="20201231", is_new="N"), _member_row()])
    result = extract_sw_member(members)
    assert result["sw_code"] == "857344.SI"
    assert result["sw_level"] == "L3"


def test_member_empty_or_bad_df_returns_none():
    assert extract_sw_member(None) is None
    assert extract_sw_member(pd.DataFrame()) is None
    bad = pd.DataFrame({"foo": [1]})
    assert extract_sw_member(bad) is None
