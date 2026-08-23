"""Domain Context 的公司驱动画像契约（ArtifactType.DRIVER_PROFILE）。

``DriverProfile`` 是 ContextRouter 输出的定型快照：命中的 playbook + 展开后的激活原语
（含完整内容）+ 主/次驱动变量 + 匹配理由 + 降级标记。下游（synthesize_insights / 报告层）
经 ``find_artifact_model`` 消费，不再重新取数、不再重新路由。

本模块只含 Pydantic 契约（无 alphabee 内部 import），避免与 loader/router 产生循环依赖。
"""

from __future__ import annotations

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


class DriverProfile(BaseModel):
    """公司驱动画像（``ArtifactType.DRIVER_PROFILE``，role group DATA）。

    由 ``driver_profile.build_driver_profile`` 组装；字段与 ``RouterResult`` 同构并补充
    展开后的原语完整内容，使下游无需回查 primitives/playbooks。
    """

    schema_version: str = "1"
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
