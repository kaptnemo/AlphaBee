"""行业基准推导（industry-context-injection Phase 0 垂直切片）。

纯函数层：从行业成分股的财务记录推导行业基准（中位数），
与数据源解耦——上游（resolve_industry_context 节点）负责取数，
本模块只负责"记录列表 → 基准"这一确定性变换，便于离线单测。

推导规则（与 docs/industry/industry-context-injection-plan.md 对齐）：
- 各基准取成分股记录的非空值**中位数**（对异常值稳健，优于均值）
- 某个字段全部缺失 → 该基准为 None，不注入 fact_values（下游回退默认阈值）
- 估值基准（pe_ttm / pb）直接透传行业指数快照值，不做中位数
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 注入 fact_values 的 canonical 字段（与 alphabee/schemas/industry.yaml 对齐）
INDUSTRY_BENCHMARK_FIELDS = (
    "industry_revenue_yoy",
    "industry_avg_roe",
    "industry_avg_debt_ratio",
    "industry_avg_gross_margin",
)

# canonical 字段名 → 基准类别（Phase 1：artifact v2 三组字典分组依据）
# valuation=估值 / financial=财务 / growth=成长（定性类由 artifact 单独判定）
BENCHMARK_CATEGORIES: dict[str, str] = {
    "industry_pe_ttm": "valuation",
    "industry_pb": "valuation",
    "industry_avg_roe": "financial",
    "industry_avg_debt_ratio": "financial",
    "industry_avg_gross_margin": "financial",
    "industry_revenue_yoy": "growth",
}

_CATEGORY_ORDER = ("valuation", "financial", "growth")


def group_benchmarks(
    flat: dict[str, float | None],
) -> tuple[dict[str, float | None], dict[str, float | None], dict[str, float | None]]:
    """把扁平 canonical 基准字典按类别分组（valuation, financial, growth）。

    未知键丢弃（防拼写漂移进入 artifact）；None 值保留（展示口径一致性）。
    """
    groups: dict[str, dict[str, float | None]] = {cat: {} for cat in _CATEGORY_ORDER}
    for key, value in flat.items():
        category = BENCHMARK_CATEGORIES.get(key)
        if category is not None:
            groups[category][key] = value
    return groups["valuation"], groups["financial"], groups["growth"]


def flatten_benchmarks(
    valuation: dict[str, float | None],
    financial: dict[str, float | None],
    growth: dict[str, float | None],
) -> dict[str, float | None]:
    """三组基准合并回扁平字典（供注入 fact_values / 展示）。"""
    merged: dict[str, float | None] = {}
    for group in (valuation, financial, growth):
        merged.update(group)
    return merged


# canonical 字段名 → IndustryBenchmarks 属性名（供 artifact 构建等外部复用）
BENCHMARK_FIELD_ATTR = {
    "industry_revenue_yoy": "revenue_yoy",
    "industry_avg_roe": "avg_roe",
    "industry_avg_debt_ratio": "avg_debt_ratio",
    "industry_avg_gross_margin": "avg_gross_margin",
}

# 成分股记录里对应的原始键（canonical 名称）
_PEER_KEY_MAP = {
    "industry_revenue_yoy": "revenue_yoy",
    "industry_avg_roe": "roe",
    "industry_avg_debt_ratio": "debt_ratio",
    "industry_avg_gross_margin": "gross_margin",
}


@dataclass
class IndustryBenchmarks:
    """一组行业基准值；None 表示该基准不可得（下游回退默认阈值）。"""

    industry: str
    sw_code: str | None = None
    as_of_date: str = ""
    peer_count: int = 0
    revenue_yoy: float | None = None
    avg_roe: float | None = None
    avg_debt_ratio: float | None = None
    avg_gross_margin: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    source_refs: list[str] = field(default_factory=list)

    def to_fact_values(self) -> dict[str, float]:
        """可注入 fact_values 的数值字段（None 不注入，保证"缺失即回退"）。"""
        out: dict[str, float] = {}
        for field_name in INDUSTRY_BENCHMARK_FIELDS:
            value = getattr(self, BENCHMARK_FIELD_ATTR[field_name])
            if value is not None:
                out[field_name] = value
        if self.pe_ttm is not None:
            out["industry_pe_ttm"] = self.pe_ttm
        if self.pb is not None:
            out["industry_pb"] = self.pb
        return out

    def to_category_dicts(
        self,
    ) -> tuple[dict[str, float | None], dict[str, float | None], dict[str, float | None]]:
        """按类别分组的基准视图（artifact v2 的三组字典，含 None 占位）。"""
        flat: dict[str, float | None] = {
            field_name: getattr(self, BENCHMARK_FIELD_ATTR[field_name]) for field_name in INDUSTRY_BENCHMARK_FIELDS
        }
        flat["industry_pe_ttm"] = self.pe_ttm
        flat["industry_pb"] = self.pb
        return group_benchmarks(flat)

    def has_financial_benchmarks(self) -> bool:
        """是否有任何财务/成长基准（区别于只有估值快照）。"""
        return any(getattr(self, BENCHMARK_FIELD_ATTR[name]) is not None for name in INDUSTRY_BENCHMARK_FIELDS)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def derive_benchmarks(
    peer_records: list[dict[str, Any]],
    *,
    industry: str,
    sw_code: str | None = None,
    as_of_date: str = "",
    pe_ttm: float | None = None,
    pb: float | None = None,
    source_refs: list[str] | None = None,
) -> IndustryBenchmarks:
    """从成分股财务记录推导行业基准。

    估值基准（Phase 1 估值补全）：
    - 优先取成分股记录的 ``pe_ttm`` / ``pb_ratio`` 中位数（**仅正值**——亏损股负
      PE 无估值水平意义，剔除避免扭曲中位数）；
    - 成分股无估值数据时回退 ``pe_ttm`` / ``pb`` 指数快照参数（Phase 0 语义，透传）；
    - 使用中位数时在 ``source_refs`` 追加 ``valuation:peer_median`` 留痕。

    Args:
        peer_records: 成分股记录列表，每条为 ``{revenue_yoy, roe, debt_ratio,
            gross_margin, pe_ttm, pb_ratio}``（canonical 键，值可为 None/缺失）。
        industry: 行业名（展示用）。
        sw_code: 申万行业指数代码。
        as_of_date: 数据日期。
        pe_ttm / pb: 行业指数估值快照（**兜底**，成分股中位数优先）。
        source_refs: 数据来源描述，用于血缘。

    Returns:
        IndustryBenchmarks，各字段为对应记录的非空值中位数。
    """
    buckets: dict[str, list[float]] = {name: [] for name in _PEER_KEY_MAP.values()}
    peer_pe: list[float] = []
    peer_pb: list[float] = []
    for record in peer_records:
        if not isinstance(record, dict):
            continue
        for peer_key in buckets:
            value = _safe_float(record.get(peer_key))
            if value is not None:
                buckets[peer_key].append(value)
        pe_value = _safe_float(record.get("pe_ttm"))
        pb_value = _safe_float(record.get("pb_ratio"))
        if pe_value is not None and pe_value > 0:
            peer_pe.append(pe_value)
        if pb_value is not None and pb_value > 0:
            peer_pb.append(pb_value)

    final_pe = _median(peer_pe)
    final_pb = _median(peer_pb)
    refs = list(source_refs or [])
    if final_pe is not None or final_pb is not None:
        refs.append("valuation:peer_median")
    if final_pe is None:  # 成分股中位数不可得 → 回退指数快照
        final_pe = _safe_float(pe_ttm)
    if final_pb is None:
        final_pb = _safe_float(pb)

    return IndustryBenchmarks(
        industry=industry,
        sw_code=sw_code,
        as_of_date=as_of_date,
        peer_count=len(peer_records),
        revenue_yoy=_median(buckets["revenue_yoy"]),
        avg_roe=_median(buckets["roe"]),
        avg_debt_ratio=_median(buckets["debt_ratio"]),
        avg_gross_margin=_median(buckets["gross_margin"]),
        pe_ttm=final_pe,
        pb=final_pb,
        source_refs=refs,
    )
