"""InsightAgent output models — structured investment viewpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    """One piece of supporting or counter evidence with source traceability."""

    statement: str = Field(description="The evidence statement")
    source: str = Field(description="Source of evidence, e.g. 'signal:receivable_quality', 'anomaly:ar_turnover_spike'")
    weight: Literal["strong", "moderate", "weak"] = Field(default="moderate", description="Evidentiary weight")


class MaterialityRank(BaseModel):
    """A judgment variable ranked by importance to the investment conclusion."""

    variable: str = Field(description="The variable name, e.g. '应收账款质量'")
    importance: Literal["critical", "high", "medium"] = Field(
        description="How critical this variable is to the conclusion"
    )
    reasoning: str = Field(description="Why this variable matters")


class InsightOutput(BaseModel):
    """Structured output from the InsightAgent — the central opinion document.

    This is the core artifact that downstream thesis and report nodes consume
    as their narrative backbone. Every field is designed to be falsifiable:
    ``what_would_change_my_mind`` explicitly states what evidence would reverse
    the conclusion.
    """

    core_view: str = Field(description="One-sentence core investment viewpoint, in buy-side analyst style")
    central_tension: str = Field(
        description="The single most important contradiction driving the analysis, "
        "e.g. '市场仍按高成长定价，但财务数据显示增长质量下降'"
    )
    main_driver: str = Field(description="The core variable that determines the conclusion")
    supporting_evidence: list[EvidenceItem] = Field(
        default_factory=list, description="Evidence supporting the core view"
    )
    counter_evidence: list[EvidenceItem] = Field(
        default_factory=list, description="Evidence contradicting or qualifying the core view"
    )
    materiality_rank: list[MaterialityRank] = Field(
        default_factory=list, description="Key variables ranked by importance (top 3-5)"
    )
    business_model_context: str = Field(
        default="", description="How the business model shapes interpretation of the data"
    )
    base_case: str = Field(default="", description="Base-case scenario narrative")
    bull_case: str = Field(default="", description="Bull-case scenario narrative — what must go right")
    bear_case: str = Field(default="", description="Bear-case scenario narrative — what could go wrong")
    what_would_change_my_mind: list[str] = Field(
        default_factory=list,
        description="Falsification conditions — evidence that would reverse the conclusion",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium", description="Overall confidence in the core view"
    )
