"""Typed contracts for active orchestrator artifacts and payload builders."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from alphabee.agents.schemas import (
    ConflictAnalysisResult,
    ConflictItem,
    ReportOutput,
    VerificationResultItem,
)
from alphabee.core import Artifact
from alphabee.market_regime.models import MarketScore, RegimeSnapshot

# Phase 1 market-regime typed payloads are re-exported here so the orchestrator's
# artifact contract convention (`find_artifact_model` / coerce helpers) exposes
# them alongside the per-symbol contracts.
__all__ = [
    "MarketScore",
    "RegimeSnapshot",
]


class FactCollectionArtifact(BaseModel):
    agent: str
    query: str
    symbol: str | None = None
    raw_response: str = ""


class DerivedFactsArtifact(BaseModel):
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rule_count: int = 0


class SignalAnalysisArtifact(BaseModel):
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rule_count: int = 0


class AnomalyReportArtifact(BaseModel):
    symbol: str = ""
    period: str = ""
    anomaly_count: int = 0
    pattern_count: int = 0
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    pattern_matches: list[dict[str, Any]] = Field(default_factory=list)


class ConflictAnalysisArtifact(BaseModel):
    symbol: str | None = None
    raw_text: str = ""
    conflicts: list[ConflictItem] = Field(default_factory=list)
    conflict_count: int = 0
    hypothesis_count: int = 0
    parse_error: str | None = None


class VerificationArtifact(BaseModel):
    symbol: str | None = None
    results: list[VerificationResultItem] = Field(default_factory=list)
    verified_count: int = 0
    rejected_count: int = 0
    unknown_count: int = 0


class ThesisIndustryContext(BaseModel):
    industry: str = ""
    sub_industry: str = ""
    market_cap_category: str = ""
    lifecycle_stage: str = ""
    business_model_summary: str = ""


class VerifiedHypothesisSummary(BaseModel):
    id: str = ""
    explanation: str = ""
    status: str = ""


class ConflictSummary(BaseModel):
    theme: str = ""
    severity: str = ""
    description: str = ""
    related_dimensions: list[str] = Field(default_factory=list)


class ConflictDataSummary(BaseModel):
    conflict_count: int = 0
    hypothesis_count: int = 0
    verified_count: int = 0
    rejected_count: int = 0
    verified_hypotheses: list[VerifiedHypothesisSummary] = Field(default_factory=list)
    conflicts_summary: list[ConflictSummary] = Field(default_factory=list)
    verification_results: list[VerificationResultItem] = Field(default_factory=list)


class ThesisArtifact(BaseModel):
    thesis: dict[str, Any] = Field(default_factory=dict)
    enhanced: dict[str, Any] | None = None
    industry_context: ThesisIndustryContext = Field(default_factory=ThesisIndustryContext)
    anomaly_data: dict[str, Any] = Field(default_factory=dict)
    conflict_data: ConflictDataSummary = Field(default_factory=ConflictDataSummary)


class InsightArtifact(BaseModel):
    """Typed artifact wrapping InsightAgent output — the central opinion document."""

    core_view: str = ""
    central_tension: str = ""
    main_driver: str = ""
    supporting_evidence: list[dict[str, Any]] = Field(default_factory=list)
    counter_evidence: list[dict[str, Any]] = Field(default_factory=list)
    materiality_rank: list[dict[str, Any]] = Field(default_factory=list)
    cross_signal_patterns: list[dict[str, Any]] = Field(default_factory=list)
    business_model_context: str = ""
    base_case: str = ""
    bull_case: str = ""
    bear_case: str = ""
    what_would_change_my_mind: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    # ── 降级标记（ROADMAP 0.4，见 docs/INSIGHT_DEGRADATION_DESIGN.md）──
    # fallback_tier: 0=完整 1=宽松救援 2=确定性兜底 3=最小骨架
    degraded: bool = False
    fallback_tier: int = 0
    degradation_reason: str = ""


class IndustryContextArtifact(BaseModel):
    """行业上下文 artifact（industry-context-injection Phase 0 垂直切片）。

    只承载行业识别信息 + 数值基准 + 降级元数据（数值基准层，定性解释层归
    DOMAIN_CONTEXT_ROADMAP）。下游通过 find_artifact_model(...) 消费，
    数值基准同时注入 fact_values 供 derived facts / signals 规则引用。
    """

    schema_version: str = "1"
    industry: str = ""
    sub_industry: str = ""
    classification_standard: str = ""  # sw_l1 / sw_l2 / ths / custom
    sw_code: str | None = None
    as_of_date: str = ""
    generated_at: str = ""
    source_refs: list[str] = Field(default_factory=list)
    # 数值基准（canonical 键，None = 该基准不可得）
    benchmarks: dict[str, float | None] = Field(default_factory=dict)
    peer_count: int | None = None
    # 降级契约（与 InsightArtifact.degraded 同模式）
    degraded: bool = False
    degraded_reason: str = ""


class ReportArtifact(ReportOutput):
    """Typed final report artifact payload."""


class ReportCompanyPayload(BaseModel):
    symbol: str = ""
    query: str = ""
    raw_response: str = ""


class ReportMetricEntry(BaseModel):
    name: str
    value: float
    level: str = ""
    interpretation: str = ""


class ReportMetricsPayload(BaseModel):
    rule_count: int = 0
    top_metrics: list[ReportMetricEntry] = Field(default_factory=list)


class ReportSignalEntry(BaseModel):
    signal_id: str
    level: str = "unknown"
    interpretation: str = ""
    thesis_impact: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class ReportSignalsPayload(BaseModel):
    rule_count: int = 0
    signals: list[ReportSignalEntry] = Field(default_factory=list)


class ReportAnomalyPayload(BaseModel):
    anomaly_count: int = 0
    pattern_count: int = 0
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    pattern_matches: list[dict[str, Any]] = Field(default_factory=list)


class ReportConflictHypothesisPayload(BaseModel):
    explanation: str = ""
    predictions: list[str] = Field(default_factory=list)
    verification_status: str = "pending"
    support_score: float | None = None
    contradiction_score: float | None = None
    confidence: float | None = None
    supporting_evidence: list[str] = Field(default_factory=list)
    refuting_evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    summary: str = ""


class ReportConflictItemPayload(BaseModel):
    theme: str = ""
    severity: str = ""
    description: str = ""
    confidence: float = 0.0
    related_dimensions: list[str] = Field(default_factory=list)
    hypotheses: list[ReportConflictHypothesisPayload] = Field(default_factory=list)


class ReportConflictAnalysisPayload(BaseModel):
    conflict_count: int = 0
    verified_count: int = 0
    rejected_count: int = 0
    conflicts: list[ReportConflictItemPayload] = Field(default_factory=list)


class ReportIssuePayload(BaseModel):
    id: str
    severity: str
    category: str
    message: str


class ReportEvidenceItem(BaseModel):
    """Evidence item from InsightAgent."""

    statement: str = ""
    source: str = ""
    weight: str = "moderate"


class ReportMaterialityRank(BaseModel):
    """Materiality rank item from InsightAgent."""

    variable: str = ""
    importance: str = ""
    reasoning: str = ""


class ReportInsightPayload(BaseModel):
    """InsightAgent output carried into the report-generation payload."""

    core_view: str = ""
    central_tension: str = ""
    main_driver: str = ""
    supporting_evidence: list[ReportEvidenceItem] = Field(default_factory=list)
    counter_evidence: list[ReportEvidenceItem] = Field(default_factory=list)
    materiality_rank: list[ReportMaterialityRank] = Field(default_factory=list)
    cross_signal_patterns: list[dict[str, Any]] = Field(default_factory=list)
    business_model_context: str = ""
    base_case: str = ""
    bull_case: str = ""
    bear_case: str = ""
    what_would_change_my_mind: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    degraded: bool = False  # 观点层是否降级产出（true 时允许报告走结构化摘要模式）


class ReportGenerationPayload(BaseModel):
    company: ReportCompanyPayload = Field(default_factory=ReportCompanyPayload)
    metrics: ReportMetricsPayload = Field(default_factory=ReportMetricsPayload)
    signals: ReportSignalsPayload = Field(default_factory=ReportSignalsPayload)
    thesis: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] | None = None
    anomaly: ReportAnomalyPayload = Field(default_factory=ReportAnomalyPayload)
    conflict_analysis: ReportConflictAnalysisPayload | None = None
    insight: ReportInsightPayload | None = None
    issues: list[ReportIssuePayload] = Field(default_factory=list)
    required_issue_disclosures: list[ReportIssuePayload] = Field(default_factory=list)


def find_artifact_model[ArtifactModelT: BaseModel](
    artifacts: list[Artifact] | list[dict[str, Any]],
    artifact_type: str,
    model_type: type[ArtifactModelT],
) -> ArtifactModelT | None:
    """Return the latest artifact payload validated as ``model_type``."""

    for artifact in reversed(artifacts):
        if isinstance(artifact, Artifact):
            if artifact.type != artifact_type:
                continue
            value = artifact.value
        else:
            if not isinstance(artifact, dict):
                continue
            if artifact.get("type") != artifact_type:
                continue
            value = artifact.get("value")

        if isinstance(value, dict):
            return model_type.model_validate(value)
    return None


def coerce_derived_facts(value: Any) -> DerivedFactsArtifact | None:
    if value is None or isinstance(value, DerivedFactsArtifact):
        return value
    if isinstance(value, dict):
        return DerivedFactsArtifact.model_validate(value)
    return None


def coerce_signal_analysis(value: Any) -> SignalAnalysisArtifact | None:
    if value is None or isinstance(value, SignalAnalysisArtifact):
        return value
    if isinstance(value, dict):
        return SignalAnalysisArtifact.model_validate(value)
    return None


def coerce_anomaly_report(value: Any) -> AnomalyReportArtifact | None:
    if value is None or isinstance(value, AnomalyReportArtifact):
        return value
    if isinstance(value, dict):
        return AnomalyReportArtifact.model_validate(value)
    return None


def coerce_conflicts_result(value: Any) -> ConflictAnalysisResult | None:
    if value is None or isinstance(value, ConflictAnalysisResult):
        return value
    if isinstance(value, dict):
        return ConflictAnalysisResult.model_validate(value)
    return None


def coerce_verification_artifact(value: Any) -> VerificationArtifact | None:
    if value is None or isinstance(value, VerificationArtifact):
        return value
    if isinstance(value, dict):
        return VerificationArtifact.model_validate(value)
    if isinstance(value, list):
        results = [VerificationResultItem.model_validate(item) for item in value]
        verified_count = sum(1 for item in results if item.status in ("verified", "partial"))
        rejected_count = sum(1 for item in results if item.status == "rejected")
        return VerificationArtifact(
            results=results,
            verified_count=verified_count,
            rejected_count=rejected_count,
            unknown_count=len(results) - verified_count - rejected_count,
        )
    return None


def coerce_market_regime(value: Any) -> RegimeSnapshot | None:
    """Coerce a ``market_regime`` artifact value into the typed ``RegimeSnapshot``."""
    if value is None or isinstance(value, RegimeSnapshot):
        return value
    if isinstance(value, dict):
        return RegimeSnapshot.model_validate(value)
    return None


def coerce_market_regime_history(value: Any) -> list[RegimeSnapshot] | None:
    """Coerce a ``market_regime_history`` artifact value into a list of snapshots."""
    if value is None:
        return None
    if isinstance(value, list):
        return [RegimeSnapshot.model_validate(item) for item in value]
    if isinstance(value, dict) and isinstance(value.get("history"), list):
        return [RegimeSnapshot.model_validate(item) for item in value["history"]]
    return None
