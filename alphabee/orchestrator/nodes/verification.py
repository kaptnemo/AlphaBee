"""Hypothesis-verification node."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alphabee.orchestrator.contracts import ConflictItem, VerificationResultItem
    from alphabee.orchestrator.state import OrchestratorState

import asyncio
import json as _json

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _extract_final_text, _finalize_step, _make_id
from alphabee.orchestrator.contracts import (
    VerificationArtifact,
    coerce_conflicts_result,
)
from alphabee.orchestrator.services.payload_builders import build_verify_context
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import parse_json


async def _verify_single_conflict(
    conflict: ConflictItem,
    shared_context: dict,
    step_id: str,
    config: RunnableConfig,
) -> tuple[list[VerificationResultItem], list[Issue]]:
    from alphabee.agents.schemas import VerificationResultList
    from alphabee.agents.verify_hypotheses.agent import verify_hypotheses_agent_factory
    from alphabee.agents.verify_hypotheses.prompts import VERIFY_HYPOTHESES_USER_TEMPLATE

    issues: list[Issue] = []
    if not conflict.hypotheses:
        return [], issues

    # 每个 conflict 下面可能挂多个“可验证假设”，
    # 这里按 conflict 为单位验证，保证同一主题的证据在一个局部上下文里被统一裁决。
    hypotheses_json = _json.dumps(
        [hypothesis.model_dump() for hypothesis in conflict.hypotheses],
        ensure_ascii=False,
        indent=2,
    )
    # shared_context 提供财务快照、估值、异常等公共证据，
    # conflict 自身再补主题与严重度，使验证 agent 明确自己要核实的矛盾点。
    ctx = {
        **shared_context,
        "conflict_theme": conflict.theme,
        "conflict_severity": conflict.severity,
    }
    context_json = _json.dumps(ctx, ensure_ascii=False, indent=2)
    user_msg = VERIFY_HYPOTHESES_USER_TEMPLATE.format(
        hypotheses_json=hypotheses_json,
        context_json=context_json,
    )

    try:
        agent = verify_hypotheses_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_msg)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"VerifyHypotheses agent failed for conflict '{conflict.theme}': {exc}",
                related_step=step_id,
            )
        )
        return [], issues

    if not raw_text:
        return [], issues

    try:
        parsed = parse_json(raw_text)
        if isinstance(parsed, list):
            # 兼容 agent 直接输出 list 的情况，避免因为包装层不一致损失验证结果。
            parsed = {"results": parsed}
        vlist = VerificationResultList.model_validate(parsed)
        return vlist.results, issues
    except Exception as exc:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="parse_error",
                message=f"VerificationResultList parse failed for conflict '{conflict.theme}': {exc}",
                related_step=step_id,
            )
        )
        return [], issues


async def verify_hypotheses(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Verify hypotheses from explore_conflicts in parallel."""

    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="verify_hypotheses",
        kind="verify_hypotheses",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )
    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    conflicts_result = coerce_conflicts_result(state.get("conflicts_result"))
    if not conflicts_result:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {**state, "steps": state.get("steps", []) + [completed_step]}

    all_hypotheses = [hypothesis for conflict in conflicts_result.conflicts for hypothesis in conflict.hypotheses]
    if not all_hypotheses:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {**state, "steps": state.get("steps", []) + [completed_step]}

    # 第二阶段验证不是重新发现 conflict，而是尝试把每个假设落到证据层：
    # verified/partial/rejected 的状态会直接影响 thesis 审查和最终 confidence。
    shared_context = build_verify_context(state, symbol)
    tasks = [
        _verify_single_conflict(conflict, shared_context, step.id, config)
        for conflict in conflicts_result.conflicts
        if conflict.hypotheses
    ]
    task_results: list[tuple[list[VerificationResultItem], list[Issue]]] = await asyncio.gather(*tasks)

    all_results: list[VerificationResultItem] = []
    for results, issues in task_results:
        all_results.extend(results)
        new_issues.extend(issues)

    result_by_hid = {result.hypothesis_id: result for result in all_results}
    for conflict in conflicts_result.conflicts:
        for hypothesis in conflict.hypotheses:
            if hypothesis.id in result_by_hid:
                # 回写 hypothesis.status，确保后续所有消费者只看 conflicts_result
                # 就能知道验证后的真实状态，而不必再额外 join results artifact。
                hypothesis.status = result_by_hid[hypothesis.id].status

    verified_ids = {hid for hid, result in result_by_hid.items() if result.status in ("verified", "partial")}
    rejected_ids = {hid for hid, result in result_by_hid.items() if result.status == "rejected"}

    verification_artifact = VerificationArtifact(
        symbol=symbol,
        results=all_results,
        verified_count=len(verified_ids),
        rejected_count=len(rejected_ids),
        unknown_count=len(all_hypotheses) - len(verified_ids) - len(rejected_ids),
    )

    new_artifacts.append(
        Artifact(
            id=_make_id("artifact"),
            type="verification_results",
            producer_step=step.id,
            value=verification_artifact.model_dump(mode="json"),
        )
    )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "issues": state.get("issues", []) + new_issues,
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "conflicts_result": conflicts_result,
        "verification_results": verification_artifact,
    }
