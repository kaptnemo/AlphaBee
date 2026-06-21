"""Report generation node — single-LLM-call report from structured thesis + review."""

from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import create_chat_model
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

    payload: dict = {
        "company": {},
        "metrics": {},
        "signals": {},
        "thesis": {},
        "review": None,
        "issues": [],
    }

    # ── fact_collection → company context ──
    fact_val = _find_artifact(artifacts, "fact_collection")
    if fact_val:
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

    # ── issues ──
    payload["issues"] = [
        {
            "severity": i.severity.value,
            "category": i.category,
            "message": i.message,
        }
        for i in issues
    ]

    return payload


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
        inputs={"artifact_count": len(state.get("artifacts", []))},
        status=StepStatus.RUNNING,
    )

    payload = _build_report_payload(state)
    prompt_text = json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        model = create_chat_model("agent.report")
        response = model.invoke([
            SystemMessage(content=REPORT_GENERATOR_PROMPT),
            HumanMessage(
                content=(
                    "请基于以下结构化数据生成财报质量体检报告。\n\n"
                    f"```json\n{prompt_text}\n```"
                )
            ),
        ])
        raw_text = extract_text(response.content)
        try:
            report_value = parse_json(raw_text)
        except ValueError:
            # If JSON parsing fails, use raw text as the report
            report_value = {"raw_markdown": raw_text, "title": "财报质量体检报告"}
    except Exception as exc:
        report_value = {
            "title": "财报质量体检报告",
            "summary": f"报告生成失败: {exc}",
            "sections": {},
            "risk_count": {},
            "overall_confidence": "unknown",
        }

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
        "final_artifact_id": report_artifact.id,
    }
