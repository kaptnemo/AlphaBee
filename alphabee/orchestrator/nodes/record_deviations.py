"""run 尾部偏离账本 sink（F0b，文档 §14.1-D；F5 追加度量层落库，§11.1 / §14.5-B）。

职责：把本 run 的 ``state["issues"]`` 写入 ``deviation_events`` 账本（§5.2 跨 run 持久化），
随后（F5）计算 §11.1 八项指标并把指标落进同库的 ``deviation_metrics``。

工程约束
--------
1. **fail-open**：DB / 配置 / 任何异常只记 ``logger.warning``，绝不打断 run（§14.0 约定 3）；
   本节点返回的 Step 记录 ``written / failed / skipped`` 计数（满足
   ``written + failed + skipped == issue_count``），便于观测被 fail-open 丢弃的量。
   F5 追加的 ``metrics_stored``（0/1）同属**计数语义**，不参与该不变式（它统计的是"每 run 一份"
   的指标行，不是 issue 条数）。
2. **不修改 state["issues"]**：只在返回的 partial state 里带 step（账本是只读派生物），
   分类解析用 ``model_copy`` 生成新对象，不回写原 issue。
3. **只依赖 ``state["issues"]`` + 本 run 账本**：不依赖上游 artifact / midterm flag，因此对
   "无 midterm / 无 review_report" 的路径同样可达（§14.1-D 注）。F5 的指标计算读
   ``state`` 的 ``steps`` / ``decisions`` / ``issues`` / ``run``，同样不新增任何前置条件。
4. **运行时读开关**：``deviation.ledger.enabled`` / ``deviation.ledger.fingerprint_normalize``
   （§14.6，配置模型随后续阶段落地）；配置缺失时默认开启，且**不在 import 期**读配置。
   F5 的指标**与账本同一开关**：关闭 ⇒ 既不写账本也不写指标（§14.8 PR8「关掉即无副作用」）。
5. **轻量 import**：``OrchestratorState`` / ``RunnableConfig`` 仅用于类型标注（TYPE_CHECKING），
   不 import ``orchestrator.collectors``（那条链会拉起 tushare 等重依赖），
   使 sink 可被独立单测而不产生外部副作用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from alphabee.core.schemas import Step, StepStatus
from alphabee.data_fetch.deviation_store import list_events, record_event
from alphabee.orchestrator.services.deviation import resolve_deviation_class
from alphabee.orchestrator.services.telemetry import compute_deviation_metrics, load_metrics, store_metrics

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


def _store_run_metrics(state: OrchestratorState, run_id: str) -> int:
    """F5（§11.1 / §14.5-B）：计算并落库本 run 的偏离指标。

    :return: ``1`` = 指标行确实可读回（落库成功）；``0`` = 任何一步失败/未成。
        内部**完全 fail-open**：``compute`` / ``store`` / 回读的异常都只 warning 并返回 0，
        绝不把度量层的故障传染给 run（§14.0 约定 3）。

    账本切片 = ``list_events(run_id=run_id)``：与 ``record_event`` 的 upsert 口径一致
    （行内 ``run_id`` 被刷新为最近一次观测所在 run），因此它正是"本 run 触碰过的偏离"。
    """
    try:
        ledger = list_events(run_id=run_id)
        store_metrics(compute_deviation_metrics(state, ledger), run_id=run_id)
        return 1 if load_metrics(run_id) is not None else 0
    except Exception as exc:  # noqa: BLE001 - 度量是旁路观测，绝不打断 run
        logger.warning("deviation metrics sink failed (fail-open): %s", exc, exc_info=True)
        return 0


async def record_deviations(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    """把本 run 的偏离写入账本（F0b）+ 落库本 run 指标（F5）；返回仅含 step 的 partial state。"""
    del config
    run = state.get("run")
    run_id = run.id if run is not None else ""
    symbol = run.context.get("symbol") if run is not None else None
    issues = list(state.get("issues") or [])

    written = 0
    failed = 0
    skipped = 0
    metrics_stored = 0

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
            # F5：指标计算与落库（同一开关；自身 fail-open，不影响上面的计数不变式）
            metrics_stored = _store_run_metrics(state, run_id)
    except Exception as exc:  # fail-open：账本是旁路观测，绝不打断 run（§14.0 约定 3）
        logger.warning("deviation ledger sink failed (fail-open): %s", exc, exc_info=True)
        # 未写成的部分计入 failed，保持 written + failed + skipped == issue_count 的不变式
        failed = len(issues) - written - skipped

    # 等价于 collectors._finalize_step(step, [], [])（无 issue/artifact → SUCCEEDED、outputs=[]），
    # 但不 import collectors：那条链会拉起 agents.facts → tushare 等重依赖（见模块 docstring 第 5 条）。
    # 写入失败不进 Issue 列表（否则账本自身的故障会递归进账本），只体现在 inputs 计数上。
    # ``metrics_stored`` 是 F5 **追加**键（既有键语义不变，§14.1-D/F5 接线约定）。
    step = Step(
        id=NODE_ID,
        kind=NODE_ID,
        inputs={
            "symbol": symbol,
            "issue_count": len(issues),
            "written": written,
            "failed": failed,
            "skipped": skipped,
            "metrics_stored": metrics_stored,
        },
        status=StepStatus.SUCCEEDED,
        outputs=[],
    )
    return {"steps": [step]}
