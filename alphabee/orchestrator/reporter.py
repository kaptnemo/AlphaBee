"""Report generation node — single-LLM-call report from structured thesis + review."""

from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ReportOutput
from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import create_chat_model, json_instruction
from alphabee.utils.pipeline import extract_text, make_id, parse_json


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _find_artifact(artifacts: list, artifact_type: str) -> dict | None:
    for a in reversed(artifacts):
        if a.type == artifact_type and isinstance(a.value, dict):
            return a.value
    return None


def _build_report_payload(state: OrchestratorState) -> dict:
    """Assemble all structured data into a single dict for the LLM."""
    artifacts = state.get("artifacts", [])
    issues = state.get("issues", [])
    run = state.get("run")

    # 这里的 payload 不是“让 LLM 再分析一次”，
    # 而是把整个管线已经产出的结构化结论压成一个稳定输入，
    # 让报告生成只做转述、编排和风险显式化。
    payload: dict = {
        "company": {},
        "metrics": {},
        "signals": {},
        "thesis": {},
        "review": None,
        "issues": [],
        "required_issue_disclosures": [],
    }

    # ── fact_collection → company context ──
    fact_val = _find_artifact(artifacts, "fact_collection")
    if fact_val:
        # raw_response 只保留截断摘要，作用是给报告提供最小必要的业务背景，
        # 避免过长 narrative 抢占 prompt，冲淡结构化结论。
        payload["company"] = {
            "symbol": fact_val.get("symbol", ""),
            "query": fact_val.get("query", ""),
            "raw_response": (fact_val.get("raw_response", "") or "")[:2000],
        }

    # ── derived_facts → key metrics ──
    derived_val = _find_artifact(artifacts, "derived_facts")
    if derived_val:
        results = derived_val.get("results", {})
        top_metrics: list[dict] = []
        for name, r in results.items():
            val = r.get(name)
            if val is not None:
                # 报告层只暴露最重要的衍生指标与解释，
                # 其目标是帮助用户理解“为什么会得出这些信号”，
                # 而不是把规则引擎的全部内部字段原样倾倒出来。
                top_metrics.append({
                    "name": name,
                    "value": round(float(val), 3),
                    "level": r.get("level", ""),
                    "interpretation": r.get("interpretation", ""),
                })
        payload["metrics"] = {
            "rule_count": derived_val.get("rule_count", 0),
            "top_metrics": top_metrics[:10],
        }

    # ── signal_analysis → risk signals ──
    signal_val = _find_artifact(artifacts, "signal_analysis")
    if signal_val:
        results = signal_val.get("results", {})
        signal_list: list[dict] = []
        for sig_id, r in results.items():
            signal_list.append({
                "signal_id": sig_id,
                "level": r.get("level", "unknown"),
                "interpretation": r.get("interpretation", ""),
                "thesis_impact": r.get("thesis_impact", {}),
                "error": r.get("error", ""),
            })
        # Sort: blocked first, then high → medium → low → none
        level_order = {"blocked": -2, "missing_fact": -1, "high": 3, "medium": 2, "low": 1, "none": 0}
        signal_list.sort(key=lambda s: level_order.get(s["level"], 0), reverse=True)
        payload["signals"] = {
            "rule_count": signal_val.get("rule_count", 0),
            "signals": signal_list,
        }

    # ── thesis_analysis → investment thesis ──
    thesis_val = _find_artifact(artifacts, "thesis_analysis")
    if thesis_val:
        # thesis 是最终“维度化观点层”，报告中的执行摘要、风险列表、
        # review findings 都会围绕它组织，因此这里直接作为主体载荷透传。
        payload["thesis"] = thesis_val.get("thesis", {})
        # If enhanced thesis exists, include cross-signal patterns
        enhanced = thesis_val.get("enhanced")
        if enhanced and enhanced.get("enhancement_applied"):
            payload["thesis"]["enhanced"] = {
                "cross_signal_patterns": enhanced.get("cross_signal_patterns", []),
                "context_notes": enhanced.get("context_notes", ""),
            }

    # ── thesis_review → review findings ──
    review_val = _find_artifact(artifacts, "thesis_review")
    if review_val:
        payload["review"] = review_val

    # ── anomaly_report → 勾稽关系异常 ──
    anomaly_val = _find_artifact(artifacts, "anomaly_report")
    if anomaly_val:
        payload["anomaly"] = {
            "anomaly_count": anomaly_val.get("anomaly_count", 0),
            "pattern_count": anomaly_val.get("pattern_count", 0),
            "anomalies": [
                a for a in anomaly_val.get("anomalies", [])
                if a.get("level") != "none"
            ],
            "pattern_matches": anomaly_val.get("pattern_matches", []),
        }
    else:
        payload["anomaly"] = {"anomaly_count": 0, "pattern_count": 0, "anomalies": [], "pattern_matches": []}

    # ── conflicts + verifications → full structured data ──
    conflicts_raw = state.get("conflicts_result")
    verification_results: list[dict] = state.get("verification_results") or []
    if conflicts_raw:
        verify_by_hid: dict[str, dict] = {}
        for vr in verification_results:
            hid = vr.get("hypothesis_id", "")
            if hid:
                verify_by_hid[hid] = vr

        enriched_conflicts: list[dict] = []
        for c in conflicts_raw.get("conflicts", []):
            enriched_hyps: list[dict] = []
            for h in c.get("hypotheses", []):
                hid = h.get("id", "")
                vr = verify_by_hid.get(hid, {})
                # 将 conflict 与 verification 合并，是为了让报告直接看到：
                # “一个疑点提出了什么假设、后来被什么证据支持/推翻、还剩哪些缺口”。
                enriched_hyps.append({
                    "explanation": h.get("explanation", ""),
                    "predictions": h.get("predictions", []),
                    "verification_status": vr.get("status", h.get("status", "pending")),
                    "support_score": vr.get("support_score"),
                    "contradiction_score": vr.get("contradiction_score"),
                    "confidence": vr.get("confidence"),
                    "supporting_evidence": vr.get("supporting_evidence", []),
                    "refuting_evidence": vr.get("refuting_evidence", []),
                    "gaps": vr.get("gaps", []),
                    "summary": vr.get("summary", ""),
                })

            enriched_conflicts.append({
                "theme": c.get("theme", ""),
                "severity": c.get("severity", ""),
                "description": c.get("description", ""),
                "confidence": c.get("confidence", 0),
                "related_dimensions": c.get("related_dimensions", []),
                "hypotheses": enriched_hyps,
            })

        verified_count = sum(
            1 for c in enriched_conflicts
            for h in c["hypotheses"]
            if h["verification_status"] in ("verified", "partial")
        )
        rejected_count = sum(
            1 for c in enriched_conflicts
            for h in c["hypotheses"]
            if h["verification_status"] == "rejected"
        )

        payload["conflict_analysis"] = {
            "conflict_count": len(enriched_conflicts),
            "verified_count": verified_count,
            "rejected_count": rejected_count,
            "conflicts": enriched_conflicts,
        }

    # ── issues ──
    payload["issues"] = [
        {
            "id": i.id,
            "severity": i.severity.value,
            "category": i.category,
            "message": i.message,
        }
        for i in issues
    ]
    payload["required_issue_disclosures"] = [
        item for item in payload["issues"]
        if item["severity"] in {"high", "critical"}
    ]

    return payload


