"""Orchestrator state — shared between collect, harness, supplement, and finalize nodes."""

from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import AnyMessage

from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.core import Artifact, Decision, Issue, Observation, Run, Step


class OrchestratorState(TypedDict, total=False):
    """Top-level orchestrator state for the active LangGraph pipeline."""

    messages: list[AnyMessage]
    run: Run
    steps: list[Step]
    artifacts: list[Artifact]
    observations: list[Observation]
    decisions: list[Decision]
    issues: list[Issue]
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
    fact_values: dict[str, float]  # 规范化数值事实，由 collect_raw_facts 填充
    financial_facts: FinancialFacts | None
    market_facts: MarketFacts | None
