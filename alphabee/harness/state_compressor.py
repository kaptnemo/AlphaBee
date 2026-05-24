"""
HarnessState compression middleware.

Compresses the HarnessState payload *before* it is serialised into an LLM prompt.
The original HarnessState object is never mutated.

Two-stage pipeline:
  1. Rule-based  – deterministic, zero latency, always runs.
  2. LLM-based   – triggered only when the rule-compressed payload is still above
                   `llm_threshold_chars`.  Uses a lightweight model to summarise
                   individual artifact `raw_response` fields.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import SystemMessage

from alphabee.core import Artifact, Issue, IssueSeverity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default rule limits
# ---------------------------------------------------------------------------

# Max chars kept in each artifact's raw_response before LLM summarisation.
DEFAULT_MAX_RAW_RESPONSE_CHARS: int = 3_000

# Artifact retention: how many artifacts of each "tier" to keep.
# Priority-tier types are always kept (latest one each).
_PRIORITY_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"report", "evaluation_report", "plan", "critique", "review"}
)
# Data-tier: keep only the latest per type (supplement rounds replace earlier ones).
_DATA_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"fundamental_analysis", "market_analysis", "risk_analysis"}
)
# Remaining slots for any other artifact types.
DEFAULT_MAX_OTHER_ARTIFACTS: int = 3

# Issue caps per severity level (keeps the most severe ones).
DEFAULT_ISSUE_CAPS: dict[str, int] = {
    "critical": 5,
    "high": 5,
    "medium": 3,
    "low": 2,
    "info": 1,
}

# Max decisions forwarded to prompt.
DEFAULT_MAX_DECISIONS: int = 8

# Prompt payload size (JSON chars) above which LLM summarisation is triggered.
DEFAULT_LLM_THRESHOLD_CHARS: int = 20_000

# Target chars per artifact raw_response after LLM summarisation.
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
# Configuration dataclass
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
    # Set to False to disable LLM summarisation entirely (rule-only mode).
    enable_llm_summarization: bool = True


# ---------------------------------------------------------------------------
# Internal helpers – rule-based
# ---------------------------------------------------------------------------

def _truncate_str(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"…[truncated {len(s) - max_chars} chars]"


def _compress_artifact_value(value: Any, max_raw: int) -> Any:
    """Return a copy of the artifact value dict with raw_response truncated."""
    if not isinstance(value, dict):
        return value
    raw = value.get("raw_response", "")
    if isinstance(raw, str) and len(raw) > max_raw:
        return {**value, "raw_response": _truncate_str(raw, max_raw)}
    return value


def _rule_select_artifacts(
    artifacts: list[Artifact],
    max_raw: int,
    max_other: int,
) -> list[dict[str, Any]]:
    """
    Select and compress artifacts using priority rules, then serialise to dicts.
    Order: priority types (latest each) → data types (latest each) → others (newest first, capped).
    """
    # Index all artifacts by type for quick lookup.
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

    # Fill remaining slots with other types, newest first.
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


def _rule_select_issues(
    issues: list[Issue],
    caps: dict[str, int],
) -> list[dict[str, Any]]:
    """Keep highest-severity issues up to their per-level cap."""
    severity_order = ["critical", "high", "medium", "low", "info"]
    by_sev: dict[str, list[Issue]] = {s: [] for s in severity_order}
    for issue in issues:
        sev = issue.severity.value if isinstance(issue.severity, IssueSeverity) else str(issue.severity)
        by_sev.setdefault(sev, []).append(issue)

    selected: list[dict[str, Any]] = []
    for sev in severity_order:
        cap = caps.get(sev, 0)
        selected.extend(i.model_dump(mode="json") for i in by_sev.get(sev, [])[:cap])
    return selected


def _estimate_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _rule_compress(state: dict[str, Any], cfg: CompressorConfig) -> dict[str, Any]:
    """
    Pure rule-based compression.  Returns a *new* dict suitable for JSON serialisation;
    the original state dict is never touched.
    """
    artifacts_raw: list[Artifact] = state.get("artifacts", [])
    issues_raw: list[Issue] = state.get("issues", [])
    decisions_raw = state.get("decisions", [])
    steps_raw = state.get("steps", [])
    observations_raw = state.get("observations", [])
    run = state.get("run")

    run_payload = run.model_dump(mode="json") if hasattr(run, "model_dump") else run
    steps_payload = [s.model_dump(mode="json") if hasattr(s, "model_dump") else s for s in steps_raw]
    obs_payload = [o.model_dump(mode="json") if hasattr(o, "model_dump") else o for o in observations_raw]

    artifacts_payload = _rule_select_artifacts(artifacts_raw, cfg.max_raw_response_chars, cfg.max_other_artifacts)
    issues_payload = _rule_select_issues(issues_raw, cfg.issue_caps)

    # Cap decisions, keep the most recent ones.
    dec_list = list(decisions_raw)[-cfg.max_decisions:]
    dec_payload = [d.model_dump(mode="json") if hasattr(d, "model_dump") else d for d in dec_list]

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
# LLM-based summarisation
# ---------------------------------------------------------------------------

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
        logger.warning("state_compressor: llm summarisation failed for artifact %s: %s", art.get("id"), exc)
        summary = _truncate_str(raw, target_chars) + "…[llm summary failed]"

    new_value = {**value, "raw_response": summary, "_llm_summarized": True}
    return {**art, "value": new_value}


async def _llm_summarize_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    model: Any,
    target_chars: int,
) -> list[dict[str, Any]]:
    return list(await asyncio.gather(*[
        _llm_summarize_one(a, model=model, target_chars=target_chars)
        for a in artifacts
    ]))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HarnessStateCompressor:
    """
    Middleware that compresses a HarnessState dict into a compact JSON string
    suitable for injecting into an LLM prompt.

    The original state is *never* modified.

    Usage (inside an async context)::

        compressor = HarnessStateCompressor()
        compressed_json = await compressor.compress(state, model=my_model)
    """

    def __init__(self, config: CompressorConfig | None = None) -> None:
        self.config = config or CompressorConfig()

    async def compress(
        self,
        state: dict[str, Any],
        *,
        model: Any,
    ) -> str:
        """
        Two-stage compression.

        1. Rule-based: always runs, deterministic.
        2. LLM-based:  runs only if rule-compressed output exceeds `llm_threshold_chars`
                       AND `enable_llm_summarization` is True.

        Returns a JSON string ready to embed in a prompt.
        """
        cfg = self.config
        compressed = _rule_compress(state, cfg)

        if cfg.enable_llm_summarization and _estimate_chars(compressed) > cfg.llm_threshold_chars:
            logger.debug(
                "state_compressor: payload %d chars exceeds threshold %d, invoking LLM summarisation",
                _estimate_chars(compressed),
                cfg.llm_threshold_chars,
            )
            compressed["artifacts"] = await _llm_summarize_artifacts(
                compressed["artifacts"],
                model=model,
                target_chars=cfg.llm_summary_target_chars,
            )

        return json.dumps(compressed, ensure_ascii=False, indent=2)

    def compress_sync(self, state: dict[str, Any]) -> str:
        """
        Rule-only synchronous compression (no LLM summarisation).
        Useful for logging / debugging where async is not available.
        """
        return json.dumps(_rule_compress(state, self.config), ensure_ascii=False, indent=2)
