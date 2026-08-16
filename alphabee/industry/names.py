"""行业名规范字典（industry-context Phase 2，B1 优化落地）。

单一事实来源（``industry_names.yaml``）：行业 key（``classification_standard:code``）
↔ 显示名 + 别名 + 行业组，消灭散落各处的硬编码中文名（thesis engine/reviewer 的
``_FINANCIAL_INDUSTRIES`` 等、company_context 的关键词抽取表）。

本模块不依赖任何数据源（无 import 副作用），可被 orchestrator / thesis / crosscheck
安全引用。完整申万 L2/L3 目录未静态收录——仅收录行业组/别名解析所需成员，
未知名称的组归属判断一律返回 False（安全降级，不误报）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_CATALOG_PATH = Path(__file__).parent / "industry_names.yaml"

# 申万二级名称的罗马数字后缀（"证券Ⅱ" → "证券"），仅用于匹配归一，不用于展示
_ROMAN_SUFFIXES = ("Ⅲ", "Ⅱ", "Ⅰ")


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(_CATALOG_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def catalog() -> dict[str, str]:
    """行业 key → 显示名（l1 + l2 合并）。"""
    data = _load()
    merged: dict[str, str] = {}
    merged.update(data.get("l1") or {})
    merged.update(data.get("l2") or {})
    return merged


def aliases() -> dict[str, str]:
    """别名 → 规范名。"""
    return dict(_load().get("aliases") or {})


def group_defs() -> dict[str, list[str]]:
    """行业组名 → 行业 key 列表。"""
    return {name: list(items) for name, items in (_load().get("groups") or {}).items()}


def group_keys(group_name: str) -> set[str]:
    """行业组名 → 行业 key 集合（未知组返回空集）。"""
    return set(group_defs().get(group_name) or [])


# ── 名称归一与解析 ─────────────────────────────────────────────────────────


def _strip_roman_suffix(name: str) -> str:
    """去掉尾部罗马数字后缀（"证券Ⅱ" → "证券"）；仅去一个。"""
    for suffix in _ROMAN_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def normalize_name(name: str) -> str:
    """别名归一 + 去罗马后缀 → 用于匹配的规范名（不用于展示）。"""
    stripped = (name or "").strip()
    if not stripped:
        return ""
    stripped = aliases().get(stripped, stripped)
    return _strip_roman_suffix(stripped)


def industry_display_name(key: str) -> str:
    """行业 key → 展示名；未知 key 回退 code 部分（``sw_l1:801780.SI`` → ``801780.SI``）。"""
    return catalog().get(key) or key.split(":", 1)[-1]


def industry_keys_for_name(name: str) -> list[str]:
    """中文名 → 匹配的行业 key 列表（别名/罗马后缀宽容，可命中多个层级）。"""
    normalized = normalize_name(name)
    if not normalized:
        return []
    keys: list[str] = []
    for key, display in catalog().items():
        if _strip_roman_suffix(display) == normalized:
            keys.append(key)
    return keys


def industry_in_group(name: str, group_name: str) -> bool:
    """中文名是否属于指定行业组（``group_name`` ∈ groups 键）。"""
    return any(key in group_keys(group_name) for key in industry_keys_for_name(name))


# ── 文本兜底抽取（迁移自 company_context._keyword_extract_industry）──────────
#
# 关键词 → 目标行业名（值尽量收敛到字典规范名；保留个别抽取专用标签如"新能源汽车"，
# 它们不在申万目录中，仅用于语境兜底）。顺序敏感：先命中先返回。

EXTRACTION_HINTS: tuple[tuple[str, str], ...] = (
    ("白酒", "白酒"),
    ("银行", "银行"),
    ("证券", "证券"),
    ("保险", "保险"),
    ("房地产", "房地产"),
    ("半导体", "半导体"),
    ("芯片", "半导体"),
    ("新能源汽车", "新能源汽车"),
    ("光伏", "光伏"),
    ("医药", "医药"),
    ("消费电子", "消费电子"),
    ("钢铁", "钢铁"),
    ("煤炭", "煤炭"),
    ("电力", "电力"),
    ("化工", "化工"),
    ("机械", "机械"),
    ("军工", "军工"),
    ("农林", "农林牧渔"),
    ("食品", "食品饮料"),
    ("家电", "家电"),
    ("纺织", "纺织服装"),
    ("建材", "建材"),
    ("建筑", "建筑装饰"),
    ("传媒", "传媒"),
    ("计算机", "计算机"),
    ("通信", "通信"),
    ("环保", "环保"),
    ("公用", "公用事业"),
    ("交通", "交通运输"),
)


def keyword_extract_industry(text: str) -> str:
    """从自由文本中按关键词兜底抽取行业名（公司语境 fallback）。"""
    lowered = (text or "").lower()
    for keyword, industry in EXTRACTION_HINTS:
        if keyword in lowered:
            return industry
    return ""
