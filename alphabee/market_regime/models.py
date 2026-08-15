"""Core data models for the market regime (market-state) subsystem.

Market regime is a *market-level* analysis track, separate from the per-symbol
orchestrator pipeline. These models carry the normalized daily snapshot, the
per-collector output contract used across ``alphabee/collectors/market_regime/``,
and the Phase 1 deterministic score outputs (``MarketScore`` / ``RegimeSnapshot``)
that are consumed via the typed-artifact convention in ``orchestrator/contracts.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


@dataclass
class CollectorOutput:
    """Result of a single market-regime collector.

    Attributes:
        values:   normalized ``{canonical_field: value}`` map (units already canonical).
        source:   provenance label, e.g. ``"akshare:stock_index_pe_lg"``.
        warnings: non-fatal notes (missing fields, fallback dates, etc.).
    """

    values: dict[str, float] = field(default_factory=dict)
    source: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class MarketIndicatorSnapshot:
    """One dated snapshot of normalized market indicators.

    ``values`` only contains canonical fields (see ``schemas/market_regime.yaml``).
    ``sources`` keeps per-field provenance for lineage tracing.
    """

    date: str  # YYYY-MM-DD
    values: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fetched_at: str = ""

    def merge(self, output: CollectorOutput) -> MarketIndicatorSnapshot:
        """Merge a collector output into this snapshot (later wins)."""
        # 业务语义：后 merge 的字段覆盖先前的同名字段。
        # collect_snapshot 中"akshare 先、tushare 后"正是依赖这里的后写覆盖，
        # 实现 auto 模式下 tushare 字段优先。sources 随字段同步记录，保证每个
        # 字段可追溯到数据源（血缘追踪）；warnings 累积各采集器的非致命提示。
        for field_name, value in output.values.items():
            self.values[field_name] = value
            if output.source:
                self.sources[field_name] = output.source
        self.warnings.extend(output.warnings)
        return self


class MarketScore(BaseModel):
    """Deterministic score output of the Phase 1 scoring engine.

    Each engine score is 0–100. ``total_score`` = weighted market score plus the
    risk-preference adjustment (``risk_preference_delta``). Scores are ``None``
    when the underlying indicators are missing (a missing indicator is never
    silently turned into 0, which would wrongly drag the aggregate down).
    """

    # 各引擎得分（0-100）：
    #   valuation_score  估值引擎（权重 30%）——ERP 分位、PE/PB 分位越低越便宜分越高
    #   trend_score      趋势引擎（权重 40%）——均线结构 + 市场宽度 + 20日动量
    #   liquidity_score  流动性引擎（权重 30%）——利率周期 + M1-M2 + 社融拐点
    # None 表示该引擎缺关键指标，绝不静默当 0 处理（避免错误拉低总分）。
    valuation_score: float | None = None
    trend_score: float | None = None
    liquidity_score: float | None = None
    # 风险偏好情绪调整（-5 ~ +5）：成交额/融资余额/ETF 资金流的情绪因子。
    # 只作为调整项叠加，不进主权重，防止情绪污染结构性判断（0 表示中性）。
    risk_preference_delta: float = 0.0
    # 总分 = 加权引擎分（估值30/趋势40/流动性30）+ 风险偏好调整，范围 0-100。
    # 低于 50 即落入"震荡/风险增加"区间，用于生成仓位建议与风险提示。
    total_score: float | None = None


class PositionAdvice(BaseModel):
    """Position-band advice with the single-week ±delta limit applied.

    ``band_low`` / ``band_high`` are the raw band range for the score;
    ``position_low`` / ``position_high`` are the *advised* range after the weekly
    delta limit. ``restricted`` is True when the limit changed the raw band, and
    the suppressed difference is recorded in ``rationale``.
    """

    # 命中的市场阶段名称（如"震荡阶段"/"熊市阶段"），来自 position.yaml 档位。
    regime: str = ""
    # 原始仓位档位区间（0-1 小数）：仅由总分决定，未受单周限制影响的"理想"范围。
    band_low: float = 0.0
    band_high: float = 0.0
    # 施加单周 ±10% 限制后的建议仓位区间（相对上周建议仓位最多移动 ±10%）。
    # None 表示无档位命中（评分无法映射到任何仓位区间）。
    position_low: float | None = None
    position_high: float | None = None
    # 本周总分相对上周总分的变动（仅作展示/追踪用，不参与限制计算）。
    weekly_change: float | None = None
    # 单周限制是否实际压缩了区间（True 说明原始档位与上周建议差距较大）。
    restricted: bool = False
    # 决策理由：未受限时记录"未受限制"，受限时记录被压制的区间差异，
    # 保证分数跳变被文档化而非被静默掩盖。
    rationale: list[str] = Field(default_factory=list)


class RegimeSnapshot(BaseModel):
    """Typed artifact payload for a weekly market-regime evaluation.

    This is the contract produced by the scoring engine and consumed by
    downstream artifact wiring via ``find_artifact_model`` / ``coerce_market_regime``.
    ``explanation`` stays empty in Phase 1 (filled by the Phase 3 explainer).
    """

    date: str = ""
    scores: MarketScore = Field(default_factory=MarketScore)
    regime: str = ""
    position_low: float | None = None
    position_high: float | None = None
    weekly_change: float | None = None
    main_drivers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    explanation: str = ""


class MarketScoreResult(BaseModel):
    """Full deterministic engine result (audit + artifact payloads together).

    ``rule_results`` keeps the raw per-rule output (value / level /
    interpretation) so every score is auditable and the Phase 3 explainer can
    trace drivers back to specific indicators.
    """

    date: str = ""
    scores: MarketScore = Field(default_factory=MarketScore)
    risk_preference_status: str = "neutral"
    missing_facts: list[str] = Field(default_factory=list)
    rule_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    position: PositionAdvice | None = None
    snapshot: RegimeSnapshot | None = None


class RegimeTransition(BaseModel):
    """One six-phase state-machine step (Phase 2.1).

    ``phase`` is the candidate classification from the rule layer; ``confidence``
    is the rule-layer confidence (0-1); ``transition_from`` is the previous week's
    phase. ``transition_valid``/``suspicious`` reflect the Markov constraint layer:
    an illegal jump (e.g. ``高位分歧`` straight to ``趋势启动``) keeps the candidate
    but is flagged ``suspicious`` for manual/LLM review instead of being silently
    accepted.
    """

    date: str = ""
    phase: str = ""
    confidence: float = 0.0
    transition_from: str | None = None
    transition_valid: bool = True
    suspicious: bool = False
    rationale: list[str] = Field(default_factory=list)


class SimilarityHit(BaseModel):
    """One historical analog found by the Phase 2.2 similar-history search."""

    date: str = ""
    phase: str = ""
    distance: float = 0.0
    forward_return: float | None = None
    max_drawdown: float | None = None


class SimilarityResult(BaseModel):
    """Similar-history search output with forward-return statistics.

    ``positive_probability`` / ``median_forward_return`` / ``median_max_drawdown``
    are computed over the top-``k`` hits. ``limitation_note`` always discloses that
    similarity is a statistical reference, never a prediction promise.
    """

    date: str = ""
    phase: str = ""
    features: dict[str, float | None] = Field(default_factory=dict)
    hits: list[SimilarityHit] = Field(default_factory=list)
    sample_size: int = 0
    positive_probability: float | None = None
    median_forward_return: float | None = None
    median_max_drawdown: float | None = None
    limitation_note: str = ""
