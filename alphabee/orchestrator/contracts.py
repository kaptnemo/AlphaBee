"""Typed contracts for active orchestrator artifacts and payload builders."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from alphabee.agents.schemas import (
    ConflictAnalysisResult,
    ConflictItem,
    FalsificationCondition,
    ReportOutput,
    VerificationResultItem,
    coerce_falsification_conditions,
)
from alphabee.core import Artifact
from alphabee.domain_context.contracts import DriverProfile  # noqa: F401  (re-export)

# Phase 1 起，行业知识资产契约迁至行业包（工作流 + 存储 + 契约同源）：
# alphabee/industry/contracts.py 是唯一 schema 所有者（v2：三组基准字典 + 元数据），
# 此处再导出保持既有 import 兼容（resolve_industry_context / 测试 / 下游 find_artifact_model）。
from alphabee.industry.contracts import IndustryContextArtifact  # noqa: F401  (re-export)
from alphabee.market_regime.models import MarketScore, RegimeSnapshot
from alphabee.midterm.models import CompanyStateArtifact

# Phase 1 market-regime typed payloads are re-exported here so the orchestrator's
# artifact contract convention (`find_artifact_model` / coerce helpers) exposes
# them alongside the per-symbol contracts.
__all__ = [
    "Artifact",
    "CompanyStateArtifact",
    "ConflictAnalysisResult",
    "ConflictItem",
    "DriverProfile",
    "FalsificationCondition",
    "IndustryContextArtifact",
    "MarketScore",
    "RegimeSnapshot",
    "ReportOutput",
    "VerificationResultItem",
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
    business_model: str = ""  # brand / odm / component / integrator / other（Phase E）
    track_label: str = ""  # 真实赛道标签（COMPANY_TRACK Phase F）
    peer_group: list[str] = Field(default_factory=list)  # 对标组代码
    peer_benchmarks: dict[str, float | None] = Field(default_factory=dict)  # peer_* 摘要


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
    what_would_change_my_mind: list[FalsificationCondition] = Field(default_factory=list)
    confidence: str = "medium"
    # ── 降级标记（ROADMAP 0.4，见 docs/design/INSIGHT_DEGRADATION_DESIGN.md）──
    # fallback_tier: 0=完整 1=宽松救援 2=确定性兜底 3=最小骨架
    degraded: bool = False
    fallback_tier: int = 0
    degradation_reason: str = ""

    @field_validator("what_would_change_my_mind", mode="before")
    @classmethod
    def _coerce_falsification_conditions(cls, v: Any) -> list[FalsificationCondition]:
        # 兼容历史 artifact 里存的纯字符串列表：默认按证伪（disconfirm）处理。
        return coerce_falsification_conditions(v)


class MidtermDecisionSummaryArtifact(BaseModel):
    """中期决策的确定性可读总结（``midterm_decision_reporter`` 产物）。

    决策点 6(a) 的展示边界：该总结只进日志与 finalize payload 的 artifacts 列表，
    不进 ``generate_report`` 的报告 payload（研究归研究、决策归决策）。``text`` 是
    完整渲染结果（不截断），供终端/日志审计；结构化字段便于下游快速取用。
    """

    symbol: str = ""
    as_of_date: str = ""
    text: str = ""
    degraded: bool = False


#: 假设生命周期状态（§6.3）：``active`` 生效中 / ``invalidated`` 已被证伪 / ``confirmed`` 已被证实。
ASSUMPTION_STATUS_ACTIVE = "active"
ASSUMPTION_STATUS_INVALIDATED = "invalidated"
ASSUMPTION_STATUS_CONFIRMED = "confirmed"
ASSUMPTION_STATUSES: tuple[str, ...] = (
    ASSUMPTION_STATUS_ACTIVE,
    ASSUMPTION_STATUS_INVALIDATED,
    ASSUMPTION_STATUS_CONFIRMED,
)


class AssumptionEntry(BaseModel):
    """假设登记簿条目（§6.3）：把"研究前提"显式化，才有检查它何时失效的抓手。

    D4（状态偏离）的根因是"假设没被登记，就无从检查它失效"。本结构是最小落地：
    ``conflicts`` / ``verification`` 在产假设时登记（``active``），后续节点（尤其
    ``synthesize_insights`` / ``run_thesis``）消费前检查——已 ``invalidated`` 的假设
    不得再作为论证前提，除非显式引用反驳证据；报告 gate 另做 D3 检查。

    字段全部带默认值，且**只 append**：历史 JSON（无新字段）可直接校验通过。
    """

    id: str
    statement: str = ""  # 如"应收增长源于军工结算周期而非恶化"
    status: str = ASSUMPTION_STATUS_ACTIVE  # active | invalidated | confirmed
    source_artifact: str = ""  # 从哪个 artifact 提出
    invalidated_by: str = ""  # 被哪个 evidence/issue 证伪

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> Any:
        """把状态归一化到小写已知值；未知值一律保守回退 ``active``（不因脏数据误判失效）。"""
        if value is None:
            return ASSUMPTION_STATUS_ACTIVE
        normalized = str(value).strip().lower()
        return normalized if normalized in ASSUMPTION_STATUSES else ASSUMPTION_STATUS_ACTIVE


class AssumptionRegistryArtifact(BaseModel):
    """假设登记簿 artifact 载荷（``ArtifactType.ASSUMPTION_REGISTRY``）。"""

    entries: list[AssumptionEntry] = Field(default_factory=list)

    @property
    def invalidated(self) -> list[AssumptionEntry]:
        """已证伪的假设条目（只读视图；顺序保持登记顺序）。"""
        return [entry for entry in self.entries if entry.status == ASSUMPTION_STATUS_INVALIDATED]


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
    what_would_change_my_mind: list[FalsificationCondition] = Field(default_factory=list)
    confidence: str = "medium"
    degraded: bool = False  # 观点层是否降级产出（true 时允许报告走结构化摘要模式）

    @field_validator("what_would_change_my_mind", mode="before")
    @classmethod
    def _coerce_falsification_conditions(cls, v: Any) -> list[FalsificationCondition]:
        return coerce_falsification_conditions(v)


class ReportCompanyTrackPayload(BaseModel):
    """报告层公司赛道摘要（COMPANY_TRACK Phase F5）。"""

    track_label: str = ""
    business_model: str = ""
    dominant_segment: str = ""
    fastest_segment: str = ""
    peer_group: list[str] = Field(default_factory=list)
    peer_benchmarks: dict[str, float | None] = Field(default_factory=dict)
    as_of_date: str = ""
    stale: bool = False
    degraded: bool = False


class ReportGenerationPayload(BaseModel):
    company: ReportCompanyPayload = Field(default_factory=ReportCompanyPayload)
    metrics: ReportMetricsPayload = Field(default_factory=ReportMetricsPayload)
    signals: ReportSignalsPayload = Field(default_factory=ReportSignalsPayload)
    thesis: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] | None = None
    anomaly: ReportAnomalyPayload = Field(default_factory=ReportAnomalyPayload)
    conflict_analysis: ReportConflictAnalysisPayload | None = None
    insight: ReportInsightPayload | None = None
    company_track: ReportCompanyTrackPayload | None = None
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


def coerce_driver_profile(value: Any) -> DriverProfile | None:
    """Coerce a ``driver_profile`` artifact value into the typed ``DriverProfile``."""
    if value is None or isinstance(value, DriverProfile):
        return value
    if isinstance(value, dict):
        return DriverProfile.model_validate(value)
    return None


def coerce_midterm_decision(value: Any) -> CompanyStateArtifact | None:
    """Coerce a ``midterm_decision`` artifact value into the typed ``CompanyStateArtifact``.

    遵循 ``find_artifact_model`` 的语义：artifact 落库时 ``value`` 是
    ``CompanyStateArtifact.model_dump(mode="json")`` 的 dict；这里只做 typed 还原，
    供下游 finalize payload / recorder 消费（Phase 2/3 接线），避免 dead-end artifact。
    """
    if value is None or isinstance(value, CompanyStateArtifact):
        return value
    if isinstance(value, dict):
        return CompanyStateArtifact.model_validate(value)
    return None