def _fallback_report(summary: str) -> dict:
    return ReportOutput(
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
    state: OrchestratorState, config: RunnableConfig,
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

    payload = _build_report_payload(state)
    prompt_text = json.dumps(payload, ensure_ascii=False, indent=2)
    rewrite_reason = state.get("report_rewrite_reason")
    issues = list(state.get("issues", []))
    prior_report = None
    if rewrite_reason:
        # 质量 gate 触发重写时，会把上一版报告一并交给模型。
        # 这样重写动作更像“定向修补”而不是完全重新生成，能减少风格漂移。
        for artifact in reversed(state.get("artifacts", [])):
            if artifact.type == "report" and isinstance(artifact.value, dict):
                prior_report = artifact.value
                break

    try:
        model = create_chat_model("agent.report")
        response = model.invoke([
            SystemMessage(content=REPORT_GENERATOR_PROMPT),
            HumanMessage(
                content=(
                    json_instruction(ReportOutput)
                    + "\n\n"
                    +
                    (
                        "请基于以下结构化数据生成财报质量体检报告。\n\n"
                        if not rewrite_reason else
                        "这是一次基于质量 gate 的重写，请优先修复以下问题后再生成新报告：\n"
                        f"- {rewrite_reason}\n\n"
                        "请保持所有判断忠实于输入 JSON，不要新增分析，只修复结构覆盖、风险披露和冲突呈现。\n\n"
                    )
                    + (
                        f"上一版报告：\n```json\n{json.dumps(prior_report, ensure_ascii=False, indent=2)}\n```\n\n"
                        if rewrite_reason and prior_report else ""
                    )
                    + f"输入数据：\n```json\n{prompt_text}\n```"
                )
            ),
        ])
        raw_text = extract_text(response.content)
        try:
            report_value = ReportOutput.model_validate(parse_json(raw_text)).model_dump(mode="json")
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
        type="report",
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
