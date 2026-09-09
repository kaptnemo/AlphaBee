"""AlphaBee 中期（midterm）决策层契约包。

本包落地 ROADMAP 7.5 / 7.6 的 typed contracts：七因子快照（FactorSnapshot）、
赔率/期望值（ExpectedValue）、S0–S5 认知状态机（CompanyStateArtifact）以及
仓位 / 组合 / 日志等决策层数据结构。

数据单向约束（alphabee-schema-steward + alphabee-pipeline-contract-steward）：
外部数据源字段 → adapter/mapping → canonical 字段 → FactorSnapshot / VariableScores
→ CompanyStateArtifact → PositionDecision / PortfolioAllocation → CompanyStateDiff
（五层分层差分，D1 已替换薄壳 SnapshotDiff）。
"""

# factors.py 采用函数内惰性导入数据源，四引擎与编排入口均为确定性纯函数，
# 顶层只依赖 models（Pydantic），因此在此顶层导入不会触发 tushare/akshare 初始化副作用。
from alphabee.midterm.bayes import ScenarioProbability, scenario_probability, update_confidence
from alphabee.midterm.classifier import ClassifierResult, classify_state
from alphabee.midterm.decision_model import evaluate, get_decision
from alphabee.midterm.factors import (
    build_crowding_factor,
    build_expectation_factor,
    build_fundamental_factor,
    build_market_factor,
    build_risk_factor,
    build_trend_factor,
    build_valuation_factor,
    get_factor_snapshot,
)
from alphabee.midterm.models import (
    ArtifactRef,
    AuditSnapshot,
    ChangeAttribution,
    CognitiveState,
    CompanyStateArtifact,
    CompanyStateDiff,
    ConfidenceDelta,
    Consistency,
    CrowdingFactor,
    DecisionJournalEntry,
    EVDiff,
    EvidenceEvent,
    ExitCondition,
    ExpectationFactor,
    ExpectationGap,
    ExpectedValue,
    FactorDelta,
    FactorScoreDelta,
    FactorSnapshot,
    FieldChange,
    FieldDelta,
    FundamentalFactor,
    HoldingWeight,
    IndustryRiskSnapshot,
    MarketFactor,
    PortfolioAllocation,
    PositionDecision,
    PositionDiff,
    ResearchTask,
    RiskFactor,
    ScenarioOutcome,
    StateBelief,
    StateShift,
    StateTransition,
    ThesisVersion,
    TrendFactor,
    ValuationFactor,
    VariableScores,
)
from alphabee.midterm.position import build_position
from alphabee.midterm.score_engine import compress_scores

__all__ = [
    "ArtifactRef",
    "AuditSnapshot",
    "ChangeAttribution",
    "CognitiveState",
    "CompanyStateArtifact",
    "CompanyStateDiff",
    "ConfidenceDelta",
    "Consistency",
    "CrowdingFactor",
    "DecisionJournalEntry",
    "EVDiff",
    "EvidenceEvent",
    "ExitCondition",
    "ExpectationFactor",
    "ExpectationGap",
    "ExpectedValue",
    "FactorDelta",
    "FactorScoreDelta",
    "FactorSnapshot",
    "FieldChange",
    "FieldDelta",
    "FundamentalFactor",
    "HoldingWeight",
    "IndustryRiskSnapshot",
    "MarketFactor",
    "PortfolioAllocation",
    "PositionDecision",
    "PositionDiff",
    "ResearchTask",
    "RiskFactor",
    "ScenarioOutcome",
    "StateBelief",
    "StateShift",
    "StateTransition",
    "ThesisVersion",
    "TrendFactor",
    "ValuationFactor",
    "VariableScores",
    "build_crowding_factor",
    "build_expectation_factor",
    "build_fundamental_factor",
    "build_market_factor",
    "build_risk_factor",
    "build_trend_factor",
    "build_valuation_factor",
    "get_factor_snapshot",
    # 四引擎 + 编排入口
    "ClassifierResult",
    "ScenarioProbability",
    "build_position",
    "classify_state",
    "compress_scores",
    "evaluate",
    "get_decision",
    "scenario_probability",
    "update_confidence",
]
