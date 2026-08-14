"""Deterministic market scoring engine (Phase 1, no LLM).

Reuses the ``derived_facts`` Engine semantics — YAML rules + topological sort +
safe-AST formula + thresholds → level/interpretation (see
``alphabee/agents/derived_facts/engine.py`` and ``registry.safe_eval_formula``).
The market-regime rules live in ``alphabee/market_regime/rules/*.yaml``; each
theme file declares multiple named rules (indicator layer → aggregation layer).

``MarketScoreEngine.score``:
  1. computes history-derived features (ERP / PE / PB percentiles over a trailing
     10-year window that never includes future dates, 20-day momentum, yield
     90-day change, turnover / margin vs 20-day averages);
  2. runs the full rule DAG over ``snapshot values + features``;
  3. assembles ``MarketScore`` from per-engine rule results, falling back to a
     renormalized weighted average when a sub-indicator is missing (a missing
     indicator degrades to ``missing_fact``, never to 0);
  4. applies the risk-preference adjustment ([-5, +5], not part of main weights);
  5. maps the total score to a position band via ``position.advise_position``.

Determinism: same input → same output; every rule result is kept in
``MarketScoreResult.rule_results`` for audit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pandas as pd
import yaml

from alphabee.agents.derived_facts.engine import Engine
from alphabee.agents.derived_facts.registry import DerivedFactRule
from alphabee.core import Decision, EvidenceRef, Issue, IssueScope, IssueSeverity
from alphabee.market_regime.models import (
    MarketIndicatorSnapshot,
    MarketScore,
    MarketScoreResult,
    PositionAdvice,
    RegimeSnapshot,
)
from alphabee.market_regime.position import advise_position
from alphabee.utils.pipeline import make_id

RULES_DIR = Path(__file__).resolve().parent / "rules"

# 每引擎的子指标（weight 字段来自 YAML 规则）
ENGINE_SUB_INDICATORS: dict[str, list[str]] = {
    "valuation_score": ["erp_score", "pe_percentile_score", "pb_percentile_score"],
    "trend_score": ["ma_structure_score", "breadth_score", "momentum_score"],
    "liquidity_score": ["rate_cycle_score", "m1_m2_score", "socfin_score"],
}
MARKET_COMPONENTS = ["valuation_score", "trend_score", "liquidity_score"]
RISK_DELTA_RULES = ["turnover_delta", "margin_delta", "etf_delta"]

RuleResult = dict[str, Any]
RuleResults = dict[str, RuleResult]

_PERCENTILE_WINDOW_YEARS = 10


class MarketRegimeRule(DerivedFactRule):
    """DerivedFactRule built from a name + spec dict (multiple rules per YAML file)."""

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.weight = float(spec.get("weight", 0.0))
        self.description = spec.get("description", "")
        self.formula = spec.get("formula", "")
        self.thresholds = spec.get("thresholds", {})
        self.interpretation = spec.get("interpretation", {})
        self.required_facts = spec.get("required_facts", [])
        self.required_derived_facts = spec.get("required_derived_facts", [])
        self.zero_division_policy = spec.get("zero_division_policy", "invalid")
        self.zero_division_error = spec.get("zero_division_error", "division_by_zero")

    def compute(self, fact_values: dict[str, float], interpretation: bool = False) -> dict[str, Any]:
        """Same as ``DerivedFactRule.compute``, but missing inputs degrade to ``missing_fact``.

        The safe AST evaluator raises ``ValueError`` for unknown variables; here we
        re-map that to ``missing_fact`` so downstream aggregation and audits can
        distinguish "no data" from "bad formula / div-by-zero".
        """
        # 关键区分：safe-AST 求值时"未知变量"（公式引用了不存在的 fact）在技术上
        # 报 invalid，但业务上等价于"该事实缺失"。这里把它重新标记为 missing_fact，
        # 使下游聚合/审计能把"没有数据"与"公式错误 / 除零"清楚分开：
        #   - missing_fact → 数据缺失，允许被重归一化恢复；
        #   - invalid / blocked → 公式或依赖问题，禁止被当作缺失恢复。
        result = super().compute(fact_values, interpretation=interpretation)
        if result.get("level") == "invalid" and "Unknown variable" in str(result.get("error", "")):
            result["level"] = "missing_fact"
            result["error"] = f"missing fact: {result.get('error')}"
        return result


def load_rules(rules_dir: str | Path | None = None) -> dict[str, MarketRegimeRule]:
    """Load all market-regime rules from ``rules/*.yaml`` (position.yaml excluded).

    Each theme YAML must contain a top-level ``rules:`` mapping of
    ``rule_name -> spec`` (or a single rule spec with a ``name`` key).
    """
    base = Path(rules_dir) if rules_dir else RULES_DIR
    rules: dict[str, MarketRegimeRule] = {}
    for path in sorted(base.glob("*.yaml")):
        if path.name == "position.yaml":
            continue
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            continue
        if "rules" in data and isinstance(data["rules"], dict):
            specs = data["rules"]
        elif "name" in data:
            specs = {str(data["name"]): data}
        else:
            continue
        for name, spec in specs.items():
            if isinstance(spec, dict):
                rules[str(name)] = MarketRegimeRule(str(name), spec)
    return rules


# ── 历史特征计算（无前视偏差） ─────────────────────────────────────────────


def _restricted_series(
    history: pd.DataFrame | None,
    column: str,
    asof_date: str | None,
) -> pd.Series:
    """Return a dated series of ``column`` with rows after ``asof_date`` excluded."""
    # 业务要点：任何历史序列都必须截断到 asof_date 为止。这是"无前视偏差"的第一道
    # 关口——percentile / momentum 只能使用评分时点之前的数据，否则会引入未来信息，
    # 造成评分系统性乐观（相当于"用后视镜评分"）。
    if history is None or history.empty or column not in history.columns:
        return pd.Series(dtype=float)
    df = history.copy()
    df["_date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["_date", column])
    if asof_date:
        df = df[df["_date"] <= pd.Timestamp(asof_date)]
    values = pd.to_numeric(df[column], errors="coerce").astype(float)
    return pd.Series(values.values, index=df["_date"].values).dropna()


def _trailing_percentile(
    series: pd.Series,
    current: float,
    window_years: int = _PERCENTILE_WINDOW_YEARS,
) -> float | None:
    """Percentile of ``current`` within the trailing ``window_years`` of ``series``.

    Only data on/before the latest series date is used (no look-ahead): the caller
    must have already restricted ``series`` to ``asof_date``.
    """
    # 业务语义：返回"当前值在过去 window_years（默认10年）里的百分位"。
    # 例如当前 ERP 高于过去10年中 90% 的样本 → erp_percentile=0.90，
    # 对应估值规则里"股债利差厚、权益便宜"。窗口只取截至序列最新点的数据，
    # 且调用方已按 asof_date 截断，双重保证无前视。
    if series.empty or pd.isna(current):
        return None
    cutoff = series.index.max() - pd.DateOffset(years=window_years)
    window = series[series.index >= cutoff]
    if window.empty:
        return None
    return float((window <= current).mean())


def _lagged_value(series: pd.Series, days: int) -> float | None:
    """Value of ``series`` roughly ``days`` calendar days before its latest point."""
    # 业务用途：对比"现在 vs 约 days 天前"的变化量（动量、利率变化、社融同比）。
    # 用"取 ≤ target 的最近一笔"而非精确位移，因为序列是日频但可能缺失交易日/假期，
    # 取最近可得值可避免 NaN 传播；数据越密结果越接近精确 interval 回报。
    if series.empty:
        return None
    target = series.index.max() - pd.Timedelta(days=days)
    prior = series[series.index <= target]
    if prior.empty:
        return None
    return float(prior.iloc[-1])


def _vs_avg_pct(series: pd.Series, current: float, days: int = 20) -> float | None:
    """Percent deviation of ``current`` from the trailing ``days`` mean of ``series``."""
    if series.empty or pd.isna(current):
        return None
    cutoff = series.index.max() - pd.Timedelta(days=days)
    window = series[series.index >= cutoff]
    if window.empty:
        return None
    avg = float(window.mean())
    if avg == 0:
        return None
    return round((current - avg) / abs(avg) * 100, 4)


def _mom_pct(series: pd.Series, current: float, days: int = 20) -> float | None:
    """Percent return over the trailing ``days`` (comparing to a value ~days ago)."""
    base = _lagged_value(series, days)
    if base is None or base == 0:
        return None
    return round((current - base) / abs(base) * 100, 4)


def compute_features(
    values: dict[str, float],
    history: pd.DataFrame | None,
    asof_date: str | None = None,
) -> dict[str, float]:
    """Derive history-based feature facts used by indicator rules.

    Returns only features that could be computed; missing ones are simply absent
    so the corresponding rules degrade to ``missing_fact``.
    """
    features: dict[str, float] = {}

    # ── 估值特征 ────────────────────────────────────────────────────────
    # ERP（股债利差）= 沪深300盈利收益率 − 中债10年国债收益率。
    # 这是"股票 vs 债券谁更划算"的核心度量：ERP 越厚 → 股相对债越便宜。
    # 此处计算其过去10年百分位（0-1），供 erp_score 规则消费。
    ep = values.get("hs300_ep_ttm")
    cn_10y = values.get("cn_10y_yield")
    if ep is not None and cn_10y is not None:
        current_erp = ep - cn_10y
        ep_series = _restricted_series(history, "hs300_ep_ttm", asof_date)
        yield_series = _restricted_series(history, "cn_10y_yield", asof_date)
        erp_series = ep_series - yield_series
        erp_series = erp_series.dropna()
        percentile = _trailing_percentile(erp_series, current_erp)
        if percentile is not None:
            features["erp_percentile"] = round(percentile, 4)

    # PE / PB 历史分位：分位越低表示当前估值在过去10年中越便宜。
    # 估值规则用 (1 - percentile) 反向映射 → 越便宜得分越高。
    for column, feature_name in (
        ("hs300_pe_ttm", "pe_percentile"),
        ("hs300_pb", "pb_percentile"),
    ):
        current = values.get(column)
        if current is not None:
            series = _restricted_series(history, column, asof_date)
            percentile = _trailing_percentile(series, current)
            if percentile is not None:
                features[feature_name] = round(percentile, 4)

    # ── 趋势特征 ────────────────────────────────────────────────────────
    # 市场宽度（breadth）：衡量市场"普涨还是分化"。优先用"站上60日线个股占比"，
    # 该指标更贴近趋势的持续性；缺失时才用"上涨家数占比"兜底（后者为单日截面，
    # 噪音更大，仅作退路）。结果直接作为 breadth_score 的输入（百分比）。
    breadth = values.get("breadth_above_ma60_pct")
    if breadth is None:
        breadth = values.get("up_stock_ratio")
    if breadth is not None:
        features["breadth_above_ma60_pct"] = round(float(breadth), 4)

    # 动量：沪深300 近20日涨跌幅（%），趋势规则的微调项。
    # 计算"现在 vs 约20个自然日前"的回报，避免短期均线噪音主导判断。
    close = values.get("hs300_close")
    if close is not None:
        close_series = _restricted_series(history, "hs300_close", asof_date)
        momentum = _mom_pct(close_series, close)
        if momentum is not None:
            features["hs300_mom_20d"] = round(momentum, 4)

    # ── 流动性特征 ──────────────────────────────────────────────────────
    # 利率周期：10年国债收益率近90天变化（百分点）。下降 = 宽松周期（利好权益）。
    # 规则按 < -0.15 / > 0.15 分档映射到 90 / 30 / 60 分。
    if cn_10y is not None:
        yield_series = _restricted_series(history, "cn_10y_yield", asof_date)
        change = _lagged_value(yield_series, 90)
        if change is not None:
            features["yield_90d_change"] = round(cn_10y - change, 4)

    # 社融拐点：本月社融增量 vs 约一年前的同比变化。
    # 业务上"社融看拐点不看绝对值"——扩张/收缩的方向比规模更重要，
    # 因此用同比变化而非绝对量。同比截断在 ±5% 防止极端值扭曲评分。
    socfin = values.get("social_financing_increment")
    if socfin is not None:
        socfin_series = _restricted_series(history, "social_financing_increment", asof_date)
        prev = _lagged_value(socfin_series, 365)
        if prev not in (None, 0):
            # 社融看拐点不看绝对值：同比变化（%）截断在 ±5，映射后落在 0-100
            features["socfin_yoy_change"] = round(_clamp((socfin - prev) / abs(prev) * 100, -5.0, 5.0), 4)

    # ── 风险偏好特征 ────────────────────────────────────────────────────
    # 成交额热度：当前成交额相对近20日均值的偏离百分比。
    # > 0 表示放量（情绪过热，追高风险），< 0 表示缩量（情绪降温）。
    # 只作 risk_preference_delta 调整项，不进主权重。
    turnover = values.get("market_turnover")
    if turnover is not None:
        t_series = _restricted_series(history, "market_turnover", asof_date)
        deviation = _vs_avg_pct(t_series, turnover, days=20)
        if deviation is not None:
            features["turnover_vs_20d_avg_pct"] = deviation

    # 杠杆热度：融资余额相对近20日均值的变化百分比（%）。
    # 融资余额扩张 = 杠杆资金进场（情绪偏热），收缩 = 去杠杆（情绪偏冷）。
    margin = values.get("margin_balance")
    if margin is not None:
        m_series = _restricted_series(history, "margin_balance", asof_date)
        deviation = _vs_avg_pct(m_series, margin, days=20)
        if deviation is not None:
            features["margin_vs_20d_avg_pct"] = deviation

    return features


# ── 评分聚合 ───────────────────────────────────────────────────────────────


def _renormalized(values_and_weights: list[tuple[float, float]]) -> float | None:
    """Weighted average over available (value, weight) pairs; None when none present."""
    # 业务语义：当子指标部分缺失时，用"仅对可得子指标做权重归一化"的加权平均，
    # 而不是把缺失项当 0 分参与计算。
    # 例：趋势引擎有 均线/宽度/动量 三项，若动量缺失，则用
    #   均线*0.3/(0.3+0.4) + 宽度*0.4/(0.3+0.4)
    # 恢复一个近似总分的估计值。这保证单一数据缺失不会把引擎分错误压低，
    # 同时把"有缺失"的事实单独上报（见 missing_facts），便于审计。
    total_weight = sum(weight for _, weight in values_and_weights)
    if total_weight <= 0:
        return None
    return round(sum(value * weight for value, weight in values_and_weights) / total_weight, 2)


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


class MarketScoreEngine:
    """Deterministic Phase 1 scoring engine over the market-regime rule DAG."""

    def __init__(self, rules: dict[str, MarketRegimeRule] | None = None) -> None:
        self.rules = dict(rules) if rules is not None else load_rules()
        self.engine = Engine(rules=cast(dict[str, DerivedFactRule], self.rules))

    # ── 单引擎分数（容忍子指标缺失：缺失项跳过并重新归一化权重） ──────

    def _engine_score(self, results: RuleResults, engine_name: str) -> float | None:
        # 引擎分计算的两条路径：
        #   1) 聚合规则本身成功 → 直接用 YAML 里定义的分项加权（最忠实于规则配置）；
        #   2) 聚合规则因缺输入被降级 → 用 ENGINE_SUB_INDICATORS 声明好的子指标列表
        #      对"可得子指标"重新归一化加权（见 _renormalized），尽力恢复引擎分。
        # 两条路径都只认 level 非 missing/invalid/blocked 的分数，缺失绝不硬填 0。
        rule_result = results.get(engine_name, {})
        value = rule_result.get(engine_name)
        if value is not None and rule_result.get("level") not in ("missing_fact", "invalid", "blocked"):
            return round(_clamp(float(value)), 2)

        pairs: list[tuple[float, float]] = []
        for sub in ENGINE_SUB_INDICATORS.get(engine_name, []):
            sub_result = results.get(sub, {})
            sub_value = sub_result.get(sub)
            if sub_value is not None and sub_result.get("level") not in ("missing_fact", "invalid", "blocked"):
                sub_rule = self.rules.get(sub)
                weight = sub_rule.weight if sub_rule else 0.0
                pairs.append((_clamp(float(sub_value)), weight))
        renormalized = _renormalized(pairs)
        return round(_clamp(renormalized), 2) if renormalized is not None else None

    def _market_score(self, results: RuleResults) -> float | None:
        # 市场总分：与引擎分相同的两层逻辑。
        # 顶层权重即 YAML 中 market_score 规则声明的
        #   估值 30% + 趋势 40% + 流动性 30%。
        # 若某个引擎整体缺失，同样用可得引擎重归一化，而不是给 0。
        rule_result = results.get("market_score", {})
        value = rule_result.get("market_score")
        if value is not None and rule_result.get("level") not in ("missing_fact", "invalid", "blocked"):
            return round(_clamp(float(value)), 2)

        pairs: list[tuple[float, float]] = []
        for engine_name in MARKET_COMPONENTS:
            engine_value = self._engine_score(results, engine_name)
            if engine_value is not None:
                rule = self.rules.get(engine_name)
                pairs.append((engine_value, rule.weight if rule else 0.0))
        renormalized = _renormalized(pairs)
        return round(_clamp(renormalized), 2) if renormalized is not None else None

    def _risk_delta(self, results: RuleResults) -> float:
        # 风险偏好调整项（-5 ~ +5）：
        #   成交额 50% + 融资余额 30% + ETF 资金流 20%（见 risk_preference.yaml）。
        # 设计意图：情绪/资金因子只作"总分的微调"，不进入结构性主权重，
        # 防止短期情绪噪音污染估值/趋势/流动性的中长期判断。
        # 与引擎分不同，缺失的情绪子项按 0 分累加（缺情绪数据≈中性情绪），
        # 因此 _risk_delta 恒有返回值（默认 0 = 中性），总分仍可计算。
        rule_result = results.get("risk_preference_delta", {})
        value = rule_result.get("risk_preference_delta")
        if value is not None and rule_result.get("level") not in ("missing_fact", "invalid", "blocked"):
            return round(_clamp(float(value), -5.0, 5.0), 2)

        total = 0.0
        for sub in RISK_DELTA_RULES:
            sub_result = results.get(sub, {})
            sub_value = sub_result.get(sub)
            if sub_value is not None and sub_result.get("level") not in ("missing_fact", "invalid", "blocked"):
                sub_rule = self.rules.get(sub)
                total += float(sub_value) * (sub_rule.weight if sub_rule else 0.0)
        return round(_clamp(total, -5.0, 5.0), 2)

    @staticmethod
    def _risk_status(delta: float) -> str:
        # 风险偏好状态三分类：|delta| < 1 视为中性（情绪正常），
        # ≥ +1 为升温（positive），≤ -1 为回落（negative）。阈值与
        # risk_preference.yaml 中 risk_preference_delta 的 level 判定保持一致。
        if delta >= 1.0:
            return "positive"
        if delta <= -1.0:
            return "negative"
        return "neutral"

    @staticmethod
    def _missing_facts(results: RuleResults) -> list[str]:
        """Report genuine missing inputs — leaf indicator failures only.

        Blocked aggregates are *recovered* by renormalization and reported
        separately by the caller only when they end up ``None``.
        """
        missing: list[str] = []
        for name, result in results.items():
            if result.get("level") in ("missing_fact", "invalid"):
                missing.append(name)
        return sorted(missing)

    # ── 主入口 ─────────────────────────────────────────────────────────────

    def score(
        self,
        snapshot: MarketIndicatorSnapshot | dict[str, float],
        history: pd.DataFrame | None = None,
        asof_date: str | None = None,
        prev_week_score: float | None = None,
    ) -> MarketScoreResult:
        """Compute the deterministic market score for one snapshot.

        Args:
            snapshot:        ``MarketIndicatorSnapshot`` or a flat ``{canonical: value}`` map.
            history:         date-indexed daily indicator history (used for percentiles /
                             momentum / yield changes). ``None`` → those rules degrade.
            asof_date:       ``YYYY-MM-DD``; history is restricted to rows on/before it.
            prev_week_score: previous week's recommended position (fraction, 0-1) for the
                             weekly delta limit; ``None`` → no restriction.

        Returns:
            ``MarketScoreResult`` containing scores, rule results (audit), missing
            facts, position advice and the ``RegimeSnapshot`` artifact payload.
        """
        # 入参归一化：既接受规范化快照对象，也接受平铺 dict（便于单测/CLI 手输）。
        if isinstance(snapshot, MarketIndicatorSnapshot):
            values = dict(snapshot.values)
            date = snapshot.date or asof_date or ""
        else:
            values = dict(snapshot)
            date = asof_date or ""

        # 打分主流程：
        #   1) compute_features 用历史序列算出 percentile/动量/变化量等派生特征；
        #   2) 特征与快照原始值合并成 fact_values 作为规则引擎输入；
        #   3) 引擎按 YAML 声明的 DAG 拓扑求值全部规则（指标层 → 聚合层），
        #      每个规则产出 值 + level + 解释，全部留在 rule_results 供审计。
        features = compute_features(values, history, asof_date)
        fact_values = {**values, **features}
        results = self.engine.run(list(self.rules.keys()), fact_values=fact_values)

        # 组装各层分数：引擎分 → 市场总分 → 叠加风险偏好调整。
        valuation = self._engine_score(results, "valuation_score")
        trend = self._engine_score(results, "trend_score")
        liquidity = self._engine_score(results, "liquidity_score")
        market = self._market_score(results)
        risk_delta = self._risk_delta(results)

        # 总分 = 结构性加权分 + 情绪调整项；结构性分缺失（引擎分全缺）时总分无效。
        total = round(_clamp(market + risk_delta), 2) if market is not None else None
        scores = MarketScore(
            valuation_score=valuation,
            trend_score=trend,
            liquidity_score=liquidity,
            risk_preference_delta=risk_delta,
            total_score=total,
        )

        position: PositionAdvice | None = None
        if total is not None:
            position = advise_position(total, prev_week_score=prev_week_score)

        # 缺失清单 = 规则层缺失 + 引擎/总分层降级失败（最终为 None 的），
        # 供报告/审计区分"缺数据"与"公式错误"。_missing_facts 只统计叶子指标，
        # 而聚合层是否真正缺失取决于上一行的重归一化能否恢复。
        missing = self._missing_facts(results)
        for name, value in (("valuation_score", valuation), ("trend_score", trend), ("liquidity_score", liquidity)):
            if value is None:
                missing.append(name)
        if market is None:
            missing.append("market_score")
        missing = sorted(set(missing))
        drivers = self._main_drivers(valuation, trend, liquidity, risk_delta)
        risks = self._risks(total, risk_delta, missing)
        snapshot_payload = RegimeSnapshot(
            date=date,
            scores=scores,
            regime=position.regime if position else "",
            position_low=position.position_low if position else None,
            position_high=position.position_high if position else None,
            weekly_change=position.weekly_change if position else None,
            main_drivers=drivers,
            risks=risks,
        )

        return MarketScoreResult(
            date=date,
            scores=scores,
            risk_preference_status=self._risk_status(risk_delta),
            missing_facts=missing,
            rule_results=results,
            position=position,
            snapshot=snapshot_payload,
        )

    @staticmethod
    def _main_drivers(
        valuation: float | None,
        trend: float | None,
        liquidity: float | None,
        risk_delta: float,
    ) -> list[str]:
        # 主要贡献因子：按"引擎分 × 权重"排序取前两名，说明本周总分主要由谁决定。
        # 权重（估值30/趋势40/流动性30）取自市场总分构成，用于把原始分换算成
        # 对总分的实际贡献。风险偏好调整显著（|delta|≥1）时也作为驱动因子列出，
        # 让"情绪因子把总分推向哪个方向"可见。无任何可得引擎分时给出占位提示。
        drivers: list[str] = []
        contributions = [
            (valuation, 0.30, "估值"),
            (trend, 0.40, "趋势"),
            (liquidity, 0.30, "流动性"),
        ]
        ranked = sorted(
            ((value * weight, label) for value, weight, label in contributions if value is not None),
            reverse=True,
        )
        for contribution, label in ranked[:2]:
            drivers.append(f"{label}引擎为主要贡献（{contribution:.1f}）")
        if abs(risk_delta) >= 1.0:
            drivers.append(f"风险偏好调整 {risk_delta:+.1f}")
        if not drivers:
            drivers.append("关键指标缺失，无法形成有效评分驱动")
        return drivers

    @staticmethod
    def _risks(
        total: float | None,
        risk_delta: float,
        missing_facts: list[str],
    ) -> list[str]:
        # 风险提示生成规则（纯规则，无 LLM）：
        #   1) 总分 < 50 → 落入 position.yaml 的"震荡/风险增加"档位（30-50）或
        #      "熊市"档位（<30），提示降低仓位；
        #   2) 风险偏好 delta ≤ -1 → 成交额/融资余额收缩，市场可能降温；
        #      delta ≥ +1 → 情绪过热，追高风险大；
        #   3) 关键数据缺失会削弱评分可信度，明确提示哪些规则被降级。
        risks: list[str] = []
        if total is not None and total < 50:
            risks.append(f"市场总分 {total:.1f}，处于震荡/风险增加区间，建议降低仓位")
        if risk_delta <= -1.0:
            risks.append("风险偏好回落（成交额/融资余额收缩），警惕市场降温")
        elif risk_delta >= 1.0:
            risks.append("风险偏好升温，追高风险上升，注意节奏")
        if missing_facts:
            risks.append(f"数据缺失：{'、'.join(missing_facts[:5])}（对应规则降级为 missing_fact）")
        if not risks:
            risks.append("暂无明显风险提示")
        return risks


def build_decision_issue(result: MarketScoreResult) -> tuple[Decision, list[Issue]]:
    """Build the orchestrator ``Decision`` + ``Issue`` payloads for a scored week.

    Phase 1.4: the market-regime graph node produces a ``Decision`` (maker
    ``market_score_engine``) carrying the total score, and an ``Issue`` when the
    score is low or key indicators are missing — mirroring the per-symbol pipeline's
    Decision/Issue contract so downstream consumers can slice risk context.
    """
    # 与个股分析流水线共享 Decision/Issue 契约：市场状态快照以"决策者 + 理由 +
    # 证据引用"的形式暴露给下游（个股分析可作为风险敞口上下文读取）。
    # rationale 用自然语言固化总分构成与建议仓位，供 LLM 层直接引用。
    total = result.scores.total_score
    rationale = (
        f"市场总分 {total:.1f}（估值 {result.scores.valuation_score}, "
        f"趋势 {result.scores.trend_score}, 流动性 {result.scores.liquidity_score}, "
        f"风险偏好调整 {result.scores.risk_preference_delta:+.1f}），"
        f"建议仓位区间 [{result.position.position_low:.0%}, {result.position.position_high:.0%}]"
        if total is not None and result.position is not None
        else f"市场评分无法计算，缺失事实：{', '.join(result.missing_facts[:5])}"
    )
    # 证据引用指向评分使用的核心 canonical 指标，使决策可追溯到原始观测
    # （估值 PE/PB、股债利差、M1-M2、市场宽度、成交额、融资余额）。
    evidence = [
        EvidenceRef(ref_id=field, ref_type="observation")
        for field in (
            "hs300_pe_ttm",
            "hs300_pb",
            "cn_10y_yield",
            "m1_m2_gap",
            "breadth_above_ma60_pct",
            "market_turnover",
            "margin_balance",
        )
    ]
    decision = Decision(
        id=make_id("decision"),
        maker="market_score_engine",
        rationale=rationale,
        confidence=1.0,
        evidence_refs=evidence,
    )

    issues: list[Issue] = []
    if total is not None and total < 50:
        issues.append(
            Issue(
                id=make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="market_regime",
                message=f"市场总分 {total:.1f} 低于 50，处于震荡/风险增加区间",
                scope=IssueScope.DATA,
                owner_node="score_market",
            )
        )
    if result.missing_facts:
        issues.append(
            Issue(
                id=make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="missing_data",
                message=f"市场评分关键指标缺失：{', '.join(result.missing_facts[:5])}",
                scope=IssueScope.DATA,
                owner_node="score_market",
            )
        )
    return decision, issues
