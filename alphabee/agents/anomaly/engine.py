"""AnomalyEngine — 勾稽关系异常检测引擎。

基于《手把手教你读财报》框架：
1. 一阶：逐条勾稽关系计算 z-score（偏离历史基线的程度）
2. 二阶：多异常组合匹配预定义模式（根因假设）

输入：FinancialFacts.snapshots（多期快照）+ 可选 extra_values
输出：AnomalyReport（可序列化 + 可展平为 fact_values）
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass
from typing import Any

from alphabee.agents.anomaly.models import (
    AnomalyPattern,
    AnomalyReport,
    CrossRule,
    MetricAnomaly,
    PatternMatch,
)
from alphabee.agents.anomaly.registry import (
    ANOMALY_PATTERNS,
    CROSS_RULES,
    ensure_loaded,
)
from alphabee.agents.facts.models import FinancialFacts

logger = structlog.get_logger(__name__)

# 基线标准差最小值，防止除零
_MIN_SIGMA = 1e-6

# z-score → 等级映射
_Z_LEVELS = [
    (2.5, "high"),
    (2.0, "medium"),
    (1.5, "low"),
]


@dataclass
class _ComputedMetric:
    """中间计算结果：一个字段的多期值序列。"""

    values: list[float]  # [current, t-1, t-2, ...]


class AnomalyEngine:
    """勾稽关系异常检测引擎。

    用法::

        engine = AnomalyEngine()
        report = engine.run(financial_facts, extra_values={"employees": 15000})
        # report.to_dict() → JSON
        # report.to_fact_values() → flat dict for signal rules
    """

    def __init__(self) -> None:
        ensure_loaded()
        self.rules: dict[str, CrossRule] = dict(CROSS_RULES)
        self.patterns: dict[str, AnomalyPattern] = dict(ANOMALY_PATTERNS)

    # ── 主入口 ────────────────────────────────────────────────────────

    def run(
        self,
        financial_facts: FinancialFacts,
        extra_values: dict[str, float] | None = None,
    ) -> AnomalyReport:
        """对多期财务数据执行异常检测。

        Args:
            financial_facts: 多期财务快照（snapshots[0] = 最新）。
            extra_values: 不在 FinancialSnapshot 中的字段（如 employees）。

        Returns:
            AnomalyReport 包含逐规则异常和模式匹配结果。
        """
        snapshots = financial_facts.snapshots
        if len(snapshots) < 2:
            return AnomalyReport(
                symbol=financial_facts.stock_code,
                period=snapshots[0].period if snapshots else "",
            )

        extra = extra_values or {}
        anomalies: list[MetricAnomaly] = []

        for rule in self.rules.values():
            try:
                anomaly = self._evaluate_rule(rule, snapshots, extra)
                if anomaly is not None:
                    anomalies.append(anomaly)
            except Exception as exc:
                logger.warning(
                    "anomaly_rule_eval_failed",
                    rule_id=rule.id,
                    error=str(exc),
                )

        # 二阶模式匹配
        matches: list[PatternMatch] = []
        for pattern in self.patterns.values():
            try:
                if pattern.all_conditions_met(anomalies):
                    triggering = [
                        a for a in anomalies
                        if a.rule_id in {c.rule_id for c in pattern.conditions}
                    ]
                    matches.append(PatternMatch(
                        pattern=pattern,
                        triggering_anomalies=triggering,
                    ))
            except Exception as exc:
                logger.warning(
                    "anomaly_pattern_match_failed",
                    pattern_id=pattern.id,
                    error=str(exc),
                )

        logger.info(
            "anomaly_engine_done",
            symbol=financial_facts.stock_code,
            anomaly_count=len(anomalies),
            triggered=sum(1 for a in anomalies if a.level != "none"),
            pattern_count=len(matches),
        )

        return AnomalyReport(
            symbol=financial_facts.stock_code,
            period=snapshots[0].period,
            anomalies=anomalies,
            pattern_matches=matches,
        )

    # ── 规则求值 ──────────────────────────────────────────────────────

    def _evaluate_rule(
        self,
        rule: CrossRule,
        snapshots: list,
        extra: dict[str, float],
    ) -> MetricAnomaly | None:
        """对单条规则求值，返回 MetricAnomaly 或 None（数据不足）。"""
        # ── 处理 codir 类型：两字段各自算 z-score ──
        if rule.rule_type == "codir":
            return self._evaluate_codir(rule, snapshots, extra)

        # ── 提取组合指标的多期值 ──
        current_val, history = self._extract_rule_values(rule, snapshots, extra)
        if current_val is None or len(history) < max(2, rule.baseline_periods // 2):
            return None

        # ── 计算基线 ──
        if rule.use_statutory:
            baseline_mean = rule.statutory_rate
            baseline_std = rule.statutory_rate * 0.05  # 法定税率 5% 波动区间
        else:
            baseline_mean, baseline_std = self._compute_baseline(history)

        if baseline_std < _MIN_SIGMA:
            baseline_std = _MIN_SIGMA

        # ── z-score ──
        z_score = (current_val - baseline_mean) / baseline_std

        # ── 方向过滤 ──
        if rule.anomaly_direction == "spike" and z_score <= 0:
            z_score = 0.0
        elif rule.anomaly_direction == "drop" and z_score >= 0:
            z_score = 0.0

        # ── 等级判定 ──
        level = self._classify_level(z_score, rule.threshold_sigma)

        return MetricAnomaly(
            rule_id=rule.id,
            rule_name=rule.name,
            z_score=z_score,
            current_value=current_val,
            baseline_mean=baseline_mean,
            baseline_std=baseline_std,
            level=level,
            book_ref=rule.book_ref,
            verify_questions=rule.verify_questions,
        )

    def _evaluate_codir(
        self,
        rule: CrossRule,
        snapshots: list,
        extra: dict[str, float],
    ) -> MetricAnomaly | None:
        """处理 codir 类型规则：两指标各自算基线，同时高才触发。"""
        a_values = self._extract_field_values(rule.metric_a, snapshots, extra)
        b_values = self._extract_field_values(rule.metric_b, snapshots, extra)

        if len(a_values) < max(2, rule.baseline_periods // 2 + 1):
            return None
        if len(b_values) < max(2, rule.baseline_periods // 2 + 1):
            return None

        a_cur, a_hist = a_values[0], a_values[1 : 1 + rule.baseline_periods]
        b_cur, b_hist = b_values[0], b_values[1 : 1 + rule.baseline_periods]

        a_mean, a_std = self._compute_baseline(a_hist)
        b_mean, b_std = self._compute_baseline(b_hist)

        a_std = max(a_std, _MIN_SIGMA)
        b_std = max(b_std, _MIN_SIGMA)

        z_a = (a_cur - a_mean) / a_std
        z_b = (b_cur - b_mean) / b_std

        # 两指标都显著高于基线 → 触发
        if z_a < rule.threshold_sigma or z_b < rule.threshold_sigma:
            z_score = 0.0
        else:
            z_score = (z_a + z_b) / 2.0

        level = self._classify_level(z_score, rule.threshold_sigma)

        # current_value 用两个 z-score 和作为综合值
        return MetricAnomaly(
            rule_id=rule.id,
            rule_name=rule.name,
            z_score=z_score,
            current_value=z_a + z_b,  # 综合偏离度
            baseline_mean=rule.threshold_sigma * 2,  # 双阈值
            baseline_std=1.0,
            level=level,
            book_ref=rule.book_ref,
            verify_questions=rule.verify_questions,
        )

    # ── 数据提取 ──────────────────────────────────────────────────────

    def _extract_field_values(
        self,
        field: str,
        snapshots: list,
        extra: dict[str, float],
    ) -> list[float]:
        """提取某个字段的多期值序列 [current, t-1, t-2, ...]。
        优先从 snapshots 提取，fallback 到 extra_values。
        """
        values: list[float] = []
        for s in snapshots:
            val = getattr(s, field, None)
            if val is not None and not (isinstance(val, float) and val != val):  # NaN
                values.append(float(val))
            elif field in extra:
                # extra_values 是单值，只在当前期可用
                values.append(float(extra[field]))
                break
        return values

    def _extract_rule_values(
        self,
        rule: CrossRule,
        snapshots: list,
        extra: dict[str, float],
    ) -> tuple[float | None, list[float]]:
        """提取组合指标的多期值。

        Returns:
            (current_value, history_list[baseline_periods])
        """
        a_vals = self._extract_field_values(rule.metric_a, snapshots, extra)
        b_vals = self._extract_field_values(rule.metric_b, snapshots, extra)

        min_len = min(len(a_vals), len(b_vals))
        if min_len < 2:
            return None, []

        # 逐期计算组合值
        combined: list[float] = []
        for i in range(min_len):
            a = a_vals[i]
            b = b_vals[i]
            if rule.rule_type == "gap":
                combined.append(a - b)
            elif rule.rule_type == "ratio":
                if b != 0:
                    combined.append(a / b)
            else:
                return None, []

        if not combined:
            return None, []

        current = combined[0]
        history = combined[1 : 1 + rule.baseline_periods]
        return current, history

    # ── 统计计算 ──────────────────────────────────────────────────────

    def _compute_baseline(self, values: list[float]) -> tuple[float, float]:
        """计算均值和标准差。"""
        n = len(values)
        if n == 0:
            return 0.0, _MIN_SIGMA
        mean = sum(values) / n
        if n == 1:
            return mean, abs(mean) * 0.1 if mean != 0 else _MIN_SIGMA
        variance = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = variance ** 0.5
        return mean, std

    def _classify_level(self, z_score: float, threshold: float) -> str:
        """z-score → 等级。"""
        abs_z = abs(z_score)
        if abs_z < threshold:
            return "none"
        for z_cut, level in _Z_LEVELS:
            if abs_z >= z_cut:
                return level
        return "low"


# ── 便捷函数 ────────────────────────────────────────────────────────


def run_anomaly_detection(
    financial_facts: FinancialFacts,
    extra_values: dict[str, float] | None = None,
) -> AnomalyReport:
    """便捷入口：创建引擎并运行。"""
    engine = AnomalyEngine()
    return engine.run(financial_facts, extra_values)
