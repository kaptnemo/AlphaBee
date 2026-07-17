"""Anomaly registry — 加载勾稽关系规则和异常模式定义。"""

from __future__ import annotations

from pathlib import Path

from alphabee.agents.anomaly.models import AnomalyPattern, CrossRule

_RULES_DIR = Path(__file__).resolve().parent

CROSS_RULES: dict[str, CrossRule] = {}
ANOMALY_PATTERNS: dict[str, AnomalyPattern] = {}

_rules_loaded = False
_patterns_loaded = False


def load_rules() -> None:
    """从 rules.yaml 加载一阶勾稽关系规则。"""
    global _rules_loaded
    import yaml

    rules_path = _RULES_DIR / "rules.yaml"
    with open(rules_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    CROSS_RULES.clear()
    for item in data.get("rules", []):
        # rules.yaml 承载的是一阶业务知识：
        # 哪两个字段构成勾稽关系、看差值还是比值、异常方向是什么。
        rule = CrossRule.from_dict(item)
        CROSS_RULES[rule.id] = rule

    _rules_loaded = True


def load_patterns() -> None:
    """从 patterns.yaml 加载二阶异常模式。"""
    global _patterns_loaded
    import yaml

    patterns_path = _RULES_DIR / "patterns.yaml"
    with open(patterns_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    ANOMALY_PATTERNS.clear()
    for item in data.get("patterns", []):
        # patterns.yaml 承载的是二阶经验模板：
        # 多条一阶异常同时成立时，更像哪类经营/财务质量问题。
        pattern = AnomalyPattern.from_dict(item)
        ANOMALY_PATTERNS[pattern.id] = pattern

    _patterns_loaded = True


def ensure_loaded() -> None:
    """确保规则和模式都已加载（幂等）。"""
    # registry 故意做成懒加载：
    # 上层直接 import anomaly 模块时不立刻读 YAML，只有真正运行检测时才初始化。
    if not _rules_loaded:
        load_rules()
    if not _patterns_loaded:
        load_patterns()
