"""主营构成 adapter 映射测试（COMPANY_TRACK Phase A，A1）。"""

import pandas as pd

from alphabee.adapters.akshare import AkShare_Adapter


def _em_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "报告日期": "2025-12-31",
                "分类类型": "按产品分类",
                "主营构成": "云计算/服务器",
                "主营收入": 2.1e10,
                "收入比例": 66.7,
                "主营成本": 1.9e10,
                "成本比例": 90.5,
                "主营利润": 2.0e9,
                "利润比例": 50.0,
                "毛利率": 9.5,
            }
        ]
    )


def test_stock_zygc_em_adapter_renames_to_canonical():
    adapted = AkShare_Adapter.adapt("stock_zygc_em", _em_fixture())
    assert "报告日期" not in adapted.columns
    # 映射后的 canonical 列（rename 保持原列顺序，用集合断言防顺序耦合）
    assert {
        "report_date",
        "biz_segment_category",
        "biz_segment_name",
        "biz_segment_revenue",
        "biz_segment_revenue_share",
        "biz_segment_cost",
        "biz_segment_profit",
        "biz_gross_margin",
    } <= set(adapted.columns)
    # 成本比例 / 利润比例 未映射 → 保持原列（仅存在于 adapter 层之上，normalize 不读取）
    assert "成本比例" in adapted.columns
    assert "利润比例" in adapted.columns
    row = adapted.iloc[0]
    assert row["biz_segment_name"] == "云计算/服务器"
    assert row["biz_segment_revenue_share"] == 66.7
    assert row["biz_gross_margin"] == 9.5


def test_unknown_method_passthrough():
    df = pd.DataFrame([{"a": 1}])
    adapted = AkShare_Adapter.adapt("no_such_api", df)
    assert list(adapted.columns) == ["a"]
