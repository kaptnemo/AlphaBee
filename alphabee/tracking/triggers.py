"""宏观环触发器检测（F4 / 设计文档 §9.2、§9.3、§14.5-A）。

**定位**：把"什么时候该重新看一眼"从**隐式习惯**变成**可测的纯函数**。本模块只做两件事：

1. :func:`detect_triggers` —— **artifact 驱动**（契约签名 `detect_triggers(symbol, *, as_of, artifact)`）：
   从 :class:`~alphabee.midterm.models.CompanyStateArtifact` 的**既有字段**读条件，并在**拿到本帧
   diff 时把判定委托给 midterm 的既有实现**（见下"复用而非重写"）；
2. :func:`detect_fact_triggers` —— **增量事实驱动**：比较新旧 :class:`~alphabee.midterm.models.FactorSnapshot`，
   给出"新财报期 / 新公告 / 行情异动"三类触发。

**★ 复用而非重写（本模块的硬约束）**：

* 退出/证伪判定 → :func:`alphabee.midterm.diff_consumers.check_exit`（**不在本模块复制其分支**）；
* 信念位移 tv / 证据到达率 → :func:`alphabee.midterm.diff_consumers.monitor_triggers`
  （**阈值用它自己的默认常量：本模块既不写第二个 tv 阈值字面量，也不重算 `0.5·Σ|ΔP|`**）；
* 调用一律走**模块属性**（``diff_consumers.check_exit(...)``）而非 import 期绑定名 ——
  这样 midterm 侧被 monkeypatch（哨兵 / 必抛）时，本模块行为会**随之变化 / 随之失败**：
  这是"真的路由到 midterm"的**可证伪**判据（import 期绑定会让哨兵失效）。

**纪律**（与 §14.0 一致）：

* **纯函数**：无 IO、无 LLM、无时钟读取（`as_of` 由调用方给），不修改入参（只读 + 返回新对象）；
* **不新增契约**：只读 midterm 既有模型字段（`stale_after` / `exit_conditions` / `factor_snapshot`），
  不新造 artifact、不改 midterm 任何文件；
* **阈值 fail-open**：:func:`thresholds_from_settings` 读 config 的 `deviation.tracking` 段，
  缺失/异常 → tv 档**不传参**（即用 `monitor_triggers` 自己的默认阈值），**模块级不读配置**。

**五类 kind 与触发条件的映射（v1 口径，逐条可测）**：

======================  ==========================================================================
kind                    触发条件（→ 判定来源）
======================  ==========================================================================
``FINANCIAL_REPORT``    增量事实侧：财报类 canonical 字段相对上一帧**实质变化**（代理判定）
``ANNOUNCEMENT``        增量事实侧：``risk.news_title`` 非空且与上一帧不同（代理判定）
``PRICE_MOVE``          ``monitor_triggers`` 的信念位移项（``tv_distance=…``）**或**
                        行情快照 ``|trend.price_change_pct| > 阈值``
``STALE_EXPIRED``       ``stale_after`` 已到期（§9.2「数据陈旧后继续持有旧结论」）
``MANUAL``              ``check_exit`` 报出退出信号（§9.4 行动类输出只允许 Tier 0/5）**或**
                        ``monitor_triggers`` 的证据到达率项（``evidence_arrival_rate=…``，高信息量
                        → 深度研究/复核）；亦由 CLI ``--trigger manual`` 直接注入
======================  ==========================================================================

**如实披露两处 v1 判断**：

* ``tv`` / 证据到达率两项**只在拿到本帧 diff 时判定**（经 ``monitor_triggers``）；单帧 artifact
  路径**不判**（宁可不判，也不自算第二份公式 —— 这正是"复用而非重写"的取舍）。该两项映射到
  ``PRICE_MOVE`` / ``MANUAL`` 是固定五类枚举下的最贴近选择，payload 带 ``source="monitor_triggers"``
  与**原始 reason 字符串**，调用方不会误读为真实涨跌幅；
* ``ANNOUNCEMENT`` 用 ``risk.news_title``、``FINANCIAL_REPORT`` 用财报字段刷新作**代理判定**：
  midterm 既有快照**没有**"公告/报告期"字段，本模块**不为此新造字段**（纪律优先），
  故以既有字段的最接近量作代理，并在 ``payload.source`` 自证为 ``*_proxy``。
"""

