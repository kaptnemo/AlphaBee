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
    version: int = 1
    description: str = ""
    # 激活条件：什么样的公司/情景命中此原语
    when_to_activate: list[str] = Field(default_factory=list)
    # 关键变量：判断此原语要看什么
    key_variables: list[str] = Field(default_factory=list)
    # 因果链条：典型传导路径
    causal_paths: list[str] = Field(default_factory=list)
    # 优先验证的问题
    priority_questions: list[str] = Field(default_factory=list)
    # 证伪信号
    disconfirming_signals: list[str] = Field(default_factory=list)
    # 优先证据来源
    preferred_sources: list[str] = Field(default_factory=list)
    # 报告切入点
    report_angles: list[str] = Field(default_factory=list)
    # 框架失效判定条件（静态，只存条件不存值，见扩展机制 #6）
    obsolescence_triggers: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    # 版本/适用期（与 CompanyTrackArtifact.stale_after 同族）
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
    version: int = 1
    description: str = ""
    # 引用的 primitive id（目录闭合约束：必须已声明）
    primitives: list[str] = Field(default_factory=list)
    # 适用标的特征（供 ContextRouter 匹配）
    match_track_labels: list[str] = Field(default_factory=list)
    match_sub_industries: list[str] = Field(default_factory=list)
    match_business_models: list[str] = Field(default_factory=list)
    # 主/次驱动变量（展示层用，可引用 primitive.key_variables）
    primary_drivers: list[str] = Field(default_factory=list)
    secondary_drivers: list[str] = Field(default_factory=list)
    # 最重要的冲突模板 + 推荐验证顺序 + 报告问题
    key_conflicts: list[str] = Field(default_factory=list)
    recommended_verification_order: list[str] = Field(default_factory=list)
    report_questions: list[str] = Field(default_factory=list)
    # 版本/适用期（与 CompanyTrackArtifact.stale_after 同族）
    valid_from: str = ""
    valid_to: str = ""
    deprecated_by: str = ""
    assumptions: list[str] = Field(default_factory=list)
