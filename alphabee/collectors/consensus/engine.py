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
- **财年对齐**：东财三个 EPS 字段是「相对研报发布年的相对年份」（``predictThisYearEps``
  指该研报视角的当年），而 payload 没有独立的财年字段（实测契约见
  ``docs/midterm/DATA_CONTRACTS_VERIFIED.md`` §1），因此一律按
  ``slot = 目标财年 - publishDate.year`` 解析字段。这样跨年时不会把 FY2026 与 FY2027
  的 EPS 混进同一个中位数（否则每年 1–6 月会出现「增长率级」的假 revision）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median as _stat_median
from typing import Any

# 东财 EPS 字段 vs 「slot = 目标财年 - 报告发布年」：slot 0/1/2 依次对应当年/次年/后年。
# 这是本模块唯一允许按发布年解析原始列名的地方，禁止再用固定字段名映射（会财年混叠）。
FY_SLOT_FIELDS: tuple[str, str, str] = (
    "predictThisYearEps",
    "predictNextYearEps",
    "predictNextTwoYearEps",
)

#: 一致预期快照的最小机构数；不足视为「不可信」→ 值置 None 并登记 ``consensus_missing``。
#: 与打分层 ``check_message_limit`` 无关，同构于 ``score_engine._MIN_CONSENSUS_COVERAGE``。
MIN_SNAPSHOT_ORGS = 3

#: 快照允许的观点过期天数（相对各自 as-of 基准日）。取值 ≈ 研报拉取窗口
#: （``build_consensus(months=12)`` ≈ 365 天）：财年对齐依赖「旧报告通过 slot=1/2
#: 贡献新财年」，收紧到更小值会牺牲跨年覆盖，因此不给更严的默认值。
EPS_LOOKBACK_DAYS: int | None = 365