from __future__ import annotations

import datetime as _dt
import enum
import logging
from dataclasses import dataclass, field
from typing import Any

from alphabee.midterm import diff_consumers
from alphabee.midterm.models import CompanyStateArtifact, CompanyStateDiff, FactorSnapshot, FundamentalFactor

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PRICE_MOVE_PCT",
    "DEFAULT_STALE_AFTER_DAYS",
    "Trigger",
    "TriggerKind",
    "TriggerThresholds",
    "detect_fact_triggers",
    "detect_triggers",
    "manual_trigger",
    "monitor_kwargs",
    "thresholds_from_settings",
]

#: 行情异动阈值（PERCENT，当日涨跌幅绝对值）缺省值；config 缺失/异常时使用。
#: （midterm 侧没有对应实现 ⇒ 这是本模块自己的常量，不构成"第二份实现"。）
DEFAULT_PRICE_MOVE_PCT = 5.0

#: 缺省数据保鲜期（天）：``stale_after`` 缺失/刷新时按 ``as_of + N`` 写入（midterm 无对应常量）。
DEFAULT_STALE_AFTER_DAYS = 7


class TriggerKind(enum.StrEnum):
    """触发来源（§14.5-A 给定的五种，**不扩枚举**）。"""

    FINANCIAL_REPORT = "financial_report"
    ANNOUNCEMENT = "announcement"
    PRICE_MOVE = "price_move"
    STALE_EXPIRED = "stale_expired"
    MANUAL = "manual"


