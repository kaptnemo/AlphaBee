"""申万分类学（判定 E）：**召回池 + 特征**（设计 §3.1/§3.3/§3.6/§6 Step 3）。

E 的两个角色（设计 §3.1）：

1. **召回池**：``business_description`` 路径额外并入「同 L3 成分」（残差桶则用 L2），
   保证真对标进入候选（治「漏」）；
2. **特征**：对 target 与每个候选计算 ``same_l3`` / ``same_l2``，并给 target 标
   ``taxonomy_reliable``（源 = 本地静态快照，实时计算）。

**绝不硬闸**（设计 §3.3/§6 Step 3）：本模块只**产出**召回池与特征，Gate
（``peer_group_build.gate_candidates``）不得以本模块的任何取值作为剔除条件。

数据来源与单一来源纪律（``data-source-contract``）：本模块只读仓库内静态快照
``alphabee/static/all_stocks.csv``（**申万 2021** L1/L2/L3，随提交刷新），本地、无网络；
target 与候选的归属**同取一份快照**，不跨分类体系（不以证监会口径 ``industry`` 去匹配申万名）。

**确定性**：``same_l3`` / ``same_l2`` / ``taxonomy_reliable`` 均为快照（``lru_cache``）上的纯函数，
同输入同输出；**代码不在快照 ⇒ ``None``**（未知，不得误判为 ``False`` —— 类别不同与未知是两回事）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from alphabee import PROJECT_ROOT

__all__ = [
    "L3_RESIDUAL_PREFIX",
    "RESIDUAL_L3_MIN_FALLBACK",
    "TAXONOMY_CSV",
    "TaxonomyEntry",
    "TaxonomyReliability",
    "annotate_same_levels",
    "assess_reliability",
    "l2_constituents",
    "l3_constituents",
    "recall_pool",
    "same_levels",
    "stock_taxonomy",
    "taxonomy_entry",
]

#: 静态股票表（申万 2021 归属快照；列：stock_code / sw_l1_code…sw_l3_name）。
TAXONOMY_CSV = PROJECT_ROOT / "alphabee" / "static" / "all_stocks.csv"

#: 残差桶判据之一：L3 名以「其他」开头（如「其他专用设备」）⇒ 同 L3 异质，分类学不可信。
L3_RESIDUAL_PREFIX = "其他"

#: 残差桶判据之二（成分数下限）的**兜底默认**：唯一权威默认处 = 配置
#: ``company_track.peer_quality.residual_l3_min_constituents``；本常量仅供缺段/异常时回落。
RESIDUAL_L3_MIN_FALLBACK = 15


@dataclass(frozen=True)
class TaxonomyEntry:
    """一只股票的申万归属（L1/L2/L3 代码 + 名称；缺失字段为空串）。"""

    code: str
    name: str = ""
    l1_code: str = ""
    l1_name: str = ""
    l2_code: str = ""
    l2_name: str = ""
    l3_code: str = ""
    l3_name: str = ""

    @property
    def has_l3(self) -> bool:
        return bool(self.l3_code)

    @property
    def has_l2(self) -> bool:
        return bool(self.l2_code)

    @property
    def is_residual_l3_name(self) -> bool:
        """L3 名是否以「其他」开头（残差桶触发条件之一）。"""
        return self.l3_name.startswith(L3_RESIDUAL_PREFIX)


@dataclass(frozen=True)
class TaxonomyReliability:
    """target 的分类学**可信度**判定结果（设计 §3.1 残差桶自适应）。"""

    reliable: bool
    reason: str
    entry: TaxonomyEntry | None = None
    l3_constituents: int = 0
    l2_constituents: int = 0

    @property
    def recall_level(self) -> str:
        """可信度下的召回层级：``l3``（可信）/ ``l2``（残差桶降级）/ ``none``（无分类学数据）。"""
        if self.reliable:
            return "l3"
        if self.entry is not None and self.entry.has_l2:
            return "l2"
        return "none"


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


@lru_cache(maxsize=1)
def stock_taxonomy() -> dict[str, TaxonomyEntry]:
    """静态快照 → ``stock_code（大写）→ TaxonomyEntry``（读失败/文件缺失 → 空表，不抛）。"""
    if not TAXONOMY_CSV.is_file():
        return {}
    table: dict[str, TaxonomyEntry] = {}
    try:
        with TAXONOMY_CSV.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                code = _clean(row.get("stock_code")).upper()
                if not code:
                    continue
                table[code] = TaxonomyEntry(
                    code=code,
                    name=_clean(row.get("company_name")),
                    l1_code=_clean(row.get("sw_l1_code")),
                    l1_name=_clean(row.get("sw_l1_name")),
                    l2_code=_clean(row.get("sw_l2_code")),
                    l2_name=_clean(row.get("sw_l2_name")),
                    l3_code=_clean(row.get("sw_l3_code")),
                    l3_name=_clean(row.get("sw_l3_name")),
                )
    except OSError:
        return {}
    return table


@lru_cache(maxsize=1)
def _members_by(level: str) -> dict[str, tuple[str, ...]]:
    """``level → {成分代码: (成员代码, …)}``（成员按快照顺序，稳定保序）。"""
    key = "l3_code" if level == "l3" else "l2_code"
    groups: dict[str, list[str]] = {}
    for code, entry in stock_taxonomy().items():
        bucket = getattr(entry, key)
        if bucket:
            groups.setdefault(bucket, []).append(code)
    return {bucket: tuple(members) for bucket, members in groups.items()}


def taxonomy_entry(code: str) -> TaxonomyEntry | None:
    """代码 → 申万归属；**不在快照 ⇒ ``None``**（未知，不伪造）。"""
    key = _clean(code).upper()
    return stock_taxonomy().get(key) if key else None


def l3_constituents(l3_code: str) -> tuple[str, ...]:
    """同 L3 成分代码（不含 target 的排除由调用方做；无该层级 ⇒ 空元组）。"""
    return _members_by("l3").get(_clean(l3_code), ())


def l2_constituents(l2_code: str) -> tuple[str, ...]:
    """同 L2 成分代码（残差桶降级召回用）。"""
    return _members_by("l2").get(_clean(l2_code), ())


def assess_reliability(
    code: str,
    *,
    min_constituents: int = RESIDUAL_L3_MIN_FALLBACK,
) -> TaxonomyReliability:
    """判定 target 的分类学可信度（设计 §3.1 残差桶自适应）。

    不可信的两个**各自独立**的触发条件：

    1. L3 名以「其他」开头（残差桶名，如「其他专用设备」）；
    2. 同 L3 成分数 < ``min_constituents``（默认 15，来自配置）。

    另有「代码不在快照 / 无 L3」两种数据缺失情形，同样判不可信（并说明原因）。
    不可信时：**召回改用 L2**、特征降权（``same_l3`` 不再注入，见 :func:`same_levels`）、
    artifact/notes 由调用方标注「分类兜底，未经业务核验」。
    """
    entry = taxonomy_entry(code)
    if entry is None:
        return TaxonomyReliability(reliable=False, reason=f"代码 {code} 不在分类学快照", entry=None)
    if not entry.has_l3:
        return TaxonomyReliability(reliable=False, reason=f"{code} 无申万 L3 归属", entry=entry)
    l3_count = len(l3_constituents(entry.l3_code))
    l2_count = len(l2_constituents(entry.l2_code))
    if entry.is_residual_l3_name:
        return TaxonomyReliability(
            reliable=False,
            reason=f"L3「{entry.l3_name}」为残差桶（以「{L3_RESIDUAL_PREFIX}」开头）",
            entry=entry,
            l3_constituents=l3_count,
            l2_constituents=l2_count,
        )
    if l3_count < int(min_constituents):
        return TaxonomyReliability(
            reliable=False,
            reason=f"L3「{entry.l3_name}」成分数 {l3_count} < {int(min_constituents)}",
            entry=entry,
            l3_constituents=l3_count,
            l2_constituents=l2_count,
        )
    return TaxonomyReliability(
        reliable=True,
        reason=f"L3「{entry.l3_name}」成分数 {l3_count} ≥ {int(min_constituents)}",
        entry=entry,
        l3_constituents=l3_count,
        l2_constituents=l2_count,
    )


def recall_pool(
    code: str,
    *,
    min_constituents: int = RESIDUAL_L3_MIN_FALLBACK,
) -> tuple[tuple[str, ...], str]:
    """E 的**召回池**（设计 §3.1）：可信 ⇒ 同 L3 成分；不可信 ⇒ 同 L2 成分（降级）。

    Returns:
        ``(成分代码元组, 层级)``；层级 ∈ ``l3`` / ``l2`` / ``none``（无可用分类学数据）。
        **只产池、不作判定依据** —— 是否采纳由下游 Gate 决定（设计 §3.3/§6 Step 3）。
    """
    reliability = assess_reliability(code, min_constituents=min_constituents)
    entry = reliability.entry
    if entry is None:
        return (), "none"
    if reliability.reliable:
        return l3_constituents(entry.l3_code), "l3"
    if entry.has_l2:
        return l2_constituents(entry.l2_code), "l2"
    return (), "none"


def same_levels(
    target_code: str,
    candidate_codes: list[str],
    *,
    l3_reliable: bool = True,
) -> dict[str, tuple[bool | None, bool | None]]:
    """逐候选给出 ``(same_l3, same_l2)``（**bool 或 None**；未知 ⇒ 两侧都 ``None``）。

    - 任一侧代码不在快照 / 该侧无对应层级 ⇒ 未知 ⇒ ``None``（不得误判为 ``False``）；
    - ``l3_reliable=False``（残差桶降级）⇒ **特征降权**：``same_l3`` 一律 ``None``
      （L3 在该场景不可信、不注入），``same_l2`` 照常计算。
    """
    target = taxonomy_entry(target_code)
    out: dict[str, tuple[bool | None, bool | None]] = {}
    for raw in candidate_codes:
        code = _clean(raw).upper()
        if not code:
            continue
        candidate = taxonomy_entry(code)
        if target is None or candidate is None or not target.has_l2 or not candidate.has_l2:
            out[code] = (None, None)
            continue
        same_l2: bool = target.l2_code == candidate.l2_code
        same_l3: bool | None = None
        if l3_reliable and target.has_l3 and candidate.has_l3:
            same_l3 = target.l3_code == candidate.l3_code
        out[code] = (same_l3, same_l2)
    return out


def annotate_same_levels(
    target_code: str,
    candidates: list[dict[str, Any]],
    *,
    l3_reliable: bool = True,
) -> list[dict[str, Any]]:
    """把 ``same_l3`` / ``same_l2`` 特征挂到候选 dict 上（**保序、不新增/不改动代码**）。

    候选缺 ``code`` 或代码未知 ⇒ 特征为 ``None``（设计 §3.6：无 E 数据时 judge 忽略该特征）。
    """
    codes = [str(item.get("code") or "") for item in candidates]
    features = same_levels(target_code, codes, l3_reliable=l3_reliable)
    annotated: list[dict[str, Any]] = []
    for item in candidates:
        merged: dict[str, Any] = dict(item)
        code = _clean(item.get("code")).upper()
        pair = features.get(code)
        if pair is not None:
            merged["same_l3"], merged["same_l2"] = pair
        annotated.append(merged)
    return annotated
