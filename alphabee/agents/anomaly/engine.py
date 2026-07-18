"""AnomalyEngine — 勾稽关系异常检测引擎。

基于《手把手教你读财报》框架：
1. 一阶：逐条勾稽关系计算 z-score（偏离历史基线的程度）
2. 二阶：多异常组合匹配预定义模式（根因假设）

输入：FinancialFacts.snapshots（多期快照）+ 可选 extra_values
输出：AnomalyReport（可序列化 + 可展平为 fact_values）
"""

from __future__ import annotations

from typing import Any

import structlog

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

# 利润表 / 现金流表中的累计口径流量项，进入异常检测前需要尽量还原为单季值。
# 否则 Q1 / 中报 / 三季报 / 年报会因为统计窗口长度不同而天然不可比。
_CUMULATIVE_FLOW_FIELDS = {
    "revenue",
    "operating_profit",
    "net_profit",
    "ebitda",
    "interest_expense",
    "income_tax_expense",
    "total_profit",
    "operating_cashflow",
    "investing_cashflow",
    "financing_cashflow",
    "capex",
    "dividends_paid",
    "salary_paid",
    "free_cashflow",
    "depreciation_amortization",
    "rd_expense",
}
_PREVIOUS_REPORT_SUFFIX = {
    "0630": "0331",
    "0930": "0630",
    "1231": "0930",
}


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
                    triggering = [a for a in anomalies if a.rule_id in {c.rule_id for c in pattern.conditions}]
                    matches.append(
                        PatternMatch(
                            pattern=pattern,
                            triggering_anomalies=triggering,
                        )
                    )
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
        current_val, history, baseline_mode, history_periods = self._extract_rule_values(
            rule,
            snapshots,
            extra,
        )
        if current_val is None or len(history) < max(2, rule.baseline_periods // 2):
            # 业务上我们宁可“暂不评价”，也不在历史样本过薄时硬算异常。
            logger.debug(
                "anomaly_rule_skipped",
                rule_id=rule.id,
                current_period=snapshots[0].period if snapshots else "",
                has_current=current_val is not None,
                history_count=len(history),
                required_history=max(2, rule.baseline_periods // 2),
                baseline_mode=baseline_mode,
                history_periods=history_periods,
            )
            return None

        # ── 计算基线 ──
        if rule.use_statutory:
            # 少数规则并不适合和历史均值比较，例如税率更适合对照法定税率。
            # 这里等价于把“制度常识”当基线，而不是把历史异常当正常。
            baseline_mean = rule.statutory_rate
            baseline_std = rule.statutory_rate * 0.05  # 法定税率 5% 波动区间
            baseline_mode = "statutory"
            history_periods = []
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
            baseline_mode=baseline_mode,
            history_periods=history_periods,
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
        a_series = self._extract_field_series(rule.metric_a, snapshots, extra)
        b_series = self._extract_field_series(rule.metric_b, snapshots, extra)

        if not a_series or not b_series:
            return None

        current_period = snapshots[0].period
        a_map = dict(a_series)
        b_map = dict(b_series)
        if current_period not in a_map or current_period not in b_map:
            logger.debug(
                "anomaly_codir_skipped_missing_current",
                rule_id=rule.id,
                current_period=current_period,
                metric_a=rule.metric_a,
                metric_a_has_current=current_period in a_map,
                metric_b=rule.metric_b,
                metric_b_has_current=current_period in b_map,
            )
            return None

        a_cur = a_map[current_period]
        b_cur = b_map[current_period]
        a_hist, a_mode, a_history_periods = self._build_history_values(
            a_series,
            current_period,
            rule.baseline_periods,
        )
        b_hist, b_mode, b_history_periods = self._build_history_values(
            b_series,
            current_period,
            rule.baseline_periods,
        )

        if len(a_hist) < max(2, rule.baseline_periods // 2):
            logger.debug(
                "anomaly_codir_skipped_short_history",
                rule_id=rule.id,
                metric=rule.metric_a,
                current_period=current_period,
                history_count=len(a_hist),
                required_history=max(2, rule.baseline_periods // 2),
                baseline_mode=a_mode,
                history_periods=a_history_periods,
            )
            return None
        if len(b_hist) < max(2, rule.baseline_periods // 2):
            logger.debug(
                "anomaly_codir_skipped_short_history",
                rule_id=rule.id,
                metric=rule.metric_b,
                current_period=current_period,
                history_count=len(b_hist),
                required_history=max(2, rule.baseline_periods // 2),
                baseline_mode=b_mode,
                history_periods=b_history_periods,
            )
            return None

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
        baseline_mode = "mixed_periods" if "mixed_periods" in {a_mode, b_mode} else "same_period"
        history_periods = a_history_periods if len(a_history_periods) >= len(b_history_periods) else b_history_periods

        # current_value 用两个 z-score 和作为综合值
        return MetricAnomaly(
            rule_id=rule.id,
            rule_name=rule.name,
            z_score=z_score,
            current_value=z_a + z_b,  # 综合偏离度
            baseline_mean=rule.threshold_sigma * 2,  # 双阈值
            baseline_std=1.0,
            level=level,
            baseline_mode=baseline_mode,
            history_periods=history_periods,
            book_ref=rule.book_ref,
            verify_questions=rule.verify_questions,
        )

    # ── 数据提取 ──────────────────────────────────────────────────────

    def _period_suffix(self, period: str) -> str:
        return period[4:] if len(period) >= 8 else ""

    def _is_cumulative_flow_field(self, field: str) -> bool:
        return field in _CUMULATIVE_FLOW_FIELDS

    def _series_suffixes(self, snapshot_by_period: dict[str, Any]) -> set[str]:
        return {self._period_suffix(period) for period in snapshot_by_period if self._period_suffix(period)}

    def _extract_raw_value(
        self,
        field: str,
        snapshot: Any,
    ) -> float | None:
        val = getattr(snapshot, field, None)
        if val is not None and not (isinstance(val, float) and val != val):
            return float(val)
        return None

    def _single_quarter_value(
        self,
        field: str,
        snapshot: Any,
        snapshot_by_period: dict[str, Any],
    ) -> float | None:
        raw = self._extract_raw_value(field, snapshot)
        if raw is None:
            return None
        if not self._is_cumulative_flow_field(field):
            return raw

        suffix = self._period_suffix(snapshot.period)
        if suffix == "0331":
            return raw

        series_suffixes = self._series_suffixes(snapshot_by_period)
        prev_suffix = _PREVIOUS_REPORT_SUFFIX.get(suffix)
        if not prev_suffix:
            return raw

        prev_period = f"{snapshot.period[:4]}{prev_suffix}"
        prev_snapshot = snapshot_by_period.get(prev_period)
        if prev_snapshot is None:
            if series_suffixes == {suffix}:
                # 全序列都只有同一报告后缀时（如全是年报），累计值彼此仍然可比，
                # 此时退回原累计口径比“整条规则失效”更稳妥。
                return raw
            logger.debug(
                "anomaly_single_quarter_missing_prev_snapshot",
                field=field,
                period=snapshot.period,
                expected_prev_period=prev_period,
            )
            return None

        prev_raw = self._extract_raw_value(field, prev_snapshot)
        if prev_raw is None:
            if series_suffixes == {suffix}:
                return raw
            logger.debug(
                "anomaly_single_quarter_missing_prev_value",
                field=field,
                period=snapshot.period,
                expected_prev_period=prev_period,
            )
            return None
        return raw - prev_raw

    def _extract_field_series(
        self,
        field: str,
        snapshots: list,
        extra: dict[str, float],
    ) -> list[tuple[str, float]]:
        """提取字段序列 [(period, value), ...]。

        对累计口径流量项优先还原为单季值；对时点项/同比项/比率项保持原值。
        """
        snapshot_by_period = {s.period: s for s in snapshots if getattr(s, "period", "")}
        values: list[tuple[str, float]] = []
        for s in snapshots:
            val = self._single_quarter_value(field, s, snapshot_by_period)
            if val is not None:
                values.append((s.period, float(val)))
            elif field in extra and not values:
                # extra_values 主要给“报表外但影响判断”的当前期字段兜底，
                # 如员工数。它不是完整历史序列，所以只允许补当前期。
                values.append((getattr(s, "period", ""), float(extra[field])))
                break
        return values

    def _build_history_values(
        self,
        series: list[tuple[str, float]],
        current_period: str,
        baseline_periods: int,
    ) -> tuple[list[float], str, list[str]]:
        """构建历史基线窗口。

        优先选择与当前期同报告后缀（Q1/中报/三季报/年报）的历史值；
        若历史过短，再用其他期值补齐，兼顾可比性与可用性。
        """
        current_suffix = self._period_suffix(current_period)
        same_period: list[tuple[str, float]] = []
        other_periods: list[tuple[str, float]] = []

        for period, value in series:
            if period == current_period:
                continue
            if self._period_suffix(period) == current_suffix:
                same_period.append((period, value))
            else:
                other_periods.append((period, value))

        history_pairs = same_period[:baseline_periods]
        if len(history_pairs) < baseline_periods:
            history_pairs.extend(other_periods[: baseline_periods - len(history_pairs)])

        history_values = [value for _, value in history_pairs]
        history_periods = [period for period, _ in history_pairs]
        baseline_mode = "same_period" if len(history_pairs) == len(same_period[:baseline_periods]) else "mixed_periods"
        return history_values, baseline_mode, history_periods

    def _extract_rule_values(
        self,
        rule: CrossRule,
        snapshots: list,
        extra: dict[str, float],
    ) -> tuple[float | None, list[float], str, list[str]]:
        """提取组合指标的多期值。

        Returns:
            (current_value, history_list[baseline_periods])
        """
        a_series = self._extract_field_series(rule.metric_a, snapshots, extra)
        b_series = self._extract_field_series(rule.metric_b, snapshots, extra)
        if not a_series or not b_series:
            logger.debug(
                "anomaly_rule_missing_series",
                rule_id=rule.id,
                metric_a=rule.metric_a,
                metric_a_points=len(a_series),
                metric_b=rule.metric_b,
                metric_b_points=len(b_series),
            )
            return None, [], "same_period", []

        b_map = dict(b_series)
        aligned: list[tuple[str, float]] = []

        # rule_type 决定我们观察的是“差值背离”还是“比例背离”：
        # gap 强调两个指标之间的绝对错位，ratio 强调相对效率或结构变化。
        for period, a in a_series:
            b = b_map.get(period)
            if b is None:
                continue
            if rule.rule_type == "gap":
                aligned.append((period, a - b))
            elif rule.rule_type == "ratio":
                if b != 0:
                    aligned.append((period, a / b))
            else:
                return None, [], "same_period", []

        if not aligned:
            logger.debug(
                "anomaly_rule_no_aligned_periods",
                rule_id=rule.id,
                metric_a=rule.metric_a,
                metric_b=rule.metric_b,
            )
            return None, [], "same_period", []

        current_period = snapshots[0].period
        combined_map = dict(aligned)
        current = combined_map.get(current_period)
        if current is None:
            logger.debug(
                "anomaly_rule_missing_current_period_value",
                rule_id=rule.id,
                current_period=current_period,
                aligned_periods=[period for period, _ in aligned],
            )
            return None, [], "same_period", []

        history, baseline_mode, history_periods = self._build_history_values(
            aligned,
            current_period,
            rule.baseline_periods,
        )
        return current, history, baseline_mode, history_periods

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
        std = variance**0.5
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
