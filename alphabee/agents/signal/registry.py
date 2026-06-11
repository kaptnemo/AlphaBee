"""Signal registry — 信号规则加载与评估。

信号规则（SignalRule）从 YAML 文件加载，每条规则包含：
- trigger_rules: 档位触发条件（high / medium / low），基于衍生事实值评估
- interpretation_templates: 各档位的文字解释
- thesis_impact: 各档位对不同 thesis 维度的影响（展开为当前档位的 flat 结果）
- critic_questions: 分析师应进一步追问的问题

评估顺序由 SEVERITY_ORDER 固定，不依赖 YAML 定义顺序。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alphabee.agents.derived_facts.registry import safe_eval_formula

RULES_DIR = Path(__file__).resolve().parent / "rules"

# 档位严重度顺序：从最高到最低逐个匹配，第一个命中的级别作为结果
SEVERITY_ORDER = ["high", "medium", "low"]


class SignalRule:
    """单条信号规则，从 YAML 文件加载并支持对 fact_values 求值。"""

    id: str
    name: str
    category: str
    dimension: str
    description: str
    required_facts: list[str]
    required_derived_facts: list[str]
    trigger_rules: dict[str, str]           # level → condition expression
    interpretation_templates: dict[str, str]
    critic_questions: list[str]
    thesis_impact: dict[str, dict[str, str]]  # dimension → {level → impact}

    def __init__(self, rule_file: Path):
        self.rule_file = rule_file
        self.id = rule_file.stem
        self._load_definition()

    def _load_definition(self) -> None:
        import yaml

        with open(self.rule_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self.id = data.get("id", self.id)
        self.name = data.get("name", self.id)
        self.category = data.get("category", "")
        self.dimension = data.get("dimension", "")
        self.description = data.get("description", "")
        self.required_facts = data.get("required_facts", [])
        self.required_derived_facts = data.get("required_derived_facts", [])
        self.interpretation_templates = data.get("interpretation_templates", {})
        self.critic_questions = data.get("critic_questions", [])
        self.thesis_impact = data.get("thesis_impact", {})

        # normalize trigger_rules: 支持两种写法
        #   写法 A（嵌套）: {high: {condition: "..."}}
        #   写法 B（平铺）: {high: "..."}
        # 统一归一化为 {level: "condition_string"}
        raw_triggers: dict[str, Any] = data.get("trigger_rules", {})
        self.trigger_rules: dict[str, str] = {}
        for level, val in raw_triggers.items():
            if isinstance(val, dict):
                self.trigger_rules[level] = val.get("condition", "")
            elif isinstance(val, str):
                self.trigger_rules[level] = val
            # 未知格式直接忽略

    # ── 评估 ──────────────────────────────────────────────────────

    def evaluate(self, fact_values: dict[str, float]) -> dict[str, Any]:
        """用 fact_values（含已计算的衍生事实值）评估本规则。

        Args:
            fact_values: 包含 canonical facts 和已展开的 derived fact 值的字典。

        Returns:
            result dict，键说明：
            - level: "high" / "medium" / "low" / "none" / "missing_fact" / "invalid"
            - interpretation: 文字解释
            - critic_questions: 追问清单
            - thesis_impact: flat dict，{dimension: impact_string}（已按命中 level 解析）
            - error: 仅在 missing_fact / invalid 时出现
        """
        # 检查直接 canonical 依赖
        missing = [f for f in self.required_facts if f not in fact_values]
        if missing:
            return {
                "level": "missing_fact",
                "error": f"缺少必要字段：{missing}",
            }

        # 检查衍生事实依赖（直接调用 evaluate 时可能未经过 engine 预处理）
        missing_derived = [f for f in self.required_derived_facts if f not in fact_values]
        if missing_derived:
            return {
                "level": "missing_fact",
                "error": f"缺少衍生事实：{missing_derived}",
            }

        # 按严重度顺序评估 trigger_rules
        for level in SEVERITY_ORDER:
            condition = self.trigger_rules.get(level)
            if not condition:
                continue
            try:
                matched = safe_eval_formula(condition, fact_values)
            except Exception as e:
                return {
                    "level": "invalid",
                    "error": f"条件 '{condition}' 求值失败：{e}",
                }
            if matched:
                return self._build_result(level, fact_values)

        # 全部条件均未命中 → none
        return self._build_result("none", fact_values)

    def _build_result(self, level: str, fact_values: dict) -> dict[str, Any]:
        """构建命中 level 后的完整结果字典。"""
        interpretation = self.interpretation_templates.get(
            level,
            self.interpretation_templates.get("none", "未发现显著风险信号。"),
        )

        # thesis_impact 展平：{dimension: impact}（已解析为当前 level 的值）
        flat_thesis_impact: dict[str, str] = {}
        for dim, level_map in self.thesis_impact.items():
            impact = level_map.get(level) or level_map.get("none", "neutral")
            flat_thesis_impact[dim] = impact

        return {
            "level": level,
            "interpretation": interpretation,
            "critic_questions": self.critic_questions,
            "thesis_impact": flat_thesis_impact,
        }


# ── 全局规则注册表 ─────────────────────────────────────────────

SIGNAL_RULES: dict[str, SignalRule] = {}


def load_signal_rules() -> None:
    """扫描 rules/ 目录，加载所有 .yaml 规则到 SIGNAL_RULES。"""
    for rule_file in RULES_DIR.glob("*.yaml"):
        rule = SignalRule(rule_file)
        SIGNAL_RULES[rule.id] = rule
