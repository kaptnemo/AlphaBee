"""Data collection node — FactCollector LLM agent + structured model extraction.

Also provides shared utility helpers used by both collectors and analyzers:
- _make_id, _extract_final_text, _find_artifact, _build_conflict_data, _finalize_step
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.facts.agent import fact_collector_agent_factory
from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.agents.facts.tools.financial_fact import get_financial_facts_model
from alphabee.agents.facts.tools.market_fact import get_market_facts_model
from alphabee.agents.schemas import ConflictAnalysisResult
from alphabee.core import (
    Artifact,
    ArtifactType,
    Issue,
    IssueSeverity,
    Run,
    RunStatus,
    Step,
    StepStatus,
)
from alphabee.orchestrator.contracts import (
    ConflictDataSummary,
    ConflictSummary,
    FactCollectionArtifact,
    VerificationArtifact,
    VerifiedHypothesisSummary,
    find_artifact_model,
)
from alphabee.orchestrator.state import OrchestratorState
from alphabee.tools.common import extract_symbols_from_query
from alphabee.utils.pipeline import extract_text, make_id

# ── shared helpers ────────────────────────────────────────────────────


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _extract_final_text(result: dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if not messages:
        return ""
    return extract_text(messages[-1].content).strip()


def _find_artifact(artifacts: list[Artifact], artifact_type: str) -> dict | None:
    """Return the most recent artifact value matching *artifact_type*, or None."""
    for a in reversed(artifacts):
        if a.type == artifact_type and isinstance(a.value, dict):
            return a.value
    return None


def _build_conflict_data(state: OrchestratorState) -> dict:
    """Summarise conflict+verification results for downstream nodes."""
    artifacts = state.get("artifacts", [])
    conflicts_raw = find_artifact_model(artifacts, ArtifactType.CONFLICTS_RESULT, ConflictAnalysisResult)
    verification_artifact = find_artifact_model(artifacts, ArtifactType.VERIFICATION_RESULTS, VerificationArtifact)
    verification_results = verification_artifact.results if verification_artifact else []

    if not conflicts_raw:
        return {}

    conflicts = conflicts_raw.conflicts
    all_hypotheses = [h for c in conflicts for h in c.hypotheses]

    verified = [h for h in all_hypotheses if h.status in ("verified", "partial")]
    rejected = [h for h in all_hypotheses if h.status == "rejected"]

    return ConflictDataSummary(
        conflict_count=len(conflicts),
        hypothesis_count=len(all_hypotheses),
        verified_count=len(verified),
        rejected_count=len(rejected),
        verified_hypotheses=[
            VerifiedHypothesisSummary(
                id=h.id,
                explanation=h.explanation,
                status=h.status,
            )
            for h in verified
        ],
        conflicts_summary=[
            ConflictSummary(
                theme=c.theme,
                severity=c.severity,
                description=c.description[:200],
                related_dimensions=list(c.related_dimensions),
            )
            for c in conflicts
        ],
        verification_results=verification_results,
    ).model_dump(mode="json")


def _finalize_step(step: Step, issues: list[Issue], artifacts: list[Artifact]) -> Step:
    """Return a copy of *step* with status and outputs set."""
    if issues and not artifacts:
        status = StepStatus.FAILED
    elif issues:
        status = StepStatus.PARTIAL
    else:
        status = StepStatus.SUCCEEDED
    return step.model_copy(update={"status": status, "outputs": [a.id for a in artifacts]})


# ── data collection helpers ───────────────────────────────────────────


def _latest_query(messages: list[AnyMessage]) -> str:
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


# ── node: collect_raw_facts ───────────────────────────────────────────


async def collect_raw_facts(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Concurrently run FactCollector LLM agent and structured model extraction.

    Produces:
    - ``fact_collection`` artifact with the agent's narrative text and metadata
    - ``fact_values`` dict in state (canonical numeric facts for downstream engines)
    - ``financial_facts`` / ``market_facts`` Pydantic objects in state
    """
    # 业务语义：
    # 编排层首先只关心“这轮用户到底想分析谁、分析什么”。
    # 后续所有节点都依赖这里抽取出的 query / symbol 作为统一上下文，
    # 因此这里既要从消息历史里还原用户最新问题，也要尽早归一到股票标的。
    query = _latest_query(state.get("messages", []))
    symbol = _first_symbol(query)

    run = Run(
        id=_make_id("orch-run"),
        goal=query or "investment analysis",
        status=RunStatus.RUNNING,
        context={"query": query, "symbol": symbol},
        started_at=datetime.now(),
    )

    step = Step(
        id="collect_raw_facts",
        kind="collect_raw_facts",
        inputs={"query": query, "symbol": symbol},
        status=StepStatus.RUNNING,
    )

    artifacts: list[Artifact] = []
    issues: list[Issue] = []

    fact_text: str = ""
    financial_facts: FinancialFacts | None = None
    market_facts: MarketFacts | None = None

    async def _run_fact_agent() -> str:
        try:
            # FactCollector 负责补齐“定性叙述层”：
            # 它会把公司、行业、经营、风险等离散信息整理成一段可读文本，
            # 供后续 thesis / review / report 节点引用，但不直接作为数值真源。
            agent = fact_collector_agent_factory()
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=query)]},
                config=config,
            )
            return _extract_final_text(result)
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="subagent_failure",
                    message=f"FactCollector agent failed: {exc}",
                    related_step=step.id,
                )
            )
            return ""

    async def _run_structured_models() -> None:
        nonlocal financial_facts, market_facts
        if not symbol:
            return
        try:
            # Structured model 负责补齐“定量事实层”：
            # financial_facts 会被衍生指标、异常检测直接消费，
            # 所以这里与 LLM narrative 并行拉取，尽量缩短首轮数据准备时间。
            financial_facts = await asyncio.to_thread(
                get_financial_facts_model,
                symbol,
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="missing_data",
                    message=f"Financial facts unavailable for {symbol}: {exc}",
                    related_step=step.id,
                )
            )
        try:
            # market_facts 提供估值、总市值等市场维度事实，
            # 它既参与信号计算，也会帮助 thesis 判断公司所处估值语境。
            market_facts = await asyncio.to_thread(
                get_market_facts_model,
                symbol,
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="missing_data",
                    message=f"Market facts unavailable for {symbol}: {exc}",
                    related_step=step.id,
                )
            )

    fact_text, _ = await asyncio.gather(
        _run_fact_agent(),
        _run_structured_models(),
    )

    # 无论定量数据是否齐全，都保留 fact_collection artifact：
    # 它是整个编排链路里“用户问题 + 标的 + 事实摘要”的统一入口，
    # 让后续节点不用再回头解析原始消息。
    artifacts.append(
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.FACT_COLLECTION,
            producer_step=step.id,
            value=FactCollectionArtifact(
                agent="FactCollector",
                query=query,
                symbol=symbol,
                raw_response=fact_text,
            ).model_dump(mode="json"),
        )
    )

    # fact_values 是后续所有规则引擎共享的 canonical numeric layer。
    # 这里只有结构化模型里可安全计算的数字，故意不混入 narrative 文本推断，
    # 以维持“数值必须来自结构化工具”的边界。
    fact_values: dict[str, float] = {}
    if financial_facts is not None:
        fact_values.update(financial_facts.to_fact_values())
    if market_facts is not None:
        fact_values.update(market_facts.to_fact_values())

    if not fact_values and symbol:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.CRITICAL,
                category="missing_data",
                message=(
                    "No structured data available for derived facts or signal "
                    "computation. Ensure the stock symbol is recognized."
                ),
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, issues, artifacts)
    return {
        "messages": state.get("messages", []),
        "run": run,
        "steps": [completed_step],
        "artifacts": artifacts,
        "observations": [],
        "decisions": [],
        "issues": issues,
        "final_artifact_id": None,
        "evaluation_artifact_id": None,
        "supplement_round": 0,
        "max_supplement_rounds": 1,
        "report_review_round": 0,
        "max_report_review_rounds": 2,
        "report_rewrite_needed": False,
        "report_rewrite_reason": None,
        "fact_values": fact_values,
        "financial_facts": financial_facts,
        "market_facts": market_facts,
        "enhance": state.get("enhance", False),
        "llm_review": state.get("llm_review", False),
    }
