from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ObservationFreshness(str, Enum):
    REALTIME = "realtime"
    RECENT = "recent"
    HISTORICAL = "historical"
    STALE = "stale"
    UNKNOWN = "unknown"


class ArtifactRoleGroup(str, Enum):
    """Coarse role classification used for per-node context slicing.

    DATA       – raw outputs from external tools / sub-agents (inputs to reporting)
    PLAN       – planning and task-structure artifacts
    NARRATIVE  – reporter-produced conclusions and final text
    REVIEW     – critic-produced feedback, checks, and annotations
    EVALUATION – evaluator-produced assessment and scoring
    OTHER      – anything that does not fit the above categories
    """
    DATA = "data"
    PLAN = "plan"
    NARRATIVE = "narrative"
    REVIEW = "review"
    EVALUATION = "evaluation"
    OTHER = "other"


# Canonical mapping from the free-form ``Artifact.type`` string to a role group.
# Used by the auto-inference validator so callers rarely need to set role_group
# explicitly.
_ARTIFACT_TYPE_TO_ROLE_GROUP: dict[str, ArtifactRoleGroup] = {
    "fundamental_analysis": ArtifactRoleGroup.DATA,
    "market_analysis": ArtifactRoleGroup.DATA,
    "risk_analysis": ArtifactRoleGroup.DATA,
    "plan": ArtifactRoleGroup.PLAN,
    "report": ArtifactRoleGroup.NARRATIVE,
    "summary": ArtifactRoleGroup.NARRATIVE,
    "conclusion": ArtifactRoleGroup.NARRATIVE,
    "critique": ArtifactRoleGroup.REVIEW,
    "review": ArtifactRoleGroup.REVIEW,
    "thesis_review": ArtifactRoleGroup.REVIEW,
    "evaluation_report": ArtifactRoleGroup.EVALUATION,
}


class IssueSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IssueStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    IGNORED = "ignored"
    DEFERRED = "deferred"


class IssueScope(str, Enum):
    """Which pipeline stage produced this issue.

    Used by the compressor to surface relevant issues to each node without
    handing every node the full issue list.
    """
    PLANNING = "planning"
    DATA = "data"
    REPORT = "report"
    REVIEW = "review"
    EVALUATION = "evaluation"


class Run(BaseModel):
    """A complete execution unit for one user goal or autonomous task."""

    id: str = Field(..., description="Unique identifier for the run.")
    goal: str = Field(..., description="The top-level task objective.")
    status: RunStatus = Field(
        default=RunStatus.PENDING,
        description="Current lifecycle status of the run.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Stable execution context such as user intent, symbol, or runtime settings.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="When the run started.",
    )
    ended_at: datetime | None = Field(
        default=None,
        description="When the run ended.",
    )


class Step(BaseModel):
    """A single executable unit inside a run."""

    id: str = Field(..., description="Unique identifier for the step.")
    kind: str = Field(
        ...,
        description="Step type such as plan, fetch_data, analyze, verify, or report.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured inputs consumed by the step.",
    )
    status: StepStatus = Field(
        default=StepStatus.PENDING,
        description="Current lifecycle status of the step.",
    )
    retries: int = Field(
        default=0,
        ge=0,
        description="How many times this step has been retried.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Step IDs that must complete before this step can run.",
    )
    outputs: list[str] = Field(
        default_factory=list,
        description="Artifact IDs produced by this step.",
    )


