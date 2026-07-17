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
        # 规则和模式在初始化时复制到实例上，
        # 这样引擎执行时面对的是一份稳定快照，避免运行中被全局注册表意外改写。
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
        # anomaly engine 的业务目标不是判断“公司好不好”，
        # 而是先回答“本期三表勾稽是否明显偏离自身历史规律”。
        # 因此它只看多期快照，不直接输出投资建议。
        snapshots = financial_facts.snapshots
        if len(snapshots) < 2:
            # 少于两期时无法形成“当期 vs 历史”基线，
            # 返回空报告而不是制造伪异常。
            return AnomalyReport(
                symbol=financial_facts.stock_code,
                period=snapshots[0].period if snapshots else "",
            )

        extra = extra_values or {}
        anomalies: list[MetricAnomaly] = []

        for rule in self.rules.values():
            try:
                # 一阶规则负责识别“单条勾稽关系”是否异常，
                # 例如利润、现金流、应收、存货等两两关系是否突然背离历史。
                anomaly = self._evaluate_rule(rule, snapshots, extra)
                if anomaly is not None:
                    anomalies.append(anomaly)
            except Exception as exc:
                logger.warning(
                    "anomaly_rule_eval_failed",
                    rule_id=rule.id,
                    error=str(exc),
                )

        # 二阶模式不是重复看 z-score，而是把多个一阶异常拼成“根因假设”：
        # 例如收入增长 + 应收激增 + 现金流恶化，组合起来才更像收入质量问题。
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
            # 业务上我们宁可“暂不评价”，也不在历史样本过薄时硬算异常。
            return None

        # ── 计算基线 ──
        if rule.use_statutory:
            # 少数规则并不适合和历史均值比较，例如税率更适合对照法定税率。
            # 这里等价于把“制度常识”当基线，而不是把历史异常当正常。
            baseline_mean = rule.statutory_rate
            baseline_std = rule.statutory_rate * 0.05  # 法定税率 5% 波动区间
        else:
            baseline_mean, baseline_std = self._compute_baseline(history)

        if baseline_std < _MIN_SIGMA:
            baseline_std = _MIN_SIGMA

        # ── z-score ──
        z_score = (current_val - baseline_mean) / baseline_std

        # ── 方向过滤 ──
        # 某些指标只有“向上异常”或“向下异常”才有业务含义。
        # 例如费用率突然抬升和突然下降，风险指向可能完全不同。
        if rule.anomaly_direction == "spike" and z_score <= 0:
            z_score = 0.0
        elif rule.anomaly_direction == "drop" and z_score >= 0:
            z_score = 0.0

        # ── 等级判定 ──
        # z-score 先判断是否越过规则触发阈值，再映射到统一 high/medium/low，
        # 方便下游 signal engine 用统一风险等级继续处理。
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
        # codir 适用于“两个指标同向异动才有意义”的场景，
        # 单看任何一个都可能只是经营节奏波动，但同时抬升更像共同指向某个风险。
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

        # 两指标都显著高于基线 → 触发。
        # 这里故意采用 AND，而不是 OR，避免把单点噪声误判成模式型异常。
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
                # extra_values 主要给“报表外但影响判断”的当前期字段兜底，
                # 如员工数。它不是完整历史序列，所以只允许补当前期。
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

        # rule_type 决定我们观察的是“差值背离”还是“比例背离”：
        # gap 强调两个指标之间的绝对错位，ratio 强调相对效率或结构变化。
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
        # 这里使用历史窗口自身的均值/波动率，核心假设是：
        # 同一家公司最值得比较的基线，首先是它自己过去几期的正常经营区间。
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
    # 给 orchestrator / notebook / 单测一个统一入口，
    # 避免上层直接依赖 registry 的加载细节。
    engine = AnomalyEngine()
    return engine.run(financial_facts, extra_values)
