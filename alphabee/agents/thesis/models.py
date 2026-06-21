"""Thesis models — 投资论点的核心数据结构。

数据流：
  SignalResults (from SignalEngine)
    → ThesisEngine → ThesisDimension[]
    → CriticEngine → CriticQuestion[]
    → InvestmentThesis (final output)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── 档位与评分映射 ──────────────────────────────────────────────────────

# 信号档位 → 风险触发强度（0.0=无风险，1.0=最高风险）
SIGNAL_LEVEL_TO_SCORE: dict[str, float] = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
    "none": 0.2,   # absence of risk is mild positive evidence (non-zero so thesis_impact.none:positive is reflected)
}

# thesis_impact 方向 → 方向分值（负=负面，正=正面）
IMPACT_TO_DIRECTION: dict[str, float] = {
    "negative": -1.0,
    "slightly_negative": -0.5,
    "neutral": 0.0,
    "slightly_positive": 0.5,
    "positive": 1.0,
}

# 综合评分 → 判断档位
# score ∈ [-1.0, 1.0]，负分表示更多负面信号，正分表示更多正面信号
JUDGMENT_THRESHOLDS: list[tuple[float, str]] = [
    (0.5, "strong_positive"),
    (0.2, "positive"),
    (-0.2, "neutral"),
    (-0.5, "negative"),
    (-1.1, "strong_negative"),  # catch-all
]

# 判断档位中文标签
JUDGMENT_LABELS: dict[str, str] = {
    "strong_positive": "🟢 非常积极",
    "positive": "🟩 偏积极",
    "neutral": "⬜ 中性",
    "negative": "🟥 偏消极",
    "strong_negative": "🔴 非常消极",
}

# Critic 类型中文标签
CRITIC_CATEGORY_LABELS: dict[str, str] = {
    "evidence_gap": "证据不足",
    "counter_evidence": "反向证据",
    "industry_cycle": "行业周期",
    "comparison": "同行对比",
    "accounting_policy": "会计政策",
    "data_freshness": "数据时效",
    "general": "通用质疑",
}

# Critic 严重度中文标签
CRITIC_SEVERITY_LABELS: dict[str, str] = {
    "critical": "❗ 关键",
    "important": "⚠️ 重要",
    "minor": "ℹ️ 一般",
}


# ── 数据结构 ─────────────────────────────────────────────────────────────


@dataclass
class EvidenceItem:
    """单条支撑证据：来源于某个信号的触发结果。"""

    signal_id: str
    signal_name: str
    level: str                # high / medium / low / none
    impact: str               # negative / slightly_negative / neutral / ...
    interpretation: str = ""  # 信号解释文字

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceItem:
        return cls(
            signal_id=data.get("signal_id", ""),
            signal_name=data.get("signal_name", ""),
            level=data.get("level", ""),
            impact=data.get("impact", ""),
            interpretation=data.get("interpretation", ""),
        )


@dataclass
class ThesisDimension:
    """单个 Thesis 维度的综合判断结果。"""

    id: str
    name: str
    judgment: str             # strong_positive / positive / neutral / negative / strong_negative
    score: float              # 综合评分，[-1.0, 1.0]
    evidence: list[EvidenceItem] = field(default_factory=list)
    interpretation: str = ""
    confidence: float = 1.0   # 0-1，信号覆盖度越高置信度越高

    @classmethod
    def from_dict(cls, data: dict[str, Any], dim_id: str = "") -> ThesisDimension:
        return cls(
            id=data.get("id", dim_id),
            name=data.get("name", ""),
            judgment=data.get("judgment", "neutral"),
            score=float(data.get("score", 0.0)),
            evidence=[EvidenceItem.from_dict(e) for e in data.get("evidence", [])],
            interpretation=data.get("interpretation", ""),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class CriticQuestion:
    """单条质疑追问。"""

    question: str
    source: str               # 来源：信号 ID 或维度 ID
    category: str             # evidence_gap / counter_evidence / industry_cycle / comparison / accounting_policy / general
    severity: str             # critical / important / minor

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CriticQuestion:
        return cls(
            question=data.get("question", ""),
            source=data.get("source", ""),
            category=data.get("category", "general"),
            severity=data.get("severity", "minor"),
        )


@dataclass
class InvestmentThesis:
    """投资论点综合结构 — ThesisEngine 的最终输出。"""

    symbol: str
    period: str               # 分析周期，如 "2023年报"
    dimensions: dict[str, ThesisDimension] = field(default_factory=dict)
    primary_risks: list[str] = field(default_factory=list)
    overall_judgment: str = "neutral"
    overall_score: float = 0.0
    critic_questions: list[CriticQuestion] = field(default_factory=list)
    signal_count: int = 0
    triggered_signal_count: int = 0  # level != "none" 的信号数

    def to_dict(self) -> dict[str, Any]:
        """序列化为可传递给 LLM 工具的字典结构。"""
        return {
            "symbol": self.symbol,
            "period": self.period,
            "overall_judgment": self.overall_judgment,
            "overall_score": round(self.overall_score, 3),
            "signal_count": self.signal_count,
            "triggered_signal_count": self.triggered_signal_count,
            "dimensions": {
                dim_id: {
                    "id": d.id,
                    "name": d.name,
                    "judgment": d.judgment,
                    "score": round(d.score, 3),
                    "confidence": round(d.confidence, 2),
                    "interpretation": d.interpretation,
                    "evidence": [
                        {
                            "signal_id": e.signal_id,
                            "signal_name": e.signal_name,
                            "level": e.level,
                            "impact": e.impact,
                            "interpretation": e.interpretation,
                        }
                        for e in d.evidence
                    ],
                }
                for dim_id, d in self.dimensions.items()
            },
            "primary_risks": self.primary_risks,
            "critic_questions": [
                {
                    "question": q.question,
                    "source": q.source,
                    "category": q.category,
                    "severity": q.severity,
                }
                for q in self.critic_questions
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvestmentThesis:
        """从 ``to_dict()`` 的输出（或 artifact value 中的 ``thesis`` 子字典）重建实例。

        兼容两种输入形态：
        - **扁平格式**：``to_dict()`` 的直接输出，顶层包含 ``symbol``、``dimensions`` 等字段。
        - **artifact 包裹格式**：orchestrator artifact value 的格式为
          ``{"thesis": {...}, "enhanced": ..., ...}``，此时先取 ``data["thesis"]`` 再解析。

        Args:
            data: 任意一种格式的字典。若为空则返回空 InvestmentThesis。

        Returns:
            重建的 ``InvestmentThesis`` 实例。
        """
        if not data:
            return cls(symbol="", period="")

        # 自动处理 artifact 包裹格式
        if "thesis" in data and isinstance(data["thesis"], dict) and "symbol" not in data:
            data = data["thesis"]

        dimensions = {
            dim_id: ThesisDimension.from_dict(d, dim_id=dim_id)
            for dim_id, d in data.get("dimensions", {}).items()
        }
        critic_questions = [
            CriticQuestion.from_dict(cq)
            for cq in data.get("critic_questions", [])
        ]

        return cls(
            symbol=data.get("symbol", ""),
            period=data.get("period", ""),
            dimensions=dimensions,
            primary_risks=list(data.get("primary_risks", [])),
            overall_judgment=data.get("overall_judgment", "neutral"),
            overall_score=float(data.get("overall_score", 0.0)),
            critic_questions=critic_questions,
            signal_count=int(data.get("signal_count", 0)),
            triggered_signal_count=int(data.get("triggered_signal_count", 0)),
        )


def score_to_judgment(score: float) -> str:
    """将 [-1.0, 1.0] 综合评分转换为判断档位。"""
    for threshold, judgment in JUDGMENT_THRESHOLDS:
        if score >= threshold:
            return judgment
    return "strong_negative"


# ── Enhancement-layer models (LLM post-processing) ──────────────────────────


@dataclass
class CompanyContext:
    """Target company context passed to the LLM enhancer.

    All fields are optional — the enhancer handles missing data by requesting
    clarification rather than fabricating it.
    """

    name: str = ""
    symbol: str = ""
    industry: str = ""
    sub_industry: str = ""
    market_cap_category: str = ""  # "large" | "mid" | "small"
    lifecycle_stage: str = ""  # "growth" | "mature" | "decline" | "cyclical"
    business_model_summary: str = ""

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class CrossSignalPattern:
    """A cross-signal pattern discovered by the LLM enhancer.

    These are qualitative insights the deterministic engine cannot produce
    because it evaluates each signal independently.
    """

    pattern_name: str
    """Human-readable name, e.g. '以账期换增长' or '高杠杆下的利润虚增'."""

    signals_involved: list[str] = field(default_factory=list)
    """Signal IDs that together form this pattern."""

    narrative: str = ""
    """LLM-generated explanation of the pattern and its investment implications."""

    severity_modifier: str = "unchanged"
    """How this pattern adjusts the risk: 'amplified' | 'mitigated' | 'unchanged'."""


@dataclass
class EnhancedThesis:
    """Thesis enriched by LLM with cross-signal patterns and context-aware analysis.

    Wraps the deterministic InvestmentThesis and adds:
    - Cross-signal patterns the deterministic engine cannot detect
    - Industry/lifecycle context-aware calibration notes
    - User-intent-adapted summary
    - Confidence disclaimer for LLM-generated content
    """

    deterministic_thesis: InvestmentThesis | None = None
    cross_signal_patterns: list[CrossSignalPattern] = field(default_factory=list)
    context_notes: str = ""
    """Industry/lifecycle/business-model notes that affect interpretation."""

    intent_adjusted_summary: str = ""
    """Summary re-weighted for the user's analytical intent."""

    llm_confidence_note: str = ""
    """Disclaimer about limitations of LLM-generated content."""

    enhancement_applied: bool = False
    """Whether LLM enhancement was actually performed."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enhancement_applied": self.enhancement_applied,
            "cross_signal_patterns": [
                {
                    "pattern_name": p.pattern_name,
                    "signals_involved": p.signals_involved,
                    "narrative": p.narrative,
                    "severity_modifier": p.severity_modifier,
                }
                for p in self.cross_signal_patterns
            ],
            "context_notes": self.context_notes,
            "intent_adjusted_summary": self.intent_adjusted_summary,
            "llm_confidence_note": self.llm_confidence_note,
        }


# ── Thesis Review models ────────────────────────────────────────────────────


@dataclass
class DimensionVerdict:
    """Single-dimension review verdict from ``ThesisReviewer``."""

    dimension_id: str          # "financial_quality" / "earnings_quality" / "credit_risk"
    dimension_name: str        # "财务质量" / "盈利质量" / "信用风险"

    status: str                # confirmed | qualified | insufficient | contested
    evidence_count: int
    key_evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)

    conflicting_signals: list[str] = field(default_factory=list)
    conflict_description: str = ""

    original_judgment: str = ""
    original_score: float = 0.0
    suggested_action: str = ""  # accept | downgrade_confidence | reconsider_with_context | needs_more_data

    issues: list[str] = field(default_factory=list)


@dataclass
class ThesisReview:
    """Complete thesis review result from ``ThesisReviewer``."""

    symbol: str
    period: str
    dimension_verdicts: dict[str, DimensionVerdict] = field(default_factory=dict)

    overall_status: str = "passed"  # passed | qualified_pass | needs_revision | blocked
    overall_rationale: str = ""

    blocking_issues: list[str] = field(default_factory=list)
    warning_issues: list[str] = field(default_factory=list)
    llm_review_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "period": self.period,
            "overall_status": self.overall_status,
            "overall_rationale": self.overall_rationale,
            "llm_review_applied": self.llm_review_applied,
            "blocking_issues": self.blocking_issues,
            "warning_issues": self.warning_issues,
            "dimension_verdicts": {
                dim_id: {
                    "dimension_id": v.dimension_id,
                    "dimension_name": v.dimension_name,
                    "status": v.status,
                    "evidence_count": v.evidence_count,
                    "key_evidence": v.key_evidence,
                    "missing_evidence": v.missing_evidence,
                    "conflicting_signals": v.conflicting_signals,
                    "conflict_description": v.conflict_description,
                    "original_judgment": v.original_judgment,
                    "original_score": v.original_score,
                    "suggested_action": v.suggested_action,
                    "issues": v.issues,
                }
                for dim_id, v in self.dimension_verdicts.items()
            },
        }
