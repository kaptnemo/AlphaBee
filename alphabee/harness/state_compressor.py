"""
Role-aware context slicing for HarnessState.

Three-tier content model
------------------------
  Tier 1  full_content   – only the node that *produces* a piece of content
                           receives it in full (e.g. reporter gets the full plan).
  Tier 2  summary/meta   – most nodes get truncated summaries and metadata.
  Tier 3  provenance     – review/evaluate nodes receive a claim→evidence map
                           instead of raw artifact dumps.

Per-node slicing
----------------
  planner  : run + output_spec + artifact_index(meta) + data_summaries
             + failure_signals[conditional: critique_summary, reusable_ids]
  reporter : run + output_spec + plan(full) + data_summaries
             + decision_summary + issues + rewrite_reason
             + prior_report[conditional: if rewriting]
  critic   : run + output_spec + report(full) + plan_summary
             + decision_summary + issues + claim_evidence_map
  evaluator: run + output_spec + final_report(full) + critique(full)
             + issues + evidence_map

Two-stage pipeline
------------------
  1. Rule-based  – deterministic, zero-latency, always runs; selects and
                   truncates per-node context.
  2. LLM-based   – triggered when rule output still exceeds
                   ``llm_threshold_chars``; summarises artifact raw_response
                   fields only.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain_core.messages import SystemMessage

from alphabee.core import Artifact, Decision, Issue, IssueSeverity, Observation, Run

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_RAW_RESPONSE_CHARS: int = 3_000
DEFAULT_MAX_PLAN_SUMMARY_CHARS: int = 500
DEFAULT_MAX_CLAIM_CHARS: int = 300
DEFAULT_MAX_SNIPPET_CHARS: int = 200
DEFAULT_MAX_DECISION_RATIONALE_CHARS: int = 300

_PRIORITY_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"report", "evaluation_report", "plan", "critique", "review"}
)
_DATA_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"fundamental_analysis", "market_analysis", "risk_analysis"}
)

DEFAULT_MAX_OTHER_ARTIFACTS: int = 3
DEFAULT_ISSUE_CAPS: dict[str, int] = {
    "critical": 5,
    "high": 5,
    "medium": 3,
    "low": 2,
    "info": 1,
}
DEFAULT_MAX_DECISIONS: int = 8
DEFAULT_LLM_THRESHOLD_CHARS: int = 20_000
DEFAULT_LLM_SUMMARY_TARGET_CHARS: int = 600

_COMPRESSOR_SYSTEM_PROMPT = """你是一个金融分析摘要助手。将以下结构化分析内容压缩为简洁摘要。

必须保留：
1. 所有数字、比率、百分比等关键量化指标（如 ROE、PE、净利润、营收增速等）
2. 核心结论与信号（机会 / 风险 / 背离）
3. 重要的异常、警告与数据缺口
4. 数据来源标识（如"FundamentalAgent"）

