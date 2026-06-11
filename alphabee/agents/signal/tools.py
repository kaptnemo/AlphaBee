"""Signal tools — 对外暴露为 agent function call 的信号评估接口。"""

from __future__ import annotations

from alphabee.agents.signal.engine import SignalEngine
from alphabee.agents.signal.registry import SIGNAL_RULES, load_signal_rules

_initialized = False


def _ensure_loaded() -> None:
    """确保信号规则已加载（惰性初始化，首次调用时触发）。"""
    global _initialized
    if not _initialized:
        load_signal_rules()
        _initialized = True

# 档位中文映射
_LEVEL_LABELS: dict[str, str] = {
    "high": "🔴 高风险",
    "medium": "🟡 中风险",
    "low": "🟢 低风险",
    "none": "✅ 无风险",
    "blocked": "⛔ 被阻塞",
    "missing_fact": "⚠️ 缺少字段",
    "invalid": "❌ 求值失败",
    "unknown": "❓ 未知规则",
}

# thesis_impact 中文映射
_IMPACT_LABELS: dict[str, str] = {
    "negative": "负面",
    "slightly_negative": "轻度负面",
    "neutral": "中性",
    "slightly_positive": "轻度正面",
    "positive": "正面",
}


def list_signal_rules() -> str:
    """列出所有可用的信号规则，包含每条规则的 ID、名称、类别和所需输入字段。

    在调用 evaluate_signals 之前，可先调用此工具确认规则 ID 和所需字段，
    以便准确准备 fact_values 字典。

    Returns:
        Markdown 格式的信号规则目录，含 ID、名称、类别、所需 canonical 字段。
    """
    _ensure_loaded()

    lines = [
        "## 可用信号规则\n",
        "| 规则 ID | 名称 | 类别 | 所需 canonical 字段 | 所需衍生事实 |",
        "|---------|------|------|---------------------|--------------|",
    ]
    for rule_id, rule in sorted(SIGNAL_RULES.items()):
        facts_str = ", ".join(f"`{f}`" for f in rule.required_facts) or "—"
        derived_str = ", ".join(f"`{f}`" for f in rule.required_derived_facts) or "—"
        lines.append(
            f"| `{rule_id}` | {rule.name} | {rule.category} | {facts_str} | {derived_str} |"
        )

    return "\n".join(lines)


def evaluate_signals(
    rule_names: list[str],
    fact_values: dict[str, float],
) -> str:
    """评估指定信号规则组合，自动计算所需衍生事实，返回每条规则的信号档位和解释。

    信号引擎会自动处理 required_derived_facts：调用衍生事实引擎按拓扑顺序计算，
    无需调用方手动预计算。调用方只需提供 canonical 字段值即可。

    适用场景：
    - 财务质量综合风险诊断（收入质量、现金流质量、债务结构）
    - 快速识别需要深度研究的风险维度
    - 输出对 thesis 的定向影响，供投资分析综合使用

    Args:
        rule_names: 要评估的信号规则 ID 列表，例如
            ["revenue_quality_risk", "cashflow_quality_risk", "debt_risk"]。
            可通过 list_signal_rules() 查看全部可用规则。
        fact_values: canonical 字段值字典，键为字段名，值为数值，例如
            {"revenue_yoy": 15.2, "accounts_receivable": 800,
             "accounts_receivable_prev": 600, "operating_cashflow": 1200,
             "net_profit": 1000}。
            字段名需与各规则的 required_facts 匹配；缺失字段的规则会被标注。

    Returns:
        Markdown 格式的信号诊断报告，包含每条规则的档位判断、文字解释、
        thesis 影响评估和追问清单；以及错误/阻塞原因（如有）。
    """
    _ensure_loaded()

    unknown_rules = [r for r in rule_names if r not in SIGNAL_RULES]
    valid_rules = [r for r in rule_names if r in SIGNAL_RULES]

    sections: list[str] = []

    if unknown_rules:
        sections.append(
            f"> ⚠️ 未知规则（已跳过）：{', '.join(f'`{r}`' for r in unknown_rules)}\n"
        )

    if not valid_rules:
        sections.append("没有可评估的规则，请检查规则 ID 和字段输入。")
        return "\n".join(sections)

    try:
        engine = SignalEngine()
        all_results = engine.run(valid_rules, fact_values)
    except Exception as e:
        return f"> ❌ 信号引擎初始化失败：{e}"

    for name in valid_rules:
        result = all_results.get(name)
        if result is None:
            sections.append(f"### `{name}`\n> ⚠️ 数据不可用 — 规则未被引擎计算\n")
            continue
        sections.append(_format_signal_result(name, result))

    return "\n".join(sections)


def _format_signal_result(name: str, result: dict) -> str:
    """将单条信号结果格式化为 Markdown 段落。"""
    level = result.get("level", "unknown")
    level_label = _LEVEL_LABELS.get(level, level)
    rule = SIGNAL_RULES.get(name)
    rule_name = rule.name if rule else name

    header = f"### `{name}` — {rule_name}"

    # 异常 level 处理
    if level in ("blocked", "missing_fact", "invalid", "unknown"):
        error = result.get("error", "")
        blocked_by = result.get("blocked_by", [])
        if blocked_by:
            deps_str = ", ".join(f"`{d}`" for d in blocked_by)
            return f"{header}\n**档位**：{level_label}\n> 上游依赖失败：{deps_str}\n> 原因：{error}\n"
        return f"{header}\n**档位**：{level_label}\n> {error}\n"

    # 正常 level 处理
    interpretation = result.get("interpretation", "")
    critic_questions: list[str] = result.get("critic_questions", [])
    thesis_impact: dict[str, str] = result.get("thesis_impact", {})

    block = [header, f"**档位**：{level_label}"]

    if interpretation:
        block.append(f"\n**解释**：{interpretation}")

    if thesis_impact:
        impact_lines = [
            f"  - {dim}：{_IMPACT_LABELS.get(impact, impact)}"
            for dim, impact in thesis_impact.items()
        ]
        block.append("\n**Thesis 影响**：\n" + "\n".join(impact_lines))

    if critic_questions:
        q_lines = [f"  - {q}" for q in critic_questions]
        block.append("\n**追问清单**：\n" + "\n".join(q_lines))

    return "\n".join(block) + "\n"
