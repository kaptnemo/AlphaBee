"""Hypothesis-verification node."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphabee.utils.prompts import json_instruction

if TYPE_CHECKING:
    from alphabee.orchestrator.contracts import ConflictItem, VerificationResultItem
    from alphabee.orchestrator.state import OrchestratorState

import asyncio
import json as _json

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ConflictAnalysisResult
from alphabee.core import Artifact, ArtifactType, Decision, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _extract_final_text, _finalize_step, _make_id
from alphabee.orchestrator.contracts import (
    VerificationArtifact,
    find_artifact_model,
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

    # 生成验证 agent 的输入 prompt，包含 hypotheses、context、输出格式约束。
    user_msg = VERIFY_HYPOTHESES_USER_TEMPLATE.format(
        hypotheses_json=hypotheses_json,
        context_json=context_json,
    ) + json_instruction(VerificationResultList)

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

    conflicts_result = find_artifact_model(
        state.get("artifacts", []), ArtifactType.CONFLICTS_RESULT, ConflictAnalysisResult
    )
    if not conflicts_result:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed_step]}

    all_hypotheses = [hypothesis for conflict in conflicts_result.conflicts for hypothesis in conflict.hypotheses]
    if not all_hypotheses:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed_step]}

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
            type=ArtifactType.VERIFICATION_RESULTS,
            producer_step=step.id,
            value=verification_artifact.model_dump(mode="json"),
        )
    )

    # ── 结算层（conflict 生命周期分层，ROADMAP 0.5）───────────────────
    # 探索阶段（explore_conflicts）产出的冲突全部是 provisional，不进入 issue；
    # 只有在这里“验证结算”之后，才允许冲突影响 thesis / review / gate：
    #
    # 1) 高严重度冲突只有在出现 verified / partial 假设时才升格为 issue，
    #    让 quality gate 要求报告显式披露（verified_conflict）；
    #    provisional / unknown 不产生 issue，避免“怀疑冒充事实”。
    # 2) rejected 假设沉淀为 decision（“已排除”记录），
    #    避免所有疑点都悬而未决，也让报告不再把已排除的怀疑写成风险。
    # 3) 把 settled 状态回写进 conflicts_result artifact（保持原 id，按
    #    _merge_by_id reducer 就地替换），下游只看 conflicts_result 即可拿到
    #    verified / partial / rejected / unknown 的显式状态。
    settled_issues: list[Issue] = []
    settled_decisions: list[Decision] = []
    for conflict in conflicts_result.conflicts:
        settled_hypotheses = [
            hypothesis
            for hypothesis in conflict.hypotheses
            if (result := result_by_hid.get(hypothesis.id)) and result.status in ("verified", "partial")
        ]
        if settled_hypotheses and conflict.severity in ("high", "critical"):
            first = settled_hypotheses[0]
            vr = result_by_hid[first.id]
            gap_hint = f" 缺口: {', '.join(vr.gaps[:3])}" if vr.gaps else ""
            settled_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH if conflict.severity == "high" else IssueSeverity.CRITICAL,
                    category="verified_conflict",
                    message=(f"[冲突已验证] {conflict.theme}: {first.explanation}. 结论: {vr.summary}" + gap_hint),
                    related_step=step.id,
                )
            )
        for hypothesis in conflict.hypotheses:
            result = result_by_hid.get(hypothesis.id)
            if result is not None and result.status == "rejected":
                settled_decisions.append(
                    Decision(
                        id=_make_id("decision"),
                        maker="conflict_verifier",
                        rationale=(
                            f"假设已排除: {conflict.theme} — {hypothesis.explanation}. 推翻理由: {result.summary}"
                        ),
                        confidence=result.contradiction_score or 0.7,
                    )
                )
    new_issues.extend(settled_issues)
    new_decisions = settled_decisions

    # 回写 settled 状态到 conflicts_result artifact（同 id 覆盖，reducer 就地替换）
    for artifact in state.get("artifacts", []):
        if artifact.type == ArtifactType.CONFLICTS_RESULT:
            new_artifacts.append(
                Artifact(
                    id=artifact.id,
                    type=artifact.type,
                    producer_step=step.id,
                    value=conflicts_result.model_dump(mode="json"),
                )
            )
            break

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed_step],
        "issues": new_issues,
        "artifacts": new_artifacts,
        "decisions": new_decisions,
    }
