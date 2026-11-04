"""F 输入序列 PIT 快照（design MIDTERM_F_DATA_GOVERNANCE_DESIGN.md §14 / P2-2）。

目的：让影子/校准能回答「这帧 F 是用哪些原始值算的」，并在数据重述后仍可回放。
**不做全历史回填**：只在 ``get_factor_snapshot`` 构建 F 之后，按
``midterm.persist_financial_inputs``（缺省 false）可选落盘一帧。

设计约束：

- :func:`build_financial_inputs` 是**纯函数**（无 IO、无系统时间），同输入 ⇒ 同
  ``content_hash``（对规范化 JSON 做 sha256）；
- ``series`` 只存 F 需要的 6 条：``revenue`` / ``operating_revenue`` /
  ``net_profit_attr_p`` / ``net_profit_dedt`` / ``gross_margin`` /
  ``operating_cashflow``，每条最多 8 期（缺失字段不写空 dict）；
- :func:`append_financial_inputs` 实现 append-only 幂等：按
  ``(symbol, as_of_date, report_period, content_hash)`` 判重；单行 append + flush +
  fsync（坏行可跳过，不回写历史）；
- :func:`load_financial_inputs` 按 ``as_of_date`` 升序读回，坏行/非对象行跳过不抛；
- 默认目录 ``data/midterm/financial_inputs/<symbol>.jsonl``（symbol 做安全文件名处理）。

接线：``factors.get_factor_snapshot`` 仅在
:func:`resolve_persist_financial_inputs` 为 True 时调用本模块；写盘失败 fail-open
（warning，不打断决策）。``build_fundamental_factor`` 保持纯函数、不写盘。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: F 需要的 6 条输入序列 → 来源记录列表（income / fina / cashflow）。
_SERIES_SOURCES: dict[str, str] = {
    "revenue": "income",
    "operating_revenue": "income",
    "net_profit_attr_p": "income",
    "net_profit_dedt": "fina",
    "gross_margin": "fina",
    "operating_cashflow": "cashflow",
}

#: 每条序列最多保留的期数（设计 §14.2：8 期 ≈ 两年季度）。
MAX_PERIODS = 8

#: 默认落盘目录（与 ``midterm.persistence`` 的 ``data/midterm/state`` 同级）。
DEFAULT_DATA_DIR = Path("data") / "midterm" / "financial_inputs"

#: 默认数据源标记（当前 F 数据全部经 tushare adapter 入 canonical）。
DEFAULT_SOURCE = "tushare"

#: 幂等键（设计 §14.2）。
_IDENTITY_FIELDS = ("symbol", "as_of_date", "report_period", "content_hash")


class FinancialInputSnapshot(BaseModel):
    """一帧 F 输入序列的 PIT 快照（append-only JSONL 的一行）。

    ``series`` 为 ``field -> {period: value}``；``period`` 统一 ``YYYYMMDD``，
    每条序列最多 :data:`MAX_PERIODS` 期。``content_hash`` 由除自身外的规范化 JSON
    计算（:func:`_content_hash`），用于幂等判重与「重述后可回放」的同一性判定。
    """

    schema_version: str = "1"
    symbol: str
    as_of_date: str  # YYYY-MM-DD（快照采集日）
    report_period: str  # YYYYMMDD（L 所用最新报告期）
    report_ann_date: str = ""
    source: str = DEFAULT_SOURCE
    series: dict[str, dict[str, float]] = Field(default_factory=dict)
    content_hash: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_period(value: Any) -> str:
    """报告期归一为 ``YYYYMMDD`` 数字串；缺失/非法 ⇒ ``""``（与 ``midterm.factors`` 同语义）。"""
    if value is None or isinstance(value, bool):
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        return ""
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return digits[:8]


def _ann_date_text(value: Any) -> str:
    """``ann_date`` 归一为 ``YYYY-MM-DD``；缺失/非法 ⇒ ``""``。"""
    if value is None or isinstance(value, bool):
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _finite(value: Any) -> float | None:
    """数值 → 有限 ``float``；缺失/布尔/非数值/NaN/Inf ⇒ ``None``（不伪造）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _records(data: Mapping[str, Any] | None, key: str) -> list[dict[str, Any]]:
    """取 ``income`` / ``fina`` / ``cashflow`` 记录列表；``dict`` 单条也接受。"""
    raw = (data or {}).get(key)
    if isinstance(raw, Mapping):
        return [dict(raw)]
    if isinstance(raw, (list, tuple)):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    return []


