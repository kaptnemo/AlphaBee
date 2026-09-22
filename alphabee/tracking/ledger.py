"""tracking 帧 → 偏离账本投影（研究连续体 P3 / D1-A2；``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §14.3 D1 + §15.3）。

**定位**：把跟踪环（F4 的 ``TrackingReport``）观测到的偏离写进**既有** ``deviation_events``
账本 —— 同一账本的**另一个观测来源**，而不是第二套记录。这样 ``--deviations`` 既能看到分析 run
的偏离，也能看到跟踪帧的偏离，§14.3 D1 的"跟踪路径可观测"由此落地。

三条设计边界（§15.3-A 的落地口径）：

1. **不改表结构**：``deviation_events`` 的 17 列已冻结（框架 §14.1-C），跟踪帧的 ``run_id`` 用
   **前缀约定** ``track:<symbol>:<as_of>`` 表达来源，而不是新增列；
2. **``deviation_class`` 显式给定**：本模块的 5 个投影点都显式写 ``deviation_class``，不走
   ``services.deviation.resolve_deviation_class`` 的 category 惰性回退（账本存的是显式分类，
   不依赖映射表当前状态）；
3. **fail-open**：任何异常（DB 不可用、字段缺失……）都只 ``logger.warning`` 并返回 ``0``，
   **绝不打断跟踪帧**（§15.0 C-3）。

**投影表**（§15.3-A，category 必须稳定 —— 账本指纹依赖它；5 个 category 均已登记
``services.deviation.CLASS_BY_CATEGORY``）：

===============================  ===========================  =============  ==================
``TrackingReport`` 条件           ``category``                 class/severity ``recovery_action``
===============================  ===========================  =============  ==================
``exit_reasons`` 非空             ``tracking_exit_signal``      D3 / HIGH      ``escalate``
``monitor_reasons`` 非空          ``tracking_drift_trigger``    D4 / MEDIUM    ``deep_research_due``
``contradiction.forced_sides``    ``tracking_contradiction_forced``  D3 / MEDIUM  ``forced_accounting``
``degraded=True``                 ``tracking_frame_degraded``   D2 / MEDIUM    ``degraded_frame``
``skipped_reason`` 非空           ``tracking_frame_skipped``    D5 / LOW       ``diff_skipped``
===============================  ===========================  =============  ==================

**``related_step`` / ``detected_at_step`` = ``"tracking"``**：跟踪帧不在
``services.deviation.NODE_ORDER`` 内，故 ``detection_latency`` 对它们返回 ``None`` —— 这是**预期**
行为（时间线用"节点序 × 时延"度量**分析 run**；跟踪帧是另一个观测来源），**不得"修"**。

**具名顺延项（本轮显式不做，见交付 output 与 ROADMAP 的登记建议）**：

* ``run_once`` 的 **reconcile 异常早退分支**（``report.degraded`` 由 reconcile 失败置位）不写账本：
  §15.3-B 的接线点只覆盖正常返回路径，扩到异常分支属新增行为，需 captain 裁定；
* 未读 ``deviation.ledger.enabled`` 总开关：§15.3-B 的开关是 ``persist``（"与帧落盘同一开关"），
  账本总开关的联动是另一处接线决策。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from alphabee.core import DeviationClass, Issue, IssueScope, IssueSeverity
from alphabee.utils.pipeline import make_id

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查：运行期不 import scheduler（避免循环依赖）
    from alphabee.tracking.scheduler import TrackingReport

logger = logging.getLogger(__name__)

__all__ = [
    "TRACKING_RUN_PREFIX",
    "TRACKING_STEP",
    "is_tracking_run_id",
    "record_tracking_deviations",
    "tracking_issues",
    "tracking_run_id",
]

#: 跟踪帧的账本 ``run_id`` 前缀（§14.3 D1 的前缀约定）。
TRACKING_RUN_PREFIX = "track"

#: 跟踪帧在账本里的步骤标识（``related_step`` / ``detected_at_step``）。
#: **不在** ``services.deviation.NODE_ORDER`` 内 ⇒ ``detection_latency`` 返回 ``None``（预期）。
TRACKING_STEP = "tracking"

#: 投影出的 ``Issue.scope``：跟踪帧是**观测/数据侧**的偏离（不是 report/planning 阶段的产物）。
#: 注：账本 ``deviation_events`` 的 17 列**不含** ``scope`` ⇒ 该字段只用于人读与将来可能的消费者。
_TRACKING_SCOPE = IssueScope.DATA


def tracking_run_id(symbol: str, as_of: str) -> str:
    """跟踪帧的账本 ``run_id`` 约定：``track:<symbol>:<as_of>``（§15.3-A）。

    用前缀约定而非新增表列：``deviation_events`` 的 17 列已冻结（框架 §14.1-C），加列成本远高于
    前缀；前缀同时让 ``--deviations`` 能按来源分组（``track:`` ⇒ ``[track]`` 标注）。
    """
    return f"{TRACKING_RUN_PREFIX}:{symbol}:{as_of}"


def is_tracking_run_id(run_id: str | None) -> bool:
    """``run_id`` 是否跟踪帧（前缀判定；供视图/度量按来源分组复用，避免各处各写一遍前缀）。"""
    return str(run_id or "").strip().startswith(f"{TRACKING_RUN_PREFIX}:")


def _as_list(value: Any) -> list[str]:
    """只接受真正的 ``list``（``None``/标量/字符串 → ``[]``：不猜测、不把 str 逐字符展开）。"""
    return [str(item) for item in value] if isinstance(value, list) else []


def _safe_str(report: Any, name: str) -> str:
    """安全取字段的字符串值（字段缺失/取值抛异常 → ``""``）。

    存在的理由：``except`` 分支里的**诊断日志**不得成为新的故障点 —— 一个"连属性都取不到"的
    报告对象不能把 fail-open 变成抛异常。
    """
    try:
        return str(getattr(report, name, "") or "")
    except Exception:  # noqa: BLE001 - 诊断路径绝不抛
        return ""


def _issue(
    *,
    category: str,
    deviation_class: DeviationClass,
    severity: IssueSeverity,
    recovery_action: str,
    message: str,
) -> Issue:
    """构造一条跟踪帧偏离（``deviation_class`` **显式给定**；``related_step`` = ``"tracking"``）。"""
    return Issue(
        id=make_id("issue"),
        severity=severity,
        category=category,
        message=message,
        related_step=TRACKING_STEP,
        scope=_TRACKING_SCOPE,
        deviation_class=deviation_class,
        detected_at_step=TRACKING_STEP,
        recovery_action=recovery_action,
    )


def tracking_issues(report: TrackingReport) -> list[Issue]:
    """把一次跟踪帧投影为偏离（§15.3-A 的投影表，5 条逐条判定）。

    message 里带 ``report.as_of`` 与**具体 reason 文本**（不是只写类别名）：账本行内 message 存
    "最近一次原始文本"，人读时才有信息量（指纹仍按归一化 message 去重，数字会被折成 ``#``）。

    任何字段缺失/类型异常都走保守路径（把该字段当空），**不抛异常**。
    """
    as_of = str(getattr(report, "as_of", "") or "")
    exit_reasons = _as_list(getattr(report, "exit_reasons", None))
    monitor_reasons = _as_list(getattr(report, "monitor_reasons", None))

    contradiction = getattr(report, "contradiction", None)
    forced_sides = _as_list(getattr(contradiction, "forced_sides", None))

    degraded = bool(getattr(report, "degraded", False))
    degraded_reason = str(getattr(report, "degraded_reason", "") or "")
    skipped_reason = str(getattr(report, "skipped_reason", "") or "")

    issues: list[Issue] = []
    if exit_reasons:
        issues.append(
            _issue(
                category="tracking_exit_signal",
                deviation_class=DeviationClass.D3_ARGUMENT,
                severity=IssueSeverity.HIGH,
                recovery_action="escalate",
                message=f"跟踪帧 {as_of} 命中退出信号（{len(exit_reasons)} 条）：{'；'.join(exit_reasons)}",
            )
        )
    if monitor_reasons:
        issues.append(
            _issue(
                category="tracking_drift_trigger",
                deviation_class=DeviationClass.D4_STATE,
                severity=IssueSeverity.MEDIUM,
                recovery_action="deep_research_due",
                message=f"跟踪帧 {as_of} 命中监控漂移触发（{len(monitor_reasons)} 条）：{'；'.join(monitor_reasons)}",
            )
        )
    if forced_sides:
        issues.append(
            _issue(
                category="tracking_contradiction_forced",
                deviation_class=DeviationClass.D3_ARGUMENT,
                severity=IssueSeverity.MEDIUM,
                recovery_action="forced_accounting",
                message=f"跟踪帧 {as_of} 存在被强制记账的反证侧（{len(forced_sides)} 条）：{'；'.join(forced_sides)}",
            )
        )
    if degraded:
        issues.append(
            _issue(
                category="tracking_frame_degraded",
                deviation_class=DeviationClass.D2_STRUCTURE,
                severity=IssueSeverity.MEDIUM,
                recovery_action="degraded_frame",
                message=f"跟踪帧 {as_of} 为降级帧：{degraded_reason or '（未给出降级原因）'}",
            )
        )
    if skipped_reason:
        issues.append(
            _issue(
                category="tracking_frame_skipped",
                deviation_class=DeviationClass.D5_CONTROL,
                severity=IssueSeverity.LOW,
                recovery_action="diff_skipped",
                message=f"跟踪帧 {as_of} 未差分（as_of 未推进）：{skipped_reason}",
            )
        )
    return issues


def record_tracking_deviations(report: TrackingReport) -> int:
    """逐条 ``record_event(issue, run_id=tracking_run_id(...), symbol=…, step_id="tracking")`` 写账本。

    复用既有 ``deviation_store.record_event`` 的**指纹去重 + upsert**（同一偏离再次观测 ⇒
    ``occurrence_count`` 递增、``last_seen_at`` / ``run_id`` 刷新），**不改表结构**。

    Returns:
        成功写入（含 upsert 命中）的条数；无偏离 → ``0``。

    fail-open：DB 不可用/任何异常 → ``0``（只 ``logger.warning``），绝不打断跟踪帧。
    """
    try:
        from alphabee.data_fetch.deviation_store import record_event

        issues = tracking_issues(report)
        if not issues:
            return 0
        symbol = _safe_str(report, "symbol")
        run_id = tracking_run_id(symbol, _safe_str(report, "as_of"))
        written = 0
        for issue in issues:
            if record_event(issue, run_id=run_id, symbol=symbol or None, step_id=TRACKING_STEP) is not None:
                written += 1
        return written
    except Exception as exc:  # noqa: BLE001 - 账本写失败绝不打断跟踪帧（§15.0 C-3 / §15.3-A）
        logger.warning(
            "tracking deviation ledger write failed (fail-open): symbol=%s as_of=%s err=%s",
            _safe_str(report, "symbol"),
            _safe_str(report, "as_of"),
            exc,
        )
        return 0
