"""研究生命周期**派生视图**（研究连续体 P4 / D3-C2；``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §14.3 D3 + §15.4）。

**定位**：把**已有**判定投影成**单一状态字**，供人读与将来可能的消费者使用；本模块
**不建独立状态轴**（§14.3 D3 采纳 C2、明确否决 C1）、**不读 config、不新增阈值、不落盘、无 IO**。

单一真源（三者都是既有函数，本模块只做字符串前缀/枚举投影）：

* ``exit_reasons`` ← :func:`alphabee.midterm.diff_consumers.check_exit`
* ``monitor_reasons`` ← :func:`alphabee.midterm.diff_consumers.monitor_triggers`（阈值即其既有常量
  ``_TV_TRIGGER=0.3`` / ``_EVIDENCE_RATE_TRIGGER=0.5``，本模块**不复制**这些数）
* ``triggers`` ← :func:`alphabee.tracking.triggers.detect_triggers`

**无 ACTIVE 态**（§15.4-A）：ACTIVE 表达"正在执行复核 run"，那是**调度器**的在途状态（由外部
cron/进程持有），不是研究对象的语义；塞进本投影会导致同一标的在不同进程里状态不同。

**优先级（判定顺序即优先级，互斥）**

===  =================  ==========================================================
序   状态               条件
===  =================  ==========================================================
1    ``invalidated``     ``exit_reasons`` 任一条以 :data:`EXIT_TRIGGERED_PREFIX` 开头
2    ``risk_alert``      ``exit_reasons`` 任一条以 :data:`EXIT_DOWNGRADE_PREFIX` 或
                         :data:`EXIT_POSITION_DIVERGENCE_PREFIX` 开头，或 ``triggers`` 含 ``PRICE_MOVE``
3    ``thesis_changed``  ``monitor_reasons`` 任一条以 :data:`MONITOR_TV_PREFIX` 开头
4    ``needs_research``  ``stale=True``，或 ``triggers`` 含 ``STALE_EXPIRED`` /
                         ``FINANCIAL_REPORT`` / ``ANNOUNCEMENT``
5    ``waiting``         其余
===  =================  ==========================================================

**同源口径（§15.4-A 的硬要求）**：四个前缀常量**来自**上游既有格式化输出；上游改文案会让这些常量
失配，故 ``tests/tracking/test_status.py`` 用**真实** ``check_exit`` / ``monitor_triggers`` /
``detect_triggers`` 对假帧的输出**驱动**本函数（而不是只手写字符串），并逐条断言常量确为真实输出的
前缀 —— 上游文案漂移会直接让同源断言转红。

**两条保守投影（如实登记，非缺陷、非本模块可自决的扩围）**

1. ``monitor_reasons`` 里**只**由 ``evidence_arrival_rate=…`` 引起（无 ``tv_distance=`` 前缀）时，
   本投影**不**改状态字（优先级表第 3 序只认 ``tv_distance=``）⇒ 结果为 ``waiting``（或更靠前的
   状态）。"证据到达率超阈"是否也算 ``thesis_changed``/``needs_research`` 属**规格外判定**，
   要改必须回 §15.4 的优先级表，不得在实现里偷偷加判据（§15.0 C-5）。
2. 只由人工确认闸产生的 ``MANUAL`` 触发（§9.4 / ``run_once`` 的 Tier 5）同样**不**改状态字
   （优先级表未列该 kind）；该情形已由 ``TrackingReport.blocked_actions`` / ``escalation_tier``
   携带，状态字不重复表达。
"""

from __future__ import annotations

import enum
from collections.abc import Sequence

from alphabee.tracking.triggers import Trigger, TriggerKind

__all__ = [
    "EXIT_DOWNGRADE_PREFIX",
    "EXIT_POSITION_DIVERGENCE_PREFIX",
    "EXIT_TRIGGERED_PREFIX",
    "MONITOR_TV_PREFIX",
    "ResearchStatus",
    "research_status",
]


