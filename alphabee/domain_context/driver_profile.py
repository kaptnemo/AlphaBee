"""组装 DriverProfile（DOMAIN_CONTEXT_ROADMAP P0 第 4 步）。

``build_driver_profile`` = ``context_router.route`` + 展开激活原语的完整内容，
产出可落 artifact 的 ``DriverProfile`` 快照。

业务逻辑（为什么要"展开完整内容"而不只存 id）：
- ``route`` 只返回「命中了哪些 primitive 的 id」，而下游（synthesize_insights / 报告层）
  真正需要的是每个原语的 key_variables / priority_questions / disconfirming_signals 等
  完整内容，用来写 main_driver / central_tension。
- 若只存 id，下游每次都要回查 ``load_primitives()`` 做二次关联，既多一次依赖、又让
  artifact 不再自洽。把完整内容**快照进 DriverProfile**，使 artifact 成为自包含的
  "公司驱动画像"，下游 ``find_artifact_model`` 拿到即可直接消费。
"""

from __future__ import annotations

from alphabee.core import Issue, IssueScope, IssueSeverity
from alphabee.domain_context.context_router import RouterInput, route
from alphabee.domain_context.contracts import ActivatedPrimitive, DriverProfile
from alphabee.domain_context.loader import load_playbooks, load_primitives
from alphabee.domain_context.schemas import PlaybookSchema, PrimitiveSchema
from alphabee.utils.pipeline import make_id

#: 政策敏感赛道关键词（P0 设计 §8.2）：路由命中这些身份信号时追加激活 ``policy_shock``。
_POLICY_SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "光伏",
    "新能源",
    "创新药",
    "生物医药",
    "游戏",
    "教培",
    "地产",
    "半导体",
    "军工",
    "稀土",
    "锂电",
    "动力电池",
)


def _is_policy_sensitive(inp: RouterInput) -> bool:
    """公司身份信号命中政策敏感赛道关键词（确定性、无 LLM）。"""
    text = " ".join(filter(None, (inp.track_label, inp.industry, inp.sub_industry, inp.business_model)))
    return any(kw in text for kw in _POLICY_SENSITIVE_KEYWORDS)


def build_driver_profile(
    symbol: str,
    router_input: RouterInput,
    *,
    generated_at: str = "",
    playbooks: dict[str, PlaybookSchema] | None = None,
    primitives: dict[str, PrimitiveSchema] | None = None,
) -> DriverProfile:
    """路由 + 展开原语内容 → ``DriverProfile`` 快照。

    Args:
        symbol: 股票代码（如 ``002714.SZ``）。
        router_input: 公司身份/结构信号（track_label / industry / sub_industry /
            business_model + sw_code / 分部结构 / 财务结构）。
        generated_at: 生成时间戳（空则留给上层填）。
        playbooks: 覆盖默认加载的 playbook（测试注入）。
        primitives: 覆盖默认加载的 primitive（测试注入）。

    Returns:
        ``DriverProfile``：命中 playbook + 展开后的激活原语（含完整内容）+ 主/次驱动变量
        + 匹配理由 + 锚定强度 + playbook 级框架知识 + fallback/degraded 标记。
    """
    playbooks = playbooks if playbooks is not None else load_playbooks()
    result = route(router_input, playbooks=playbooks)
    primitives = primitives if primitives is not None else load_primitives()

    # ── 展开：把命中的 primitive id 换成完整内容快照 ─────────────────
    # route 产出的是「哪些框架被激活」（id 列表）；这里把每个 id 对应的原语内容
    # （key_variables / priority_questions / …）拷进 ActivatedPrimitive，使 DriverProfile
    # 成为自包含画像。缺的原语直接跳过（防御：catalog 闭合校验下不应发生）。
    activated: list[ActivatedPrimitive] = []
    for ctx in result.activated_contexts:
        prim = primitives.get(ctx.context)
        if prim is None:
            continue
        activated.append(
            ActivatedPrimitive(
                id=prim.id,
                score=ctx.score,
                trend=ctx.trend,
                description=prim.description,
                key_variables=prim.key_variables,
                priority_questions=prim.priority_questions,
                disconfirming_signals=prim.disconfirming_signals,
                preferred_sources=prim.preferred_sources,
                report_angles=prim.report_angles,
                causal_paths=prim.causal_paths,
                when_to_activate=prim.when_to_activate,
            )
        )

    # P0 设计 §8.2：政策敏感赛道（policy_shock）不依赖具体 playbook，命中身份信号即追加激活；
    # 下游 build_policy_exposure_issue 据此产出 policy_exposure Issue（不进 regime 主权重）。
    if _is_policy_sensitive(router_input) and not any(p.id == "policy_shock" for p in activated):
        policy_prim = primitives.get("policy_shock")
        if policy_prim is not None:
            activated.append(
                ActivatedPrimitive(
                    id=policy_prim.id,
                    score=1.0,
                    trend="stable",
                    description=policy_prim.description,
                    key_variables=policy_prim.key_variables,
                    priority_questions=policy_prim.priority_questions,
                    disconfirming_signals=policy_prim.disconfirming_signals,
                    preferred_sources=policy_prim.preferred_sources,
                    report_angles=policy_prim.report_angles,
                    causal_paths=policy_prim.causal_paths,
                    when_to_activate=policy_prim.when_to_activate,
                )
            )

    # playbook 级框架知识（关键冲突 / 验证顺序 / 报告问题）此前只活在 YAML 里，
    # 画像快照拿不到；补进来让下游无需回查 playbook 目录即可使用。
    playbook = playbooks.get(result.playbook_id)

    return DriverProfile(
        symbol=symbol,
        generated_at=generated_at,
        playbook=result.playbook_id,
        playbook_version=result.playbook_version,
        activated_primitives=activated,
        primary_drivers=result.primary_drivers,
        secondary_drivers=result.secondary_drivers,
        why_selected=result.why_selected,
        fallback=result.fallback,
        degraded=result.degraded,
        degraded_reason=result.degraded_reason,
        anchor_strength=result.anchor_strength,
        key_conflicts=list(playbook.key_conflicts) if playbook else [],
        recommended_verification_order=list(playbook.recommended_verification_order) if playbook else [],
        report_questions=list(playbook.report_questions) if playbook else [],
    )


def build_policy_exposure_issue(profile: DriverProfile) -> Issue | None:
    """选中 ``policy_shock`` primitive ⇒ 产出 ``policy_exposure`` Issue（确定性、无 LLM）。

    P0 设计 §8.2：政策敏感赛道是外生变量，需人工复核政策方向与受益/受损定位；
    该 Issue 只进报告"系统问题"段与偏离账本，**不进 regime 主权重**。
    未选中 ``policy_shock`` ⇒ ``None``（零 Issue）。分类登记在
    ``alphabee.orchestrator.services.deviation.CLASS_BY_CATEGORY``（policy_exposure → D4_STATE）。
    """
    if not any(p.id == "policy_shock" for p in profile.activated_primitives):
        return None
    return Issue(
        id=make_id("issue"),
        severity=IssueSeverity.MEDIUM,
        category="policy_exposure",
        message=(
            "公司落入政策敏感赛道（policy_shock 原语命中），政策变量可能改变盈利边界，"
            "需人工复核政策方向与受益/受损定位。"
        ),
        scope=IssueScope.DATA,
        owner_node="resolve_driver_profile",
    )
