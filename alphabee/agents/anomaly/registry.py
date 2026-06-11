"""Anomaly registry — 加载勾稽关系规则和异常模式定义。"""

from __future__ import annotations

from pathlib import Path

from alphabee.agents.anomaly.models import AnomalyPattern, CrossRule

_RULES_DIR = Path(__file__).resolve().parent

CROSS_RULES: dict[str, CrossRule] = {}
ANOMALY_PATTERNS: dict[str, AnomalyPattern] = {}

_loaded = False


def load_rules() -> None:
    """从 rules.yaml 加载一阶勾稽关系规则。"""
    global _loaded
    import yaml

    rules_path = _RULES_DIR / "rules.yaml"
    with open(rules_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    CROSS_RULES.clear()
    for item in data.get("rules", []):
        rule = CrossRule.from_dict(item)
        CROSS_RULES[rule.id] = rule

    _loaded = True


def load_patterns() -> None:
    """从 patterns.yaml 加载二阶异常模式。"""
    import yaml

    patterns_path = _RULES_DIR / "patterns.yaml"
    with open(patterns_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    ANOMALY_PATTERNS.clear()
    for item in data.get("patterns", []):
        pattern = AnomalyPattern.from_dict(item)
        ANOMALY_PATTERNS[pattern.id] = pattern


def ensure_loaded() -> None:
    """确保规则和模式都已加载（幂等）。"""
    if not _loaded:
        load_rules()
        load_patterns()
