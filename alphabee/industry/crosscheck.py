"""多来源行业交叉校验（industry-context Phase 2，item 9）。

对同一行业名在三个来源间交叉校验，产出**标准化 industry facts**（canonical 字段）：

- ``sw``：申万行业分类（tushare ``index_classify``，权威分类）→ sw_code / level；
- ``ths``：同花顺行业板块（akshare ``stock_board_industry_name_ths``）→ ths 板块代码；
- ``em``：东方财富行业板块快照（akshare ``stock_board_industry_name_em``）→ 板块 PE/PB
  （在申万指数 PE/PB 缺失时作为估值补源，见 Phase 1 估值缺口）。

纯函数 ``crosscheck_industry`` 不触网（输入即各来源 canonical 行），便于离线单测；
``fetch_industry_crosscheck`` 做 best-effort 采集。外部列名只存在于采集函数内部
（经 adapter 后一律 canonical）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from alphabee.industry.classification import match_sw_industry
from alphabee.industry.names import normalize_name


@dataclass
class SourceMatch:
    """单个来源的命中记录（字段全为 canonical）。"""

    source: str  # sw / ths / em
    industry: str  # 该来源口径下的行业名
    code: str = ""  # sw_code / industry_code
    level: str = ""  # L1 / L2 / L3（仅 sw）
    valuation: dict[str, float | None] = field(default_factory=dict)  # industry_pe_ttm / industry_pb


@dataclass
class IndustryCrossCheck:
    """多来源交叉校验结果（标准化 industry facts）。"""

    query: str  # 查询的行业名
    matches: list[SourceMatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    canonical_name: str = ""  # 多数来源一致的显示名（权重：sw > ths > em）
    canonical_valuation: dict[str, float | None] = field(default_factory=dict)
    sources_hit: int = 0

    def as_facts(self) -> dict[str, Any]:
        """标准化 industry facts（canonical 键，供下游/调试消费）。"""
        return {
            "query": self.query,
            "industry": self.canonical_name or self.query,
            "industry_pe_ttm": self.canonical_valuation.get("industry_pe_ttm"),
            "industry_pb": self.canonical_valuation.get("industry_pb"),
            "sources_hit": self.sources_hit,
            "warnings": list(self.warnings),
        }


def _pick_row(rows: list[dict], industry: str) -> dict | None:
    """canonical 行列表中按行业名匹配（精确优先，前缀兜底）。"""
    if not industry:
        return None
    exact = [row for row in rows if str(row.get("industry_name") or "") == industry]
    if exact:
        return exact[0]
    pref = [row for row in rows if str(row.get("industry_name") or "").startswith(industry)]
    return pref[0] if pref else None


def _match_sw(industry: str, frames: dict[str, Any]) -> SourceMatch | None:
    code, level = match_sw_industry(industry, frames)
    if not code or not level:
        return None
    frame = frames.get(level)
    name = ""
    if frame is not None and "sw_code" in frame.columns and "industry_name" in frame.columns:
        rows = frame[frame["sw_code"].astype(str) == code]
        if not rows.empty:
            name = str(rows.iloc[0].get("industry_name") or "")
    return SourceMatch(source="sw", industry=name or industry, code=code, level=level)


def crosscheck_industry(
    query: str,
    sw_frames: dict[str, Any],
    ths_rows: list[dict],
    em_rows: list[dict],
    sw_valuation: dict[str, float | None] | None = None,
) -> IndustryCrossCheck:
    """多来源交叉校验（纯函数，不触网）。

    Args:
        query: 待校验的行业名（如 "半导体"）。
        sw_frames: ``{level: DataFrame}``（canonical 列 industry_name / sw_code）。
        ths_rows: 同花顺板块行（canonical：industry_name / industry_code）。
        em_rows: 东方财富板块行（canonical：industry_name / industry_pe_ttm / industry_pb）。
        sw_valuation: 申万指数估值快照（industry_pe_ttm / industry_pb，可空），
            在 EM 未命中时兜底。
    """
    result = IndustryCrossCheck(query=query)

    sw = _match_sw(query, sw_frames)
    ths_row = _pick_row(ths_rows, query)
    em_row = _pick_row(em_rows, query)

    if sw is not None:
        result.matches.append(sw)
    else:
        result.warnings.append(f"申万分类未命中: {query}")

    if ths_row is not None:
        result.matches.append(
            SourceMatch(
                source="ths",
                industry=str(ths_row.get("industry_name") or query),
                code=str(ths_row.get("industry_code") or ""),
            )
        )
    else:
        result.warnings.append(f"同花顺板块未命中: {query}")

    if em_row is not None:
        result.matches.append(
            SourceMatch(
                source="em",
                industry=str(em_row.get("industry_name") or query),
                valuation={
                    "industry_pe_ttm": _safe_float(em_row.get("industry_pe_ttm")),
                    "industry_pb": _safe_float(em_row.get("industry_pb")),
                },
            )
        )
    else:
        result.warnings.append(f"东方财富板块未命中: {query}")

    result.sources_hit = len(result.matches)

    # ── canonical 名称：多数来源一致（归一后比较），平票时 sw 优先 ──
    names = [match.industry for match in result.matches]
    if names:
        normalized = [normalize_name(name) or name for name in names]
        counter = Counter(normalized)
        best_norm = max(counter, key=lambda item: (counter[item], -names.index(item)))
        index = normalized.index(best_norm)
        result.canonical_name = names[index]

    # ── 来源漂移告警：同 query 在不同来源口径名称不一致 ──
    if len({normalize_name(m.industry) or m.industry for m in result.matches}) > 1:
        result.warnings.append(
            "多来源行业名不一致（口径漂移）: " + " / ".join(f"{m.source}={m.industry}" for m in result.matches)
        )

    # ── canonical 估值：EM 快照优先（含 PE/PB），缺失时回退申万快照 ──
    em_valuation = next((m.valuation for m in result.matches if m.source == "em"), {})
    if any(v is not None for v in em_valuation.values()):
        result.canonical_valuation = dict(em_valuation)
    elif sw_valuation:
        result.canonical_valuation = dict(sw_valuation)

    return result


def _safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def fetch_industry_crosscheck(industry: str) -> IndustryCrossCheck:
    """best-effort 采集三来源数据后交叉校验（离线/CLI 用）。

    任何来源失败只记 warning，不中断；未知行业返回全 missed。
    """
    sw_frames: dict[str, Any] = {}
    ths_rows: list[dict] = []
    em_rows: list[dict] = []
    warnings: list[str] = []

    try:
        from alphabee.collectors.tushare.helper import TuShareHelper
        from alphabee.industry.classification import _SW_LEVELS

        with TuShareHelper() as helper:
            for level in _SW_LEVELS:
                try:
                    df = helper.index_classify(level=level, src="SW2021").data
                    if df is not None and not df.empty:
                        sw_frames[level] = df
                except Exception as exc:
                    warnings.append(f"申万分类({level})获取失败: {exc}")
    except Exception as exc:
        warnings.append(f"tushare 不可用: {exc}")

    try:
        from alphabee.collectors.akshare.helper import AkShareHelper

        with AkShareHelper() as helper:
            try:
                df = helper.stock_board_industry_name_ths().to_dataframe()
                ths_rows = df.to_dict(orient="records") if not df.empty else []
            except Exception as exc:
                warnings.append(f"同花顺板块列表获取失败: {exc}")
            try:
                df = helper.stock_board_industry_name_em().to_dataframe()
                em_rows = df.to_dict(orient="records") if not df.empty else []
            except Exception as exc:
                warnings.append(f"东方财富板块快照获取失败: {exc}")
    except Exception as exc:
        warnings.append(f"akshare 不可用: {exc}")

    result = crosscheck_industry(industry, sw_frames, ths_rows, em_rows)
    result.warnings = warnings + result.warnings
    return result
