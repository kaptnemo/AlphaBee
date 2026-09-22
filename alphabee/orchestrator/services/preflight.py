"""入口前置校验（研究连续体 P2 / D2-B2；``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §14.3 D2 + §15.2）。

**定位**：把"带过期/未对账状态直接继续"从**静默**变成**显式且入账**（§11 验收 2 的修订口径：
继续是允许的，但不可能**静默地**继续）。本模块是**纯规则**判定：无网络、无 LLM、不写盘，
只读两类**既有产物**（§15.2-A，不新增存储）：

1. ``data/midterm/state/<symbol>.jsonl`` 的最新一帧（``midterm.persistence.latest_artifact``，
   F4 已写）→ 帧的 ``stale_after`` 与 ``today`` 比较得 ``stale``；
2. ``data/tracking/alerts/<symbol>.jsonl`` 的**最后一条可用行**（``TrackingReport`` JSON，F4 已写）
   → ``triggers`` / ``exit_reasons`` / ``monitor_reasons`` 非空 ⇒ ``pending``（逐条摘要）。

本模块**不写任何东西**、**不新增判定与阈值**（§15.0 C-4/C-5）：陈旧与否只复用帧自带的
``stale_after`` 字段（写入侧是 ``tracking`` 的既有口径），触发与否只复用告警末行的既有字段。

``blocking`` 的精确语义（本仓实现口径，逐字登记于 ROADMAP「行为变更登记」P2 行）::

    hit      = checked and (stale or pending)   # 命中"带陈旧/未对账状态启动"
    blocking = hit and block_enabled            # 该次调用是否**允许阻断**

* **记录侧**（``orchestrator/collectors.py::collect_raw_facts``）以 ``block_enabled=False`` 调用
  ⇒ 该次调用**永不阻断**，只取"命中与否"决定是否把这次启动写进账本（§15.2 设计决策 3：
  "记录恒发生"）；
* **阻断侧**（CLI 入口 gate，``apps/cli/main.py``）把配置 ``deviation.tracking.block_stale_runs``
  作为 ``block_enabled`` 传入 ⇒ ``block_stale_runs=false`` 时 ``blocking`` 恒 ``False``
  （只记录不阻断，§15.2-D 的开关语义）。

> **规格措辞偏差（已登记 ROADMAP，设计文档为权威规格、本轮不改）**：§15.2-B 的示意片段写作
> ``check_preflight(symbol, block_enabled=False)`` 之后判 ``if verdict.blocking:`` 来记账。在
> 上面的口径下该片段恒不成立（``block_enabled=False`` ⇒ ``blocking=False``），即"记录恒发生"
> 会落空；故实现按 §15.2 **设计决策 3** 取"命中"判据（``checked and (stale or pending)``）记账，
> 而把 ``blocking`` 留给阻断侧。

**fail-open（§15.0 C-3）**：帧文件缺失/损坏、告警 JSONL 坏行、日期非法、路径是文件而非目录……
任何异常都只 ``logger.warning`` 并返回 ``blocking=False`` 的 verdict —— **永不抛异常**，
入口校验绝不打断主链。这与"无帧 ⇒ 不阻断"（§15.2 设计决策 2：首次研究必须放行）同向。

**依赖方向**：本模块只 import 标准库 + Pydantic；``midterm.persistence`` 的 import 推迟到函数体内
（本模块会被 ``orchestrator.collectors`` 在分析主链上 import，避免 import 期副作用）。帧目录/告警
目录的缺省值在此以常量声明而不 import ``midterm`` / ``tracking``（后者会把 ``tushare`` 链拉进
分析主链），两处口径由 ``tests/orchestrator/test_preflight.py::test_default_dirs_match_upstream_owners``
钉住，防漂移。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_ALERT_DIR", "DEFAULT_STATE_DIR", "PreflightVerdict", "check_preflight"]

#: 认知状态帧目录（缺省；与 ``midterm.persistence.DEFAULT_DATA_DIR`` 同口径 —— must-not-drift，有专测）。
DEFAULT_STATE_DIR: Path = Path("data") / "midterm" / "state"

#: 跟踪告警目录（缺省；与 ``tracking.scheduler.DEFAULT_ALERT_DIR`` 同口径 —— must-not-drift，有专测）。
DEFAULT_ALERT_DIR: Path = Path("data") / "tracking" / "alerts"


class PreflightVerdict(BaseModel):
    """入口前置校验结果（§15.2-A 的十字段；新增字段只允许 append 且带默认值，§15.0 C-1）。"""

    symbol: str = ""
    checked: bool = False  # False = 无持久化帧（首次研究，不算陈旧、不阻断）
    latest_frame_id: str = ""
    as_of_date: str = ""
    stale: bool = False
    stale_after: str = ""
    days_since: int | None = None
    pending: list[str] = Field(default_factory=list)  # 未消费触发摘要（来自告警末行）
    blocking: bool = False
    note: str = ""


def _parse_date(value: Any) -> _dt.date | None:
    """``YYYY-MM-DD`` → ``date``；缺失/非法 → ``None``（不猜测、不抛错）。"""
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _today_date(today: str | None) -> _dt.date:
    """``today`` → ``date``；缺失/非法 → 系统当日（与帧写入侧同口径的本地日期）。"""
    return _parse_date(today) or _dt.date.today()


def _as_list(value: Any) -> list[Any]:
    """只接受真正的 ``list``（``None``/标量/字符串 → ``[]``：不猜测、不把 str 逐字符展开）。"""
    return list(value) if isinstance(value, list) else []


def _latest_frame(symbol: str, state_dir: Path) -> Any | None:
    """取最新一帧（``midterm.persistence.latest_artifact``；无记录 → ``None``）。

    ``midterm`` 的 import 推迟到函数体内：本模块在分析主链上被 import，import 期不留副作用。
    """
    from alphabee.midterm.persistence import latest_artifact

    return latest_artifact(symbol, state_dir)


def _read_last_alert_row(symbol: str, alert_dir: Path) -> dict[str, Any] | None:
    """读 ``<alert_dir>/<symbol>.jsonl`` 的**最后一条可用行**（坏行/非对象行跳过；无 → ``None``）。

    与 §15.2-A 的"最后一行"同向，但对**损坏行**更宽容（§15.2-E："告警含损坏行 → 跳过该行、
    不抛异常"）：JSON 解析失败的行直接跳过，取随后最后一条可解析的**对象**行。
    """
    path = Path(alert_dir) / f"{symbol}.jsonl"
    if not path.is_file():
        return None
    last: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                last = row
    return last


def _pending_digest(row: dict[str, Any]) -> list[str]:
    """告警末行 → 未消费触发摘要（确定性、逐条、保持原序）。

    * ``triggers[].kind`` → ``trigger:<kind>``
    * ``exit_reasons[]`` → ``exit:<reason>``
    * ``monitor_reasons[]`` → ``monitor:<reason>``
    """
    pending: list[str] = []
    for trigger in _as_list(row.get("triggers")):
        kind = str(trigger.get("kind") or "").strip() if isinstance(trigger, dict) else str(trigger).strip()
        pending.append(f"trigger:{kind}" if kind else "trigger:?")
    for reason in _as_list(row.get("exit_reasons")):
        pending.append(f"exit:{str(reason).strip()}")
    for reason in _as_list(row.get("monitor_reasons")):
        pending.append(f"monitor:{str(reason).strip()}")
    return pending


def _check_preflight(
    symbol: str | None,
    *,
    state_dir: str | Path | None = None,
    alert_dir: str | Path | None = None,
    today: str | None = None,
    block_enabled: bool = True,
) -> PreflightVerdict:
    """:func:`check_preflight` 的确定性内核（异常由外层统一兜住，见 fail-open 契约）。"""
    name = str(symbol or "").strip()
    if not name:
        # 无标的（如纯对话模式下没有 query）→ 无"状态"可谈，绝不阻断。
        return PreflightVerdict(note="未识别标的（symbol 为空）⇒ 不阻断")

    frame = _latest_frame(name, Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR)
    alert_row = _read_last_alert_row(name, Path(alert_dir) if alert_dir is not None else DEFAULT_ALERT_DIR)
    pending = _pending_digest(alert_row or {})

    if frame is None:
        # §15.2 设计决策 2：无帧 = 首次研究，不属于"带过期状态继续"，必须放行。
        # ``pending`` 仍如实回填（来自另一份既有产物、与"有无帧"无关），但**不**参与判定。
        return PreflightVerdict(
            symbol=name,
            checked=False,
            pending=pending,
            note="无持久化帧（首次研究）⇒ 不算陈旧、不阻断",
        )

    today_date = _today_date(today)
    as_of_raw = str(getattr(frame, "as_of_date", "") or "")
    stale_after_raw = str(getattr(frame, "stale_after", "") or "")
    as_of_date = _parse_date(as_of_raw)
    stale_after_date = _parse_date(stale_after_raw)

    # 口径：``stale`` 由帧自带的 ``stale_after`` 判定（到期即陈旧；缺字段/非法 → 不判、不猜测）；
    # ``days_since`` 是**帧龄**（today − as_of_date），与 ``stale_after`` 不是同一口径 —— 不做非负夹取
    # （未来 dated 的帧会得到负数，宁可暴露也不静默抹平，与 ``services/deviation.detection_latency``
    # 的既有取向一致）。
    stale = stale_after_date is not None and stale_after_date < today_date
    days_since = (today_date - as_of_date).days if as_of_date is not None else None

    hit = bool(stale or pending)
    blocking = bool(hit and block_enabled)

    reasons: list[str] = []
    if stale:
        reasons.append(f"最新帧已陈旧（stale_after={stale_after_raw} < {today_date.isoformat()}）")
    if pending:
        reasons.append(f"存在 {len(pending)} 条未消费触发（告警末行）")
    if not reasons:
        reasons.append("最新帧未陈旧且无未消费触发")
    reasons.append("已阻断（blocking=true）" if blocking else "仅记录不阻断（blocking=false）")

    return PreflightVerdict(
        symbol=name,
        checked=True,
        # 帧 id 与 ``midterm.persistence._artifact_id`` 同口径（``symbol:as_of_date``）：
        # 持久化层不把 id 放进模型，故此处按同一规则重建（不猜测、可复算）。
        latest_frame_id=f"{getattr(frame, 'symbol', '') or name}:{as_of_raw}",
        as_of_date=as_of_raw,
        stale=stale,
        stale_after=stale_after_raw,
        days_since=days_since,
        pending=pending,
        blocking=blocking,
        note="；".join(reasons),
    )


def check_preflight(
    symbol: str | None,
    *,
    state_dir: str | Path | None = None,
    alert_dir: str | Path | None = None,
    today: str | None = None,
    block_enabled: bool = True,
) -> PreflightVerdict:
    """入口前置校验（§14.3 D2 的 B2）。纯规则、无网络、无 LLM、**永不抛异常**。

    Args:
        symbol: 股票代码（调用方解析后的形态，如 ``600519.SH``）。空/``None`` → 一律不阻断。
            **不做代码归一化**（不猜测）：调用方传什么形态就查什么形态的文件名（帧与告警
            两处写入侧用的是同一形态）。
        state_dir: 认知状态帧目录；缺省 :data:`DEFAULT_STATE_DIR`（``data/midterm/state``）。
        alert_dir: 跟踪告警目录；缺省 :data:`DEFAULT_ALERT_DIR`（``data/tracking/alerts``）。
        today: ``YYYY-MM-DD``；缺省系统当日（可注入 ⇒ 判定可复算、不依赖运行日期）。
        block_enabled: 本次调用是否**允许阻断**（记录侧传 ``False`` = 只取命中、永不阻断；
            CLI 阻断侧传 ``deviation.tracking.block_stale_runs``）。

    Returns:
        :class:`PreflightVerdict`。``blocking = checked and (stale or pending) and block_enabled``。

    任何异常（帧不可读 / JSON 坏行 / 日期非法 / 路径异常……）→ ``logger.warning`` +
    ``blocking=False`` 的 verdict（fail-open，§15.0 C-3）。
    """
    try:
        return _check_preflight(
            symbol,
            state_dir=state_dir,
            alert_dir=alert_dir,
            today=today,
            block_enabled=block_enabled,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open：入口校验绝不打断主链（§15.0 C-3）
        logger.warning("preflight check failed (fail-open, not blocking): symbol=%s err=%s", symbol, exc)
        return PreflightVerdict(symbol=str(symbol or ""), note=f"入口前置校验 fail-open（{exc}）")
