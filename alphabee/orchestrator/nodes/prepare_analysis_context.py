"""prepare_analysis_context node — 抽取用户问题与标的，建立/复用 run 上下文并做入口前置校验。

从 ``collectors.collect_raw_facts`` 拆出（语义逐字保留）：

1. 从消息历史还原用户最新问题（``_latest_query``）；
2. 从问题中解析首个股票标的（``_first_symbol``）；
3. 无入参 run 时新建，已有 run 时复用并**合并** context（研究连续体 P6 / §15.6-C）；
4. 入口前置校验的**记录侧**（§15.2-B）：命中"带陈旧/未对账状态启动"时写 ``stale_state_run`` issue。

产出 ``run.context`` 是整条编排链统一的 query / symbol 上下文：
下游 ``collect_raw_facts`` 及所有节点都只从 ``run.context`` 读取，不再各自解析消息。
"""

from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import (
    DeviationClass,
    Issue,
    IssueScope,
    IssueSeverity,
    Run,
    RunStatus,
    Step,
    StepStatus,
)
from alphabee.orchestrator.services.deviation import record_deviation
from alphabee.orchestrator.services.preflight import check_preflight
from alphabee.orchestrator.state import OrchestratorState
from alphabee.tools.common import extract_symbols_from_query
from alphabee.utils.pipeline import extract_text, make_id


def _latest_query(messages: list[AnyMessage]) -> str:
    """还原用户最新一条人类消息文本；无人类消息时退回末条消息文本。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            text = extract_text(message.content).strip()
            if text:
                return text
    return extract_text(messages[-1].content).strip() if messages else ""


def _first_symbol(query: str) -> str | None:
    """Extract the first stock symbol from a query string."""
    symbols = extract_symbols_from_query(query)
    if symbols:
        return list(symbols.values())[0]
    return None


async def prepare_analysis_context(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """解析用户问题 + 标的，并用它们建立或合并 run.context。"""
    del config
    query = _latest_query(state.get("messages", []))
    symbol = _first_symbol(query)

    # ── 研究连续体 P6（§15.6-C）：已有 run 则复用/合并 context，不覆盖调用方注入值 ──
    # `run` 是 last-write-wins（无 reducer），因此必须在本节点一次性定稿：
    # 调用方已注入 run 时保留 id / goal / status / started_at，只并入本节点解析的 query
    # （以及可用时的 symbol）；未注入时走原分支新建 run（语义与拆分前逐字一致）。
    incoming_run = state.get("run")
    if incoming_run is not None:
        merged_context = {**dict(getattr(incoming_run, "context", None) or {}), "query": query}
        if symbol:
            # 查询串里解析出标的才覆盖注入值；解析不到时**保留**调用方注入的 symbol（不写成 None）
            merged_context["symbol"] = symbol
        run = incoming_run.model_copy(update={"context": merged_context})
    else:
        run = Run(
            id=make_id("orch-run"),
            goal=query or "investment analysis",
            status=RunStatus.RUNNING,
            context={"query": query, "symbol": symbol},
            started_at=datetime.now(),
        )

    step = Step(
        id="prepare_analysis_context",
        kind="prepare_analysis_context",
        inputs={"query": query, "symbol": symbol},
        status=StepStatus.RUNNING,
    )

    issues: list[Issue] = []

    # ── 研究连续体 P2（D2-B2）：入口前置校验的**记录侧**（§15.2-B）──
    # "记录"与"阻断"解耦（§15.2 设计决策 3）：这里以 ``block_enabled=False`` 调用 ⇒ 本次调用
    # **永不阻断**分析（首次研究必放行），只在**命中**"带陈旧/未对账状态启动"时把这一事实写进
    # issues（D5/MEDIUM，category 已登记 CLASS_BY_CATEGORY）。因此即使用户在 CLI 侧用
    # ``--allow-stale`` 显式放行，账本/state 里仍留下"这次是在陈旧状态下跑的"——这正是
    # §11 验收 2 修订口径"不可能**静默地**继续"的落点。
    # 判定本身是纯规则 + fail-open（services/preflight.py）：无帧/读盘异常 ⇒ 不产 issue，
    # 不改变无状态标的的现状行为。
    verdict = check_preflight(symbol, block_enabled=False)
    if verdict.checked and (verdict.stale or verdict.pending):
        age = "—" if verdict.days_since is None else str(verdict.days_since)
        issues.append(
            record_deviation(
                DeviationClass.D5_CONTROL,
                IssueSeverity.MEDIUM,
                f"在未对账的陈旧状态下启动分析：最新帧 {verdict.latest_frame_id or '—'}"
                f"（as_of={verdict.as_of_date or '—'}，{age} 天前；"
                f"待消费触发 {len(verdict.pending)} 条）。"
                "本次结论可能滞后于最新事件。",
                detected_at_step="prepare_analysis_context",
                category="stale_state_run",
                scope=IssueScope.PLANNING,
                recovery_action="proceeded_without_reconcile",
            )
        )

    completed_step = step.model_copy(
        update={"status": StepStatus.PARTIAL if issues else StepStatus.SUCCEEDED, "outputs": []}
    )
    return {"run": run, "steps": [completed_step], "issues": issues}
