"""行业事实归一化（industry-context Phase 1，normalize_industry_schema 节点）。

职责（外部字段只存在于 adapter/采集层，本层输出一律 canonical）：
1. 把采集到的**源单位记录**映射为 AlphaBee canonical 记录（``revenue_yoy`` / ``roe`` /
   ``debt_ratio`` / ``gross_margin``），并做**强制单位转换**（B3 口径风险落地）：

   | canonical 字段 | 目标单位 | 转换 |
   |---|---|---|
   | ``revenue_yoy`` | PERCENT（百分点） | 原样（与公司侧 ``revenue_yoy`` 一致，供 market_share_change 直接相减） |
   | ``roe`` | RATIO | ÷100 |
   | ``debt_ratio`` | RATIO | ÷100 |
   | ``gross_margin`` | RATIO | ÷100 |

2. 保留报告期元数据（``end_date`` / ``ann_date``），供报告期对齐判断
   （``assess_period_alignment``，B3 的周期部分）。

**输入契约**：``normalize_industry_records`` 的输入是**源单位行**（百分比值）——
即 Tushare ``fina_indicator`` 经 adapter 重命名后的行（列名已是 canonical 名，但数值
仍为源单位：``revenue_yoy`` / ``roe`` / ``debt_to_assets`` / ``gross_margin`` / ``period`` /
``stock_code``）。**不要传入已归一化的 ratio 记录**（会重复 ÷100）。

背景（docs/industry-context-phase1-design.md §2.1）：Phase 0 的 ``data.py`` 有两个潜在缺陷，
本模块即修复点——① 读取 adapter 重命名前的列名（``tr_yoy`` / ``grossprofit_margin``），
导致 ``industry_revenue_yoy`` / ``industry_avg_gross_margin`` 恒为 None；
② 百分比原值直接当 RATIO 注入，导致 roe_level / debt_ratio 相对阈值恒不命中。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from alphabee.industry.contracts import PeriodAlignment

# canonical 字段 → 输入行键（Tushare fina_indicator 经 adapter 重命名后的列名，
# 见 alphabee/adapters/tushare/financial_mapping.yaml：or_yoy→revenue_yoy、
# grossprofit_margin→gross_margin；roe / debt_to_assets 保持原名）
_TUSHARE_RAW_KEYS: dict[str, str] = {
    "revenue_yoy": "revenue_yoy",  # 营业收入同比增长率(%)，保持百分比（百分点）
    "roe": "roe",  # 净资产收益率(%) → ÷100
    "debt_ratio": "debt_to_assets",  # 资产负债率(%) → ÷100
    "gross_margin": "gross_margin",  # 销售毛利率(%) → ÷100
}

# 需要从百分比转换为 ratio 的 canonical 字段（RATIO 口径）
_PERCENT_TO_RATIO = frozenset({"roe", "debt_ratio", "gross_margin"})

CANONICAL_FIELDS = tuple(_TUSHARE_RAW_KEYS)


def _safe_float(value: Any) -> float | None:
    """宽容转 float；None/NaN/非法 → None（与 tools._utils.safe_float 默认 0 不同：
    基准推导里 0 会污染中位数，必须用 None 表达缺失）。"""
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_industry_records(
    raw_records: list[dict[str, Any]],
    *,
    source: str = "tushare",
) -> list[dict[str, Any]]:
    """把源单位记录归一化为 canonical 记录（单位转换 + 报告期元数据）。

    Args:
        raw_records: 源单位行字典列表（Tushare fina_indicator adapter 重命名后的行）。
        source: 数据源标识，决定列映射表（v1 仅 ``tushare``）。

    Returns:
        canonical 记录列表，每条为 ``{revenue_yoy, roe, debt_ratio, gross_margin,
        end_date, ann_date, ts_code}``（数值字段 None 表示缺失）；完全无可用数值
        字段的记录被过滤，保证 ``peer_count`` 有意义。
    """
    mapper = _SOURCE_MAPPERS.get(source)
    if mapper is None:  # 未知源：保守回退为"无记录"，不猜测列名
        return []
    return [record for record in (mapper(row) for row in raw_records) if record is not None]


def _tushare_mapper(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    canonical: dict[str, Any] = {}
    has_numeric = False
    for field, input_key in _TUSHARE_RAW_KEYS.items():
        value = _safe_float(row.get(input_key))
        if value is not None:
            has_numeric = True
            if field in _PERCENT_TO_RATIO:
                value = value / 100.0
        canonical[field] = value
    if not has_numeric:
        return None
    canonical["end_date"] = _as_str(row.get("period") or row.get("end_date"))
    canonical["ann_date"] = _as_str(row.get("ann_date"))
    canonical["ts_code"] = _as_str(row.get("stock_code") or row.get("ts_code"))
    return canonical


_SOURCE_MAPPERS: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
    "tushare": _tushare_mapper,
}


def assess_period_alignment(records: list[dict[str, Any]]) -> PeriodAlignment:
    """评估成分股记录的报告期对齐状态（B3 周期部分）。

    规则：
    - 全部同一 ``end_date`` → ``aligned``；
    - 单一主导期覆盖 ≥80% → ``mostly_aligned``；
    - 其余（含**完全没有报告期信息**——无法确认对齐）→ ``mixed``。

    只有 aligned / mostly_aligned 才允许保留 growth 基准（``growth_usable()``），
    mixed 时审核节点会置空 ``industry_revenue_yoy`` 并留痕，避免口径错配数值流入规则。
    """
    counts: dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        period = _as_str(record.get("end_date"))
        if period:
            counts[period] = counts.get(period, 0) + 1

    if not counts:
        # 无报告期信息 = 无法确认对齐 → 按"无法对齐"从严处理（B3）
        return PeriodAlignment(status="mixed", dominant_period=None, period_counts={})

    dominant, dominant_count = max(counts.items(), key=lambda item: item[1])
    total = len(records)
    ratio = dominant_count / total if total else 0.0
    if len(counts) == 1:
        status = "aligned"
    elif ratio >= 0.8:
        status = "mostly_aligned"
    else:
        status = "mixed"
    return PeriodAlignment(
        status=status,
        dominant_period=dominant,
        period_counts=dict(counts),
    )