def _latest(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按 ``period`` 取最新一条记录（无合法期 ⇒ None）。"""
    valid = [(period, record) for record in records if (period := _normalize_period(record.get("period")))]
    if not valid:
        return None
    return max(valid, key=lambda pair: pair[0])[1]


def _series_for(records: list[dict[str, Any]], field: str) -> dict[str, float]:
    """从记录列表提取 ``field`` 的 ``period -> value``（最多 :data:`MAX_PERIODS` 期）。"""
    points: dict[str, float] = {}
    for record in records:
        period = _normalize_period(record.get("period"))
        value = _finite(record.get(field))
        if period and value is not None:
            points[period] = value
    if not points:
        return {}
    keep = sorted(points)[-MAX_PERIODS:]
    return {period: points[period] for period in keep}


def _content_hash(snapshot: FinancialInputSnapshot) -> str:
    """对除 ``content_hash`` 外的规范化 JSON 做 sha256（确定性、与字段顺序无关）。"""
    payload = snapshot.model_dump(mode="json", exclude={"content_hash"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_symbol_filename(symbol: str) -> str:
    """symbol → 安全文件名（替换路径分隔符，避免目录穿越）；空串 ⇒ ``_unknown``。"""
    name = str(symbol or "").strip()
    for char in ("/", "\\", "\x00"):
        name = name.replace(char, "_")
    return name or "_unknown"


def _snapshot_path(symbol: str, base_dir: Path | None = None) -> Path:
    base = Path(base_dir) if base_dir is not None else DEFAULT_DATA_DIR
    return base / f"{_safe_symbol_filename(symbol)}.jsonl"


def _read_rows(path: Path) -> list[Any]:
    """读回 JSONL 全部行（文件缺失 ⇒ ``[]``；坏行/空行跳过，不抛）。"""
    if not path.exists():
        return []
    rows: list[Any] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    rows.append(json.loads(text))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:  # noqa: BLE001 - 读失败按「无历史」处理（append 仍可工作）
        logger.warning("读取 F 输入快照失败（忽略历史）：%s", exc)
    return rows


def _identity(snapshot: FinancialInputSnapshot) -> tuple[str, str, str, str]:
    return (
        snapshot.symbol,
        snapshot.as_of_date,
        snapshot.report_period,
        snapshot.content_hash,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 公开 API
# ─────────────────────────────────────────────────────────────────────────────


def build_financial_inputs(
    fin_data: Mapping[str, Any] | None,
    *,
    as_of_date: str,
) -> FinancialInputSnapshot:
    """构建一帧 F 输入序列快照（**纯函数**：无 IO、不读系统时间、确定性）。

    Args:
        fin_data: ``get_financial_fact`` 的多期嵌套结构（``income`` / ``fina`` /
            ``cashflow`` 记录已是 canonical 字段名）。
        as_of_date: 快照采集日（``YYYY-MM-DD``，由调用方传入，保证可回放）。

    Returns:
        :class:`FinancialInputSnapshot`：``series`` 逐条最多 8 期（缺失字段不写空
        dict）；``content_hash`` 稳定（同输入 ⇒ 同 hash）。
    """
    data: Mapping[str, Any] = fin_data or {}
    income = _records(data, "income")
    fina = _records(data, "fina")
    cashflow = _records(data, "cashflow")
    sources = {"income": income, "fina": fina, "cashflow": cashflow}

    series: dict[str, dict[str, float]] = {}
    for field, source in _SERIES_SOURCES.items():
        values = _series_for(sources[source], field)
        if values:  # 缺失字段不写空 dict（不伪造「有数据」）
            series[field] = values

    latest_fina = _latest(fina)
    latest_income = _latest(income)
    report_period = _normalize_period((latest_fina or {}).get("period")) or _normalize_period(
        (latest_income or {}).get("period")
    )
    report_ann_date = ""
    for candidate in (latest_fina, latest_income):
        if candidate is None:
            continue
        if _normalize_period(candidate.get("period")) == report_period:
            report_ann_date = _ann_date_text(candidate.get("ann_date")) or report_ann_date
    if not report_ann_date:
        for candidate in (latest_fina, latest_income):
            if candidate is not None and (text := _ann_date_text(candidate.get("ann_date"))):
                report_ann_date = text
                break

    snapshot = FinancialInputSnapshot(
        symbol=str(data.get("stock_code") or ""),
        as_of_date=str(as_of_date or ""),
        report_period=report_period,
        report_ann_date=report_ann_date,
        series=series,
    )
    snapshot.content_hash = _content_hash(snapshot)
    return snapshot


def append_financial_inputs(
    snapshot: FinancialInputSnapshot,
    base_dir: Path | None = None,
) -> bool:
    """追加一帧 PIT 快照（幂等 + 单行原子写）；返回是否**新写入**。

    幂等键 ``(symbol, as_of_date, report_period, content_hash)``：已有同键行 ⇒
    ``False``（不重复追加）。写盘为单行 append + flush + fsync（进程崩溃最多留下
    半行，读回时按坏行跳过，不影响已写入行）。
    """
    path = _snapshot_path(snapshot.symbol, base_dir)
    identity = _identity(snapshot)
    for row in _read_rows(path):
        if not isinstance(row, Mapping):
            continue
        try:
            existing = FinancialInputSnapshot.model_validate(row)
        except Exception:  # noqa: BLE001 - 坏行不参与幂等判重（读回时也跳过）
            continue
        if _identity(existing) == identity:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    record = snapshot.model_dump(mode="json")
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def load_financial_inputs(
    symbol: str,
    *,
    base_dir: Path | None = None,
) -> list[FinancialInputSnapshot]:
    """读回某标的全部 PIT 快照（按 ``as_of_date`` 升序；坏行/非对象行跳过）。"""
    path = _snapshot_path(symbol, base_dir)
    snapshots: list[FinancialInputSnapshot] = []
    for row in _read_rows(path):
        if not isinstance(row, Mapping):
            continue
        try:
            snapshots.append(FinancialInputSnapshot.model_validate(row))
        except Exception:  # noqa: BLE001 - 坏行/旧 schema 不阻断读回
            continue
    snapshots.sort(key=lambda item: item.as_of_date)
    return snapshots


def default_data_dir() -> Path:
    """默认落盘目录（``data/midterm/financial_inputs``；按需创建）。"""
    return DEFAULT_DATA_DIR


def resolve_persist_financial_inputs(enabled: bool | None = None) -> bool:
    """解析 P2-2 落盘开关（``midterm.persist_financial_inputs``）。

    - 显式 ``bool`` 优先；
    - ``None`` ⇒ 读配置；**缺段/缺字段** ⇒ 动态读
      ``MidtermClassifierSettings.model_fields["persist_financial_inputs"].default``
      （当前 false；不在本模块硬编码）；
    - 读配置异常 / 取值非 bool ⇒ ``False``（fail-open：绝不因配置坏掉而写盘）。
    """
    if enabled is not None:
        return enabled if isinstance(enabled, bool) else False
    try:
        from alphabee.config import MidtermClassifierSettings, get_settings

        midterm = getattr(get_settings(), "midterm", None)
        configured = getattr(midterm, "persist_financial_inputs", None)
        if configured is None:  # 缺段/缺字段 ⇒ 回退到模型默认（动态读取，不硬编码）
            default = MidtermClassifierSettings.model_fields["persist_financial_inputs"].default
            return default if isinstance(default, bool) else False
        return configured if isinstance(configured, bool) else False
    except Exception as exc:  # noqa: BLE001 - 读配置失败绝不打断决策
        logger.warning("midterm.persist_financial_inputs 配置不可用（%s），回落 False", exc)
        return False