# 13 个 consensus canonical 字段（与 schemas/consensus.yaml、midterm/models.py 对齐）

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
    - ``fy_anchor``：EPS 相关字段锚定的财年（= ``as_of_date`` 所在自然年）。``eps_fy1``
      指的就是这个财年的一致预期，下游/报告据此判断是否已跨年换口径。
    - ``eps_basis``：canonical 字段 → 该值所用快照的机构数（样本量披露；不足
      ``MIN_SNAPSHOT_ORGS`` 时对应值已置 ``None``）。
    """

    values: dict[str, float | int | None] = field(default_factory=dict)
    source: str = "eastmoney:report/list"
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    as_of_date: str = ""
    report_count: int = 0
    fy_anchor: int | None = None
    eps_basis: dict[str, int] = field(default_factory=dict)


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
    """(recent / past - 1) × 100，PERCENT 口径；任一缺失或 ``past <= 0`` → None。

    ``past < 0``（亏损基数）时百分比修正没有意义（例如 -0.5 → 1.9 会得到 -480%），
    统一视为不可算，由调用方降级为 ``consensus_missing``。
    """
    if past is None or recent is None or past <= 0:
        return None
    return round((recent / past - 1.0) * 100.0, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 时序/评级聚合纯函数
# ─────────────────────────────────────────────────────────────────────────────
def _org_fiscal_snapshot(
    reports: list[dict[str, Any]],
    fiscal_year: int,
    asof: date,
    lookback_days: int | None = EPS_LOOKBACK_DAYS,
) -> tuple[float | None, int]:
    """as-of 快照：每家机构对 ``fiscal_year`` 的最新观点 → (横截面中位数, 机构数)。

    「对某财年的观点」按 ``slot = fiscal_year - publishDate.year`` 解析：
    slot 0/1/2 依次取 ``predictThisYearEps`` / ``predictNextYearEps`` /
    ``predictNextTwoYearEps``；slot 越界（该研报说不到这个财年）则跳过。

    每家机构只保留 ``publishDate ≤ asof`` 的最新一条，因此跨年时新老口径被对齐到
    同一财年（老报告通过 slot=1/2 贡献新财年），不会把两个财年的 EPS 混进同一中位数。
    ``lookback_days`` 非空时，早于 ``asof - lookback_days`` 的研报视为观点过期。
    """
    latest: dict[str, tuple[date, float]] = {}
    for r in reports:
        org = str(r.get("orgCode") or "").strip()
        if not org:
            continue
        d = _parse_date(r.get("publishDate"))
        if d is None or d > asof:
            continue
        if lookback_days is not None and (asof - d).days > lookback_days:
            continue
        slot = fiscal_year - d.year
        if not 0 <= slot <= 2:
            continue
        v = _to_float(r.get(FY_SLOT_FIELDS[slot]))
        if v is None:
            continue
        cur = latest.get(org)
        if cur is None or d > cur[0]:
            latest[org] = (d, v)
    vals = [v for _, v in latest.values()]
    return (_median(vals) if vals else None), len(vals)


def _revision(
    reports: list[dict[str, Any]],
    fiscal_year: int,
    asof: date,
    lag_days: int,
    lookback_days: int | None = EPS_LOOKBACK_DAYS,
) -> tuple[float | None, int]:
    """同一财年的 ``lag_days`` 修正幅度（PERCENT）→ (值, 两侧快照的最小机构数)。

    分子分母都对同一 ``fiscal_year`` 求 :func:`_org_fiscal_snapshot`，因此是
    「同财年、同群体规则、同窗口宽度」的可比量：跨年时不会退化成 FY2/FY1 的增长率。
    任一快照缺失、非正值（亏损基数）或机构数 < ``MIN_SNAPSHOT_ORGS`` → ``None``。
    """
    now_v, now_n = _org_fiscal_snapshot(reports, fiscal_year, asof, lookback_days)
    past_v, past_n = _org_fiscal_snapshot(reports, fiscal_year, asof - timedelta(days=lag_days), lookback_days)
    n = min(now_n, past_n)
    if now_v is None or past_v is None or now_v <= 0 or past_v <= 0:
        return None, n
    if n < MIN_SNAPSHOT_ORGS:
        return None, n
    return round((now_v / past_v - 1.0) * 100.0, 4), n


def _revision_acceleration(
    reports: list[dict[str, Any]],
    fiscal_year: int,
    asof: date,
    *,
    lag_days: int = 30,
    lookback_days: int | None = EPS_LOOKBACK_DAYS,
) -> tuple[float | None, str | None, int]:
    """修正加速度（pp / ``lag_days`` 天）：``r(t) - r(t-lag)`` → (值, 原因, 机构数)。

    与对外披露的 ``eps_fy1_revision_1m`` **同源**：``r(t)`` 就是 :func:`_revision` 在
    同一 ``fiscal_year`` 上的返回值，因此加速度与 revision 不会各说各话。
    二阶差分必须在**固定时间网格**上取（``t`` / ``t-lag`` / ``t-2·lag``），不能用
    「最后两个有研报的发布日」——那样会把间隔 1 天与间隔 3 个月的变化当成同一量纲。
    """
    rev_now, n_now = _revision(reports, fiscal_year, asof, lag_days, lookback_days)
    if rev_now is None:
        return None, f"revision(t) unavailable (n={n_now})", n_now
    rev_prev, n_prev = _revision(reports, fiscal_year, asof - timedelta(days=lag_days), lag_days, lookback_days)
    if rev_prev is None:
        return None, f"revision(t-{lag_days}d) unavailable (n={n_prev})", min(n_now, n_prev)
    return round(rev_now - rev_prev, 4), None, min(n_now, n_prev)


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
    """覆盖机构数（优先 ``orgCode``，回退 ``orgSName``/``orgName``）；无机构信息时退化为研报条数。

    回退链不能省：``orgCode`` 是东财原始列（实测 100% 覆盖），但一旦上游换源/裁剪字段，
    静默退化到「研报条数」会把覆盖度按条数放大（同一家发 5 篇算 5 家），
    进而让下游的覆盖护栏失效。
    """
    if not reports:
        return None
    orgs = {str(r.get("orgCode") or r.get("orgSName") or r.get("orgName") or "").strip() for r in reports}
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

    EPS 相关字段（``eps_fy1/2/3`` 与三项 revision、``revision_acceleration``）统一按
    **锚定财年** ``FY = as_of_date 所在自然年`` 计算：``eps_fy1`` 指 FY 年，``eps_fy2``
    指 FY+1，``eps_fy3`` 指 FY+2；跨年时老研报通过 slot 解析继续贡献新财年，不会出现
    「同一中位数混两个财年」或「1 月假上修」。快照机构数 < ``MIN_SNAPSHOT_ORGS`` 时
    对应值置 ``None`` 并登记 ``consensus_missing``（宁缺勿错），实际样本量见
    :attr:`ConsensusOutput.eps_basis`。

    Args:
        reports: 东财研报列表原始记录（每条含 ``predictThisYearEps`` / ``emRatingValue`` 等）。
        as_of_date: 聚合基准日（``YYYY-MM-DD``）；缺省用今天。同时决定锚定财年。
        target_price_coverage_threshold: 目标价覆盖率阈值（0-1），低于则降级为 missing。
        rating_window_days: 评级上/下调统计窗口（天），缺省 30。
    """
    asof = _parse_date(as_of_date) or date.today()
    reports = list(reports or [])
    out = ConsensusOutput(as_of_date=asof.isoformat(), report_count=len(reports), fy_anchor=asof.year)

    values = out.values
    missing = out.missing

    for f in CONSENSUS_FIELDS:
        values[f] = None

    # 覆盖机构数
    values["coverage_count"] = _coverage_count(reports)

    if not reports:
        missing.extend(f"consensus_missing: {f}" for f in CONSENSUS_FIELDS)
        return out

    # ── 1. 一致预期 EPS：锚定财年 FY = asof 年；每家机构取能覆盖该财年的最新一篇 ──
    fy_anchor = asof.year
    for offset, canon in enumerate(("eps_fy1", "eps_fy2", "eps_fy3")):
        v, n = _org_fiscal_snapshot(reports, fy_anchor + offset, asof)
        out.eps_basis[canon] = n
        values[canon] = v if n >= MIN_SNAPSHOT_ORGS else None
        if values[canon] is None:
            missing.append(f"consensus_missing: {canon}")

    # ── 2. EPS revision（同一锚定财年的 as-of 回放；PERCENT，缺失/样本不足置 null）──
    for lag_days, offset, canon in (
        (30, 0, "eps_fy1_revision_1m"),
        (90, 0, "eps_fy1_revision_3m"),
        (30, 1, "eps_fy2_revision_1m"),
    ):
        rev, n = _revision(reports, fy_anchor + offset, asof, lag_days)
        values[canon] = rev
        out.eps_basis[canon] = n
        if rev is None:
            missing.append(f"consensus_missing: {canon}")

    # ── 3. revision_acceleration（revision_1m 的二阶差分，固定 30 天网格）──
    accel, _reason, accel_n = _revision_acceleration(reports, fy_anchor, asof)
    values["revision_acceleration"] = accel
    out.eps_basis["revision_acceleration"] = accel_n
    if accel is None:
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
