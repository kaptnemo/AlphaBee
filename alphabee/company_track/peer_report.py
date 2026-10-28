"""本地财报「主要业务」片段（对标组在线兜底来源）。

本模块把「找哪一期报告」拆成两个互相独立的口径，避免"最新报告"与"内容最全的报告"
互相冒充：

- **财务锚定（发布时序）**：``_latest_report_manifest`` 按报告期**发布先后**排序
  （同一年内 一季报 → 半年报 → 三季报 → 年报；跨年按年），谁最后披露谁是"最新一期"。
  三季报入库后它就应压过同年半年报——这正是"报告期锚点"该有的语义。
- **内容抽取（完整度优先）**：``_content_report_manifest`` 仍按 年报 > 半年报 > 季报
  排序。季报没有「第三节 管理层讨论与分析 → 主要业务」章节，硬换三季报会让片段质量骤降。

``fetch_local_report_fragments`` 用**内容口径**取文（片段来源），同时在 ``meta`` 里给出
两套期间：``report_period``（片段实际来源）与 ``latest_report_period``（发布时序最新），
调用方不必再猜"这段文字是哪一期"。

定位：``reports/<公司名>(<代码>)/财报/<报告期>/.report_manifest.json``；取文：manifest 的
``full_text_path``（``reports_full/.../<报告>.md`` 全文），截取 ``## 第三节`` 至 ``## 第四节``。

fail-open：无本地报告 / manifest 缺失 / 全文不可读 → 空片段 + ``meta.note``（降级不编造）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alphabee import PROJECT_ROOT

__all__ = ["fetch_local_report_fragments", "latest_report_period"]

REPORT_DIR = PROJECT_ROOT / "reports"
_MANIFEST_NAME = ".report_manifest.json"

#: 片段字符上限（控制 LLM prompt token；管理层讨论章节通常远小于此）。
_DEFAULT_MAX_CHARS = 12000

#: 发布时序（财务锚定）：同一年内 一季报 → 半年报 → 三季报 → 年报 依次推进。
#: 顺序敏感：``"半年度报告"`` 含子串 ``"年度报告"``，必须先匹配更具体的半年报。
_CHRONO_RANK = (
    ("半年度报告", 2),
    ("第三季度报告", 3),
    ("三季度报告", 3),
    ("第二季度报告", 2),
    ("二季度报告", 2),
    ("第一季度报告", 1),
    ("一季度报告", 1),
    ("年度报告", 4),
)

#: 内容完整度优先级（片段抽取）：年报 > 半年报 > 三季报 > 一季报/二季报。
#: 季报没有「主要业务」章节，故内容口径**不**按发布时序排。
#: 顺序敏感：``"半年度报告"`` 含子串 ``"年度报告"``，必须先匹配更具体的半年报。
_CONTENT_RANK = (
    ("半年度报告", 4),
    ("年度报告", 5),
    ("第三季度报告", 3),
    ("三季度报告", 3),
    ("第二季度报告", 2),
    ("二季度报告", 2),
    ("第一季度报告", 1),
    ("一季度报告", 1),
)


def _rank(period: str, table: tuple[tuple[str, int], ...]) -> tuple[int, int]:
    """``("2026年半年度报告", _CHRONO_RANK)`` → ``(2026, 2)``（无法解析 → ``(0, 0)``）。"""
    year_match = re.search(r"(\d{4})", period)
    year = int(year_match.group(1)) if year_match else 0
    rank = 0
    for needle, value in table:
        if needle in period:
            rank = value
            break
    return (year, rank)


def _period_sort_key(period: str) -> tuple[int, int]:
    """财务锚定排序键：按**发布时序**（2026三季报 ``(2026, 3)`` > 2026半年报 ``(2026, 2)``）。"""
    return _rank(period, _CHRONO_RANK)


def _content_sort_key(period: str) -> tuple[int, int]:
    """片段抽取排序键：按**内容完整度**（2026年报 ``(2026, 4)`` > 2026半年报 ``(2026, 2)``）。"""
    return _rank(period, _CONTENT_RANK)


def _company_report_dir(symbol: str) -> Path | None:
    """按 6 位代码定位 ``reports/<公司名>(<代码>)`` 目录（无 → ``None``）。"""
    digits = str(symbol or "").strip().split(".")[0]
    if not digits.isdigit():
        return None
    matches = sorted(REPORT_DIR.glob(f"*({digits})")) if REPORT_DIR.is_dir() else []
    return matches[0] if matches else None


def _manifests(symbol: str) -> list[tuple[str, dict[str, Any]]]:
    """列出标的全部 manifest：``[(report_period, manifest), ...]``（读失败即跳过）。"""
    company_dir = _company_report_dir(symbol)
    if company_dir is None:
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    for manifest_path in (company_dir / "财报").glob(f"*/{_MANIFEST_NAME}"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        period = str(manifest.get("report_period") or manifest_path.parent.name)
        found.append((period, manifest))
    return found


def _latest_report_manifest(symbol: str) -> dict[str, Any] | None:
    """财务锚定：**按发布时序**取最新一期报告的 manifest（无 → ``None``）。"""
    candidates = _manifests(symbol)
    if not candidates:
        return None
    return max(candidates, key=lambda item: _period_sort_key(item[0]))[1]


def _content_report_manifest(symbol: str) -> dict[str, Any] | None:
    """内容抽取：取**内容最完整**的年报/半年报优先报告（无 → ``None``）。"""
    candidates = _manifests(symbol)
    if not candidates:
        return None
    return max(candidates, key=lambda item: _content_sort_key(item[0]))[1]


def latest_report_period(symbol: str) -> str:
    """标的**发布时序**最新一期的 ``report_period`` 标签（无本地财报 → ``""``）。"""
    manifest = _latest_report_manifest(symbol)
    if manifest is None:
        return ""
    return str(manifest.get("report_period") or "")


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
    """取标的本地财报「管理层讨论与分析」章节作为 LLM 对标抽取片段（fail-open）。

    取文用**内容口径**（年报/半年报优先，季报无该章节）；``meta.latest_report_period``
    另行给出**发布时序**最新一期，供调用方做报告期锚定。

    Returns:
        ``(fragments, meta)``：``fragments`` 为章节文本列表（可能为空）；
        ``meta`` 含 ``note`` / ``report_period``（片段来源期）/ ``latest_report_period``
        （发布时序最新期）/ ``full_text_path`` / ``error``。
    """
    meta: dict[str, Any] = {
        "note": "",
        "report_period": "",
        "latest_report_period": "",
        "full_text_path": "",
        "error": None,
    }
    manifest = _content_report_manifest(symbol)
    if manifest is None:
        meta["note"] = "无本地财报（reports/ 未命中该标的），跳过本地报告兜底"
        return [], meta

    meta["report_period"] = str(manifest.get("report_period") or "")
    meta["latest_report_period"] = latest_report_period(symbol)
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
