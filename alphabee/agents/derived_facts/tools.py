"""DerivedFacts tools — 对外暴露为 agent function call 的衍生事实计算接口。"""

from __future__ import annotations

from alphabee.agents.derived_facts.engine import Engine
from alphabee.agents.derived_facts.registry import RULES, load_rules

# 模块加载时一次性初始化规则注册表
load_rules()


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
    fact_values: dict[str, float],
) -> str:
    """对指定规则组合计算衍生事实，返回每条规则的计算值、档位判断和业务解释。

    支持链式依赖：若某条规则在 required_derived_facts 中声明依赖另一条衍生事实，
    引擎会自动按拓扑顺序先计算依赖规则，并将结果注入后续规则的输入集。

    适用场景：
    - 判断公司财务质量（现金流、应收账款、存货）
    - 评估成长质量（利润杠杆、市场份额变化）
    - 分析偿债能力（资产负债率、利息保障、流动比率）
    - 衡量估值合理性（PEG、PB-ROE 匹配、估值压缩）
    - 综合多维度给出信号灯式诊断

    Args:
        rule_names: 要计算的规则名称列表，例如
            ["cashflow_quality", "receivable_pressure", "debt_ratio"]。
            可通过 list_derived_fact_rules() 查看全部可用规则。
        fact_values: 事实字段值字典，键为字段名，值为数值，例如
            {"operating_cashflow": 1200, "net_profit": 1000,
             "accounts_receivable": 800, "revenue": 5000}。
            字段名需与规则的 required_facts 匹配；缺失字段的规则会被跳过并标注。

    Returns:
        Markdown 格式的衍生事实分析报告，包含每条规则的计算值、档位判断、
        业务解释，以及字段缺失/计算错误的说明。
    """
    if not RULES:
        load_rules()

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
        all_results = engine.run(valid_rules, fact_values)
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

