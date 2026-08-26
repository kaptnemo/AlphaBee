"""Insight synthesis node — runs InsightAgent between verification and thesis.

四级降级阶梯（ROADMAP 0.4，见 docs/design/INSIGHT_DEGRADATION_DESIGN.md）：

- Tier 0: 严格解析成功
- Tier 1: ``lenient_parse`` 宽松救援（修结构不补内容）
- Tier 2: ``build_fallback_insight`` 确定性兜底（复用 build_insight_context 的 dict）
- Tier 3: ``build_minimal_insight`` 最小骨架

任何失败模式下 INSIGHT_ANALYSIS artifact 都必然存在，观点层永不整层丢失；
降级信息随 artifact 落库（degraded / fallback_tier / degradation_reason）。
"""

from __future__ import annotations

import json as _json
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _extract_final_text, _finalize_step, _make_id
from alphabee.orchestrator.contracts import InsightArtifact
from alphabee.orchestrator.services.payload_builders import build_insight_context
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import parse_json


def _insight_artifact(
    step_id: str,
    output: Any,
    *,
    tier: int,
    reason: str,
) -> Artifact:
    """Build the INSIGHT_ANALYSIS artifact from an InsightOutput, with degradation metadata."""
    return Artifact(
        id=_make_id("artifact"),
        type=ArtifactType.INSIGHT_ANALYSIS,
        producer_step=step_id,
        value=InsightArtifact(
            core_view=output.core_view,
            central_tension=output.central_tension,
            main_driver=output.main_driver,
            supporting_evidence=[e.model_dump(mode="json") for e in output.supporting_evidence],
            counter_evidence=[e.model_dump(mode="json") for e in output.counter_evidence],
            materiality_rank=[m.model_dump(mode="json") for m in output.materiality_rank],
            cross_signal_patterns=[p.model_dump(mode="json") for p in output.cross_signal_patterns],
            business_model_context=output.business_model_context,
            base_case=output.base_case,
            bull_case=output.bull_case,
            bear_case=output.bear_case,
            what_would_change_my_mind=list(output.what_would_change_my_mind),
            confidence=output.confidence,
            degraded=tier >= 1,
            fallback_tier=tier,
            degradation_reason=reason,
        ).model_dump(mode="json"),
    )


async def synthesize_insights(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Run the InsightAgent to synthesize upstream findings into a central viewpoint.

    This node consumes signals, anomalies, conflicts, and verification results
    and produces an ``insight_analysis`` artifact that downstream thesis and
    report nodes can use as their narrative backbone.

    Insertion point: verify_hypotheses → synthesize_insights → run_thesis
    """
    from alphabee.agents.insights.agent import insight_agent_factory
    from alphabee.agents.insights.models import InsightOutput
    from alphabee.agents.insights.prompts import INSIGHT_AGENT_USER_TEMPLATE
    from alphabee.agents.insights.rescue import build_fallback_insight, build_minimal_insight, lenient_parse

    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="synthesize_insights",
        kind="synthesize_insights",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    # ── Build context ────────────────────────────────────────────────
    try:
        context = build_insight_context(state, symbol)
    except Exception as exc:
        # Tier 3：上下文构建失败，产出最小骨架，保证观点 artifact 存在
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="context_build_failure",
                message=f"Failed to build insight context: {exc}",
                related_step=step.id,
            )
        )
        reason = f"context_build_failure: {exc}"
        minimal = build_minimal_insight(symbol, reason)
        new_artifacts.append(_insight_artifact(step.id, minimal, tier=3, reason=reason))
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="insight_degraded",
                message=f"Insight 降级产出（tier=3）: {reason[:200]}",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            "steps": [completed_step],
            "issues": new_issues,
            "artifacts": new_artifacts,
        }

    # ── Run InsightAgent ─────────────────────────────────────────────
    try:
        context_json = _json.dumps(context, ensure_ascii=False, indent=2)
        user_msg = INSIGHT_AGENT_USER_TEMPLATE.substitute(context_json=context_json)
        agent = insight_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_msg)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        # Tier 3：agent 调用失败
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"InsightAgent failed: {exc}",
                related_step=step.id,
            )
        )
        reason = f"agent_failure: {exc}"
        minimal = build_minimal_insight(symbol, reason)
        new_artifacts.append(_insight_artifact(step.id, minimal, tier=3, reason=reason))
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="insight_degraded",
                message=f"Insight 降级产出（tier=3）: {reason[:200]}",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            "steps": [completed_step],
            "issues": new_issues,
            "artifacts": new_artifacts,
        }

    # ── Parse output（Tier 0 → Tier 1 → Tier 2 → Tier 3）─────────────
    if not raw_text:
        # Tier 3：空响应
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="empty_response",
                message="InsightAgent returned empty response.",
                related_step=step.id,
            )
        )
        reason = "empty_response"
        minimal = build_minimal_insight(symbol, reason)
        new_artifacts.append(_insight_artifact(step.id, minimal, tier=3, reason=reason))
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="insight_degraded",
                message=f"Insight 降级产出（tier=3）: {reason}",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            "steps": [completed_step],
            "issues": new_issues,
            "artifacts": new_artifacts,
        }

    try:
        # Tier 0：严格解析
        parsed = parse_json(raw_text)
        insight_output = InsightOutput.model_validate(parsed)
        tier, reason = 0, ""
    except Exception as strict_exc:
        # Tier 1：宽松救援
        rescued = lenient_parse(raw_text)
        if rescued is not None:
            insight_output, reason = rescued
            tier = 1
        else:
            # Tier 2：确定性兜底（复用已构建的 context，无 LLM）
            insight_output = build_fallback_insight(context, symbol)
            has_content = bool(
                insight_output.core_view
                or insight_output.central_tension
                or insight_output.main_driver
                or insight_output.supporting_evidence
                or insight_output.counter_evidence
                or insight_output.materiality_rank
                or insight_output.what_would_change_my_mind
            )
            if has_content:
                tier = 2
                reason = f"strict_parse_failed: {strict_exc}"
            else:
                # Tier 3：上下文本身无数据
                insight_output = build_minimal_insight(symbol, "empty_context")
                tier = 3
                reason = "empty_context"

    if tier >= 1:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="insight_degraded",
                message=f"Insight 降级产出（tier={tier}）: {reason[:200]}",
                related_step=step.id,
            )
        )

    new_artifacts.append(_insight_artifact(step.id, insight_output, tier=tier, reason=reason))

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed_step],
        "artifacts": new_artifacts,
        "issues": new_issues,
    }
