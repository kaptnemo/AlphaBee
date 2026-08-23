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

from alphabee.domain_context.context_router import RouterInput, route
from alphabee.domain_context.contracts import ActivatedPrimitive, DriverProfile
from alphabee.domain_context.loader import load_primitives
from alphabee.domain_context.schemas import PlaybookSchema, PrimitiveSchema


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
        router_input: 公司身份信号（track_label / industry / sub_industry / business_model）。
        generated_at: 生成时间戳（空则留给上层填）。
        playbooks: 覆盖默认加载的 playbook（测试注入）。
        primitives: 覆盖默认加载的 primitive（测试注入）。

    Returns:
        ``DriverProfile``：命中 playbook + 展开后的激活原语（含完整内容）+ 主/次驱动变量
        + 匹配理由 + fallback/degraded 标记。
    """
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
            )
        )

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
    )
