"""Orchestrator state — shared between collect, harness, supplement, and finalize nodes."""

from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import AnyMessage

from alphabee.core import Artifact, Decision, Issue, Observation, Run, Step


class OrchestratorState(TypedDict, total=False):
    """Top-level orchestrator state, compatible with HarnessRuntime input.
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
    # ── 节点间传递的中间数据 ─────────────────────────
    fact_values: dict       # 规范化数值事实，由 collect_raw_facts 填充
    financial_facts: object  # FinancialFacts | None，供 AnomalyEngine 使用
    market_facts: object     # MarketFacts | None，供 _build_company_context 使用
    derived_facts: dict      # 衍生事实，由 run_analysis_engines 填充
    signal_analysis: dict    # 信号分析结果，由 run_analysis_engines 填充
    anomaly_report: dict | None  # 异常报告，由 run_analysis_engines 填充
    conflicts_result: dict | None  # ConflictAnalysisResult.model_dump()，由 explore_conflicts 生成
    verification_results: list | None  # list[VerificationResultItem.model_dump()]，由 verify_hypotheses 生成
    # ── 由各节点生成的中间产物 ─────────────────────────