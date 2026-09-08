"""Consensus aggregation engine — pure functions, no network I/O.

输入：东方财富研报列表接口的原始记录列表（即
:func:`alphabee.tools.eastmoney.get_eastmoney_report_list` 返回的 ``reports`` 列表，
其每条保留东财原始列名如 ``predictThisYearEps`` / ``emRatingValue``）。
本模块是 *source-specific collector*：它是唯一允许直接读东财原始列名的层，
输出一律使用 canonical 字段名（见 ``alphabee/schemas/consensus.yaml``）。

契约（以 docs/midterm/DATA_CONTRACTS_VERIFIED.md §1 实测为准，覆盖设计文档旧假设）：

- ``emRatingValue`` 编码方向：持有=1 / 增持=2 / 买入=3（数值越大越看多）。
  ``rating_mean`` 直接取 ``emRatingValue`` 均值，禁止照搬设计文档「买入1」反向假设。
- 评级上/下调方向 = 同一记录内 ``emRatingValue`` 与 ``lastEmRatingValue`` 的差分；
  **禁止读 ``ratingChange``**（非恒定分类码）。
- 目标价 ``indvAimPriceT`` 覆盖率低（实测 4/41 ~ 29/100）：覆盖率低于阈值时置
  ``target_price = None`` 并显式产出 ``consensus_missing: target_price``，绝不静默回退 0。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median as _stat_median
from typing import Any

# 13 个 consensus canonical 字段（与 schemas/consensus.yaml、midterm/models.py 对齐）
EPS_SOURCE_FIELDS: dict[str, str] = {
    "eps_fy1": "predictThisYearEps",
    "eps_fy2": "predictNextYearEps",
    "eps_fy3": "predictNextTwoYearEps",
}

CONSENSUS_FIELDS: tuple[str, ...] = (
    "eps_fy1",
    "eps_fy2",
    "eps_fy3",
    "target_price",
    "rating_mean",
    "coverage_count",
    "eps_fy1_revision_1m",
    "eps_fy1_revision_3m",
    "eps_fy2_revision_1m",
    "revision_breadth",
    "revision_acceleration",
    "rating_upgrade_1m",
    "rating_downgrade_1m",
)

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y%m%d",
    "%Y/%m/%d",
)


@dataclass
class ConsensusOutput:
    """一次个股一致预期聚合结果。

    - ``values``：canonical 字段 → 值（``None`` 表示缺失，绝不静默回退 0）。
    - ``missing``：显式缺失 issue 列表，形如 ``consensus_missing: target_price``。
    - ``warnings``：非致命提示（降级说明等）。
    """

    values: dict[str, float | int | None] = field(default_factory=dict)
    source: str = "eastmoney:report/list"
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    as_of_date: str = ""
    report_count: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# 基础纯函数
# ─────────────────────────────────────────────────────────────────────────────
def _to_float(value: Any) -> float | None:
    """把东财返回的 str/int/float/空串 归一为 float；空串/无效值 → None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "--", "None", "nan", "NaN", "null"):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    """把东财 ``publishDate``（``YYYY-MM-DD HH:MM:SS.000``）等归一为 ``date``。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    return float(_stat_median(values)) if values else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pct_change(past: float | None, recent: float | None) -> float | None:
    """(recent / past - 1) × 100，PERCENT 口径；任一缺失或 past==0 → None。"""
    if past is None or recent is None or past == 0:
        return None
    return round((recent / past - 1.0) * 100.0, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 时序/评级聚合纯函数
# ─────────────────────────────────────────────────────────────────────────────
def _daily_median_series(reports: list[dict[str, Any]], source_field: str) -> list[tuple[date, float]]:
    """按 ``publishDate`` 聚合某 EPS 字段的日中位数，返回按日期升序的序列。"""
    by_date: dict[date, list[float]] = {}
    for r in reports:
        d = _parse_date(r.get("publishDate"))
        if d is None:
            continue
        v = _to_float(r.get(source_field))
        if v is None:
            continue
        by_date.setdefault(d, []).append(v)
    return [(d, _stat_median(vals)) for d, vals in sorted(by_date.items())]


def _latest_at_or_before(series: list[tuple[date, float]], asof: date) -> float | None:
    """返回 ``asof`` 当日或之前最近一天的中位数。"""
    for d, v in reversed(series):
        if d <= asof:
            return v
    return None


def _revision_series(daily_series: list[tuple[date, float]], window_days: int) -> list[tuple[date, float]]:
    """逐日计算 revision_1m/3m 序列（PERCENT），供加速度（二阶差分）使用。"""
    out: list[tuple[date, float]] = []
    for d, v in daily_series:
        past = _latest_at_or_before(daily_series, d - timedelta(days=window_days))
        rev = _pct_change(past, v)
        if rev is not None:
            out.append((d, rev))
    return out


def _rating_mean(reports: list[dict[str, Any]]) -> float | None:
    """``emRatingValue`` 均值（持有1/增持2/买入3，越大越看多）。"""
    vals = [v for v in (_to_float(r.get("emRatingValue")) for r in reports) if v is not None]
    return _mean(vals)


def _rating_direction_counts(
    reports: list[dict[str, Any]],
    asof: date,
    window_days: int,
) -> tuple[int | None, int | None]:
    """近 ``window_days`` 内评级上/下调次数。

    方向 = 同一记录内 ``emRatingValue`` vs ``lastEmRatingValue`` 差分
    （**不读 ratingChange**）。窗口内无任何可比记录 → ``(None, None)``。
    """
    cutoff = asof - timedelta(days=window_days)
    ups = downs = 0
    valid = False
    for r in reports:
        d = _parse_date(r.get("publishDate"))
        if d is None or not (cutoff <= d <= asof):
            continue
        cur = _to_float(r.get("emRatingValue"))
        prev = _to_float(r.get("lastEmRatingValue"))
        if cur is None or prev is None:
            continue
        valid = True
        if cur > prev:
            ups += 1
        elif cur < prev:
            downs += 1
    if not valid:
        return None, None
    return ups, downs


def _coverage_count(reports: list[dict[str, Any]]) -> int | None:
    """覆盖机构数（去重 orgSName/orgName）；无机构信息时退化为研报条数。"""
    if not reports:
        return None
    orgs = {str(r.get("orgSName") or r.get("orgName") or "") for r in reports}
    orgs.discard("")
    return len(orgs) if orgs else len(reports)


# ─────────────────────────────────────────────────────────────────────────────
# 主聚合入口
# ─────────────────────────────────────────────────────────────────────────────
def aggregate_consensus(
    reports: list[dict[str, Any]],
    *,
    as_of_date: str | None = None,
    target_price_coverage_threshold: float = 0.3,
    rating_window_days: int = 30,
) -> ConsensusOutput:
    """把东财研报原始记录聚合成 13 个 consensus canonical 字段。

    Args:
        reports: 东财研报列表原始记录（每条含 ``predictThisYearEps`` / ``emRatingValue`` 等）。
        as_of_date: 聚合基准日（``YYYY-MM-DD``）；缺省用今天。
        target_price_coverage_threshold: 目标价覆盖率阈值（0-1），低于则降级为 missing。
        rating_window_days: 评级上/下调统计窗口（天），缺省 30。
    """
    asof = _parse_date(as_of_date) or date.today()
    reports = list(reports or [])
    out = ConsensusOutput(as_of_date=asof.isoformat(), report_count=len(reports))

    values = out.values
    missing = out.missing

    for f in CONSENSUS_FIELDS:
        values[f] = None

    # 覆盖机构数
    values["coverage_count"] = _coverage_count(reports)

    if not reports:
        missing.extend(f"consensus_missing: {f}" for f in CONSENSUS_FIELDS)
        return out

    # ── 1. 一致预期 EPS（按报告期聚合同日中位数，取基准日最近一天）──
    series = {canon: _daily_median_series(reports, src) for canon, src in EPS_SOURCE_FIELDS.items()}
    for canon in ("eps_fy1", "eps_fy2", "eps_fy3"):
        v = _latest_at_or_before(series[canon], asof)
        values[canon] = v
        if v is None:
            missing.append(f"consensus_missing: {canon}")

    # ── 2. EPS revision（PERCENT，缺失置 null）──
    for window, canon in ((30, "eps_fy1_revision_1m"), (90, "eps_fy1_revision_3m")):
        past = _latest_at_or_before(series["eps_fy1"], asof - timedelta(days=window))
        values[canon] = _pct_change(past, values["eps_fy1"])
        if values[canon] is None:
            missing.append(f"consensus_missing: {canon}")

    past = _latest_at_or_before(series["eps_fy2"], asof - timedelta(days=30))
    values["eps_fy2_revision_1m"] = _pct_change(past, values["eps_fy2"])
    if values["eps_fy2_revision_1m"] is None:
        missing.append("consensus_missing: eps_fy2_revision_1m")

    # ── 3. revision_acceleration（revision_1m 的二阶差分）──
    rev_series = _revision_series(series["eps_fy1"], 30)
    if len(rev_series) >= 2:
        values["revision_acceleration"] = round(rev_series[-1][1] - rev_series[-2][1], 4)
    else:
        values["revision_acceleration"] = None
        missing.append("consensus_missing: revision_acceleration")

    # ── 4. rating_mean（emRatingValue 均值，越大越看多）──
    values["rating_mean"] = _rating_mean(reports)
    if values["rating_mean"] is None:
        missing.append("consensus_missing: rating_mean")

    # ── 5. 评级上/下调 + revision_breadth（emRatingValue vs lastEmRatingValue 差分）──
    ups, downs = _rating_direction_counts(reports, asof, rating_window_days)
    values["rating_upgrade_1m"] = ups
    values["rating_downgrade_1m"] = downs
    if ups is None or downs is None:
        missing.append("consensus_missing: rating_upgrade_1m")
        missing.append("consensus_missing: rating_downgrade_1m")
        values["revision_breadth"] = None
        missing.append("consensus_missing: revision_breadth")
    elif (ups + downs) > 0:
        values["revision_breadth"] = round(ups / (ups + downs), 4)
    else:
        values["revision_breadth"] = None
        missing.append("consensus_missing: revision_breadth")

    # ── 6. target_price（有值子集中位数；覆盖率 < 阈值 → 显式降级）──
    tp_vals = [v for v in (_to_float(r.get("indvAimPriceT")) for r in reports) if v is not None]
    tp = _median(tp_vals)
    tp_coverage = len(tp_vals) / len(reports) if reports else 0.0
    if tp is None:
        values["target_price"] = None
        missing.append("consensus_missing: target_price")
    elif tp_coverage < target_price_coverage_threshold:
        values["target_price"] = None
        missing.append("consensus_missing: target_price")
        out.warnings.append(
            f"target_price dropped: coverage {tp_coverage:.1%} < threshold {target_price_coverage_threshold:.0%}"
        )
    else:
        values["target_price"] = tp

    return out
