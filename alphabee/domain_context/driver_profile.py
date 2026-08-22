"""组装 DriverProfile（DOMAIN_CONTEXT_ROADMAP P0 第 4 步）。

``build_driver_profile`` = ``context_router.route`` + 展开激活原语的完整内容，
产出可落 artifact 的 ``DriverProfile`` 快照。
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
