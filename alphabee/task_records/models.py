"""Task records — 任务执行记录与规则自蒸馏数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _make_id() -> str:
    return f"task-{uuid4().hex[:12]}"


# ═══════════════════════════════════════════════════════════════════
# 子记录结构
# ═══════════════════════════════════════════════════════════════════


class StageTiming(BaseModel):
    stage: str  # collect_facts / review_thesis / generate_report / finalize
    elapsed_s: float


class SignalResult(BaseModel):
    signal_id: str
    level: str = ""  # high / medium / low / none / blocked / missing_fact
    interpretation: str = ""


class AnomalyResult(BaseModel):
    rule_id: str
    z_score: float = 0.0
    level: str = ""  # high / medium / low / none
    pattern_ids: list[str] = Field(default_factory=list)


class DimensionVerdictSummary(BaseModel):
    dim_id: str
    dim_name: str = ""
    status: str = ""  # confirmed / qualified / insufficient / contested
    evidence_count: int = 0
    judgment: str = ""  # strong_positive / positive / neutral / negative / strong_negative
    score: float = 0.0
    confidence: float = 0.0


class IssueRecord(BaseModel):
    severity: str = ""  # low / medium / high / critical
    category: str = ""  # thesis_gap / missing_data / subagent_failure / etc.
    message: str = ""
    related_step: str = ""


# ═══════════════════════════════════════════════════════════════════
# 主记录
# ═══════════════════════════════════════════════════════════════════


class TaskRecord(BaseModel):
    """单次任务执行的完整记录。"""

    # ── 元信息 ──
    task_id: str = Field(default_factory=_make_id)
    query: str = ""
    symbol: str | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    flags: dict[str, bool] = Field(default_factory=dict)  # {enhance: true, llm_review: false}

    # ── 耗时 ──
    total_duration_s: float = 0.0
    stage_timings: list[StageTiming] = Field(default_factory=list)

    # ── 事实层 ──
    fact_value_count: int = 0
    derived_fact_count: int = 0
    derived_blocked_count: int = 0

    # ── 信号层 ──
    signal_count: int = 0
    signal_results: list[SignalResult] = Field(default_factory=list)

    # ── 异常层 ──
    anomaly_triggered_count: int = 0
    anomaly_pattern_count: int = 0
    anomaly_details: list[AnomalyResult] = Field(default_factory=list)

    # ── 论点层 ──
    thesis_dimensions: list[DimensionVerdictSummary] = Field(default_factory=list)
    overall_judgment: str = ""

    # ── 审查层 ──
    review_overall_status: str = ""  # passed / qualified_pass / needs_revision / blocked
    review_dimension_verdicts: list[DimensionVerdictSummary] = Field(default_factory=list)

    # ── 问题清单 ──
    issues: list[IssueRecord] = Field(default_factory=list)

    # ── 报告元信息 ──
    overall_confidence: str = ""  # high / medium / low
    risk_count: dict[str, int] = Field(default_factory=dict)
    report_raw: dict[str, Any] = Field(default_factory=dict)  # 完整报告 JSON

    # ── 标的上下文 ──
    company_industry: str = ""
    company_lifecycle: str = ""
    company_market_cap: str = ""

    def to_summary(self) -> dict[str, Any]:
        """产出可用作统计输入的精简摘要。"""
        signal_levels = {}
        for s in self.signal_results:
            signal_levels[s.signal_id] = s.level
        issue_categories = {}
        for i in self.issues:
            issue_categories[i.category] = issue_categories.get(i.category, 0) + 1
        return {
            "task_id": self.task_id,
            "symbol": self.symbol,
            "flags": self.flags,
            "total_duration_s": self.total_duration_s,
            "overall_confidence": self.overall_confidence,
            "overall_judgment": self.overall_judgment,
            "review_status": self.review_overall_status,
            "signal_levels": signal_levels,
            "anomaly_triggered": self.anomaly_triggered_count,
            "anomaly_patterns": self.anomaly_pattern_count,
            "issue_count": len(self.issues),
            "issue_categories": issue_categories,
            "industry": self.company_industry,
            "lifecycle": self.company_lifecycle,
        }
