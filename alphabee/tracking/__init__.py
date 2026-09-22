"""宏观环自动调度（F4 / 设计文档 §9、§14.5-A）。

对外只暴露四件事：**触发判定**（:mod:`alphabee.tracking.triggers`，纯函数）、
**一次性 reconcile 调度**（:mod:`alphabee.tracking.scheduler`，复用 midterm 引擎）、
**偏离账本投影**（:mod:`alphabee.tracking.ledger`，研究连续体 P3 / §15.3）与
**研究生命周期派生视图**（:mod:`alphabee.tracking.status`，研究连续体 P4 / §15.4）。
行动类输出永不自动执行（§9.4 红线，:func:`~alphabee.tracking.scheduler.require_human_confirm`
恒返回 ``False``）。
"""

from alphabee.tracking.ledger import (
    TRACKING_RUN_PREFIX,
    record_tracking_deviations,
    tracking_issues,
    tracking_run_id,
)
from alphabee.tracking.scheduler import (
    ACTION_CLASS_GATE_TIERS,
    DEFAULT_STALE_AFTER_DAYS,
    ContradictionAccounting,
    TrackingReport,
    default_alert_dir,
    enforce_attribution_accounting,
    main,
    reconcile,
    require_human_confirm,
    run_once,
    run_watchlist,
)
from alphabee.tracking.status import ResearchStatus, research_status
from alphabee.tracking.triggers import (
    DEFAULT_PRICE_MOVE_PCT,
    Trigger,
    TriggerKind,
    TriggerThresholds,
    detect_fact_triggers,
    detect_triggers,
    manual_trigger,
    thresholds_from_settings,
)

__all__ = [
    "ACTION_CLASS_GATE_TIERS",
    "DEFAULT_PRICE_MOVE_PCT",
    "DEFAULT_STALE_AFTER_DAYS",
    "TRACKING_RUN_PREFIX",
    "ResearchStatus",
    "ContradictionAccounting",
    "TrackingReport",
    "Trigger",
    "TriggerKind",
    "TriggerThresholds",
    "default_alert_dir",
    "detect_fact_triggers",
    "detect_triggers",
    "enforce_attribution_accounting",
    "main",
    "manual_trigger",
    "reconcile",
    "record_tracking_deviations",
    "research_status",
    "require_human_confirm",
    "run_once",
    "run_watchlist",
    "thresholds_from_settings",
    "tracking_issues",
    "tracking_run_id",
]
