"""Domain Context 知识资产的 canonical schema（DOMAIN_CONTEXT_ROADMAP P0 第 1 步）。

Primitive（分析原语）：稳定、可跨行业复用的分析积木。
Playbook（组合框架）：命名的 primitive 集合 + 匹配/展示元数据（不是独立一级概念）。

两者都走 Pydantic 严格校验（``extra="forbid"``），保证 YAML 与 schema 不漂移——
这是 DOMAIN_CONTEXT_ROADMAP「Review 记录」问题 #2（primitive 无 canonical schema）的落地。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PrimitiveSchema(BaseModel):
    """一个分析原语（``domain_primitives/*.yaml``）。

    字段约定对齐 DOMAIN_CONTEXT_ROADMAP「扩展机制 #5 统一接口规范」：
    必填仅 ``id``；其余为可选（缺省空），新增原语时按此 schema 扩展，不各自发明字段。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    # 知识版本：分析框架会随产业演化（如面板"去周期化"、养殖"成本曲线右移"），
    # 版本号让框架是「当前可用的框架」而非「永远正确的知识」，过时可升级/废弃。
    version: int = 1
    # 一句话商业故事：这个原语在讲什么（供报告层转述，也让人读 YAML 即懂意图）。
    description: str = ""
    # 激活条件：什么样的公司/经营情景该用这套框架。这是「分析师选框架」的触发条件，
    # 与 ContextRouter 的 match_*（自动路由）互补——前者是语义，后者是机械匹配。
    when_to_activate: list[str] = Field(default_factory=list)
    # 关键变量：判断这个框架要看哪些指标。决定「研究/报告应该盯什么数据」，
    # 是驱动画像里"看什么"的核心，区别于 business_model 的"怎么解读财务口径"。
    key_variables: list[str] = Field(default_factory=list)
    # 因果链条：变量 → 中间环节 → 盈利结果的传导。解释"为什么这些变量重要"，
    # 是框架的推理骨架（如"能繁母猪去化 → 供给收缩 → 猪价上行"）。
    causal_paths: list[str] = Field(default_factory=list)
    # 优先验证的问题：报告要回答的核心问题，决定分析主线，避免套通用 ROE/PEG 模板。
    priority_questions: list[str] = Field(default_factory=list)
    # 证伪信号：什么证据出现会推翻这个框架。是「可证伪性」的来源，也反向约束
    # 报告不能把框架用成"永远成立"的解释（如周期框架下"价格与利润解耦"就是证伪信号）。
    disconfirming_signals: list[str] = Field(default_factory=list)
    # 优先证据来源：这些变量去哪查，决定研究任务（ResearchTask）的数据采集方向。
    preferred_sources: list[str] = Field(default_factory=list)
    # 报告切入点：给下游报告层的叙事角度（如"周期定位/盈利弹性"），是原语 → 报告的落点。
    report_angles: list[str] = Field(default_factory=list)
    # 框架失效判定条件：只存「静态条件」不存「当前值」（见扩展机制 #6）——
    # 例：新业务收入占比 > 30% 时 commodity_cycle 框架失效；这是"框架何时退役"的开关。
    obsolescence_triggers: list[str] = Field(default_factory=list)
    # 隐含前提（如"价格由供需决定""成本可比较"），显式化以便审查框架是否适用。
    assumptions: list[str] = Field(default_factory=list)
    # 适用期/废弃标记（与 CompanyTrackArtifact.stale_after 同族）：框架会过时，需要生命周期。
    valid_from: str = ""
    valid_to: str = ""
    deprecated_by: str = ""


class PlaybookSchema(BaseModel):
    """一个组合框架（``domain_playbooks/*.yaml``）——命名的 primitive 集合。

    ``primitives`` 只允许引用已声明的 primitive id（目录闭合约束，由
    ``loader.validate_closure`` 校验）；匹配字段供 ContextRouter（P0 第 3 步）消费。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    # 知识版本：同 PrimitiveSchema.version——框架会随产业演化升级。
    version: int = 1
    # 一句话商业故事：这个组合框架在讲什么（如"生猪养殖 = 猪价周期 + 生物资产 + 成本曲线"）。
    description: str = ""
    # 引用的 primitive id（目录闭合约束：必须已声明）。
    # 业务含义：playbook 是「命名的 primitive 集合」，不是新知识——组合决定"这家公司该看
    # 哪几套框架叠加"，展开后下游只看到 primitive 列表（避免 activated_contexts 混入两种单位）。
    primitives: list[str] = Field(default_factory=list)
    # 适用标的特征（供 ContextRouter 匹配）：一个框架"面向哪类公司"。
    # track_label（真实赛道）/ sub_industry（申万行业）/ business_model（archetype）三路信号，
    # 分别对应不同的可信度（见 context_router 的权重注释）。
    match_track_labels: list[str] = Field(default_factory=list)
    match_sub_industries: list[str] = Field(default_factory=list)
    match_business_models: list[str] = Field(default_factory=list)
    # 主/次驱动变量：报告主线的「题眼」（变量名，如"猪价""能繁母猪"），
    # 是 DriverProfile 下游写 main_driver/central_tension 的直接素材。
    primary_drivers: list[str] = Field(default_factory=list)
    secondary_drivers: list[str] = Field(default_factory=list)
    # 最重要的冲突模板：该框架下最常见的「多空分歧点」（如"盈利修复来自价格还是成本"），
    # 供冲突探索阶段按框架生成更有针对性的假设，而非通用冲突模板。
    key_conflicts: list[str] = Field(default_factory=list)
    # 推荐验证顺序：研究该框架时的证据优先级（先看什么最能区分多空），
    # 是 verify_hypotheses 按 context 动态切验证优先级的依据。
    recommended_verification_order: list[str] = Field(default_factory=list)
    # 报告应围绕哪些问题写：框架 → 报告的最终落点，决定"这家公司的报告像不像该行业的分析"。
    report_questions: list[str] = Field(default_factory=list)
    # 适用期/废弃标记（同 PrimitiveSchema）。
    valid_from: str = ""
    valid_to: str = ""
    deprecated_by: str = ""
    assumptions: list[str] = Field(default_factory=list)
