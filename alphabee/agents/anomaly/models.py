"""Anomaly engine data models — 勾稽关系异常检测。

一阶：单指标偏离自身历史基线 (MetricAnomaly + CrossRule)
二阶：多异常指标模式匹配 → 根因假设 (AnomalyPattern + PatternMatch)
输出：AnomalyReport（可序列化为 fact_values）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CrossRule:
    """一阶勾稽关系检查规则定义（从 YAML 加载）。"""

    id: str
    name: str
    description: str
    metric_a: str                          # 第一个指标（canonical 字段名）
    metric_b: str                          # 第二个指标
    rule_type: str                         # "gap" | "ratio" | "codir"
    anomaly_direction: str                 # "spike" | "drop" | "any"
    threshold_sigma: float = 2.0           # z-score 阈值
    baseline_periods: int = 4              # 基线计算期数（不含当期）
    book_ref: str = ""                     # 《手财》章节引用
    verify_questions: list[str] = field(default_factory=list)  # 排查路径
    # 特殊：对照法定税率而非历史基线
    use_statutory: bool = False
    statutory_rate: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrossRule:
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            metric_a=data["metric_a"],
            metric_b=data["metric_b"],
            rule_type=data.get("rule_type", "gap"),
            anomaly_direction=data.get("anomaly_direction", "any"),
            threshold_sigma=data.get("threshold_sigma", 2.0),
            baseline_periods=data.get("baseline_periods", 4),
            book_ref=data.get("book_ref", ""),
            verify_questions=data.get("verify_questions", []),
            use_statutory=data.get("use_statutory", False),
            statutory_rate=data.get("statutory_rate", 0.0),
        )


@dataclass
class MetricAnomaly:
    """单个勾稽关系检查结果 — 当前值偏离基线的程度。"""

    rule_id: str
    rule_name: str
    z_score: float                         # 正值=高于基线, 负值=低于基线
    current_value: float
    baseline_mean: float
    baseline_std: float
    level: str = "none"                    # "high" | "medium" | "low" | "none"
    book_ref: str = ""
    verify_questions: list[str] = field(default_factory=list)  # 排查路径

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "z_score": round(self.z_score, 2),
            "current_value": round(self.current_value, 4),
            "baseline_mean": round(self.baseline_mean, 4),
            "baseline_std": round(self.baseline_std, 4),
            "level": self.level,
            "book_ref": self.book_ref,
            "verify_questions": self.verify_questions,
        }


@dataclass
class AnomalyPattern:
    """二阶异常模式定义（从 YAML 加载）—— 多异常组合 → 根因假设。"""

    id: str
    name: str
    severity: str                          # "high" | "medium" | "low"
    risk_dimension: str                    # "financial_quality" | "earnings_quality" | "credit_risk"
    conditions: list[AnomalyPatternCondition] = field(default_factory=list)
    explanation: str = ""
    verify_questions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnomalyPattern:
        conditions = [
            AnomalyPatternCondition.from_dict(c)
            for c in data.get("conditions", [])
        ]
        return cls(
            id=data["id"],
            name=data["name"],
            severity=data.get("severity", "medium"),
            risk_dimension=data.get("risk_dimension", "financial_quality"),
            conditions=conditions,
            explanation=data.get("explanation", ""),
            verify_questions=data.get("verify_questions", []),
        )

    def all_conditions_met(self, anomalies: list[MetricAnomaly]) -> bool:
        """检查所有条件是否都被触发的异常满足。"""
        if not self.conditions:
            return False
        anomaly_map = {a.rule_id: a for a in anomalies}
        for cond in self.conditions:
            if not cond.matches(anomaly_map):
                return False
        return True


@dataclass
class AnomalyPatternCondition:
    """单个模式条件：某条勾稽关系规则必须触发，且方向/阈值匹配。"""

    rule_id: str
    min_zscore: float = 1.5               # |z| 至少这么大
    direction: str = "any"                # "spike" | "drop" | "any"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnomalyPatternCondition:
        return cls(
            rule_id=data["rule_id"],
            min_zscore=data.get("min_zscore", 1.5),
            direction=data.get("direction", "any"),
        )

    def matches(self, anomaly_map: dict[str, MetricAnomaly]) -> bool:
        a = anomaly_map.get(self.rule_id)
        if a is None or a.level == "none":
            return False
        if abs(a.z_score) < self.min_zscore:
            return False
        if self.direction == "spike" and a.z_score <= 0:
            return False
        if self.direction == "drop" and a.z_score >= 0:
            return False
        return True


@dataclass
class PatternMatch:
    """一次成功的模式匹配。"""

    pattern: AnomalyPattern
    triggering_anomalies: list[MetricAnomaly] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern.id,
            "pattern_name": self.pattern.name,
            "severity": self.pattern.severity,
            "risk_dimension": self.pattern.risk_dimension,
            "explanation": self.pattern.explanation,
            "verify_questions": self.pattern.verify_questions,
            "triggering_rules": [a.rule_id for a in self.triggering_anomalies],
        }


@dataclass
class AnomalyReport:
    """异常检测完整报告。"""

    symbol: str
    period: str
    anomalies: list[MetricAnomaly] = field(default_factory=list)
    pattern_matches: list[PatternMatch] = field(default_factory=list)
    baseline_info: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "period": self.period,
            "anomaly_count": len(self.anomalies),
            "pattern_count": len(self.pattern_matches),
            "anomalies": [a.to_dict() for a in self.anomalies],
            "pattern_matches": [pm.to_dict() for pm in self.pattern_matches],
        }

    def to_fact_values(self) -> dict[str, float]:
        """展平为 fact_values — 供信号规则引用。"""
        from alphabee.agents.anomaly.registry import ANOMALY_PATTERNS, ensure_loaded

        ensure_loaded()
        result: dict[str, float] = {}

        # 逐规则异常标志 + z-score
        for a in self.anomalies:
            result[f"anomaly_{a.rule_id}_zscore"] = a.z_score
            result[f"anomaly_{a.rule_id}_triggered"] = 1.0 if a.level != "none" else 0.0

        # 汇总
        triggered = [a for a in self.anomalies if a.level != "none"]
        result["anomaly_triggered_count"] = float(len(triggered))
        result["anomaly_pattern_count"] = float(len(self.pattern_matches))

        # 最强异常
        if triggered:
            max_a = max(triggered, key=lambda a: abs(a.z_score))
            result["anomaly_max_zscore"] = abs(max_a.z_score)
            high_count = sum(1 for a in triggered if a.level == "high")
            result["anomaly_high_count"] = float(high_count)
        else:
            result["anomaly_max_zscore"] = 0.0
            result["anomaly_high_count"] = 0.0

        # 模式标志
        match_ids = {pm.pattern.id for pm in self.pattern_matches}
        for pid in ANOMALY_PATTERNS:
            result[f"anomaly_pattern_{pid}"] = 1.0 if pid in match_ids else 0.0

        return result
