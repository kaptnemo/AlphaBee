"""Orchestrator state — shared between collect, harness, supplement, and finalize nodes."""

from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import AnyMessage

from alphabee.core import Artifact, Decision, Issue, Observation, Run, Step


class OrchestratorState(TypedDict, total=False):
    """Top-level orchestrator state, compatible with HarnessRuntime input.

    Mirrors CrossAnalysisState from agents_legacy/cross/agent.py so the
    harness can consume it without changes.
    """

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
    # ── 控制标志（由 main.py 注入）──────────────────
    enhance: bool       # 启用 LLM 增强层（跨信号模式 + 行业语境化）
    llm_review: bool    # 启用 LLM 审查层（定性证据充分性 / 一致性 / 语境适配）