@dataclass(frozen=True)
class Trigger:
    """一条触发（frozen dataclass：可比较、可入 set、不可被下游误改）。"""

    kind: TriggerKind
    symbol: str
    reason: str
    # ``payload`` 是 dict（不可哈希）⇒ 显式 ``hash=False``：让 frozen 实例**仍可哈希**，
    # 同时 equality 照旧逐字段比较（含 payload）。
    payload: dict[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class TriggerThresholds:
    """触发阈值集合（可被 config 覆盖；缺省即"用 midterm 自己的默认值"）。

    ``tv_distance`` 缺省为 **``None``** = 调用 ``monitor_triggers`` 时**不传该参数**，
    于是用它自己的默认阈值 —— 本模块因此**没有任何第二份 tv 阈值字面量**，
    也就不可能与 midterm 漂移。
    """

    tv_distance: float | None = None
    price_move_pct: float = DEFAULT_PRICE_MOVE_PCT
    stale_grace_days: int = 0  # 到期额外宽限天数（0 = 到期即触发）
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS  # 新帧写入的保鲜期（§9.2 复用）


def _config_section() -> Any:
    """读 ``settings.deviation.tracking``（**运行时**逐层 getattr；缺失/异常 → ``None``）。

    与 ``services.detection.detection_switches`` 同构的 fail-open 契约：配置不可用绝不抛错，
    也不在模块 import 期读取。``DeviationSettings`` 当前**没有** ``tracking`` 段，故正常情况
    下这里返回 ``None``、阈值回落 midterm 默认；将来补上该段即自动生效（无需改本模块）。
    """
    try:
        from alphabee.config import get_settings

        return getattr(getattr(get_settings(), "deviation", None), "tracking", None)
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断跟踪
        logger.warning("tracking settings unavailable (fail-open, midterm defaults used): %s", exc)
        return None


def _read_number(section: Any, name: str, default: float | None) -> float | None:
    """从配置段读数值；缺失 / 非数值 / 非有限 → ``default``（不猜测语义）。"""
    value = getattr(section, name, None)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf → 缺省
        return default
    return number


def thresholds_from_settings() -> TriggerThresholds:
    """阈值读 config（``deviation.tracking``）→ 缺失/异常 **fail-open 到 midterm 既有默认**。

    逐个字段独立判定：配置段存在但只给了部分字段时，未给的字段仍落回缺省（tv 档 = ``None``
    = 用 ``monitor_triggers`` 自己的默认阈值）。
    """
    section = _config_section()
    if section is None:
        return TriggerThresholds()
    grace = getattr(section, "stale_grace_days", None)
    fresh = getattr(section, "stale_after_days", None)
    return TriggerThresholds(
        tv_distance=_read_number(section, "tv_distance", None),
        price_move_pct=_read_number(section, "price_move_pct", DEFAULT_PRICE_MOVE_PCT) or DEFAULT_PRICE_MOVE_PCT,
        stale_grace_days=grace if isinstance(grace, int) and not isinstance(grace, bool) and grace >= 0 else 0,
        stale_after_days=fresh
        if isinstance(fresh, int) and not isinstance(fresh, bool) and fresh > 0
        else DEFAULT_STALE_AFTER_DAYS,
    )


def monitor_kwargs(limits: TriggerThresholds) -> dict[str, float]:
    """调用 ``monitor_triggers`` 时的阈值关键字：缺省（``None``）**不传** ⇒ 用它的默认常量。"""
    return {} if limits.tv_distance is None else {"tv_threshold": float(limits.tv_distance)}


# ── 只读取值小工具（不改入参、不猜测缺失） ──────────────────────────────────


def _parse_date(value: str | None) -> _dt.date | None:
    """``YYYY-MM-DD`` → ``date``；缺失/非法 → ``None``（不猜测、不抛错）。"""
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _factor_snapshot(artifact: CompanyStateArtifact | None) -> FactorSnapshot | None:
    snapshot = getattr(artifact, "factor_snapshot", None)
    return snapshot if isinstance(snapshot, FactorSnapshot) else None


def _price_move_triggers(symbol: str, change: Any, threshold: float) -> list[Trigger]:
    """行情异动（读快照字段 + 与阈值比较；midterm 无对应实现，故在本模块判定）。"""
    if isinstance(change, bool) or not isinstance(change, int | float):
        return []
    if abs(float(change)) <= threshold:
        return []
    return [
        Trigger(
            kind=TriggerKind.PRICE_MOVE,
            symbol=symbol,
            reason=f"行情异动：price_change_pct={float(change):+.2f}% 绝对值超阈值 {threshold:g}%",
            payload={"source": "price_snapshot", "price_change_pct": float(change), "threshold": threshold},
        )
    ]


# ── artifact 驱动：detect_triggers（§14.5-A 的契约签名） ─────────────────────


def detect_triggers(
    symbol: str,
    *,
    as_of: str,
    artifact: CompanyStateArtifact | None,
    frame_diff: CompanyStateDiff | None = None,
    thresholds: TriggerThresholds | None = None,
) -> list[Trigger]:
    """纯函数：从**已有 artifact**（+ 可选本帧 diff）判定"该不该重新看一眼"（§14.5-A 契约签名）。

    覆盖 §14.5-A 要求的三类条件，**判定尽量委托 midterm 既有实现**：

    1. **``stale_after`` 到期** → :attr:`TriggerKind.STALE_EXPIRED`（读 artifact 字段；midterm 无对应实现）；
    2. **退出/证伪信号** → :attr:`TriggerKind.MANUAL`：有 ``frame_diff`` → 委托
       :func:`alphabee.midterm.diff_consumers.check_exit`（**含 exit_conditions_met / 状态降级 /
       仓位带背离三条分支**，一条都不复制）；无 diff（单帧）→ 只读 ``exit_conditions[*].met`` 兜底；
    3. **信念位移 / 证据到达率超阈值** → 有 ``frame_diff`` 时委托
       :func:`alphabee.midterm.diff_consumers.monitor_triggers`（``tv_distance=…`` → ``PRICE_MOVE``，
       ``evidence_arrival_rate=…`` → ``MANUAL``）；**无 diff 时不判定**（不自算第二份公式）；
    4. 附带：artifact 内最近一帧快照的 ``|price_change_pct|`` 超阈值 → ``PRICE_MOVE``。

    Args:
        symbol: 标的代码（仅写入 ``Trigger.symbol``，不做格式校验/归一）。
        as_of: 判定时点（``YYYY-MM-DD``）；非法/缺失 → 不产 stale 触发（不猜测时间）。
        artifact: 上一帧认知状态产物；``None`` → 返回 ``[]``（不猜测）。
        frame_diff: 本帧 :class:`CompanyStateDiff`（``run_once`` 会传入）；``None`` = 单帧判定。
        thresholds: 阈值覆盖（缺省 :func:`thresholds_from_settings`）。

    Returns:
        触发列表（顺序稳定：stale → exit → monitor → price），同一条件不重复产出。
    """
    limits = thresholds or thresholds_from_settings()
    if artifact is None:
        return []

    triggers: list[Trigger] = []

    # 1) stale_after 到期（§9.2；读字段判定）
    stale = _parse_date(getattr(artifact, "stale_after", None))
    now = _parse_date(as_of)
    if stale is not None and now is not None:
        overdue = (now - stale).days
        if overdue > limits.stale_grace_days:
            triggers.append(
                Trigger(
                    kind=TriggerKind.STALE_EXPIRED,
                    symbol=symbol,
                    reason=f"数据陈旧：stale_after={stale.isoformat()} 已过期 {overdue} 天（as_of={now.isoformat()}）",
                    payload={
                        "stale_after": stale.isoformat(),
                        "as_of": now.isoformat(),
                        "overdue_days": overdue,
                        "grace_days": limits.stale_grace_days,
                    },
                )
            )

    # 2) 退出/证伪信号（§9.4 行动类输出只允许 Tier 0/5）：两条来源**合并成一条** MANUAL
    #    —— 有 diff 时委托 midterm 的 check_exit（不复制其三条分支）；
    #    artifact 侧已置 met 的条件则作"持续告警"补充（避免上一帧已触发、本帧被漏掉）。
    exit_reasons: list[str] = []
    sources: list[str] = []
    if frame_diff is not None:
        exit_signal = diff_consumers.check_exit(frame_diff)
        if exit_signal.should_exit:
            exit_reasons.extend(str(reason) for reason in exit_signal.reasons)
            sources.append("check_exit")
    met = [c for c in (getattr(artifact, "exit_conditions", None) or []) if getattr(c, "met", False)]
    if met:
        kinds = "、".join(str(getattr(c, "kind", "") or "unnamed") for c in met)
        exit_reasons.append(f"证伪/退出条件已触发：{kinds}")
        sources.append("artifact_exit_conditions")
    if exit_reasons:
        triggers.append(
            Trigger(
                kind=TriggerKind.MANUAL,
                symbol=symbol,
                reason=(f"退出/证伪信号：{'；'.join(exit_reasons)}（§9.4 行动类输出只允许 Tier 0/5，需人工复核）"),
                payload={
                    "sources": sources,
                    "reasons": exit_reasons,
                    "exit_conditions": [
                        {"kind": getattr(c, "kind", ""), "condition": getattr(c, "condition", "")} for c in met
                    ],
                },
            )
        )

    # 3) 信念位移 / 证据到达率：有 diff 时**委托** monitor_triggers（阈值走它的默认或显式覆盖）
    if frame_diff is not None:
        monitor = diff_consumers.monitor_triggers(frame_diff, **monitor_kwargs(limits))
        for reason in monitor.triggers:
            is_tv = str(reason).startswith("tv_distance=")
            triggers.append(
                Trigger(
                    kind=TriggerKind.PRICE_MOVE if is_tv else TriggerKind.MANUAL,
                    symbol=symbol,
                    reason=(
                        f"监控触发（monitor_triggers）：{reason}"
                        + ("（§9.3 信念位移超阈值 → 复核 run）" if is_tv else "（高信息量 → 深度研究/人工复核）")
                    ),
                    payload={"source": "monitor_triggers", "reason": reason, "threshold": limits.tv_distance},
                )
            )

    # 4) artifact 内最近一帧的行情异动
    snapshot = _factor_snapshot(artifact)
    triggers += _price_move_triggers(
        symbol, getattr(getattr(snapshot, "trend", None), "price_change_pct", None), limits.price_move_pct
    )

    return triggers


# ── 增量事实驱动：detect_fact_triggers ──────────────────────────────────────

#: 财报类 canonical 字段（用于"新报告期 / 财报刷新"的**代理判定**；与 midterm 的 F 因子同源）。
_FUNDAMENTAL_FIELDS: tuple[str, ...] = tuple(FundamentalFactor.model_fields)


def _fundamental_refreshed(curr: FactorSnapshot, prev: FactorSnapshot) -> list[str]:
    """两份快照的财报类字段**实质变化**清单（代理判定"新报告期/财报刷新"）。

    只比较同名同口径字段；``None`` → 值（appeared）与 值 → ``None``（disappeared）也算变化
    （数据补齐/消失都是事实层变化）。浮点按**精确不等**比较：本函数只回答"变没变"，
    阈值化留给下游，避免在这里发明容差口径。
    """
    changed: list[str] = []
    curr_fundamental = getattr(curr, "fundamental", None)
    prev_fundamental = getattr(prev, "fundamental", None)
    for name in _FUNDAMENTAL_FIELDS:
        if getattr(curr_fundamental, name, None) != getattr(prev_fundamental, name, None):
            changed.append(name)
    return changed


def detect_fact_triggers(
    symbol: str,
    *,
    snapshot: FactorSnapshot,
    prev_snapshot: FactorSnapshot | None = None,
    thresholds: TriggerThresholds | None = None,
) -> list[Trigger]:
    """纯函数：增量事实侧的触发（新财报期 / 新公告 / 行情异动）。

    * 首帧（``prev_snapshot is None``）→ **不产** FINANCIAL_REPORT / ANNOUNCEMENT
      （无比较基准 = 基线登记，不猜测"新"）；
    * ``FINANCIAL_REPORT``：财报类 canonical 字段相对上一帧实质变化（**代理判定**，见模块 docstring）；
    * ``ANNOUNCEMENT``：``risk.news_title`` 非空且与上一帧不同（**代理判定**）；
    * ``PRICE_MOVE``：``|trend.price_change_pct| > 阈值``（与 artifact 侧同口径，去重由调用方负责）。

    Args:
        symbol: 标的代码。
        snapshot: 本帧快照（``get_factor_snapshot`` 产物）。
        prev_snapshot: 上一帧快照（``artifact.factor_snapshot``）；``None`` = 首帧。
        thresholds: 阈值覆盖。

    Returns:
        触发列表（顺序稳定：financial_report → announcement → price_move）。
    """
    limits = thresholds or thresholds_from_settings()
    triggers: list[Trigger] = []

    if prev_snapshot is not None:
        changed = _fundamental_refreshed(snapshot, prev_snapshot)
        if changed:
            triggers.append(
                Trigger(
                    kind=TriggerKind.FINANCIAL_REPORT,
                    symbol=symbol,
                    reason=(
                        f"财报类字段刷新 {len(changed)} 项（代理判定新报告期/财报更新）："
                        f"{'、'.join(changed[:5])}{'…' if len(changed) > 5 else ''}"
                    ),
                    payload={"changed_fields": changed, "source": "fundamental_refresh_proxy"},
                )
            )

        news_title = str(getattr(getattr(snapshot, "risk", None), "news_title", "") or "").strip()
        prev_news_title = str(getattr(getattr(prev_snapshot, "risk", None), "news_title", "") or "").strip()
        if news_title and news_title != prev_news_title:
            triggers.append(
                Trigger(
                    kind=TriggerKind.ANNOUNCEMENT,
                    symbol=symbol,
                    reason=f"公告/新闻标题变化（代理判定）：{news_title[:60]}",
                    payload={"news_title": news_title, "prev_news_title": prev_news_title, "source": "news_proxy"},
                )
            )

    triggers += _price_move_triggers(
        symbol, getattr(getattr(snapshot, "trend", None), "price_change_pct", None), limits.price_move_pct
    )
    return triggers


def manual_trigger(symbol: str, reason: str = "人工触发") -> Trigger:
    """人工触发（CLI ``--trigger manual`` 用）：不属于任何自动判定，故单独构造。"""
    return Trigger(kind=TriggerKind.MANUAL, symbol=symbol, reason=reason, payload={"source": "manual"})