输出要求：
- 纯文本，不超过 {max_chars} 字符
- 不加 Markdown 格式
- 使用原文语言（中文/英文）
"""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CompressorConfig:
    """Tunable parameters for HarnessStateCompressor."""

    max_raw_response_chars: int = DEFAULT_MAX_RAW_RESPONSE_CHARS
    max_other_artifacts: int = DEFAULT_MAX_OTHER_ARTIFACTS
    issue_caps: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_ISSUE_CAPS))
    max_decisions: int = DEFAULT_MAX_DECISIONS
    llm_threshold_chars: int = DEFAULT_LLM_THRESHOLD_CHARS
    llm_summary_target_chars: int = DEFAULT_LLM_SUMMARY_TARGET_CHARS
    enable_llm_summarization: bool = True
    max_plan_summary_chars: int = DEFAULT_MAX_PLAN_SUMMARY_CHARS
    max_claim_chars: int = DEFAULT_MAX_CLAIM_CHARS
    max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS
    max_decision_rationale_chars: int = DEFAULT_MAX_DECISION_RATIONALE_CHARS


class NodeKind(StrEnum):
    PLANNER = "plan"
    REPORTER = "report"
    CRITIC = "critic"
    EVALUATOR = "evaluate"


# ---------------------------------------------------------------------------
# Low-level utilities
# ---------------------------------------------------------------------------


def _truncate_str(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"…[truncated {len(s) - max_chars} chars]"


def _compress_artifact_value(value: Any, max_raw: int) -> Any:
    """Return a copy of an artifact value dict with raw_response truncated."""
    if not isinstance(value, dict):
        return value
    raw = value.get("raw_response", "")
    if isinstance(raw, str) and len(raw) > max_raw:
        return {**value, "raw_response": _truncate_str(raw, max_raw)}
    return value


def _estimate_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Three-tier artifact serialisers
# ---------------------------------------------------------------------------


def _artifact_meta_only(art: Artifact) -> dict[str, Any]:
    """Tier 3 (lightest): id, type, producer_step, schema_version — no value."""
    return {
        "id": art.id,
        "type": art.type,
        "producer_step": art.producer_step,
        "schema_version": art.schema_version,
        "path": art.path,
    }


def _artifact_with_summary(art: Artifact, max_raw: int) -> dict[str, Any]:
    """Tier 2: full artifact with raw_response truncated."""
    d = art.model_dump(mode="json")
    d["value"] = _compress_artifact_value(art.value, max_raw)
    return d


def _artifact_full(art: Artifact) -> dict[str, Any]:
    """Tier 1 (full): no truncation."""
    return art.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Issue selection
# ---------------------------------------------------------------------------


def _rule_select_issues(
    issues: list[Issue],
    caps: dict[str, int],
) -> list[dict[str, Any]]:
    """Keep highest-severity issues up to their per-level cap."""
    severity_order = ["critical", "high", "medium", "low", "info"]
    by_sev: dict[str, list[Issue]] = {s: [] for s in severity_order}
    for issue in issues:
        sev = (
            issue.severity.value
            if isinstance(issue.severity, IssueSeverity)
            else str(issue.severity)
        )
        by_sev.setdefault(sev, []).append(issue)
    selected: list[dict[str, Any]] = []
    for sev in severity_order:
        cap = caps.get(sev, 0)
        selected.extend(i.model_dump(mode="json") for i in by_sev.get(sev, [])[:cap])
    return selected


# ---------------------------------------------------------------------------
# Cross-cutting helpers
# ---------------------------------------------------------------------------


def _build_decision_summary(
    decisions: list[Any],
    max_decisions: int,
    max_rationale_chars: int,
) -> list[dict[str, Any]]:
    """
    Compressed decision list: id, maker, rationale (truncated), confidence,
    based_on.  Keeps the most recent *max_decisions* entries.
    """
    result: list[dict[str, Any]] = []
    for d in list(decisions)[-max_decisions:]:
        dd = d.model_dump(mode="json") if hasattr(d, "model_dump") else dict(d)
        rationale = dd.get("rationale", "")
        if isinstance(rationale, str) and len(rationale) > max_rationale_chars:
            dd = {**dd, "rationale": _truncate_str(rationale, max_rationale_chars)}
        result.append(dd)
    return result


def _build_evidence_map(
    decisions: list[Any],
    artifacts: list[Artifact],
    observations: list[Observation],
    max_claim_chars: int,
    max_snippet_chars: int,
) -> list[dict[str, Any]]:
    """
    Build a claim → evidence map from decisions' *based_on* references.

    Each entry: { decision_id, claim, confidence, evidence: [{artifact_id|
    observation_id, type|source, snippet|payload_preview}] }

    Gives critic/evaluator fine-grained traceability without handing them
    the full raw_response of every artifact.
    """
    art_by_id: dict[str, Artifact] = {a.id: a for a in artifacts}
    obs_by_id: dict[str, Observation] = {o.id: o for o in observations}

    evidence_map: list[dict[str, Any]] = []
    for decision in decisions:
        if hasattr(decision, "based_on"):
            d_id = decision.id
            d_rationale = decision.rationale
            d_confidence = decision.confidence
            d_based_on: list[str] = decision.based_on
        else:
            d_id = decision.get("id", "")
            d_rationale = decision.get("rationale", "")
            d_confidence = decision.get("confidence", 0.0)
            d_based_on = decision.get("based_on", [])

        if not d_based_on:
            continue

        snippets: list[dict[str, Any]] = []
        for ref_id in d_based_on:
            if ref_id in art_by_id:
                art = art_by_id[ref_id]
                val = art.value
                entry: dict[str, Any] = {"artifact_id": ref_id, "type": art.type}
                if isinstance(val, dict):
                    raw = val.get("raw_response", "")
                    if isinstance(raw, str) and raw:
                        entry["snippet"] = _truncate_str(raw, max_snippet_chars)
                snippets.append(entry)
            elif ref_id in obs_by_id:
                obs = obs_by_id[ref_id]
                payload_str = (
                    json.dumps(obs.payload, ensure_ascii=False) if obs.payload else ""
                )
                snippets.append(
                    {
                        "observation_id": ref_id,
                        "source": obs.source,
                        "payload_preview": _truncate_str(payload_str, max_snippet_chars),
                    }
                )

        evidence_map.append(
            {
                "decision_id": d_id,
                "claim": _truncate_str(d_rationale, max_claim_chars),
                "confidence": d_confidence,
                "evidence": snippets,
            }
        )

    return evidence_map


def _extract_output_spec(run: Any) -> dict[str, Any]:
    """Extract output-format hints from run.context (if present)."""
    ctx: dict[str, Any] = {}
    if hasattr(run, "context") and isinstance(run.context, dict):
        ctx = run.context
    return {
        k: ctx[k]
        for k in (
            "audience",
            "language",
            "tone",
            "length",
            "required_sections",
            "citation_style",
            "format",
        )
        if k in ctx
    }


def _run_payload_minimal(run: Any) -> dict[str, Any]:
    """Return only run id, goal, status, and context (directive context)."""
    if hasattr(run, "model_dump"):
        d = run.model_dump(mode="json")
    else:
        d = dict(run) if run else {}
    return {k: d.get(k) for k in ("id", "goal", "status", "context") if k in d}


def _latest_per_type(
    artifacts: list[Artifact],
    types: frozenset[str],
) -> dict[str, Artifact]:
    """Return the most recent artifact for each type in *types*."""
    result: dict[str, Artifact] = {}
    for a in artifacts:
        if a.type in types:
            result[a.type] = a  # later overwrites = keeps latest
    return result


# ---------------------------------------------------------------------------
# Per-node context builders
# ---------------------------------------------------------------------------


def _build_planner_context(
    state: dict[str, Any],
    cfg: CompressorConfig,
) -> dict[str, Any]:
    """
    Planner receives:
      - run (full) + output_spec
      - artifact_index: lightweight metadata for all artifacts
      - data_artifact_summaries: latest data artifact per type (truncated)
      - issues (capped by severity)
      - previous_failure [conditional]: critique_summary + reusable artifact ids
        (only when rewrite_reason is set, i.e. a replanning scenario)

    Planner does NOT receive:
      - full report / critique text
      - raw data artifact responses in their entirety
      - reporter drafting details
    """
    artifacts_raw: list[Artifact] = state.get("artifacts", [])
    issues_raw: list[Issue] = state.get("issues", [])
    run = state.get("run")

    run_payload = run.model_dump(mode="json") if hasattr(run, "model_dump") else (run or {})
    output_spec = _extract_output_spec(run)

    artifact_index = [_artifact_meta_only(a) for a in artifacts_raw]

    data_arts = _latest_per_type(artifacts_raw, _DATA_ARTIFACT_TYPES)
    data_summaries = [
        _artifact_with_summary(a, cfg.max_raw_response_chars) for a in data_arts.values()
    ]

    issues_payload = _rule_select_issues(issues_raw, cfg.issue_caps)

    result: dict[str, Any] = {
        "run": run_payload,
        "output_spec": output_spec,
        "artifact_index": artifact_index,
        "data_artifact_summaries": data_summaries,
        "issues": issues_payload,
        "reporter_round": state.get("reporter_round", 0),
        "critic_round": state.get("critic_round", 0),
        "max_reporter_rounds": state.get("max_reporter_rounds", 3),
        "rewrite_reason": state.get("rewrite_reason"),
    }

    if state.get("rewrite_reason"):
        critique_arts = [a for a in artifacts_raw if a.type in {"critique", "review"}]
        critique_summary: str | None = None
        if critique_arts:
            val = critique_arts[-1].value
            if isinstance(val, dict):
                raw = val.get("raw_response", "")
                critique_summary = _truncate_str(
                    raw or json.dumps(val, ensure_ascii=False),
                    cfg.max_plan_summary_chars,
                )
            elif val is not None:
                critique_summary = _truncate_str(str(val), cfg.max_plan_summary_chars)

        result["previous_failure"] = {
            "critique_summary": critique_summary,
            "reusable_artifact_ids": [a.id for a in artifacts_raw if a.type in _DATA_ARTIFACT_TYPES],
        }

    return result


def _build_reporter_context(
    state: dict[str, Any],
    cfg: CompressorConfig,
) -> dict[str, Any]:
    """
    Reporter receives:
      - run goal + output_spec
      - plan (full – tier 1)
      - data_artifacts: latest per type (tier 2 – truncated)
      - decision_summary: compressed decision list (not full rationale)
      - issues + rewrite_reason
      - prior_report [conditional]: when rewriting, the previous report (tier 2)

    Reporter does NOT receive:
      - full step logs or tool traces
      - complete critique chain
      - unrelated raw_response from data artifacts
    """
    artifacts_raw: list[Artifact] = state.get("artifacts", [])
    issues_raw: list[Issue] = state.get("issues", [])
    decisions_raw: list[Any] = state.get("decisions", [])
    run = state.get("run")

    run_minimal = _run_payload_minimal(run)
    output_spec = _extract_output_spec(run)

    plan_arts = [a for a in artifacts_raw if a.type == "plan"]
    plan_payload = _artifact_full(plan_arts[-1]) if plan_arts else None

    data_arts = _latest_per_type(artifacts_raw, _DATA_ARTIFACT_TYPES)
    data_summaries = [
        _artifact_with_summary(a, cfg.max_raw_response_chars) for a in data_arts.values()
    ]

    decision_summary = _build_decision_summary(
        decisions_raw, cfg.max_decisions, cfg.max_decision_rationale_chars
    )
    issues_payload = _rule_select_issues(issues_raw, cfg.issue_caps)

    result: dict[str, Any] = {
        "run": run_minimal,
        "output_spec": output_spec,
        "plan": plan_payload,
        "data_artifacts": data_summaries,
        "decision_summary": decision_summary,
        "issues": issues_payload,
        "rewrite_reason": state.get("rewrite_reason"),
        "reporter_round": state.get("reporter_round", 0),
        "max_reporter_rounds": state.get("max_reporter_rounds", 3),
    }

    if state.get("rewrite_reason"):
        report_arts = [a for a in artifacts_raw if a.type == "report"]
        if report_arts:
            prior = report_arts[-1]
            prior_max = max(cfg.max_raw_response_chars, 1_500)
            result["prior_report"] = _artifact_with_summary(prior, prior_max)

    return result


def _build_critic_context(
    state: dict[str, Any],
    cfg: CompressorConfig,
) -> dict[str, Any]:
    """
    Critic receives:
      - run goal + output_spec
      - report (full – tier 1): the primary object under review
      - plan_summary (truncated – tier 2): enough to check alignment
      - decision_summary: compressed decision results
      - issues (capped)
      - claim_evidence_map (tier 3): claim → evidence snippets from decisions

    Critic does NOT receive:
      - full data artifact raw_response
      - planner / reporter step logs
    """
    artifacts_raw: list[Artifact] = state.get("artifacts", [])
    issues_raw: list[Issue] = state.get("issues", [])
    decisions_raw: list[Any] = state.get("decisions", [])
    observations_raw: list[Observation] = state.get("observations", [])
    run = state.get("run")

    run_minimal = _run_payload_minimal(run)
    output_spec = _extract_output_spec(run)

    report_arts = [a for a in artifacts_raw if a.type == "report"]
    report_payload = _artifact_full(report_arts[-1]) if report_arts else None

    plan_arts = [a for a in artifacts_raw if a.type == "plan"]
    plan_summary: str | None = None
    if plan_arts:
        val = plan_arts[-1].value
        if isinstance(val, dict):
            raw = val.get("raw_response", "")
            plan_summary = _truncate_str(
                raw or json.dumps(val, ensure_ascii=False),
                cfg.max_plan_summary_chars,
            )
        elif val is not None:
            plan_summary = _truncate_str(str(val), cfg.max_plan_summary_chars)

    decision_summary = _build_decision_summary(
        decisions_raw, cfg.max_decisions, cfg.max_decision_rationale_chars
    )
    evidence_map = _build_evidence_map(
        decisions_raw,
        artifacts_raw,
        observations_raw,
        cfg.max_claim_chars,
        cfg.max_snippet_chars,
    )
    issues_payload = _rule_select_issues(issues_raw, cfg.issue_caps)

    return {
        "run": run_minimal,
        "output_spec": output_spec,
        "plan_summary": plan_summary,
        "report": report_payload,
        "issues": issues_payload,
        "decision_summary": decision_summary,
        "claim_evidence_map": evidence_map,
        "critic_round": state.get("critic_round", 0),
        "reporter_round": state.get("reporter_round", 0),
        "rewrite_reason": state.get("rewrite_reason"),
    }


def _build_evaluator_context(
    state: dict[str, Any],
    cfg: CompressorConfig,
) -> dict[str, Any]:
    """
    Evaluator receives:
      - run goal + output_spec
      - final_report (full – tier 1)
      - critique_result (full – tier 1)
      - issues with lifecycle status (capped)
      - evidence_map (tier 3): for grounding / hallucination checks

    Evaluator does NOT receive:
      - full data artifact raw_response
      - planner steps or reporter drafting history
    """
    artifacts_raw: list[Artifact] = state.get("artifacts", [])
    issues_raw: list[Issue] = state.get("issues", [])
    decisions_raw: list[Any] = state.get("decisions", [])
    observations_raw: list[Observation] = state.get("observations", [])
    run = state.get("run")

    run_minimal = _run_payload_minimal(run)
    output_spec = _extract_output_spec(run)

    report_arts = [a for a in artifacts_raw if a.type == "report"]
    final_report = _artifact_full(report_arts[-1]) if report_arts else None

    critique_arts = [a for a in artifacts_raw if a.type in {"critique", "review"}]
    latest_critique = _artifact_full(critique_arts[-1]) if critique_arts else None

    evidence_map = _build_evidence_map(
        decisions_raw,
        artifacts_raw,
        observations_raw,
        cfg.max_claim_chars,
        cfg.max_snippet_chars,
    )
    issues_payload = _rule_select_issues(issues_raw, cfg.issue_caps)

    return {
        "run": run_minimal,
        "output_spec": output_spec,
        "final_report": final_report,
        "critique_result": latest_critique,
        "issues": issues_payload,
        "evidence_map": evidence_map,
        "final_artifact_id": state.get("final_artifact_id"),
        "evaluation_artifact_id": state.get("evaluation_artifact_id"),
        "reporter_round": state.get("reporter_round", 0),
        "critic_round": state.get("critic_round", 0),
    }


# ---------------------------------------------------------------------------
# Legacy fallback (used when node_kind is None)
# ---------------------------------------------------------------------------


def _rule_select_artifacts_legacy(
    artifacts: list[Artifact],
    max_raw: int,
    max_other: int,
) -> list[dict[str, Any]]:
    by_type: dict[str, list[Artifact]] = {}
    for a in artifacts:
        by_type.setdefault(a.type, []).append(a)

    selected_ids: set[str] = set()
    selected: list[Artifact] = []

    def _add_latest(atype: str) -> None:
        if atype in by_type and by_type[atype]:
            art = by_type[atype][-1]
            if art.id not in selected_ids:
                selected.append(art)
                selected_ids.add(art.id)

    for atype in sorted(_PRIORITY_ARTIFACT_TYPES):
        _add_latest(atype)
    for atype in sorted(_DATA_ARTIFACT_TYPES):
        _add_latest(atype)

    remaining = max_other
    for art in reversed(artifacts):
        if remaining <= 0:
            break
        if art.id not in selected_ids:
            selected.append(art)
            selected_ids.add(art.id)
            remaining -= 1

    result: list[dict[str, Any]] = []
    for art in selected:
        d = art.model_dump(mode="json")
        d["value"] = _compress_artifact_value(art.value, max_raw)
        result.append(d)
    return result


def _rule_compress_legacy(state: dict[str, Any], cfg: CompressorConfig) -> dict[str, Any]:
    """Backward-compatible compression when no node_kind is supplied."""
    artifacts_raw: list[Artifact] = state.get("artifacts", [])
    issues_raw: list[Issue] = state.get("issues", [])
    decisions_raw = state.get("decisions", [])
    steps_raw = state.get("steps", [])
    observations_raw = state.get("observations", [])
    run = state.get("run")

    run_payload = run.model_dump(mode="json") if hasattr(run, "model_dump") else run
    steps_payload = [
        s.model_dump(mode="json") if hasattr(s, "model_dump") else s for s in steps_raw
    ]
    obs_payload = [
        o.model_dump(mode="json") if hasattr(o, "model_dump") else o for o in observations_raw
    ]
    artifacts_payload = _rule_select_artifacts_legacy(
        artifacts_raw, cfg.max_raw_response_chars, cfg.max_other_artifacts
    )
    issues_payload = _rule_select_issues(issues_raw, cfg.issue_caps)
    dec_list = list(decisions_raw)[-cfg.max_decisions:]
    dec_payload = [
        d.model_dump(mode="json") if hasattr(d, "model_dump") else d for d in dec_list
    ]

    return {
        "run": run_payload,
        "steps": steps_payload,
        "artifacts": artifacts_payload,
        "observations": obs_payload,
        "decisions": dec_payload,
        "issues": issues_payload,
        "final_artifact_id": state.get("final_artifact_id"),
        "evaluation_artifact_id": state.get("evaluation_artifact_id"),
        "reporter_round": state.get("reporter_round", 0),
        "critic_round": state.get("critic_round", 0),
        "max_reporter_rounds": state.get("max_reporter_rounds", 3),
        "latest_step_output": state.get("latest_step_output"),
        "rewrite_reason": state.get("rewrite_reason"),
    }


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------


def _rule_compress(
    state: dict[str, Any],
    cfg: CompressorConfig,
    node_kind: NodeKind | None = None,
) -> dict[str, Any]:
    """Dispatch to the per-node context builder or the legacy fallback."""
    if node_kind == NodeKind.PLANNER:
        return _build_planner_context(state, cfg)
    if node_kind == NodeKind.REPORTER:
        return _build_reporter_context(state, cfg)
    if node_kind == NodeKind.CRITIC:
        return _build_critic_context(state, cfg)
    if node_kind == NodeKind.EVALUATOR:
        return _build_evaluator_context(state, cfg)
    return _rule_compress_legacy(state, cfg)


# ---------------------------------------------------------------------------
# LLM-based summarisation
# ---------------------------------------------------------------------------


async def _identity(item: Any) -> Any:
    return item


async def _llm_summarize_one(
    art: dict[str, Any],
    *,
    model: Any,
    target_chars: int,
) -> dict[str, Any]:
    """Summarise the raw_response of a single artifact dict using the LLM."""
    value = art.get("value", {})
    if not isinstance(value, dict):
        return art
    raw = value.get("raw_response", "")
    if not isinstance(raw, str) or len(raw) <= target_chars:
        return art

    system = _COMPRESSOR_SYSTEM_PROMPT.format(max_chars=target_chars)
    messages = [SystemMessage(content=system), {"role": "user", "content": raw}]
    try:
        response = await model.ainvoke(messages)
        summary = response.content.strip() if hasattr(response, "content") else str(response)
    except Exception as exc:
        logger.warning(
            "state_compressor: llm summarisation failed for artifact %s: %s",
            art.get("id"),
            exc,
        )
        summary = _truncate_str(raw, target_chars) + "…[llm summary failed]"

    new_value = {**value, "raw_response": summary, "_llm_summarized": True}
    return {**art, "value": new_value}


async def _llm_summarize_context_artifacts(
    compressed: dict[str, Any],
    *,
    model: Any,
    target_chars: int,
) -> dict[str, Any]:
    """
    Run LLM summarisation on all artifact values found anywhere in the
    compressed node context.  Handles both list fields and single-artifact
    fields so the caller does not need to know the per-node key layout.
    """
    result = dict(compressed)

    for key in ("artifacts", "data_artifacts", "data_artifact_summaries"):
        val = result.get(key)
        if not isinstance(val, list):
            continue
        tasks = [
            _llm_summarize_one(item, model=model, target_chars=target_chars)
            if isinstance(item, dict) and "value" in item
            else _identity(item)
            for item in val
        ]
        result[key] = list(await asyncio.gather(*tasks))

    for key in (
        "plan",
        "report",
        "final_report",
        "critique_result",
        "prior_report",
    ):
        val = result.get(key)
        if isinstance(val, dict) and "value" in val:
            result[key] = await _llm_summarize_one(val, model=model, target_chars=target_chars)

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class HarnessStateCompressor:
    """
    Role-aware middleware that compresses a HarnessState dict into a compact
    JSON string for LLM prompt injection.

    Each node kind receives only the context tier it needs:

      - planner:   artifact index + data summaries (+ failure signals if replanning)
      - reporter:  plan + data artifacts + decision summary
      - critic:    report + claim_evidence_map + decision summary
      - evaluator: report + critique + issue lifecycle + evidence map

    The original state is *never* modified.

    Usage (inside an async context)::

        compressor = HarnessStateCompressor()
        compressed_json = await compressor.compress(
            state, model=my_model, node_kind=NodeKind.REPORTER
        )
    """

    def __init__(self, config: CompressorConfig | None = None) -> None:
        self.config = config or CompressorConfig()

    async def compress(
        self,
        state: dict[str, Any],
        *,
        model: Any,
        node_kind: NodeKind | None = None,
    ) -> str:
        """
        Two-stage role-aware compression.

        1. Rule-based: per-node context selection (deterministic, zero latency).
        2. LLM-based:  triggered only when rule output still exceeds
                       ``llm_threshold_chars`` AND ``enable_llm_summarization``
                       is True.

        Returns a JSON string ready to embed in a prompt.
        """
        cfg = self.config
        compressed = _rule_compress(state, cfg, node_kind)

        if cfg.enable_llm_summarization and _estimate_chars(compressed) > cfg.llm_threshold_chars:
            logger.debug(
                "state_compressor: payload %d chars exceeds threshold %d,"
                " invoking LLM summarisation (node=%s)",
                _estimate_chars(compressed),
                cfg.llm_threshold_chars,
                node_kind,
            )
            compressed = await _llm_summarize_context_artifacts(
                compressed,
                model=model,
                target_chars=cfg.llm_summary_target_chars,
            )

        return json.dumps(compressed, ensure_ascii=False, indent=2)

    def compress_sync(
        self,
        state: dict[str, Any],
        node_kind: NodeKind | None = None,
    ) -> str:
        """
        Rule-only synchronous compression (no LLM summarisation).
        Useful for logging / debugging.
        """
        return json.dumps(
            _rule_compress(state, self.config, node_kind), ensure_ascii=False, indent=2
        )
