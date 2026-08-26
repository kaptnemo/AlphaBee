"""商业模式定位（COMPANY_TRACK Phase E，E1/E2）。

四类 archetype（赚的钱模式不同，分析维度不同）：

| archetype | 定义 | 关注点 |
|---|---|---|
| ``brand`` | 品牌/解决方案商 | 研发费用率、渠道、品牌溢价 |
| ``odm`` | ODM/OEM 代工商 | 产能利用率、良率、大客户集中度 |
| ``component`` | 核心零部件商 | 产品迭代、技术壁垒 |
| ``integrator`` | 软硬件集成商 | 生态绑定、交付能力 |
| ``other`` | 其他/待确认 | — |

> **与 domain_context 的边界**：本 archetype 是「财务解读口径」（管"怎么读"，校准阈值/及格线，
> 如 ODM 低毛利不等于恶化），是 `domain_context.ContextRouter` 的输入信号之一；它不产 playbook、
> 不与 primitives/playbooks 竞争（后者管"看什么"，即驱动变量框架）。详见
> docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md「与 business_model archetype 的边界」。

E2 分类器：规则启发（毛利率带 + 研发费率带 + 大客户集中度佐证）为主，
LLM 复核为辅（``agent.business_model``，失败回退规则）。
"""

from __future__ import annotations

BUSINESS_MODELS = ("brand", "odm", "component", "integrator", "other")

BUSINESS_MODEL_LABELS: dict[str, str] = {
    "brand": "品牌/解决方案商",
    "odm": "ODM/OEM 代工商",
    "component": "核心零部件商",
    "integrator": "软硬件集成商",
    "other": "其他/待确认",
}

BUSINESS_MODEL_FOCUS: dict[str, str] = {
    "brand": "研发费用率/渠道/品牌溢价",
    "odm": "产能利用率/良率/大客户集中度",
    "component": "产品迭代/技术壁垒",
    "integrator": "生态绑定/交付能力",
    "other": "需更多信息",
}

# v1 规则带（毛利率/研发费率均为 RATIO 口径；可调，分析师可改）
_ODM_MAX_MARGIN = 0.20
_ODM_MAX_RD = 0.08
_COMPONENT_MIN_MARGIN = 0.40
_COMPONENT_MIN_RD = 0.12
_INTEGRATOR_MIN_MARGIN = 0.20
_INTEGRATOR_MIN_RD = 0.10
_CUSTOMER_CONCENTRATION_ODM_HINT = 0.50  # 大客户集中度 ≥50% 佐证代工模式


def _rule_classify(
    gross_margin: float | None,
    rd_ratio: float | None,
    customer_concentration: float | None,
) -> tuple[str, str]:
    parts: list[str] = []
    if gross_margin is not None:
        parts.append(f"毛利率 {gross_margin * 100:.1f}%")
    if rd_ratio is not None:
        parts.append(f"研发费率 {rd_ratio * 100:.1f}%")
    if customer_concentration is not None:
        parts.append(f"大客户集中度 {customer_concentration * 100:.0f}%")
    evidence = "；".join(parts) if parts else "缺少关键指标"

    if gross_margin is None or rd_ratio is None:
        return "other", f"{evidence}（指标不足，无法判定商业模式）"
    if gross_margin < _ODM_MAX_MARGIN and rd_ratio < _ODM_MAX_RD:
        model = "odm"
    elif gross_margin >= _COMPONENT_MIN_MARGIN and rd_ratio >= _COMPONENT_MIN_RD:
        model = "component"
    elif gross_margin >= _COMPONENT_MIN_MARGIN and rd_ratio < _COMPONENT_MIN_RD:
        model = "brand"
    elif gross_margin >= _INTEGRATOR_MIN_MARGIN and rd_ratio >= _INTEGRATOR_MIN_RD:
        model = "integrator"
    else:
        return "other", f"{evidence}（不在典型带内，需人工确认）"

    if (
        model == "odm"
        and customer_concentration is not None
        and customer_concentration >= _CUSTOMER_CONCENTRATION_ODM_HINT
    ):
        evidence += "；高客户集中度佐证代工模式"
    return model, evidence


def _llm_classify(
    gross_margin: float | None,
    rd_ratio: float | None,
    customer_concentration: float | None,
    rule_model: str,
    context: str,
) -> tuple[str, str] | None:
    """LLM 复核（agent.business_model）；任何失败返回 None（回退规则）。"""
    try:
        from alphabee.utils.llm import create_chat_model
        from alphabee.utils.pipeline import parse_json

        metrics = {
            "毛利率": gross_margin,
            "研发费率": rd_ratio,
            "大客户集中度": customer_concentration,
        }
        prompt = (
            "你是商业模式分析师。基于以下指标，判断公司属于哪类商业模式："
            f"{BUSINESS_MODEL_LABELS}。规则层判定: {rule_model}。\n"
            '只输出 JSON: {"business_model": "brand|odm|component|integrator|other", '
            '"evidence": "一句话依据（引用具体指标）"}。\n'
            f"指标: {metrics}\n公司语境: {context or '（无）'}"
        )
        model = create_chat_model("agent.business_model")
        raw = model.invoke(prompt).content
        parsed = parse_json(str(raw))
        if not isinstance(parsed, dict):
            return None
        model_name = str(parsed.get("business_model") or "").strip()
        if model_name not in BUSINESS_MODELS:
            return None
        return model_name, str(parsed.get("evidence") or "").strip()
    except Exception:
        return None


def classify_business_model(
    gross_margin: float | None = None,
    rd_ratio: float | None = None,
    *,
    customer_concentration: float | None = None,
    use_llm: bool = False,
    context: str = "",
) -> tuple[str, str]:
    """商业模式分类 → ``(business_model, evidence)``。

    Args:
        gross_margin: 毛利率（RATIO，0.4 = 40%）。
        rd_ratio: 研发费率（RATIO，研发费用/营收）。
        customer_concentration: 大客户集中度（RATIO，可选佐证）。
        use_llm: 是否 LLM 复核（失败回退规则）。
        context: 公司语境（LLM 参考，如主营描述）。

    Returns:
        (business_model, evidence)；指标不足时 ``other`` + 说明（不猜测）。
    """
    rule_model, evidence = _rule_classify(gross_margin, rd_ratio, customer_concentration)
    if use_llm:
        refined = _llm_classify(gross_margin, rd_ratio, customer_concentration, rule_model, context)
        if refined is not None:
            return refined
    return rule_model, evidence
