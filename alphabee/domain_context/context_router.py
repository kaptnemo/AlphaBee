"""ContextRouter（DOMAIN_CONTEXT_ROADMAP P0 第 3 步）——规则版公司 → playbook 匹配。

确定性、可单测的公司→框架路由：用 `track_label` / `industry` / `sub_industry` /
`business_model`（archetype）对 playbook 的 ``match_*`` 字段打分，命中最高分 playbook
并展开为其 primitive 集合；无命中时回退 ``generic_fundamental``。

设计约定：
- **映射表 = playbook 的 ``match_*`` 字段**（而非独立 router_mapping.yaml）。它们已随
  ``PlaybookSchema`` 拥有 schema + 版本，数据驱动、非硬编码——这是 Review 问题 #1 的落地。
- **business_model 只作低权输入信号**（权重 1），不产 playbook、不竞争（见
  DOMAIN_CONTEXT_ROADMAP「与 business_model archetype 的边界」）。
- 命中打分：track_label=3 > sub_industry/industry=2 > business_model=1；同分按 playbook id 决平。
- P0 不评分、不排序：``activated_contexts`` 的 ``score`` 统一 1.0，``trend`` 统一 "stable"（P2 引入）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from alphabee.domain_context.loader import load_playbooks
from alphabee.domain_context.schemas import PlaybookSchema

GENERIC_FALLBACK_ID = "generic_fundamental"

# 命中信号权重：越「具体」的匹配越强（track_label 最贴近公司业务，archetype 最泛化）
_WEIGHT_TRACK_LABEL = 3
_WEIGHT_SUB_INDUSTRY = 2
_WEIGHT_BUSINESS_MODEL = 1


class ActivatedContext(BaseModel):
    """一个已激活的分析原语（playbook 已展开为 primitive）。"""

    context: str  # primitive id
    score: float = 1.0  # P0 统一 1.0；P2 引入 context score/ranking
    trend: str = "stable"


class RouterInput(BaseModel):
    """ContextRouter 的输入（全部来自已落地产物，不重复取数）。"""

    symbol: str = ""
    track_label: str = ""  # COMPANY_TRACK.track_label
    industry: str = ""  # INDUSTRY_CONTEXT.industry
    sub_industry: str = ""  # INDUSTRY_CONTEXT.sub_industry
    business_model: str = ""  # COMPANY_TRACK.business_model（archetype）
    business_model_summary: str = ""  # 公司业务描述（P0 暂不参与匹配，保留字段）

    def has_identity_signals(self) -> bool:
        """是否携带任何可用于匹配的身份信号（全空 = 输入缺失，应标记降级）。"""
        return bool(
            (self.track_label or "").strip()
            or (self.industry or "").strip()
            or (self.sub_industry or "").strip()
            or (self.business_model or "").strip()
        )


class RouterResult(BaseModel):
    """ContextRouter 输出：命中的 playbook + 展开后的 primitive + 匹配理由 + 降级标记。"""

    playbook_id: str = ""
    playbook_version: int = 1
    activated_contexts: list[ActivatedContext] = Field(default_factory=list)
    # 主/次驱动变量（变量名，如「猪价」「能繁母猪」，用于报告主线；非 primitive id）
    primary_drivers: list[str] = Field(default_factory=list)
    secondary_drivers: list[str] = Field(default_factory=list)
    why_selected: list[str] = Field(default_factory=list)
    fallback: bool = False  # True = 未命中任何专用 playbook，回退 generic_fundamental
    degraded: bool = False  # True = 输入缺失导致无法正常匹配（非"普通无命中"）
    degraded_reason: str = ""


def _contains(a: str, b: str) -> bool:
    """双向子串匹配（大小写/空白不敏感）：``a`` 与 ``b`` 任一包含另一方即命中。"""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    return a in b or b in a


def _score_playbook(inp: RouterInput, pb: PlaybookSchema) -> tuple[int, list[str]]:
    """对单个 playbook 打分，返回 (score, 命中理由)。score=0 表示未命中。"""
    score = 0
    reasons: list[str] = []

    if inp.track_label and any(_contains(inp.track_label, t) for t in pb.match_track_labels):
        score += _WEIGHT_TRACK_LABEL
        reasons.append("track_label_match")

    # industry / sub_industry 任一命中即 +2（不重复计）
    industries = [x for x in (inp.industry, inp.sub_industry) if (x or "").strip()]
    if industries and any(_contains(x, s) for x in industries for s in pb.match_sub_industries):
        score += _WEIGHT_SUB_INDUSTRY
        reasons.append("sub_industry_match")

    if inp.business_model and inp.business_model in pb.match_business_models:
        score += _WEIGHT_BUSINESS_MODEL
        reasons.append("business_model_match")

    return score, reasons


def _build_result(
    playbook_id: str,
    playbook: PlaybookSchema,
    why_selected: list[str],
    *,
    fallback: bool,
    degraded: bool,
    degraded_reason: str = "",
) -> RouterResult:
    return RouterResult(
        playbook_id=playbook_id,
        playbook_version=playbook.version,
        activated_contexts=[ActivatedContext(context=c) for c in playbook.primitives],
        primary_drivers=list(playbook.primary_drivers),
        secondary_drivers=list(playbook.secondary_drivers),
        why_selected=why_selected,
        fallback=fallback,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


def route(
    inp: RouterInput,
    playbooks: dict[str, PlaybookSchema] | None = None,
) -> RouterResult:
    """规则版路由：公司 → 命中 playbook（展开为 primitive）。

    Args:
        inp: 公司身份信号（track_label / industry / sub_industry / business_model）。
        playbooks: 覆盖默认加载的 playbook（None 时用 ``load_playbooks()``，供测试注入）。

    Returns:
        ``RouterResult``。无命中时回退 ``generic_fundamental`` 并置 ``fallback=True``；
        身份信号全空时额外置 ``degraded=True``（输入缺失，区别于"普通无命中"）。
    """
    playbooks = playbooks if playbooks is not None else load_playbooks()

    scored: list[tuple[int, str, PlaybookSchema, list[str]]] = []
    for playbook_id, playbook in playbooks.items():
        if playbook_id == GENERIC_FALLBACK_ID:
            continue
        score, reasons = _score_playbook(inp, playbook)
        if score > 0:
            scored.append((score, playbook_id, playbook, reasons))

    # 最高分优先，同分按 playbook id 决平（保证确定性）
    scored.sort(key=lambda item: (-item[0], item[1]))

    if scored:
        score, playbook_id, playbook, reasons = scored[0]
        return _build_result(playbook_id, playbook, reasons, fallback=False, degraded=False)

    fallback = playbooks.get(GENERIC_FALLBACK_ID)
    if fallback is None:
        # 理论上不会发生（catalog 必含 generic_fundamental），防御性兜底
        return RouterResult(degraded=True, degraded_reason="generic_fallback_missing")

    degraded = not inp.has_identity_signals()
    return _build_result(
        GENERIC_FALLBACK_ID,
        fallback,
        why_selected=["no_playbook_matched"],
        fallback=True,
        degraded=degraded,
        degraded_reason="identity_signals_missing" if degraded else "",
    )