class Artifact(BaseModel):
    """A durable output produced by a step."""

    id: str = Field(..., description="Unique identifier for the artifact.")
    type: str = Field(
        ...,
        description="Artifact type such as table, report, snapshot, conclusion, or chart.",
    )
    path: str | None = Field(
        default=None,
        description="Optional filesystem path when the artifact is stored on disk.",
    )
    value: Any | None = Field(
        default=None,
        description="Inline value when the artifact is stored in memory.",
    )
    producer_step: str = Field(
        ...,
        description="The step ID that produced this artifact.",
    )
    schema_version: str = Field(
        default="1.0",
        description="Schema version for artifact payload compatibility.",
    )
    role_group: ArtifactRoleGroup = Field(
        default=ArtifactRoleGroup.OTHER,
        description=(
            "Role group for context slicing: data / plan / narrative / review / evaluation. "
            "Auto-inferred from ``type`` when not explicitly set."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _infer_role_group(cls, data: Any) -> Any:
        """Set role_group from type when the caller did not provide it."""
        if isinstance(data, dict) and not data.get("role_group"):
            artifact_type = data.get("type", "")
            inferred = _ARTIFACT_TYPE_TO_ROLE_GROUP.get(artifact_type, ArtifactRoleGroup.OTHER)
            return {**data, "role_group": inferred}
        return data


class Observation(BaseModel):
    """A structured external fact collected from tools or data providers."""

    id: str = Field(..., description="Unique identifier for the observation.")
    source: str = Field(
        ...,
        description="Source of the observation, such as tushare, akshare, web_search, or manual_input.",
    )
    timestamp: datetime = Field(
        ...,
        description="When the observation was captured or published.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw or normalized fact payload.",
    )
    freshness: ObservationFreshness = Field(
        default=ObservationFreshness.UNKNOWN,
        description="How fresh the observation is relative to the task context.",
    )


class EvidenceRef(BaseModel):
    """A typed reference from a Decision to supporting evidence."""

    ref_id: str = Field(..., description="ID of the referenced artifact, observation, or decision.")
    ref_type: Literal["artifact", "observation", "decision"] = Field(
        ...,
        description="Kind of entity being referenced.",
    )


class Decision(BaseModel):
    """An intermediate conclusion produced by a rule or an agent."""

    id: str = Field(..., description="Unique identifier for the decision.")
    maker: str = Field(
        ...,
        description="Rule name or agent name that produced the decision.",
    )
    rationale: str = Field(
        ...,
        description="Human-readable reasoning for the decision.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score between 0 and 1.",
    )
    based_on: list[str] = Field(
        default_factory=list,
        description="Referenced observation, artifact, or decision IDs used as evidence.",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        description=(
            "Typed evidence references. When populated, supersedes ``based_on`` "
            "for evidence-map building so the compressor does not need to guess ref_type."
        ),
    )

    def resolved_evidence(
        self,
        artifact_ids: set[str],
        observation_ids: set[str],
    ) -> list[EvidenceRef]:
        """Return typed EvidenceRef list.

        Uses ``evidence_refs`` if already populated; otherwise infers ``ref_type``
        from ``based_on`` by looking up against the caller-supplied id sets.
        """
        if self.evidence_refs:
            return self.evidence_refs
        result: list[EvidenceRef] = []
        for ref_id in self.based_on:
            if ref_id in artifact_ids:
                ref_type: Literal["artifact", "observation", "decision"] = "artifact"
            elif ref_id in observation_ids:
                ref_type = "observation"
            else:
                ref_type = "decision"
            result.append(EvidenceRef(ref_id=ref_id, ref_type=ref_type))
        return result


class Issue(BaseModel):
    """A tracked problem, gap, conflict, or verification need."""

    id: str = Field(..., description="Unique identifier for the issue.")
    severity: IssueSeverity = Field(
        ...,
        description="Business severity of the issue.",
    )
    category: str = Field(
        ...,
        description="Issue category such as missing_data, conflict, failure, or verification_needed.",
    )
    message: str = Field(
        ...,
        description="Human-readable issue summary.",
    )
    related_step: str | None = Field(
        default=None,
        description="Related step ID when the issue is tied to one step.",
    )
    related_artifact: str | None = Field(
        default=None,
        description="Related artifact ID when the issue is tied to one artifact.",
    )
    status: IssueStatus = Field(
        default=IssueStatus.OPEN,
        description="Lifecycle status of the issue: open, resolved, ignored, or deferred.",
    )
    owner_node: str | None = Field(
        default=None,
        description="The node responsible for resolving this issue (e.g. reporter, critic).",
    )
    resolution_evidence: str | None = Field(
        default=None,
        description="Artifact ID or description that demonstrates resolution of this issue.",
    )
    scope: IssueScope = Field(
        default=IssueScope.REPORT,
        description="Pipeline stage that produced this issue (planning/data/report/review/evaluation).",
    )



class EvaluateMetrics(BaseModel):
    # 定量评估指标
    #  - schema_validity：最终输出是否符合预期 schema
    #  - artifact_coverage：是否覆盖应有模块，如 summary / opportunities / risks / divergences / issues
    #  - evidence_coverage：关键结论里有多少带 based_on
    #  - numeric_consistency：数字是否自洽，是否和 artifacts/observations 冲突
    #  - issue_handling：发现的数据缺口有没有在结果里显式体现
    #  - cross_source_consistency：基本面/行情/风险结论是否互相打架
    #  - freshness_score：是否标明数据时效，是否混用旧数据和新数据
    #  - grounding_score：最终结论有多少是可追溯到 artifact / observation 的
    schema_validity: bool = Field(
        ...,
        description="Whether the final output conforms to the expected schema.",
    )
    artifact_coverage: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Proportion of expected modules (e.g., summary, opportunities, risks, divergences, issues) that are covered in the output.",
    )
    evidence_coverage: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Proportion of key conclusions that have based_on references to artifacts or observations.",
    )
    numeric_consistency: bool = Field(
        ...,
        description="Whether all numeric values in the output are self-consistent and do not conflict with referenced artifacts or observations.",
    )
    issue_handling: bool = Field(
        ...,
        description="Whether identified data gaps or issues are explicitly reflected in the output.",
    )
    cross_source_consistency: bool = Field(
        ...,
        description="Whether conclusions drawn from different sources (fundamental, market, risk) are consistent with each other.",
    )
    freshness_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="A score indicating how well the output distinguishes between fresh and stale data, and whether it appropriately uses up-to-date information.",
    )
    grounding_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="A score indicating the extent to which final conclusions can be traced back to specific artifacts or observations.",
    )

    # 定性评估指标
    #  - 结论是否清晰
    #  - 是否真正做了交叉分析，而不是重复三个子代理原话
    #  - 是否区分事实 / 推断 / 不确定性
    #  - 风险提示是否充分
    #  - 是否存在过度自信
    #  - 是否对用户真正有用

    conclusion_clarity: str = Field(
        ...,
        description="Qualitative assessment of how clear and actionable the conclusions are.",
    )
    cross_analysis_depth: str = Field(
        ...,
        description="Qualitative assessment of the depth of cross-analysis performed, beyond simply restating subagent outputs.",
    )
    fact_inference_distinction: str = Field(
        ...,
        description="Qualitative assessment of how well the output distinguishes between observed facts, inferred conclusions, and areas of uncertainty.",
    )
    risk_warning_sufficiency: str = Field(
        ...,
        description="Qualitative assessment of whether risk warnings are sufficiently highlighted and explained.",
    )
    overconfidence_presence: str = Field(
        ...,
        description="Qualitative assessment of whether the output exhibits signs of overconfidence, such as making strong assertions without sufficient evidence.",
    )
    user_usefulness: str = Field(
        ...,
        description="Qualitative assessment of whether the output is genuinely useful and actionable for the user.",
    )


