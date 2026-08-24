"""行业知识工作流契约（industry-context Phase 1）。

本模块是行业知识资产（``IndustryContextArtifact``）与离线工作流（``IndustryContextWorkflow``）
的 typed 契约所在地，取代 Phase 0 时放在 ``orchestrator/contracts.py`` 的扁平 v1 版本。

设计要点（见 docs/industry/industry-context-phase1-design.md）：
- 数值基准按类别分三组字典（valuation / financial / growth），键一律用 canonical 字段名，
  与 ``fact_values`` 注入同构（单一命名空间，无 ``_median`` 之类第二套命名）。
- 匹配键为 ``classification_standard + industry_code``；``sw_code`` 是申万源代码
  （sw 场景下与 ``industry_code`` 相同）。
- 降级契约（B2）：``degraded`` / ``stale`` 默认 False；离线产物 stale 恒 False，
  在线读取过期版本时（Phase 3）由 resolve 节点置 True。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

# 分类体系枚举（主计划 B1：匹配键一律用 classification_standard + 行业代码）
CLASSIFICATION_STANDARDS = ("sw_l1", "sw_l2", "ths", "custom")


class IndustryTarget(BaseModel):
    """一次行业研究工作流的目标：按标的解析或直接指定行业。

    两种形态二选一：
    - ``symbol`` 给定：collect 节点经 ``get_industry_fact()`` 解析行业与估值；
    - ``classification_standard + industry_code`` 给定：直接定位（``name`` 仅展示用）。
    """

    symbol: str | None = None
    classification_standard: str = ""
    industry_code: str = ""  # 匹配键的行业代码（sw_l1 → "801120.SI"）
    industry_name: str = ""  # 展示名（"白酒"），不参与匹配
    sub_industry: str = ""

    def is_direct(self) -> bool:
        return bool(self.classification_standard and self.industry_code)

    def describe(self) -> str:
        if self.symbol:
            return f"symbol={self.symbol}"
        return f"{self.classification_standard}:{self.industry_code}"


@dataclass
class PeriodAlignment:
    """成分股报告期对齐状态（B3 口径风险的周期部分）。"""

    status: str  # aligned / mostly_aligned / mixed
    dominant_period: str | None = None
    period_counts: dict[str, int] = field(default_factory=dict)

    def growth_usable(self) -> bool:
        """growth 基准是否可产出：只有 aligned / mostly_aligned 允许保留。"""
        return self.status in ("aligned", "mostly_aligned")


class IndustryQualitative(BaseModel):
    """定性解释层（v1 保持空或轻量，见 DOMAIN_CONTEXT_ROADMAP 划界 A2）。"""

    lifecycle_stage: str | None = None
    business_model_summary: str = ""
    industry_chain: dict[str, list[str]] = Field(default_factory=dict)
    key_drivers: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    synthesized_by: str = "none"  # none / llm（溯源：是谁产出的定性块）
    synthesis_notes: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.business_model_summary or self.industry_chain or self.key_drivers or self.risk_factors)


class IndustryReview(BaseModel):
    """审核结论（review_industry_context 节点产出）。"""

    status: str = "needs_review"  # approved / needs_review / rejected
    notes: list[str] = Field(default_factory=list)
    confidence: float | None = None  # 0-1 启发式
    stale_after: str | None = None  # 建议过期日（按类别最早到期）
    reviewed_at: str = ""


class IndustryContextArtifact(BaseModel):
    """行业上下文 artifact（v2，行业知识工作流产出，也是在线注入的消费形状）。

    字段语义与主计划 1.3 对齐，差异仅在基准键名：统一用 canonical 字段名
    （``industry_pe_ttm`` / ``industry_avg_roe`` / …），不带 ``_median`` 后缀。
    """

    schema_version: str = "2"
    industry: str = ""
    sub_industry: str = ""
    classification_standard: str = ""  # sw_l1 / sw_l2 / ths / custom
    industry_code: str = ""  # 匹配键的行业代码（sw_l1 → "801120.SI"）
    sw_code: str | None = None  # 申万源代码（sw 场景下与 industry_code 相同）
    as_of_date: str = ""  # 数据截止日 YYYY-MM-DD
    generated_at: str = ""  # ISO8601
    stale_after: str | None = None  # 过期日（按类别最早到期）
    source_refs: list[str] = Field(default_factory=list)
    confidence: float | None = None

    # ── 定性（v1 保持空或轻量）──────────────────────────────────────
    lifecycle_stage: str | None = None
    business_model_summary: str = ""
    industry_chain: dict[str, list[str]] = Field(default_factory=dict)
    key_drivers: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)

    # ── 数值基准（canonical 键，None = 该基准不可得）────────────────
    valuation_benchmarks: dict[str, float | None] = Field(default_factory=dict)
    financial_benchmarks: dict[str, float | None] = Field(default_factory=dict)
    growth_benchmarks: dict[str, float | None] = Field(default_factory=dict)
    peer_universe: list[str] = Field(default_factory=list)
    peer_count: int | None = None

    # ── 审核与降级 ─────────────────────────────────────────────────
    review_status: str | None = None  # approved / needs_review / rejected
    review_notes: list[str] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""
    stale: bool = False  # B2：离线产物初始 False，在线读取过期版本时置 True

    # ── helpers ─────────────────────────────────────────────────────

    def benchmark_fact_values(self) -> dict[str, float]:
        """展平成可注入 ``fact_values`` 的扁平 canonical 字典（丢弃 None）。

        在线节点/引擎注入复用；None 不注入，保证"缺失即回退默认阈值"。
        """
        out: dict[str, float] = {}
        for group in (
            self.valuation_benchmarks,
            self.financial_benchmarks,
            self.growth_benchmarks,
        ):
            for key, value in group.items():
                if value is not None:
                    out[key] = value
        return out

    def all_benchmarks(self) -> dict[str, float | None]:
        """三组基准合并为扁平视图（含 None，供展示/血缘）。"""
        merged: dict[str, float | None] = {}
        for group in (
            self.valuation_benchmarks,
            self.financial_benchmarks,
            self.growth_benchmarks,
        ):
            merged.update(group)
        return merged

    def present_benchmark_categories(self) -> set[str]:
        """实际存在数值的基准类别（用于按类别计算过期，见 persistence）。"""
        present: set[str] = set()
        if any(v is not None for v in self.valuation_benchmarks.values()):
            present.add("valuation")
        if any(v is not None for v in self.financial_benchmarks.values()):
            present.add("financial")
        if any(v is not None for v in self.growth_benchmarks.values()):
            present.add("growth")
        if (
            self.lifecycle_stage
            or self.business_model_summary
            or self.industry_chain
            or self.key_drivers
            or self.risk_factors
        ):
            present.add("qualitative")
        return present


@dataclass
class WorkflowOptions:
    """行业研究工作流的运行选项。"""

    qualitative_mode: str = "none"  # none / llm（v1 默认关闭，见 DOMAIN_CONTEXT_ROADMAP 划界）
    as_of_date: str | None = None  # 覆盖数据截止日（默认今天）
    peer_limit: int = 20  # 成分股抽样上限
    store: Any | None = None  # IndustryProfileStore（persist 节点使用；None → 默认存储）


@dataclass
class IndustryWorkflowState:
    """离线工作流内部状态：节点间传递，最终组装成 artifact。"""

    target: IndustryTarget
    raw_facts: dict[str, Any] = field(default_factory=dict)
    canonical_records: list[dict[str, Any]] = field(default_factory=list)
    period_alignment: PeriodAlignment | None = None
    benchmarks: Any = None  # IndustryBenchmarks | None（延迟 import 避免循环）
    qualitative: IndustryQualitative = field(default_factory=IndustryQualitative)
    review: IndustryReview = field(default_factory=IndustryReview)
    artifact: IndustryContextArtifact | None = None
    persist_path: str | None = None
    errors: list[str] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""
    growth_blocked: bool = False  # B3：报告期口径无法对齐 → growth 基准置空
