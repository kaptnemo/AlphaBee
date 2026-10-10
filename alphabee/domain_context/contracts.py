"""Domain Context 的公司驱动画像契约（ArtifactType.DRIVER_PROFILE）。

``DriverProfile`` 是 ContextRouter 输出的定型快照：命中的 playbook + 展开后的激活原语
（含完整内容）+ 主/次驱动变量 + 匹配理由 + 降级标记。下游（synthesize_insights / 报告层）
经 ``find_artifact_model`` 消费，不再重新取数、不再重新路由。

本模块只含 Pydantic 契约（无 alphabee 内部 import），避免与 loader/router 产生循环依赖。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ActivatedPrimitive(BaseModel):
    """一个已激活原语的快照（含完整内容，供报告注入直接消费）。"""

    id: str
    score: float = 1.0  # P0 统一 1.0；P2 引入 context score/ranking
    trend: str = "stable"
    description: str = ""
    key_variables: list[str] = Field(default_factory=list)
    priority_questions: list[str] = Field(default_factory=list)
    disconfirming_signals: list[str] = Field(default_factory=list)
    preferred_sources: list[str] = Field(default_factory=list)
    report_angles: list[str] = Field(default_factory=list)
    # 因果链与激活条件此前在快照展开时被丢弃，导致下游只能看到"看什么变量"而看不到
    # "为什么看"和"什么情况下适用"。补进快照让画像自洽（无需回查原语目录）。
    causal_paths: list[str] = Field(default_factory=list)
    when_to_activate: list[str] = Field(default_factory=list)


class DriverObservable(BaseModel):
    """一个驱动变量的可观测指标（研究/验证任务的取数落点）。"""

    name: str
    source: str = ""  # 数据来源（如 tushare:index_classify / 年报 / 行业协会）
    cadence: str = ""  # 观测频率（月度/季度/年度）


class DriverEvidence(BaseModel):
    """一条驱动假设的证据引用（可溯源：谁说的、在哪、原文片段）。"""

    kind: str = ""  # 证据类型（如 结构化事实 / 年报原文 / 行业数据）
    ref: str = ""  # 引用定位（artifact id / 报告章节 / URL）
    quote: str = ""


class DriverHypothesis(BaseModel):
    """一条标的特异的驱动假设（D1 研究层产出；D0 只声明契约、不生成）。

    与 playbook 的 ``primary_drivers``（框架级变量名）不同，本模型描述的是"这家公司的
    这个变量"：机制、在本公司的表现形式、可观测指标、证伪条件与证据。
    """

    variable: str
    role: str = "primary"  # primary / secondary / risk
    mechanism: str = ""  # 传导机制（如 猪价上行 → 售价抬升 → 头均利润修复）
    company_form: str = ""  # 该变量在本公司的具体表现（分部/产品线/口径）
    observables: list[DriverObservable] = Field(default_factory=list)
    falsifiers: list[str] = Field(default_factory=list)  # 证伪信号
    evidence: list[DriverEvidence] = Field(default_factory=list)
    confidence: float = 0.0
    matched_primitive: str = ""  # 命中的分析原语 id（与框架对齐）


class ResearchQuestion(BaseModel):
    """一条研究议程问题（决定报告要回答什么、优先看什么证据）。"""

    question: str
    why_matters: str = ""
    decisive_evidence: list[str] = Field(default_factory=list)  # 能区分多空的关键证据
    preferred_sources: list[str] = Field(default_factory=list)
    priority: str = "high"  # critical / high / medium
    status: str = "open"  # open / answered


class DriverProfile(BaseModel):
    """公司驱动画像（``ArtifactType.DRIVER_PROFILE``，role group DATA）。

    由 ``driver_profile.build_driver_profile`` 组装；字段与 ``RouterResult`` 同构并补充
    展开后的原语完整内容，使下游无需回查 primitives/playbooks。
    """

    schema_version: str = "2"
    symbol: str = ""
    generated_at: str = ""
    # 命中的组合框架 id。业务含义：这是「这家公司该用哪套分析框架」的最终裁决——
    # hog_cycle（看猪价/存栏/成本）、mining_services（看订单/CAPEX/项目）、
    # generic_fundamental（兜底，看通用财务）。下游报告据此决定分析主线。
    playbook: str = ""
    playbook_version: int = 1
    activated_primitives: list[ActivatedPrimitive] = Field(default_factory=list)
    # 主/次驱动变量（变量名，如「猪价」「能繁母猪」，用于报告主线；非 primitive id）。
    # 业务含义：playbook 只是「框架」，「驱动变量」才是报告真正要围绕的"题眼"——
    # 决定 central_tension / main_driver 写什么。
    primary_drivers: list[str] = Field(default_factory=list)
    secondary_drivers: list[str] = Field(default_factory=list)
    # 为什么命中这个框架（track_label_match / sub_industry_match / …）：
    # 可解释性来源，让"为什么给这家公司选了猪周期框架"可审计、可反驳。
    why_selected: list[str] = Field(default_factory=list)
    # fallback = 普通无命中（大多数公司），回退通用框架，非异常、不报警；
    # degraded = 输入缺失（INDUSTRY_CONTEXT/COMPANY_TRACK 全缺），是数据链路问题，必须留痕。
    # 两者分开，是为了让报告层能区分"真的没有专用框架"和"没拿到数据所以没匹配上"。
    fallback: bool = False
    degraded: bool = False
    degraded_reason: str = ""

    # ── v2（只增不减，全部带默认值，旧 artifact 反序列化不受影响）──────────
    # 画像来源：rule = 纯规则路由（D0 现状）；llm = 研究层生成；hybrid = 规则打底 + 研究补充。
    provenance: str = "rule"
    # 锚定强度（strong / weak / none）："命中/未命中"二元判据无法表达"靠什么命中的"，
    # 而锚太弱（仅 archetype / 仅宽 L1 行业）时应当让研究层复核。
    anchor_strength: str = ""
    # playbook 级框架知识（此前只存在于 YAML，未进画像快照，下游拿不到）。
    key_conflicts: list[str] = Field(default_factory=list)
    recommended_verification_order: list[str] = Field(default_factory=list)
    report_questions: list[str] = Field(default_factory=list)
    # D1 研究层产出（D0 只声明契约与默认值，不生成）：
    driver_hypotheses: list[DriverHypothesis] = Field(default_factory=list)
    research_agenda: list[ResearchQuestion] = Field(default_factory=list)
    novel_drivers: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    unverified_drivers: list[str] = Field(default_factory=list)
    research_confidence: float = 0.0
    # 研究元信息（预算/耗时/降级原因等自由结构）；类型取 dict[str, Any] 以满足 strict mypy。
    research_meta: dict[str, Any] = Field(default_factory=dict)
