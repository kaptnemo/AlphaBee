"""Orchestrator state definitions for the active LangGraph pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages

from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.core import Artifact, Decision, Issue, Observation, Run, Step


def _append_items[T](left: list[T] | None, right: list[T] | None) -> list[T]:
    """Append incremental list updates while tolerating missing state."""
    return [*(left or []), *(right or [])]


def _merge_by_id[T](left: Sequence[T] | None, right: Sequence[T] | None) -> list[T]:
    """Merge entity lists by ``id`` so updates can replace prior entries in-place."""
    merged = list(left or [])
    if not right:
        return merged

    index_by_id = {
        item_id: index for index, item in enumerate(merged) if (item_id := getattr(item, "id", None)) is not None
    }
    for item in right:
        item_id = getattr(item, "id", None)
        if item_id is not None and item_id in index_by_id:
            merged[index_by_id[item_id]] = item
            continue
        if item_id is not None:
            index_by_id[item_id] = len(merged)
        merged.append(item)
    return merged


def _merge_fact_values(
    left: dict[str, float] | None,
    right: dict[str, float] | None,
) -> dict[str, float]:
    """Merge fact-value deltas, letting later nodes overwrite refreshed keys."""
    merged = dict(left or {})
    if right:
        merged.update(right)
    return merged


class OrchestratorState(TypedDict, total=False):
    """Top-level orchestrator state for the active LangGraph pipeline."""

    messages: Annotated[list[AnyMessage], add_messages]
    run: Run
    steps: Annotated[list[Step], _append_items]
    artifacts: Annotated[list[Artifact], _merge_by_id]
    observations: Annotated[list[Observation], _merge_by_id]
    decisions: Annotated[list[Decision], _merge_by_id]
    issues: Annotated[list[Issue], _merge_by_id]
    final_artifact_id: str | None
    evaluation_artifact_id: str | None
    supplement_round: int
    max_supplement_rounds: int
    report_review_round: int
    max_report_review_rounds: int
    report_rewrite_needed: bool
    report_rewrite_reason: str | None
    # ── 控制标志（由 main.py 注入）──────────────────
    enhance: bool  # 启用 LLM 增强层（跨信号模式 + 行业语境化）
    llm_review: bool  # 启用 LLM 审查层（定性证据充分性 / 一致性 / 语境适配）
    # ── 节点间传递的中间数据 ─────────────────────────
    fact_values: Annotated[dict[str, float], _merge_fact_values]  # 规范化数值事实，由 collect_raw_facts 填充
    financial_facts: FinancialFacts | None
    market_facts: MarketFacts | None
