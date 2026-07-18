from typing import Literal

from pydantic import BaseModel, Field

ThesisDimensionId = Literal[
    "financial_quality",
    "operational_stability",
    "earnings_quality",
    "competitive_moat",
    "valuation_fit",
    "capital_efficiency",
    "credit_risk",
    "growth_quality",
]


class VerificationItem(BaseModel):
    """验证项"""

    id: str
    hypothesis_id: str
    questions: list[str]  # 验证假设的具体问题或实验设计
    preferred_sources: list[str]  # financial_facts / market_facts / news / web_search / announcement
    acceptance_criteria: str  # 验证假设成立的标准或阈值
    priority: Literal["high", "medium", "low"]  # 验证优先级


class HypothesisItem(BaseModel):
    """假设项"""

    id: str
    conflict_id: str  # 所属冲突项 id
    explanation: str  # 假设解释
    predictions: list[str]  # 如果假设成立，应该观察到的现象或结果
    required_evidence: list[str]  # 支持假设成立的证据或数据
    score: float  # 假设的可信度评分 0~1
    status: Literal["pending", "verified", "partial", "rejected"] = "pending"
    supporting_claims: list[str] = Field(default_factory=list)  # 支持它的 artifact/observation id
    refuting_claims: list[str] = Field(default_factory=list)  # 反对它的 id
    verification_items: list[VerificationItem] = Field(default_factory=list)


class VerificationResultItem(BaseModel):
    """验证结果"""

    id: str
    hypothesis_id: str
    status: Literal["verified", "partial", "rejected", "unknown"]
    support_score: float
    contradiction_score: float
    confidence: float
    supporting_evidence: list[str] = Field(default_factory=list)
    refuting_evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    summary: str


class VerificationResultList(BaseModel):
    """verify_hypotheses 节点的整体输出"""

    results: list[VerificationResultItem]


class ConflictItem(BaseModel):
    """冲突项"""

    id: str
    theme: str  # 冲突主题，如"盈利增长但现金流恶化"
    description: str  # 一句话描述
    related_dimensions: list[ThesisDimensionId]
    supporting_claims: list[str] = Field(default_factory=list)
    contradicting_claims: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high", "critical"]
    confidence: float  # 0~1
    status: Literal["open", "resolved", "rejected"] = "open"
    hypotheses: list[HypothesisItem] = Field(default_factory=list)


class ConflictAnalysisResult(BaseModel):
    """冲突分析结果"""

    conflicts: list[ConflictItem]


class ReportSections(BaseModel):
    executive_summary: str
    key_metrics: str
    signal_analysis: str
    anomaly_detection: str
    conflict_analysis: str
    investment_thesis: str
    review_findings: str
    risks: str
    disclaimer: str


class ReportOutput(BaseModel):
    title: str
    sections: ReportSections
    summary: str
    risk_count: dict[str, int] = Field(default_factory=dict)
    overall_confidence: Literal["high", "medium", "low", "unknown"]
    disclosed_issue_ids: list[str]