class EvaluationAssessment(BaseModel):
    """Qualitative evaluator output produced by the evaluator agent."""

    summary: str = Field(..., description="Overall summary of the evaluation.")
    strengths: list[str] = Field(default_factory=list, description="Main strengths of the result.")
    weaknesses: list[str] = Field(default_factory=list, description="Main weaknesses of the result.")
    blocking_issues: list[str] = Field(
        default_factory=list,
        description="Problems severe enough to block or weaken release confidence.",
    )
    passed: bool = Field(..., description="Whether the result passes the evaluation bar.")
    recommendation: str = Field(..., description="Concise evaluator recommendation.")
    improvement_actions: list[str] = Field(
        default_factory=list,
        description="Concrete actions to improve the result.",
    )


class EvaluationReport(BaseModel):
    """Combined quantitative and qualitative evaluation result."""

    metrics: EvaluateMetrics = Field(..., description="Deterministic and qualitative evaluation metrics.")
    summary: str = Field(..., description="Overall evaluation summary.")
    strengths: list[str] = Field(default_factory=list, description="Main strengths of the result.")
    weaknesses: list[str] = Field(default_factory=list, description="Main weaknesses of the result.")
    blocking_issues: list[str] = Field(
        default_factory=list,
        description="Problems severe enough to block or weaken release confidence.",
    )
    passed: bool = Field(..., description="Whether the result passes the evaluation bar.")
    recommendation: str = Field(..., description="Concise evaluator recommendation.")
    improvement_actions: list[str] = Field(
        default_factory=list,
        description="Concrete actions to improve the result.",
    )


class AlphaBeeState(BaseModel):
    user_id: str | None = None
    user_query: str

    intent: str | None = None
    symbol: str | None = None
    market: str = "A_SHARE"

    market_data: dict[str, Any] = Field(default_factory=dict)
    news_data: list[dict[str, Any]] = Field(default_factory=list)
    fundamental_analysis: dict[str, Any] = Field(default_factory=dict)
    technical_analysis: dict[str, Any] = Field(default_factory=dict)
    drisk_analysis: dict[str, Any] = Field(default_factory=dict)
    strategy_result: dict[str, Any] = Field(default_factory=dict)

    run: Run | None = Field(default=None, description="Current harness run record.")
    steps: list[Step] = Field(default_factory=list, description="Execution steps in the current run.")
    artifacts: list[Artifact] = Field(default_factory=list, description="Artifacts accumulated during execution.")
    observations: list[Observation] = Field(default_factory=list, description="Observed external facts.")
    decisions: list[Decision] = Field(default_factory=list, description="Intermediate decisions.")
    issues: list[Issue] = Field(default_factory=list, description="Tracked execution or analysis issues.")

    errors: list[str] = Field(default_factory=list)
    final_answer: str | None = None
