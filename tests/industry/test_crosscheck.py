"""多来源行业交叉校验单元测试（industry-context Phase 2，item 9）。"""

import pandas as pd

from alphabee.industry.crosscheck import crosscheck_industry


def _sw_frames():
    return {
        "L1": pd.DataFrame(
            [("电子", "801080.SI"), ("银行", "801780.SI"), ("通信", "801770.SI")],
            columns=["industry_name", "sw_code"],
        ),
        "L2": pd.DataFrame(
            [("半导体", "801081.SI"), ("通信设备", "801772.SI"), ("白酒Ⅱ", "801125.SI")],
            columns=["industry_name", "sw_code"],
        ),
    }


def _ths_rows():
    return [
        {"industry_name": "半导体", "industry_code": "885460"},
        {"industry_name": "银行", "industry_code": "885604"},
        {"industry_name": "通信设备", "industry_code": "885452"},
    ]


def _em_rows():
    return [
        {"industry_name": "半导体", "industry_pe_ttm": 65.2, "industry_pb": 4.8},
        {"industry_name": "银行", "industry_pe_ttm": 5.8, "industry_pb": 0.6},
        {"industry_name": "通信设备", "industry_pe_ttm": 30.1, "industry_pb": 3.2},
    ]


# ── 全命中 ─────────────────────────────────────────────────────────────────


def test_all_sources_hit():
    result = crosscheck_industry("半导体", _sw_frames(), _ths_rows(), _em_rows())
    assert result.sources_hit == 3
    assert [m.source for m in result.matches] == ["sw", "ths", "em"]
    sw = next(m for m in result.matches if m.source == "sw")
    assert sw.code == "801081.SI" and sw.level == "L2"
    ths = next(m for m in result.matches if m.source == "ths")
    assert ths.code == "885460"
    em = next(m for m in result.matches if m.source == "em")
    assert em.valuation["industry_pe_ttm"] == 65.2

    # canonical：EM 估值优先；名称多数一致
    assert result.canonical_name == "半导体"
    assert result.canonical_valuation == {"industry_pe_ttm": 65.2, "industry_pb": 4.8}
    assert not [w for w in result.warnings if "未命中" in w]


# ── 部分缺失与回退 ─────────────────────────────────────────────────────────


def test_em_missed_falls_back_to_sw_valuation():
    result = crosscheck_industry(
        "半导体",
        _sw_frames(),
        _ths_rows(),
        [],
        sw_valuation={"industry_pe_ttm": 50.0, "industry_pb": 5.0},
    )
    assert result.sources_hit == 2
    assert any("东方财富" in w for w in result.warnings)
    # 无 EM → 回退申万估值快照
    assert result.canonical_valuation == {"industry_pe_ttm": 50.0, "industry_pb": 5.0}


def test_all_missed_yields_empty_facts():
    result = crosscheck_industry("量子计算", {}, [], [])
    assert result.sources_hit == 0
    assert len([w for w in result.warnings if "未命中" in w]) == 3
    assert result.canonical_name == ""
    assert result.canonical_valuation == {}
    facts = result.as_facts()
    assert facts["industry"] == "量子计算"
    assert facts["industry_pe_ttm"] is None


# ── 口径漂移告警 ───────────────────────────────────────────────────────────


def test_name_drift_across_sources_warns():
    # 前缀匹配会命中"通信设备"，与申万"通信"不一致 → 漂移告警
    result = crosscheck_industry("通信", _sw_frames(), _ths_rows(), _em_rows())
    assert result.sources_hit == 3
    assert any("口径漂移" in w for w in result.warnings)


def test_prefix_fallback_matching():
    # "半导" 无精确命中时走前缀 → 半导体
    result = crosscheck_industry("半导", _sw_frames(), _ths_rows(), _em_rows())
    assert result.sources_hit == 3
    assert result.canonical_name == "半导体"


# ── 标准化 facts ───────────────────────────────────────────────────────────


def test_as_facts_shape():
    result = crosscheck_industry("银行", _sw_frames(), _ths_rows(), _em_rows())
    facts = result.as_facts()
    assert facts["query"] == "银行"
    assert facts["industry"] == "银行"
    assert facts["industry_pe_ttm"] == 5.8
    assert facts["sources_hit"] == 3
    assert isinstance(facts["warnings"], list)
