"""DerivedFacts tools — 对外暴露为 agent function call 的衍生事实计算接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from alphabee.agents.derived_facts.engine import Engine
from alphabee.agents.derived_facts.registry import RULES, load_rules
from alphabee.agents.facts.tools.financial_fact import extract_financial_facts
from alphabee.agents.facts.tools.market_fact import extract_market_facts

if TYPE_CHECKING:
    from alphabee.agents.facts.models import FinancialFacts, MarketFacts

# 模块加载时一次性初始化规则注册表
load_rules()


def extract_and_merge_facts(
    financial_data: dict[str, Any] | None = None,
    market_data: dict[str, Any] | None = None,
    extra_fields: dict[str, float] | None = None,
    *,
    financial_facts: FinancialFacts | None = None,
    market_facts: MarketFacts | None = None,
) -> dict[str, float]:
    """从原始 fact tool 输出或 Pydantic 模型中提取 canonical 字段值并合并为平面 dict。

    这是 Fact Collection 层与 DerivedFacts Engine 之间的桥接函数，支持两种输入形式：

    - **原始 dict**（``financial_data`` / ``market_data``）：
      通过 ``extract_financial_facts()`` / ``extract_market_facts()`` 提取。
    - **Pydantic 模型**（``financial_facts`` / ``market_facts``）：
      直接调用 ``.to_fact_values()`` 展开，无需额外提取步骤。

    两种形式可混用，模型值优先级高于原始 dict（后写入覆盖先写入）。

    返回的 dict 可直接传入 evaluate_derived_facts() 的 fact_values 参数。

    Example:
        # 原始 dict 路径
        >>> fin = get_financial_fact("600519.SH")
        >>> mkt = get_market_fact("600519.SH")
        >>> values = extract_and_merge_facts(financial_data=fin, market_data=mkt)

        # Pydantic 模型路径
        >>> fin_model = get_financial_facts_model("600519.SH")
        >>> mkt_model = get_market_facts_model("600519.SH")
        >>> values = extract_and_merge_facts(financial_facts=fin_model, market_facts=mkt_model)

        >>> evaluate_derived_facts(["cashflow_quality", "debt_ratio"], values)
    """
    merged: dict[str, float] = {}

    # ── 原始 dict 路径 ────────────────────────────────────────────
    if financial_data is not None:
        merged.update(extract_financial_facts(financial_data))

    if market_data is not None:
        merged.update(extract_market_facts(market_data))

    # ── Pydantic 模型路径（覆盖同名字段）────────────────────────
    if financial_facts is not None:
        merged.update(financial_facts.to_fact_values())

    if market_facts is not None:
        merged.update(market_facts.to_fact_values())

    if extra_fields is not None:
        merged.update(extra_fields)

    return merged


def list_derived_fact_rules() -> str:
    """列出所有可用的衍生事实规则，包含每条规则的名称、描述和所需输入字段。

    在调用 evaluate_derived_facts 之前，可先调用此工具确认规则名称和所需字段，
    以便准确准备 fact_values 字典。

    Returns:
        Markdown 格式的规则目录，含规则名、描述、所需字段。
    """
    if not RULES:
        load_rules()

    lines = ["## 可用衍生事实规则\n", "| 规则名 | 描述 | 所需字段 |", "|--------|------|----------|"]
    for name, rule in sorted(RULES.items()):
        fields = ", ".join(f"`{f}`" for f in rule.required_facts)
        lines.append(f"| `{name}` | {rule.description} | {fields} |")

    return "\n".join(lines)


def evaluate_derived_facts(
    rule_names: list[str],
    fact_values: dict[str, float] | None = None,
    *,
    financial_data: dict[str, Any] | None = None,
    market_data: dict[str, Any] | None = None,
    financial_facts: FinancialFacts | None = None,
    market_facts: MarketFacts | None = None,
    extra_fields: dict[str, float] | None = None,
) -> str:
    """对指定规则组合计算衍生事实，返回每条规则的计算值、档位判断和业务解释。

    支持三种输入方式，自动识别，可混用：

    1. **平面值**（程序化调用）：传入 ``fact_values`` 字典。
    2. **Pydantic 模型**（推荐）：传入 ``financial_facts`` / ``market_facts``，
       引擎直接调用 ``.to_fact_values()`` 提取字段，无需手工转换。
    3. **原始 dict**（LLM Agent 调用）：传入 ``financial_data`` / ``market_data``，
       内部自动通过提取层转为 canonical 字段。

    优先级：``fact_values`` < 原始 dict 提取 < Pydantic 模型 < ``extra_fields``。

    支持链式依赖：若某条规则在 required_derived_facts 中声明依赖另一条衍生事实，
    引擎会自动按拓扑顺序先计算依赖规则，并将结果注入后续规则的输入集。

    适用场景：
    - 判断公司财务质量（现金流、应收账款、存货）
    - 评估成长质量（利润杠杆、市场份额变化）
    - 分析偿债能力（资产负债率、利息保障、流动比率）
    - 衡量估值合理性（PEG、PB-ROE 匹配、估值压缩）
    - 综合多维度给出信号灯式诊断

    Args:
        rule_names:       要计算的规则名称列表，例如
                          ["cashflow_quality", "receivable_pressure", "debt_ratio"]。
                          可通过 list_derived_fact_rules() 查看全部可用规则。
        fact_values:      平面 canonical 字段值字典（程序化路径），例如
                          {"operating_cashflow": 1200, "net_profit": 1000}。
        financial_facts:  ``FinancialFacts`` 模型实例（推荐路径），自动展开为 canonical 字段。
        market_facts:     ``MarketFacts`` 模型实例（推荐路径），自动展开为 canonical 字段。
        financial_data:   get_financial_fact() 的原始返回 dict（LLM 路径）。
        market_data:      get_market_fact() 的原始返回 dict（LLM 路径）。
        extra_fields:     手动补充的字段，如 {"industry_revenue_yoy": 12.5}，优先级最高。

    Returns:
        Markdown 格式的衍生事实分析报告，包含每条规则的计算值、档位判断、
        业务解释，以及字段缺失/计算错误的说明。
    """
    if not RULES:
        load_rules()

    # ── 合并所有输入源 ────────────────────────────────────────────
    merged_values = dict(fact_values) if fact_values else {}

    if financial_data is not None or market_data is not None:
        merged_values.update(
            extract_and_merge_facts(
                financial_data=financial_data,
                market_data=market_data,
            )
        )

    if financial_facts is not None:
        merged_values.update(financial_facts.to_fact_values())

    if market_facts is not None:
        merged_values.update(market_facts.to_fact_values())

    if extra_fields is not None:
        merged_values.update(extra_fields)

    unknown_rules = [r for r in rule_names if r not in RULES]
    valid_rules = [r for r in rule_names if r in RULES]

    sections: list[str] = []

    if unknown_rules:
        sections.append(
            f"> ⚠️ 未知规则（已跳过）：{', '.join(f'`{r}`' for r in unknown_rules)}\n"
        )

    if not valid_rules:
        sections.append("没有可计算的规则，请检查规则名称和字段输入。")
        return "\n".join(sections)

    try:
        engine = Engine()
        all_results = engine.run(valid_rules, merged_values)
    except Exception as e:
        return f"> ❌ 引擎初始化失败：{e}"

    requested_set = set(valid_rules)
    transitive_deps = [n for n in all_results if n not in requested_set]

    # ── 主结果：请求的规则 ──────────────────────────────────────────
    for name in valid_rules:
        result = all_results.get(name)
        if result is None:
            sections.append(
                f"### `{name}`\n"
                f"> ⚠️ 数据不可用 — 规则未被引擎计算\n"
            )
            continue

        sections.append(_format_result(name, result))

    # ── 附录：自动计算的传递依赖（若有）──────────────────────────
    if transitive_deps:
        sections.append("---\n#### 自动计算的衍生事实依赖\n")
        for name in transitive_deps:
            result = all_results[name]
            sections.append(_format_result(name, result))

    if not sections:
        return "没有可计算的规则，请检查规则名称和字段输入。"

    return "\n".join(sections)


def _format_result(name: str, result: dict) -> str:
    """将单条规则结果格式化为 Markdown 段落。"""
    level = result.get("level", "unknown")
    raw_value = result.get(name)
    interp = result.get("interpretation", "")
    error = result.get("error", "")
    blocked_by = result.get("blocked_by", [])

    rule = RULES.get(name)
    desc = rule.description if rule else ""

    header = f"### `{name}`" + (f"  —  {desc}" if desc else "")

    if level == "blocked":
        deps_str = ", ".join(f"`{d}`" for d in blocked_by)
        return f"{header}\n> ❌ 被阻塞 — 上游依赖失败：{deps_str}\n> 原因：{error}\n"

    if level in ("invalid", "missing_fact"):
        return f"{header}\n> ❌ 计算失败：{error}\n"

    if raw_value is None:
        missing_facts = [f for f in (rule.required_facts if rule else []) if f not in {}]
        return (
            f"{header}\n"
            f"> ⚠️ 数据不可用 — 缺少字段：{error or '未知'}\n"
        )

    value_str = f"{raw_value:.4g}" if isinstance(raw_value, float) else str(raw_value)

    block = [
        header,
        f"- **计算值**：{value_str}",
        f"- **档位**：`{level}`",
    ]
    if interp:
        block.append(f"- **解释**：{interp}")
    return "\n".join(block) + "\n"