class ResearchStatus(enum.StrEnum):
    """研究生命周期状态字（§15.4-A 五态；**无 ACTIVE**，见模块 docstring）。"""

    INVALIDATED = "invalidated"  # thesis 被证伪（退出条件触发）
    RISK_ALERT = "risk_alert"  # 状态降级 / 仓位背离 / 行情异动
    THESIS_CHANGED = "thesis_changed"  # 信念漂移超阈（tv_distance）
    NEEDS_RESEARCH = "needs_research"  # 陈旧 / 新财报公告 / watch 到期
    WAITING = "waiting"  # 无触发


# ── 同源前缀常量（来源 = 上游既有格式化输出；同源断言见 tests/tracking/test_status.py） ──

#: ``check_exit``：``f"退出条件触发：{','.join(d.exit_conditions_met)}"``（diff_consumers:55）
EXIT_TRIGGERED_PREFIX = "退出条件触发"

#: ``check_exit``：``f"状态降级：{argmax_from}→{argmax_to}"``（diff_consumers:57）
EXIT_DOWNGRADE_PREFIX = "状态降级"

#: ``check_exit``：固定文案 ``"仓位带与实际仓位背离"``（diff_consumers:59）
EXIT_POSITION_DIVERGENCE_PREFIX = "仓位带与实际仓位背离"

#: ``monitor_triggers``：``f"tv_distance={tv:.3f}>{tv_threshold}"``（diff_consumers:119）
MONITOR_TV_PREFIX = "tv_distance="

#: 第 4 序认定的触发类别（新财报 / 公告 / 陈旧到期 ⇒ 需要重新研究）
_NEEDS_RESEARCH_KINDS = frozenset(
    {
        TriggerKind.STALE_EXPIRED,
        TriggerKind.FINANCIAL_REPORT,
        TriggerKind.ANNOUNCEMENT,
    }
)


def research_status(
    *,
    exit_reasons: Sequence[str] = (),
    monitor_reasons: Sequence[str] = (),
    triggers: Sequence[Trigger] = (),
    stale: bool = False,
) -> ResearchStatus:
    """研究生命周期**派生视图**（§14.3 D3 的 C2；纯投影，见模块 docstring 的优先级表）。

    Args:
        exit_reasons: ``check_exit(diff).reasons``（退出条件触发 / 状态降级 / 仓位带背离）。
        monitor_reasons: ``monitor_triggers(diff).triggers``（``tv_distance=…`` / ``evidence_arrival_rate=…``）。
        triggers: ``detect_triggers(...)`` 的触发列表（按 ``kind`` 投影）。
        stale: 帧是否已陈旧（调用方按 ``STALE_EXPIRED`` 触发判定；与 ``triggers`` 同源）。

    Returns:
        :class:`ResearchStatus`：五态之一，**互斥**（判定顺序即优先级）。

    纯函数：不读 config、不新增阈值、不落盘、无 IO、无可变状态；同一输入恒返回同一结果。
    """
    exits = tuple(exit_reasons or ())
    monitors = tuple(monitor_reasons or ())
    kinds = {getattr(trigger, "kind", None) for trigger in (triggers or ())}

    if any(str(reason).startswith(EXIT_TRIGGERED_PREFIX) for reason in exits):
        return ResearchStatus.INVALIDATED
    if (
        any(str(reason).startswith(EXIT_DOWNGRADE_PREFIX) for reason in exits)
        or any(str(reason).startswith(EXIT_POSITION_DIVERGENCE_PREFIX) for reason in exits)
        or TriggerKind.PRICE_MOVE in kinds
    ):
        return ResearchStatus.RISK_ALERT
    if any(str(reason).startswith(MONITOR_TV_PREFIX) for reason in monitors):
        return ResearchStatus.THESIS_CHANGED
    if stale or (kinds & _NEEDS_RESEARCH_KINDS):
        return ResearchStatus.NEEDS_RESEARCH
    return ResearchStatus.WAITING
