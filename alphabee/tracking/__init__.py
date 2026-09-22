"""宏观环自动调度（F4 / 设计文档 §9、§14.5-A）。

对外只暴露五件事：

1. **触发判定**（:mod:`alphabee.tracking.triggers`，纯函数）；
2. **一次性 reconcile 调度**（:mod:`alphabee.tracking.scheduler`，复用 midterm 引擎）；
3. **偏离账本投影**（:mod:`alphabee.tracking.ledger`，研究连续体 P3 / §15.3）；
4. **研究生命周期派生视图**（:mod:`alphabee.tracking.status`，研究连续体 P4 / §15.4）；
5. **L2 引擎协议**（:mod:`alphabee.tracking.engine`，研究连续体 P6 / §8）—— 具体适配器在
   :mod:`alphabee.tracking.engines`，**刻意不在此处导出**，以保持 ``import alphabee.tracking`` 轻量
   （不拉起 ``orchestrator`` 链）。
行动类输出永不自动执行（§9.4 红线，:func:`~alphabee.tracking.scheduler.require_human_confirm`
恒返回 ``False``）。
"""

from alphabee.tracking.engine import ResearchContext, ResearchEngine, ResearchOutput
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
    "ResearchContext",
    "ResearchEngine",
    "ResearchOutput",
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
