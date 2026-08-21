from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    # 生命周期（ROADMAP 0.5 冲突生命周期分层）：
    #   * explore_conflicts 产出时为 pending（provisional）；
    #   * verify_hypotheses 结算后回写为 verified / partial / rejected / unknown
    #     （unknown = 证据未闭环，保持 provisional，与 pending 同等级对待）。
    # 注意：unknown 必须在这里显式列出来，否则验证结果回写后 conflicts_result
    # artifact 会在下游重新校验（如 generate_report）时抛 ValidationError。
    status: Literal["pending", "verified", "partial", "rejected", "unknown"] = "pending"
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
    """verify_hypotheses 节点的整体输出

    model_config.json_schema_extra.example 是 json_instruction() 的
    few-shot 示例来源。修改本模型的字段时，请同步更新 example 中的
    示例数据，确保示例与实际结构一致，避免 LLM 受过期示例误导。
    """

    # json_instruction() 从此处提取示例，注入到 verify_hypotheses agent 的
    # 系统 prompt 中，用于引导 LLM 输出正确格式的 JSON。
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "results": [
                    {
                        "id": "v1",
                        "hypothesis_id": "h1",
                        "status": "verified",
                        "support_score": 0.85,
                        "contradiction_score": 0.10,
                        "confidence": 0.80,
                        "supporting_evidence": ["应收账款周转天数连续3期上升：120→135→148天"],
                        "refuting_evidence": [],
                        "gaps": ["缺少同行业可比公司的周转天数数据"],
                        "summary": "应收账款恶化趋势被多期财务数据证实，假设基本成立",
                    }
                ]
            }
        }
    )

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
    """冲突分析结果

    model_config.json_schema_extra.example 是 json_instruction() 的
    few-shot 示例来源。修改本模型或其嵌套模型（ConflictItem、
    HypothesisItem、VerificationItem）的字段时，请同步更新 example
    中的对应示例数据。
    """

    # json_instruction() 从此处提取示例，注入到 explore_conflicts agent 的
    # 系统 prompt 中。示例包含完整的嵌套结构（conflict → hypothesis →
    # verification_item），确保 LLM 理解三层嵌套的正确输出格式。
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "conflicts": [
                    {
                        "id": "conflict_1",
                        "theme": "盈利改善但现金流恶化",
                        "description": "净利润同比+15%，但经营现金流同比-20%，应收账款周转天数上升",
                        "related_dimensions": ["earnings_quality", "financial_quality"],
                        "supporting_claims": ["net_profit_yoy=0.15", "operating_cashflow_yoy=-0.20"],
                        "contradicting_claims": [],
                        "severity": "high",
                        "confidence": 0.82,
                        "status": "open",
                        "hypotheses": [
                            {
                                "id": "h1",
                                "conflict_id": "conflict_1",
                                "explanation": "收入确认前置，回款滞后",
                                "predictions": [
                                    "应收账款周转天数连续上升",
                                    "经营现金流/净利润比值持续低于1",
                                    "合同负债下降或增速弱于收入",
                                ],
                                "required_evidence": ["financial_facts", "announcement"],
                                "score": 0.75,
                                "status": "pending",
                                "supporting_claims": [],
                                "refuting_claims": [],
                                "verification_items": [
                                    {
                                        "id": "v1",
                                        "hypothesis_id": "h1",
                                        "questions": [
                                            "近4期应收账款周转天数是否持续上升？",
                                            "经营现金流/净利润是否<1？",
                                        ],
                                        "preferred_sources": ["financial_facts"],
                                        "acceptance_criteria": "至少2条预测成立，且无强反证",
                                        "priority": "high",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    )

    conflicts: list[ConflictItem]


class ReportSections(BaseModel):
    executive_summary: str
    investment_viewpoint: str
    scenario_analysis: str
    key_metrics: str
    signal_analysis: str
    anomaly_detection: str
    conflict_analysis: str
    dimension_analysis: str
    company_track: str = ""  # 公司赛道/对标组对比（COMPANY_TRACK Phase F，可空）
    review_findings: str
    falsification_conditions: str
    risks: str
    disclaimer: str


class ReportOutput(BaseModel):
    # json_instruction() 从此处提取示例，注入到 reporter agent 的系统 prompt
    # 中。示例覆盖了所有 12 个 report sections，确保 LLM 理解报告的完整结构。
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "贵州茅台(600519) 财报质量体检报告",
                "sections": {
                    "executive_summary": "公司整体财务质量稳健，盈利能力突出，现金流充裕。需关注批价波动对渠道利润的挤压效应。",
                    "investment_viewpoint": "核心观点是品牌壁垒仍强，但需验证渠道价格压力是否会削弱盈利质量。",
                    "scenario_analysis": "基准情景为渠道利润温和承压但现金流保持稳健；乐观情景取决于批价企稳；悲观情景来自需求收缩。",
                    "key_metrics": "ROE 32%，经营现金流/净利润 1.05，应收账款周转天数 2 天。",
                    "signal_analysis": "盈利质量信号整体偏正面，现金流信号中性。",
                    "anomaly_detection": "未发现明显财务异常模式。",
                    "conflict_analysis": "批价下行与营收增长之间存在轻微背离。",
                    "dimension_analysis": "品牌护城河深厚，直销占比提升驱动吨价上行，但需持续审查渠道价格与现金流质量。",
                    "company_track": "公司真实赛道为高端白酒，对标组（五粮液/泸州老窖）ROE 中位数 25%，公司 32% 显著领先。",
                    "review_findings": "报告覆盖度完备，风险披露充分，无阻塞性问题。",
                    "falsification_conditions": "若批价持续下行并传导至现金流弱化，则当前核心观点需要下修。",
                    "risks": "宏观经济下行导致高端消费收缩；批价持续下滑压缩渠道利润。",
                    "disclaimer": "本报告基于公开数据自动生成，不构成投资建议。",
                },
                "summary": "财务质量优异，品牌壁垒深厚，中长期价值确定性强。",
                "risk_count": {"high": 1, "medium": 2, "low": 2},
                "overall_confidence": "high",
                "disclosed_issue_ids": ["issue_001"],
            }
        }
    )

    title: str
    sections: ReportSections
    summary: str
    risk_count: dict[str, int] = Field(default_factory=dict)
    overall_confidence: Literal["high", "medium", "low", "unknown"]
    disclosed_issue_ids: list[str]
