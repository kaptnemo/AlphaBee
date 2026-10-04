"""TTM（滚动 12 个月）还原工具。

数据源（Tushare income）默认返回**报告期累计值**（三季报 = 前三季度累计），
而 PE-TTM 这类估值指标是**滚动 12 个月**口径。两者直接相除会造成口径错配，
因此需要先把累计值还原为 TTM 值。

恒等式（对季报 / 半年报 / 年报一致成立）：

    TTM_t = 上年年报累计 + 本期累计 − 上年同期累计

年报时退化为当年年报本身（上年年报 == 上年同期累计），即 TTM 同比 = 年报同比。

用法：

    >>> series = {"20241231": 100.0, "20240930": 70.0, "20230930": 60.0, "20231231": 80.0}
    >>> ttm_from_cumulative(series, "20240930")   # 80 + 70 - 60
    90.0
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["ttm_from_cumulative", "ttm_yoy"]


def _prev_year_period(period: str, years: int = 1) -> str:
    """返回 period 前 N 年的同一报告期（如 "20240930" → "20230930"）。"""
    return f"{int(period[:4]) - years}{period[4:]}"


def ttm_from_cumulative(
    series: Mapping[str, float | None],
    period: str,
) -> float | None:
    """由累计值序列还原 ``period`` 报告期的 TTM 值。

    Args:
        series: ``{报告期(YYYYMMDD): 累计值}``；值缺失时用 ``None`` 占位。
        period: 目标报告期（YYYYMMDD），如 "20240930"。

    Returns:
        TTM 值；缺少上年年报、上年同期或本期数据时返回 ``None``（不猜、不补零）。
    """
    if not period or len(period) != 8 or not period.isdigit():
        return None

    ytd = series.get(period)
    prev_ytd = series.get(_prev_year_period(period))
    prev_fy = series.get(f"{int(period[:4]) - 1}1231")

    if ytd is None or prev_ytd is None or prev_fy is None:
        return None
    return prev_fy + ytd - prev_ytd


def ttm_yoy(
    series: Mapping[str, float | None],
    period: str,
) -> float | None:
    """``period`` 报告期的 TTM 同比增速（%）。

    与 PE-TTM 同口径：分子分母都是滚动 12 个月。

    Args:
        series: ``{报告期(YYYYMMDD): 累计值}``。
        period: 目标报告期（YYYYMMDD）。

    Returns:
        TTM 同比增速（%）；基期 TTM ≤ 0（去年同期亏损）或数据不足时返回 ``None``
        —— 亏损基期下的增速没有经济含义，不制造数字。
    """
    ttm_now = ttm_from_cumulative(series, period)
    ttm_prev = ttm_from_cumulative(series, _prev_year_period(period))

    if ttm_now is None or ttm_prev is None or ttm_prev <= 0:
        return None
    return (ttm_now - ttm_prev) / ttm_prev * 100.0
