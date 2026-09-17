"""run 尾部偏离账本 sink（F0b，文档 §14.1-D）。

职责：把本 run 的 ``state["issues"]`` 写入 ``deviation_events`` 账本（§5.2 跨 run 持久化）。
副作用集中在这一个节点，F5 会在此追加 ``compute_deviation_metrics`` + ``store_metrics``。

工程约束
--------
1. **fail-open**：DB / 配置 / 任何异常只记 ``logger.warning``，绝不打断 run（§14.0 约定 3）；
   本节点返回的 Step 记录 ``written / failed / skipped`` 计数（满足
   ``written + failed + skipped == issue_count``），便于观测被 fail-open 丢弃的量。
2. **不修改 state["issues"]**：只在返回的 partial state 里带 step（账本是只读派生物），
   分类解析用 ``model_copy`` 生成新对象，不回写原 issue。
3. **只依赖 ``state["issues"]``**：不依赖上游 artifact / midterm flag，因此对
   "无 midterm / 无 review_report" 的路径同样可达（§14.1-D 注）。
4. **运行时读开关**：``deviation.ledger.enabled`` / ``deviation.ledger.fingerprint_normalize``
   （§14.6，配置模型随后续阶段落地）；配置缺失时默认开启，且**不在 import 期**读配置。
5. **轻量 import**：``OrchestratorState`` / ``RunnableConfig`` 仅用于类型标注（TYPE_CHECKING），
   不 import ``orchestrator.collectors``（那条链会拉起 tushare 等重依赖），
   使 sink 可被独立单测而不产生外部副作用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from alphabee.core.schemas import Step, StepStatus
from alphabee.data_fetch.deviation_store import record_event
from alphabee.orchestrator.services.deviation import resolve_deviation_class

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from langchain_core.runnables import RunnableConfig

    from alphabee.orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

#: 节点 id（与 ``agent.py`` 的 ``add_node`` 名、``services/deviation.NODE_ORDER`` 登记一致）。
NODE_ID = "record_deviations"

#: 配置缺省开关（§14.6 默认值）。
_DEFAULT_ENABLED = True
_DEFAULT_FINGERPRINT_NORMALIZE = True


def ledger_switches() -> tuple[bool, bool]:
    """读取账本开关：``(enabled, fingerprint_normalize)``。

    运行时读取 ``get_settings().deviation.ledger``（§14.6），容忍配置缺失：
    字段/配置段不存在或读取异常 → ``(True, True)``（与 §14.6 默认值一致，fail-open）。
    配置模型（``DeviationSettings``）由后续阶段加入 ``alphabee/config``，
    因此这里必须容忍 ``AttributeError``，而不是假定字段存在。
    """
    try:
        from alphabee.config import get_settings

        ledger = getattr(getattr(get_settings(), "deviation", None), "ledger", None)
        if ledger is None:
            return _DEFAULT_ENABLED, _DEFAULT_FINGERPRINT_NORMALIZE
        return (
            bool(getattr(ledger, "enabled", _DEFAULT_ENABLED)),
            bool(getattr(ledger, "fingerprint_normalize", _DEFAULT_FINGERPRINT_NORMALIZE)),
        )
    except Exception as exc:
        logger.warning("deviation ledger settings unavailable (fail-open, default on): %s", exc)
        return _DEFAULT_ENABLED, _DEFAULT_FINGERPRINT_NORMALIZE


async def record_deviations(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    """把本 run 的偏离写入账本；返回仅含 step 的 partial state。"""
    del config
    run = state.get("run")
    run_id = run.id if run is not None else ""
    symbol = run.context.get("symbol") if run is not None else None
    issues = list(state.get("issues") or [])

    written = 0
    failed = 0
    skipped = 0

    try:
        enabled, fingerprint_normalize = ledger_switches()
        if not enabled:
            skipped = len(issues)
            logger.info("deviation ledger disabled by config; skipped %d issue(s)", skipped)
        else:
            for issue in issues:
                # 分类惰性解析：只在本节点生成副本，不回写 state["issues"]
                resolved_issue = (
                    issue
                    if issue.deviation_class is not None
                    else issue.model_copy(update={"deviation_class": resolve_deviation_class(issue)})
                )
                event = record_event(
                    resolved_issue,
                    run_id=run_id,
                    symbol=symbol,
                    step_id=issue.related_step,
                    normalize=fingerprint_normalize,
                )
                if event is None:
                    failed += 1
                else:
                    written += 1
    except Exception as exc:  # fail-open：账本是旁路观测，绝不打断 run（§14.0 约定 3）
        logger.warning("deviation ledger sink failed (fail-open): %s", exc, exc_info=True)
        # 未写成的部分计入 failed，保持 written + failed + skipped == issue_count 的不变式
        failed = len(issues) - written - skipped

    # 等价于 collectors._finalize_step(step, [], [])（无 issue/artifact → SUCCEEDED、outputs=[]），
    # 但不 import collectors：那条链会拉起 agents.facts → tushare 等重依赖（见模块 docstring 第 5 条）。
    # 写入失败不进 Issue 列表（否则账本自身的故障会递归进账本），只体现在 inputs 计数上。
    step = Step(
        id=NODE_ID,
        kind=NODE_ID,
        inputs={
            "symbol": symbol,
            "issue_count": len(issues),
            "written": written,
            "failed": failed,
            "skipped": skipped,
        },
        status=StepStatus.SUCCEEDED,
        outputs=[],
    )
    return {"steps": [step]}
