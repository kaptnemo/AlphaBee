"""本地财报「主要业务」片段（对标组在线兜底来源）。

年报/半年报的「第三节 管理层讨论与分析 → 报告期内公司从事的主要业务」含行业定位、
主营业务/产品描述，是对标选股最可靠的一手依据（比东财研报摘要完整得多）。
本模块从**本地已解析财报**定位标的最近一期报告，截取该章节作为 LLM 对标抽取的片段：

- 定位：``reports/<公司名>(<代码>)/财报/<报告期>/.report_manifest.json``（``report_period``
  给出年期与报告类型，取最近一期：年报 > 半年报 > 季报）；
- 取文：manifest 的 ``full_text_path``（``reports_full/.../<报告>.md`` 全文），截取
  ``## 第三节`` 至 ``## 第四节``（管理层讨论与分析）。

fail-open：无本地报告 / manifest 缺失 / 全文不可读 → 空片段 + ``meta.note``（降级不编造）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alphabee import PROJECT_ROOT

__all__ = ["fetch_local_report_fragments"]

REPORT_DIR = PROJECT_ROOT / "reports"
_MANIFEST_NAME = ".report_manifest.json"

#: 片段字符上限（控制 LLM prompt token；管理层讨论章节通常远小于此）。
_DEFAULT_MAX_CHARS = 12000

#: 报告类型优先级：年报 > 半年报 > 三季报 > 一季报/二季报（同一年内取更高期次）。
#: 顺序敏感：``"半年度报告"`` 含子串 ``"年度报告"``，必须先匹配更具体的半年报。
_PERIOD_RANK = (
    ("半年度报告", 4),
    ("年度报告", 5),
    ("第三季度报告", 3),
    ("三季度报告", 3),
    ("第二季度报告", 2),
    ("二季度报告", 2),
    ("第一季度报告", 1),
    ("一季度报告", 1),
)


def _period_sort_key(period: str) -> tuple[int, int]:
    """``"2026年半年度报告"`` → ``(2026, 4)``（无法解析 → ``(0, 0)``）。"""
    year_match = re.search(r"(\d{4})", period)
    year = int(year_match.group(1)) if year_match else 0
    rank = 0
    for needle, value in _PERIOD_RANK:
        if needle in period:
            rank = value
            break
    return (year, rank)


def _company_report_dir(symbol: str) -> Path | None:
    """按 6 位代码定位 ``reports/<公司名>(<代码>)`` 目录（无 → ``None``）。"""
    digits = str(symbol or "").strip().split(".")[0]
    if not digits.isdigit():
        return None
    matches = sorted(REPORT_DIR.glob(f"*({digits})")) if REPORT_DIR.is_dir() else []
    return matches[0] if matches else None


def _latest_report_manifest(symbol: str) -> dict[str, Any] | None:
    """取标的最近一期报告的 manifest（按 ``report_period`` 年份+类型排序）。"""
    company_dir = _company_report_dir(symbol)
    if company_dir is None:
        return None
    candidates: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for manifest_path in (company_dir / "财报").glob(f"*/{_MANIFEST_NAME}"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        period = str(manifest.get("report_period") or manifest_path.parent.name)
        candidates.append((_period_sort_key(period), manifest))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _extract_management_section(text: str, *, max_chars: int) -> str:
    """截取「第三节 管理层讨论与分析」至「第四节」（无第四节则截到上限）。"""
    start = re.search(r"^##\s*第三节[^\n]*", text, flags=re.MULTILINE)
    if start is None:
        return text[:max_chars].strip()
    tail = text[start.start() :]
    end = re.search(r"^##\s*第四节[^\n]*", tail, flags=re.MULTILINE)
    section = tail[: end.start()] if end is not None else tail[:max_chars]
    return section[:max_chars].strip()


def fetch_local_report_fragments(
    symbol: str,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> tuple[list[str], dict[str, Any]]:
    """取标的最近一期本地财报「管理层讨论与分析」章节作为 LLM 对标抽取片段（fail-open）。

    Returns:
        ``(fragments, meta)``：``fragments`` 为章节文本列表（可能为空）；
        ``meta`` 含 ``note`` / ``report_period`` / ``full_text_path`` / ``error``。
    """
    meta: dict[str, Any] = {"note": "", "report_period": "", "full_text_path": "", "error": None}
    manifest = _latest_report_manifest(symbol)
    if manifest is None:
        meta["note"] = "无本地财报（reports/ 未命中该标的），跳过本地报告兜底"
        return [], meta

    meta["report_period"] = str(manifest.get("report_period") or "")
    full_text_path = str(manifest.get("full_text_path") or "")
    meta["full_text_path"] = full_text_path
    if not full_text_path:
        meta["note"] = "本地财报 manifest 无 full_text_path"
        return [], meta

    try:
        text = Path(full_text_path).read_text(encoding="utf-8")
    except OSError as exc:
        meta["error"] = str(exc)
        meta["note"] = f"本地财报全文不可读: {exc}"
        return [], meta

    section = _extract_management_section(text, max_chars=max_chars)
    if not section:
        meta["note"] = "本地财报未截取到管理层讨论与分析章节"
        return [], meta
    return [section], meta
