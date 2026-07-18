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
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)


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


class ReportGenerationPayload(BaseModel):
    company: ReportCompanyPayload = Field(default_factory=ReportCompanyPayload)
    metrics: ReportMetricsPayload = Field(default_factory=ReportMetricsPayload)
    signals: ReportSignalsPayload = Field(default_factory=ReportSignalsPayload)
    thesis: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] | None = None
    anomaly: ReportAnomalyPayload = Field(default_factory=ReportAnomalyPayload)
    conflict_analysis: ReportConflictAnalysisPayload | None = None
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
