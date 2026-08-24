"""Domain Context（DOMAIN_CONTEXT_ROADMAP）——定性叙事层。

P0 第 1 步（✅）：分析原语与组合框架的 canonical schema + 加载 + 目录闭合校验。
P0 第 2 步（✅）：6 primitive + 2 playbook + 1 通用兜底清单（已随 YAML 落地）。
P0 第 3 步（✅）：ContextRouter —— 规则版公司 → playbook 匹配（含 fallback / 降级 / version）。
P0 第 4 步（✅）：DriverProfile 契约 + build_driver_profile（展开激活原语）。
P0 第 5 步（待）：注入 synthesize_insights。

公共 API：
- ``PrimitiveSchema`` / ``PlaybookSchema`` —— 知识资产 canonical schema
- ``DomainContextCatalog`` —— 加载并校验后的目录（primitives + playbooks）
- ``load_primitives`` / ``load_playbooks`` / ``load_catalog`` —— 加载与校验
- ``validate_closure`` —— 目录闭合校验（playbook 只能引用已声明的 primitive）
- ``route`` / ``RouterInput`` / ``RouterResult`` / ``ActivatedContext`` —— ContextRouter
- ``DriverProfile`` / ``ActivatedPrimitive`` —— 公司驱动画像契约
- ``build_driver_profile`` —— 组装 DriverProfile

详见 docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md。
"""

from alphabee.domain_context.context_router import (
    GENERIC_FALLBACK_ID,
    ActivatedContext,
    RouterInput,
    RouterResult,
    route,
)
from alphabee.domain_context.contracts import ActivatedPrimitive, DriverProfile
from alphabee.domain_context.driver_profile import build_driver_profile
from alphabee.domain_context.loader import (
    DomainContextCatalog,
    load_catalog,
    load_playbooks,
    load_primitives,
    validate_closure,
)
from alphabee.domain_context.schemas import PlaybookSchema, PrimitiveSchema

__all__ = [
    "ActivatedContext",
    "ActivatedPrimitive",
    "DomainContextCatalog",
    "DriverProfile",
    "GENERIC_FALLBACK_ID",
    "PlaybookSchema",
    "PrimitiveSchema",
    "RouterInput",
    "RouterResult",
    "build_driver_profile",
    "load_catalog",
    "load_playbooks",
    "load_primitives",
    "route",
    "validate_closure",
]
