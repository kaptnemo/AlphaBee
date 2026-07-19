"""Report generation node — single-LLM-call report from structured thesis + review."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ReportOutput
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.contracts import ReportArtifact
from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT
from alphabee.orchestrator.services.payload_builders import (
    build_report_generation_payload,
)
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import create_chat_model, json_instruction
from alphabee.utils.pipeline import extract_text, make_id, parse_json


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _fallback_report(summary: str) -> dict:
    return ReportArtifact(
        title="财报质量体检报告",
        sections={
            "executive_summary": summary,
            "key_metrics": "",
            "signal_analysis": "",
            "anomaly_detection": "",
            "conflict_analysis": "",
            "investment_thesis": "",
            "review_findings": "",
            "risks": "",
            "disclaimer": "",
        },
        summary=summary,
        risk_count={},
        overall_confidence="unknown",
        disclosed_issue_ids=[],
    ).model_dump(mode="json")


async def generate_report(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Generate the final report from structured thesis, review, and data artifacts.

    Makes a single LLM call with all structured context, producing a
    template-driven Markdown report.
    """
    step = Step(
        id="generate_report",
        kind="generate_report",
        inputs={
            "artifact_count": len(state.get("artifacts", [])),
            "rewrite_reason": state.get("report_rewrite_reason"),
        },
        status=StepStatus.RUNNING,
    )

    payload = build_report_generation_payload(state)
    prompt_text = payload.model_dump_json(indent=2)
    rewrite_reason = state.get("report_rewrite_reason")
    issues = list(state.get("issues", []))
    prior_report = None
    if rewrite_reason:
        # 质量 gate 触发重写时，会把上一版报告一并交给模型。
        # 这样重写动作更像“定向修补”而不是完全重新生成，能减少风格漂移。
        for artifact in reversed(state.get("artifacts", [])):
            if artifact.type == ArtifactType.REPORT and isinstance(artifact.value, dict):
                prior_report = artifact.value
                break

    try:
        model = create_chat_model("agent.report")
        response = model.invoke(
            [
                SystemMessage(content=REPORT_GENERATOR_PROMPT),
                HumanMessage(
                    content=(
                        json_instruction(ReportOutput)
                        + "\n\n"
                        + (
                            "请基于以下结构化数据生成财报质量体检报告。\n\n"
                            if not rewrite_reason
                            else "这是一次基于质量 gate 的重写，请优先修复以下问题后再生成新报告：\n"
                            f"- {rewrite_reason}\n\n"
                            "请保持所有判断忠实于输入 JSON，不要新增分析，只修复结构覆盖、风险披露和冲突呈现。\n\n"
                        )
                        + (
                            f"上一版报告：\n```json\n{json.dumps(prior_report, ensure_ascii=False, indent=2)}\n```\n\n"
                            if rewrite_reason and prior_report
                            else ""
                        )
                        + f"输入数据：\n```json\n{prompt_text}\n```"
                    )
                ),
            ]
        )
        raw_text = extract_text(response.content)
        try:
            report_value = ReportArtifact.model_validate(parse_json(raw_text)).model_dump(mode="json")
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="parse_error",
                    message=f"ReportOutput parse failed: {exc}",
                    related_step=step.id,
                )
            )
            report_value = _fallback_report(
                f"报告生成结果不符合结构化 schema，已降级保存错误信息。原始输出：{raw_text[:500]}"
            )
    except Exception as exc:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"Report generation failed: {exc}",
                related_step=step.id,
            )
        )
        report_value = _fallback_report(f"报告生成失败: {exc}")

    report_artifact = Artifact(
        id=_make_id("artifact"),
        type=ArtifactType.REPORT,
        producer_step=step.id,
        value=report_value,
    )

    completed_step = step.model_copy(
        update={
            "status": StepStatus.SUCCEEDED,
            "outputs": [report_artifact.id],
        }
    )

    return {
        **state,
        "steps": [*state.get("steps", []), completed_step],
        "artifacts": [*state.get("artifacts", []), report_artifact],
        "issues": issues,
        "final_artifact_id": report_artifact.id,
        "report_rewrite_needed": False,
        "report_rewrite_reason": None,
    }
