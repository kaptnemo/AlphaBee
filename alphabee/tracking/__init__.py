"""宏观环自动调度（F4 / 设计文档 §9、§14.5-A）。

对外只暴露两件事：**触发判定**（:mod:`alphabee.tracking.triggers`，纯函数）与
**一次性 reconcile 调度**（:mod:`alphabee.tracking.scheduler`，复用 midterm 引擎）。
行动类输出永不自动执行（§9.4 红线，:func:`~alphabee.tracking.scheduler.require_human_confirm`
恒返回 ``False``）。
"""

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
    "require_human_confirm",
    "run_once",
    "run_watchlist",
    "thresholds_from_settings",
]
