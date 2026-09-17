"""ThesisReviewer — deterministic + optional LLM review of InvestmentThesis quality.

Evaluates each dimension for evidence sufficiency, signal consistency,
context appropriateness, and missing checks.

另含 §8 放大审计（F3）：:class:`AmplificationAudit` / :func:`audit_amplification`
——把 α>1 的 **WEIGHTED** 边（§8.1 四条）的加权方向与信号/冲突证据方向做确定性比对，
由 :meth:`ThesisReviewer.review` 挂到 ``ThesisReview.amplification_audit``（append-only 字段）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from alphabee.agents.thesis.engine import _CONFLICT_PENALTY, INSIGHT_CONFIDENCE_WEIGHTS
from alphabee.agents.thesis.models import (
    IMPACT_TO_DIRECTION,
    SIGNAL_LEVEL_TO_SCORE,
    CompanyContext,
    DimensionVerdict,
    InvestmentThesis,
    ThesisReview,
)
from alphabee.agents.thesis.prompts import REVIEWER_SYSTEM_PROMPT, REVIEWER_USER_TEMPLATE
from alphabee.core.schemas import ArtifactType
from alphabee.industry.names import industry_in_group
from alphabee.utils import create_structured_model
from alphabee.utils.pipeline import extract_text, parse_json

logger = structlog.get_logger(__name__)

__all__ = [
    "AMPLIFICATION_EDGE_AUDITORS",
    "AmplificationAudit",
    "AmplificationContext",
    "ThesisReviewer",
    "attach_amplification_audit",
    "audit_amplification",
]

_STATUS_RANK = {"contested": 3, "insufficient": 2, "qualified": 1, "confirmed": 0}


class ThesisReviewer:
    """Two-layer thesis quality auditor.

    Layer 1 (deterministic, always runs): rule-based checks for obvious
    issues like zero-confidence dimensions, single-evidence verdicts, and
    directionally conflicting signals.

    Layer 2 (LLM, opt-in): qualitative review covering evidence sufficiency,
    signal consistency, context appropriateness, and missing checks.
    """

    def __init__(self) -> None:
        self._model: Any | None = None

    @property
    def _llm(self) -> Any:
        if self._model is None:
            self._model = create_structured_model("agent.thesis.reviewer")
        return self._model

    # ── Public API ──────────────────────────────────────────────────────────

    def review(
        self,
        thesis: InvestmentThesis,
        signal_results: dict[str, dict[str, Any]] | None = None,
        company_context: CompanyContext | None = None,
        use_llm: bool = False,
        amplification: AmplificationContext | None = None,
    ) -> ThesisReview:
        """Audit thesis quality and produce a ``ThesisReview``.

        Args:
            thesis: ``InvestmentThesis`` from ``ThesisEngine``.
            signal_results: Raw signal evaluation results for detail access.
            company_context: Optional industry / lifecycle context.
            use_llm: When True, run Layer 2 LLM review in addition to
                deterministic checks.
            amplification: §8 放大审计输入（F3）。``None``（缺省）→ **严格 no-op**：
                不产出 ``amplification_audit`` 字段值，行为与 F3 之前逐字段一致；
                非 None → 在维度 verdict 之后运行 :func:`attach_amplification_audit`。

        Returns:
            ``ThesisReview`` with per-dimension verdicts and overall status.
        """
        ctx = company_context or CompanyContext()
        signals = signal_results or {}

        verdicts: dict[str, DimensionVerdict] = {}
        for dim_id, dim in thesis.dimensions.items():
            verdicts[dim_id] = self._layer1_check(dim_id, dim, signals, ctx)

        # Layer 2 — LLM qualitative review
        if use_llm:
            try:
                llm_data = self._call_llm(thesis, signals, ctx)
                verdicts = self._merge_llm(verdicts, llm_data)
            except Exception as exc:
                logger.warning("thesis_reviewer_llm_failed", error=str(exc))

        # §8 放大审计（F3）：挂钩点在 ``review()``（经 ``_build_review`` 落地为 append-only 字段），
        # 调用方（review_thesis 节点）只消费 ``review.amplification_audit``，不重复实现判读逻辑。
        review = self._build_review(thesis, verdicts, use_llm, amplification=amplification)

        return review

    # ── Layer 1 — deterministic checks ──────────────────────────────────────

    def _layer1_check(
        self,
        dim_id: str,
        dim: Any,
        signal_results: dict[str, dict[str, Any]],
        ctx: CompanyContext,
    ) -> DimensionVerdict:
        dim_name = dim.name if hasattr(dim, "name") else dim_id
        evidence = dim.evidence if hasattr(dim, "evidence") else []
        confidence = dim.confidence if hasattr(dim, "confidence") else 1.0
        judgment = dim.judgment if hasattr(dim, "judgment") else ""
        score = dim.score if hasattr(dim, "score") else 0.0

        issues: list[str] = []
        status = "confirmed"
        suggested_action = "accept"

        # ── Rule 1: zero confidence ──
        if confidence == 0 or (not evidence):
            issues.append("该维度无信号覆盖，判断不可靠")
            status = "insufficient"
            suggested_action = "needs_more_data"

        # ── Rule 2: single evidence ──
        elif len(evidence) == 1:
            ev = evidence[0]
            sig_name = ev.signal_name if hasattr(ev, "signal_name") else getattr(ev, "signal_id", "?")
            issues.append(f"仅 {sig_name} 一条信号支撑，证据单薄")
            if status == "confirmed":
                status = "qualified"
                suggested_action = "downgrade_confidence"

        # ── Rule 3: conflicting signal directions ──
        if evidence and confidence > 0:
            positive = 0
            negative = 0
            strong_positive = 0
            strong_negative = 0
            for ev in evidence:
                impact = ev.impact if hasattr(ev, "impact") else ""
                ev_level = ev.level if hasattr(ev, "level") else ""
                if impact == "positive":
                    positive += 1
                    # Only count as strong_positive if the risk signal actually fired
                    # (non-none level). A none:positive means "no risk found" — it is
                    # mild positive evidence, not a strong counter-signal.
                    if ev_level and ev_level != "none":
                        strong_positive += 1
                elif impact == "slightly_positive":
                    positive += 1
                elif impact == "negative":
                    negative += 1
                    strong_negative += 1
                elif impact == "slightly_negative":
                    negative += 1
            if positive > 0 and negative > 0:
                issues.append(f"信号方向冲突：{positive} 条正面 vs {negative} 条负面")
                # Severe conflict: strong-vs-strong or multi-signal clashes.
                # Note: none-level "positive" evidence is NOT counted as strong_positive
                # because it represents absence-of-risk, not an affirmative positive finding.
                is_severe = (strong_positive >= 1 and strong_negative >= 1) or (positive >= 2 and negative >= 2)
                if is_severe:
                    if status != "insufficient":
                        status = "contested"
                    suggested_action = "reconsider_with_context"
                else:
                    # Mild conflict: thesis scoring already handles via weighting
                    if status not in ("insufficient", "contested"):
                        status = "qualified"
                    suggested_action = "reconsider_with_context"

        # ── Rule 3b: conflict resolution check — does thesis score already
        # reflect the conflicting evidence? ──
        if status == "contested" and evidence and confidence > 0:
            # Count direction and severity of evidence vs thesis judgment
            _JUDGMENT_DIRECTION: dict[str, str] = {
                "strong_positive": "positive",
                "positive": "positive",
                "neutral": "neutral",
                "strong_negative": "negative",
                "negative": "negative",
            }
            thesis_dir = _JUDGMENT_DIRECTION.get(judgment, "neutral")
            thesis_is_extreme = judgment.startswith("strong_")

            # Keep contested if thesis is extreme BUT strong counter-evidence exists
            if thesis_is_extreme and (
                (thesis_dir == "positive" and strong_negative >= 1)
                or (thesis_dir == "negative" and strong_positive >= 1)
            ):
                issues.append(f"thesis 判断为{judgment}但存在较强反向信号，评分与证据方向存在结构性矛盾")

            # Thesis neutral → conflict already priced in via weighted avg
            elif thesis_dir == "neutral":
                status = "qualified"
                suggested_action = "downgrade_confidence"
                issues.append("信号方向虽有分歧但综合评分已给出中性判断，分歧已在加权平均中消化")

            # Thesis negative but negative signals dominate → thesis aligned
            elif thesis_dir == "negative" and negative >= positive:
                status = "qualified"
                suggested_action = "downgrade_confidence"
                issues.append(
                    "多空信号并存但 negative 方向占优，与 thesis 判断一致，仅需关注正面信号代表的风险缓释因素"
                )

            # Thesis positive but positive signals dominate → thesis aligned
            elif thesis_dir == "positive" and positive >= negative:
                status = "qualified"
                suggested_action = "downgrade_confidence"
                issues.append("多空信号并存但 positive 方向占优，与 thesis 判断一致，仅需关注负面信号的后续演变")

            # Surviving contested: thesis direction contradicts evidence majority
            else:
                issues.append("thesis 判断方向与证据多数方向不一致，存在结构性矛盾")

        # ── Rule 4: industry-context-aware calibration ──
        # 行业组归属统一走行业名规范字典（Phase 2 字段治理，B1）：
        # alphabee/industry/industry_names.yaml groups.{high_leverage, high_rd, financial}
        if dim_id == "credit_risk":
            if not ctx.industry:
                issues.append("缺少行业负债率基准，杠杆判断可能需要校准")
            elif industry_in_group(ctx.industry, "high_leverage"):
                issues.append(f"{ctx.industry}行业天然高杠杆，负债率较高可能属于正常经营特征")

        if dim_id == "earnings_quality" and ctx.industry:
            if industry_in_group(ctx.industry, "high_rd") and (judgment in ("negative", "strong_negative")):
                issues.append(f"{ctx.industry}行业研发投入高，短期盈利弱化可能是战略投入而非经营恶化")
                if status == "contested":
                    status = "qualified"
                    suggested_action = "reconsider_with_context"

        if dim_id == "financial_quality" and ctx.industry:
            if industry_in_group(ctx.industry, "financial"):
                issues.append(f"{ctx.industry}行业财务报表结构与一般企业不同，部分通用财务指标适用性有限")
            if ctx.lifecycle_stage == "growth" and (judgment in ("negative", "strong_negative")):
                issues.append("成长期公司盈利指标偏低可能是加速扩张的正常代价")
                if status == "contested":
                    status = "qualified"
                    suggested_action = "reconsider_with_context"

        # ── Rule 5: business-model lens（COMPANY_TRACK Phase E3）──
        # 按商业模式 archetype 切换审查口径：ODM 不以品牌商毛利标准衡量、
        # 核心零部件商短期盈利弱化可能是产品迭代的战略投入。
        if ctx.business_model == "odm" and dim_id in ("earnings_quality", "financial_quality"):
            if judgment in ("negative", "strong_negative"):
                issues.append("ODM 代工商业模式毛利率低属常态，不应以品牌商毛利标准衡量")
                if status == "contested":
                    status = "qualified"
                    suggested_action = "reconsider_with_context"
        if ctx.business_model == "component" and dim_id == "earnings_quality":
            if judgment in ("negative", "strong_negative"):
                issues.append("核心零部件商研发投入高，短期盈利弱化可能是产品迭代的战略投入")
                if status == "contested":
                    status = "qualified"
                    suggested_action = "reconsider_with_context"

        # Sort by severity: contested > insufficient > qualified > confirmed
        return DimensionVerdict(
            dimension_id=dim_id,
            dimension_name=dim_name,
            status=status,
            evidence_count=len(evidence),
            key_evidence=[e.signal_id if hasattr(e, "signal_id") else str(e) for e in evidence[:3]],
            missing_evidence=[],
            conflicting_signals=[],
            conflict_description="",
            original_judgment=judgment,
            original_score=score,
            suggested_action=suggested_action,
            issues=issues,
        )

    # ── Layer 2 — LLM review ────────────────────────────────────────────────

    def _call_llm(
        self,
        thesis: InvestmentThesis,
        signal_results: dict[str, dict[str, Any]],
        ctx: CompanyContext,
    ) -> dict[str, Any]:
        thesis_json = json.dumps(thesis.to_dict(), ensure_ascii=False, indent=2)

        signal_details: dict[str, Any] = {}
        for sig_id, result in signal_results.items():
            signal_details[sig_id] = {
                "level": result.get("level", "unknown"),
                "interpretation": result.get("interpretation", ""),
                "thesis_impact": result.get("thesis_impact", {}),
            }

        signal_json = json.dumps(signal_details, ensure_ascii=False, indent=2)
        context_json = json.dumps(ctx.to_dict(), ensure_ascii=False, indent=2)

        prompt = REVIEWER_USER_TEMPLATE.format(
            thesis_json=thesis_json,
            signal_details_json=signal_json,
            company_context_json=context_json,
        )

        response = self._llm.invoke(
            [
                SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
        raw_text = self._extract_text(response.content)
        return self._parse_json(raw_text)

    def _merge_llm(
        self,
        verdicts: dict[str, DimensionVerdict],
        llm_data: dict[str, Any],
    ) -> dict[str, DimensionVerdict]:
        dim_reviews = llm_data.get("dimension_reviews", {})
        for dim_id, review in dim_reviews.items():
            if dim_id not in verdicts:
                continue
            v = verdicts[dim_id]

            if not review.get("evidence_sufficient", True):
                if v.status == "confirmed":
                    v.status = "qualified"
                v.missing_evidence.append("证据不充分：" + review.get("evidence_rationale", ""))

            if not review.get("signals_consistent", True):
                # Only promote to contested when Layer 1 found NO conflict at all.
                # If Layer 1 already detected conflicts and downgraded to qualified
                # (e.g. via Rule 3b), don't let LLM re-promote — the deterministic
                # resolution should be respected.
                if v.status == "confirmed":
                    v.status = "contested"
                v.conflict_description = review.get("consistency_rationale", "")

            if not review.get("context_appropriate", True):
                v.issues.append("语境不适配：" + review.get("context_rationale", "需结合行业背景校准"))

            missing = review.get("missing_checks", [])
            if missing:
                v.missing_evidence.extend(missing)

            suggested = review.get("suggested_action", "")
            if suggested:
                v.suggested_action = suggested

            verdicts[dim_id] = v

        return verdicts

    def _build_review(
        self,
        thesis: InvestmentThesis,
        verdicts: dict[str, DimensionVerdict],
        llm_applied: bool,
        amplification: AmplificationContext | None = None,
    ) -> ThesisReview:
        """组装 ``ThesisReview``；``amplification`` 非 None 时同批挂上 §8 放大审计结论。"""
        blocking: list[str] = []
        warnings: list[str] = []

        for v in verdicts.values():
            for issue in v.issues:
                if v.status in ("contested", "insufficient"):
                    blocking.append(f"[{v.dimension_name}] {issue}")
                else:
                    warnings.append(f"[{v.dimension_name}] {issue}")

        # Determine overall status from the worst dimension verdict
        worst_status = "confirmed"
        for v in verdicts.values():
            if _STATUS_RANK.get(v.status, 0) > _STATUS_RANK.get(worst_status, 0):
                worst_status = v.status

        overall_map = {
            "contested": "blocked",
            "insufficient": "needs_revision",
            "qualified": "qualified_pass",
            "confirmed": "passed",
        }
        overall_status = overall_map.get(worst_status, "passed")

        return ThesisReview(
            symbol=thesis.symbol,
            period=thesis.period,
            dimension_verdicts=verdicts,
            overall_status=overall_status,
            overall_rationale=(f"最差维度状态: {worst_status}。{len(blocking)} 项阻断性问题, {len(warnings)} 项警告。"),
            blocking_issues=blocking,
            warning_issues=warnings,
            llm_review_applied=llm_applied,
            # §8 放大审计（F3）：一次性进构造器，不做事后对象变异；``None`` 即"未审计"。
            amplification_audit=_audit_with_context(thesis, amplification),
        )

    # ── Text / JSON helpers ──────────────────────────────────────────────────

    def _extract_text(self, content: Any) -> str:
        return extract_text(content)

    def _parse_json(self, text: str) -> dict[str, Any]:
        return cast(dict[str, Any], parse_json(text))


# ══════════════════════════════════════════════════════════════════════════════
# §8 放大标注 + 加权边审计（F3；§8.1 / §8.2 / §14.4-B）
#
# §8 的核心动作是"给每条『上游 artifact → 下游消费』的边标注传输模式，对 α>1 的边强制审计"。
# 本段落地 §8.1 四条 **WEIGHTED** 边的审计分支，产出 :class:`AmplificationAudit`：
# 它随 ``ThesisReview`` 一起返回（append-only 字段），由 ``review_thesis`` 节点转成
# ``Decision(maker="amplification_audit")``（§8.2 规则 2），方向不一致时再转成一条 D3 偏离。
#
# **为什么是 L1 确定性**：§16 反模式明确"不做 LLM 检测器"；方向比对所需输入
# （insight 的 supporting/counter 计数与 core_view 关键词、signal 的 level×impact 方向分、
# verified conflict 的严重度）全部已是结构化字段，交给 LLM 只会引入不可复现的判定。
# ``use_llm=True``（docstring 口径的 L2 叠加）在 v1 是**显式非目标**：不调用任何 LLM，
# 并会在 rationale 里留下 ``[L2 未启用]`` 标记，避免"看起来做了 L2"的静默错觉。
# ══════════════════════════════════════════════════════════════════════════════

#: 方向判定的死区：均分落在 ±ε 内视为**中性**（不因噪声把两侧方向判成不一致）。
_DIRECTION_EPSILON = 0.05

#: §8.1 四条 WEIGHTED 边的**自动选边**顺序（= §8.1 表序）。显式 ``edge=`` 可直达任一边分支。
_EDGE_PRIORITY: tuple[str, ...] = (
    "insight->thesis",
    "anomaly->fact_values->signal",
    "verified_conflict->dimension_score",
    "insight->report",
)

#: 每条 WEIGHTED 边的上游 artifact 标识（``AmplificationAudit.upstream_artifact`` 的取值来源，
#: 用 :class:`~alphabee.core.schemas.ArtifactType` 的规范值，避免手写字符串漂移）。
_EDGE_UPSTREAM_ARTIFACT: dict[str, str] = {
    "insight->thesis": ArtifactType.INSIGHT_ANALYSIS.value,
    "anomaly->fact_values->signal": f"{ArtifactType.ANOMALY_REPORT.value}->{ArtifactType.SIGNAL_ANALYSIS.value}",
    "verified_conflict->dimension_score": ArtifactType.CONFLICTS_RESULT.value,
    "insight->report": f"{ArtifactType.INSIGHT_ANALYSIS.value}->{ArtifactType.REPORT.value}",
}

#: insight 方向判读的多空关键词（core_view 关键词面；计数面见 :func:`_insight_direction`）。
_BULLISH_KEYWORDS: tuple[str, ...] = ("看好", "积极", "上行", "改善", "增长", "提升", "修复", "超预期", "低估", "机会")
_BEARISH_KEYWORDS: tuple[str, ...] = ("看空", "承压", "下行", "恶化", "下滑", "衰退", "亏损", "高估", "减持", "风险")

#: 异常投影的强度折算（与 ``engine._consume_anomaly_report`` 的 ``level_scale=0.45`` 同源），
#: 仅用于把异常证据折算成与其他证据**同量级**的方向票。
_ANOMALY_LEVEL_SCALE = 0.45


@dataclass
class AmplificationAudit:
    """一条 WEIGHTED 边的放大审计结论（§14.4-B 的字段集合，逐字一致）。"""

    edge: str  # 契约 key，如 "insight->thesis"
    upstream_artifact: str  # 上游 artifact 规范标识
    weight: float | None  # 该边的显式权重（无单一系数 → None）
    direction_consistent: bool  # 加权方向 vs 信号/冲突证据方向是否一致
    rationale: str  # 人类可读判据（含两侧方向与计数，供 Decision / 报告回溯）


@dataclass
class AmplificationContext:
    """审计输入（§14.4-B 的四个参数，由调用方组装后交给 :meth:`ThesisReviewer.review`）。

    存在的理由：``audit_amplification`` 需要三种上游产物，若把它们逐个加成 ``review()`` 的
    位置参数，会让既有调用方与测试替身的签名同时漂移；用显式容器既保住 ``review()`` 的
    向后兼容（``amplification=None`` 即严格 no-op），又让"审计输入"成为可单独构造、可测的值。
    """

    insight: Any | None = None  # InsightArtifact / 其 dict 负载 / {"core_view": ...}
    signals: Any | None = None  # SignalAnalysisArtifact / {"results": {...}}
    conflicts: Any | None = None  # ConflictAnalysisResult / {"conflicts": [...]}
    edge: str | None = None  # None → 自动选边（见 :func:`audit_amplification`）

    def is_empty(self) -> bool:
        """三种上游产物全缺 → 无审计输入（自动选边下必然返回 ``None``）。"""
        return self.insight is None and self.signals is None and self.conflicts is None


# ── 只读取值适配（兼容 pydantic 模型 / dataclass / 裸 dict 三种载荷） ─────────


def _field(payload: Any, key: str, default: Any = None) -> Any:
    """``payload`` 的字段取值：``Mapping`` 走键、其它对象走属性；缺失一律返回 ``default``。"""
    if payload is None:
        return default
    if isinstance(payload, Mapping):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _dimensions(thesis: Any) -> dict[str, Any]:
    """取维度映射：``InvestmentThesis.dimensions`` / ``ThesisArtifact.thesis["dimensions"]`` / 裸 dict。"""
    dimensions = _field(thesis, "dimensions")
    if isinstance(dimensions, Mapping):
        return dict(dimensions)
    inner = _field(thesis, "thesis")  # ThesisArtifact 包裹格式
    if isinstance(inner, Mapping):
        nested = inner.get("dimensions")
        if isinstance(nested, Mapping):
            return dict(nested)
    return {}


def _evidence_items(thesis: Any) -> list[Any]:
    """所有维度的 evidence 条目（含 anomaly 投影产生的条目）。"""
    items: list[Any] = []
    for dim in _dimensions(thesis).values():
        evidence = _field(dim, "evidence")
        if isinstance(evidence, (list, tuple)):
            items.extend(evidence)
    return items


def _signal_results(signals: Any) -> dict[str, Any]:
    """取 ``{signal_id: result}``：``SignalAnalysisArtifact.results`` 或裸 results 映射。"""
    results = _field(signals, "results")
    if isinstance(results, Mapping):
        return dict(results)
    if isinstance(signals, Mapping) and not isinstance(signals.get("results"), Mapping):
        return {k: v for k, v in signals.items() if isinstance(v, Mapping)}
    return {}


def _conflict_items(conflicts: Any) -> list[Any]:
    """取冲突条目列表：``ConflictAnalysisResult.conflicts`` 或 ``{"conflicts": [...]}``。"""
    items = _field(conflicts, "conflicts")
    return list(items) if isinstance(items, (list, tuple)) else []


def _hypotheses(conflict: Any) -> list[Any]:
    items = _field(conflict, "hypotheses")
    return list(items) if isinstance(items, (list, tuple)) else []


def _conflict_severity(conflict: Any) -> str:
    return str(_field(conflict, "severity", "medium") or "medium").strip().lower()


def _sign(value: float) -> int:
    """方向符号（+1 看多 / -1 看空 / 0 中性），带 :data:`_DIRECTION_EPSILON` 死区。"""
    if value > _DIRECTION_EPSILON:
        return 1
    if value < -_DIRECTION_EPSILON:
        return -1
    return 0


def _direction_label(direction: int) -> str:
    return {1: "正面", -1: "负面", 0: "中性"}[direction]


# ── 方向判读（L1 确定性） ────────────────────────────────────────────────────


def _insight_direction(insight: Any) -> tuple[int, str]:
    """insight 方向：``supporting_evidence`` / ``counter_evidence`` 计数（主）+ core_view 关键词（辅）。"""
    if insight is None:
        return 0, "insight 缺失"
    supporting = _field(insight, "supporting_evidence") or []
    counter = _field(insight, "counter_evidence") or []
    core_view = str(_field(insight, "core_view", "") or "")
    n_support = len(supporting) if isinstance(supporting, (list, tuple)) else 0
    n_counter = len(counter) if isinstance(counter, (list, tuple)) else 0
    n_bull = sum(core_view.count(keyword) for keyword in _BULLISH_KEYWORDS)
    n_bear = sum(core_view.count(keyword) for keyword in _BEARISH_KEYWORDS)
    score = float(n_support - n_counter + n_bull - n_bear)
    direction = _sign(score)
    note = (
        f"insight 方向={_direction_label(direction)}（支撑 {n_support} / 反证 {n_counter} 条，"
        f"core_view 多空关键词 {n_bull}/{n_bear}，净分 {score:g}）"
    )
    return direction, note


def _signal_direction(signals: Any) -> tuple[list[float], str]:
    """signal 方向票：``level_score × impact_direction``（与 ThesisEngine 打分同口径，正=看多）。"""
    votes: list[float] = []
    for signal_id, result in _signal_results(signals).items():
        level = str((result or {}).get("level", "") or "")
        level_score = SIGNAL_LEVEL_TO_SCORE.get(level)
        if level_score is None:
            continue  # blocked / missing_fact / unknown → 不参与方向判定（与引擎一致）
        impacts = result.get("thesis_impact") or {}
        for impact in impacts.values() if isinstance(impacts, Mapping) else ():
            direction = IMPACT_TO_DIRECTION.get(str(impact), 0.0)
            if direction != 0.0:
                votes.append(level_score * direction)
    note = f"signal 方向票 {len(votes)} 条" if votes else "signal 方向票 0 条（无可判读信号）"
    return votes, note


def _anomaly_votes(thesis: Any) -> tuple[list[float], int]:
    """异常投影证据的方向票（``source_type == "anomaly"``）与其**留痕缺失**条数。"""
    votes: list[float] = []
    untraced = 0
    for item in _evidence_items(thesis):
        if str(_field(item, "source_type", "") or "") != "anomaly":
            continue
        level_score = SIGNAL_LEVEL_TO_SCORE.get(str(_field(item, "level", "") or ""))
        if level_score is None:
            continue
        direction = IMPACT_TO_DIRECTION.get(str(_field(item, "impact", "") or ""), 0.0)
        if direction != 0.0:
            votes.append(_ANOMALY_LEVEL_SCALE * level_score * direction)
        # §8.1 第 2 行的审计要求"投影时记录 source=anomaly_engine"：留痕 = 可回溯到 pattern id。
        signal_id = str(_field(item, "signal_id", "") or "")
        if not signal_id.startswith("anomaly_pattern:"):
            untraced += 1
    return votes, untraced


def _conflict_votes(conflicts: Any) -> tuple[list[float], list[str]]:
    """已验证 / 部分验证冲突的方向票（固定为负）+ 人类可读判据明细。"""
    votes: list[float] = []
    notes: list[str] = []
    for conflict in _conflict_items(conflicts):
        severity = _conflict_severity(conflict)
        penalty = _CONFLICT_PENALTY.get(severity, 0.0)
        statuses = {str(_field(hypothesis, "status", "") or "").strip().lower() for hypothesis in _hypotheses(conflict)}
        settled = statuses & {"verified", "partial"}
        if not settled:
            continue
        # 扣分幅度用 engine 的同一张表（-0.8 为最强档），折算成 [-1, 0] 的方向票。
        strength = penalty / max(_CONFLICT_PENALTY.values())
        votes.append(-strength)
        notes.append(f"冲突『{_field(conflict, 'theme', '') or '未命名'}』severity={severity} 扣分={penalty:g}")
    return votes, notes


def _evidence_direction(signals: Any, conflicts: Any, thesis: Any) -> tuple[int, str]:
    """信号 / 异常 / 已结算冲突的**合并方向分**（正=看多），供 insight 加权方向比对。"""
    votes, signal_note = _signal_direction(signals)
    anomaly_votes, untraced = _anomaly_votes(thesis)
    conflict_votes, conflict_notes = _conflict_votes(conflicts)
    votes = [*votes, *anomaly_votes, *conflict_votes]
    if not votes:
        return 0, f"{signal_note}；异常投影票 0 条；已验证冲突 0 条 → 证据方向不可判（中性）"
    mean = sum(votes) / len(votes)
    direction = _sign(mean)
    details = [signal_note]
    if anomaly_votes:
        details.append(f"异常投影票 {len(anomaly_votes)} 条（留痕缺失 {untraced} 条）")
    if conflict_notes:
        details.append("；".join(conflict_notes))
    return direction, f"证据方向={_direction_label(direction)}（合并均分 {mean:+.3f}）：{'；'.join(details)}"


def _thesis_direction(thesis: Any) -> tuple[int, str]:
    """thesis 方向：有置信度维度的 score 均值符号（与 ``engine._compute_overall`` 同口径）。"""
    scores = [
        float(_field(dim, "score", 0.0) or 0.0)
        for dim in _dimensions(thesis).values()
        if float(_field(dim, "confidence", 0.0) or 0.0) > 0
    ]
    if not scores:
        return 0, "thesis 无有效维度 → 方向不可判（中性）"
    mean = sum(scores) / len(scores)
    direction = _sign(mean)
    return direction, f"thesis 方向={_direction_label(direction)}（{len(scores)} 个维度均分 {mean:+.3f}）"


# ── 四条 WEIGHTED 边的审计分支（§8.1 逐行） ──────────────────────────────────


def _audit_insight_to_thesis(thesis: Any, insight: Any, signals: Any, conflicts: Any) -> AmplificationAudit:
    """§8.1 第 1 行：``insight -> thesis``（confidence 分档乘法）。

    审计要求（逐字）："``review_thesis`` 必须检查『加权方向是否与信号/冲突证据一致』（现状是盲传）"。
    """
    insight_direction, insight_note = _insight_direction(insight)
    evidence_direction, evidence_note = _evidence_direction(signals, conflicts, thesis)
    consistent = insight_direction == 0 or evidence_direction == 0 or insight_direction == evidence_direction

    confidence = str(_field(insight, "confidence", "medium") or "medium").strip().lower()
    weight = INSIGHT_CONFIDENCE_WEIGHTS.get(confidence, INSIGHT_CONFIDENCE_WEIGHTS["medium"])
    damping = _degradation_cap(insight)
    damping_note = ""
    if damping is not None:
        weight = min(weight, damping)
        damping_note = f"；消费降级产物 → 叠加一档阻尼封顶 ×{damping:g}（§7.2 规则 2）"

    verdict = "一致" if consistent else "**不一致**"
    rationale = (
        f"加权方向审计（{verdict}）：{insight_note}；{evidence_note}；权重 {confidence} → ×{weight:g}{damping_note}"
    )
    return AmplificationAudit(
        edge="insight->thesis",
        upstream_artifact=_EDGE_UPSTREAM_ARTIFACT["insight->thesis"],
        weight=weight,
        direction_consistent=consistent,
        rationale=rationale,
    )


def _audit_anomaly_projection(thesis: Any, insight: Any, signals: Any, conflicts: Any) -> AmplificationAudit:
    """§8.1 第 2 行：``anomaly -> fact_values -> signal``。

    审计要求（逐字）："投影时记录 ``source=anomaly_engine``，review 抽查伪异常率"。
    L1 可确定性核查的部分：**留痕完备性**——每条由异常投影进入 thesis 的证据都必须能回溯到
    pattern id（``anomaly_pattern:<id>``）；留痕缺失条数即"伪异常率"的可核查代理量
    （无痕 = 无法回溯来源 = 潜在伪异常）。其余（异常本身的真伪）不在 L1 可达面内。
    """
    votes, untraced = _anomaly_votes(thesis)
    consistent = untraced == 0
    total = len(votes)
    if total == 0:
        rationale = "异常投影审计：thesis 证据中无 anomaly 投影条目（无可核查的伪异常面）"
    else:
        rate = untraced / total
        rationale = (
            f"异常投影审计（{'一致' if consistent else '**不一致**'}）：投影证据 {total} 条，"
            f"留痕缺失 {untraced} 条（伪异常率代理 {rate:.0%}）"
        )
    return AmplificationAudit(
        edge="anomaly->fact_values->signal",
        upstream_artifact=_EDGE_UPSTREAM_ARTIFACT["anomaly->fact_values->signal"],
        weight=None,  # 投影强度由 pattern severity 决定，无单一显式系数（故不虚构一个数字）
        direction_consistent=consistent,
        rationale=rationale,
    )


def _audit_conflict_penalty(thesis: Any, insight: Any, signals: Any, conflicts: Any) -> AmplificationAudit:
    """§8.1 第 3 行：``verified conflict -> dimension 扣分``。

    审计要求（逐字）："结算状态（verified/partial/rejected）已是先决条件 ✅，补『扣分幅度 vs
    冲突严重度』一致性检查"。L1 口径：**每个已结算（verified/partial）且 severity≥high 的冲突，
    其关联维度不得仍为正向判断**（否则说明扣分没有落到方向上）；权重取 engine 的同一张
    ``_CONFLICT_PENALTY``（不复制第二张表，避免"文档一个值、代码另一个值"）。
    """
    offenders: list[str] = []
    checked = 0
    max_weight: float | None = None
    dimensions = _dimensions(thesis)
    for conflict in _conflict_items(conflicts):
        severity = _conflict_severity(conflict)
        if severity not in ("high", "critical"):
            continue
        statuses = {str(_field(h, "status", "") or "").strip().lower() for h in _hypotheses(conflict)}
        if not (statuses & {"verified", "partial"}):
            continue
        related = _field(conflict, "related_dimensions") or []
        penalty = _CONFLICT_PENALTY.get(severity, 0.0)
        max_weight = penalty if max_weight is None else max(max_weight, penalty)
        theme = str(_field(conflict, "theme", "") or "未命名")
        for dim_id in related if isinstance(related, (list, tuple)) else ():
            dim = dimensions.get(str(dim_id))
            if dim is None:
                continue
            checked += 1
            judgment = str(_field(dim, "judgment", "") or "")
            if judgment in ("positive", "strong_positive"):
                offenders.append(f"维度'{str(dim_id)}' 仍为 {judgment}（冲突『{theme}』severity={severity}）")
    consistent = not offenders
    rationale = (
        f"冲突扣分一致性审计（{'一致' if consistent else '**不一致**'}）：核查 {checked} 个"
        f"（已结算且 severity≥high）冲突 × 维度对，期望扣分幅度={max_weight if max_weight is not None else 0:g}"
    )
    if offenders:
        rationale += "；方向未落地：" + "；".join(offenders[:3])
    return AmplificationAudit(
        edge="verified_conflict->dimension_score",
        upstream_artifact=_EDGE_UPSTREAM_ARTIFACT["verified_conflict->dimension_score"],
        weight=max_weight,
        direction_consistent=consistent,
        rationale=rationale,
    )


def _audit_insight_to_report(thesis: Any, insight: Any, signals: Any, conflicts: Any) -> AmplificationAudit:
    """§8.1 第 4 行：``insight -> report 主线``。

    审计要求（逐字）："report gate 已有 ``cross_source_consistency``，补『主线 vs thesis 判断方向
    一致性』检查"。在 review_thesis 侧先做 thesis 维度（后续报告的骨架）与 insight 主线的方向比对。
    """
    insight_direction, insight_note = _insight_direction(insight)
    thesis_direction, thesis_note = _thesis_direction(thesis)
    consistent = insight_direction == 0 or thesis_direction == 0 or insight_direction == thesis_direction
    rationale = (
        f"主线方向审计（{'一致' if consistent else '**不一致**'}）：{insight_note}；{thesis_note}；"
        "该边无显式系数（α 由观点强度决定），故 weight=None"
    )
    return AmplificationAudit(
        edge="insight->report",
        upstream_artifact=_EDGE_UPSTREAM_ARTIFACT["insight->report"],
        weight=None,
        direction_consistent=consistent,
        rationale=rationale,
    )


#: 边 key → 审计分支（§8.1 四条 WEIGHTED 边一一对应；只读视图，供覆盖守卫逐边断言）。
_EDGE_AUDITORS: dict[str, Callable[[Any, Any, Any, Any], AmplificationAudit]] = {
    "insight->thesis": _audit_insight_to_thesis,
    "anomaly->fact_values->signal": _audit_anomaly_projection,
    "verified_conflict->dimension_score": _audit_conflict_penalty,
    "insight->report": _audit_insight_to_report,
}

#: 公开只读别名：覆盖守卫按 §8.1 表逐边核对分支存在性（不得修改返回值）。
AMPLIFICATION_EDGE_AUDITORS: Mapping[str, Callable[[Any, Any, Any, Any], AmplificationAudit]] = MappingProxyType(
    dict(_EDGE_AUDITORS)
)


def _degradation_cap(insight: Any) -> float | None:
    """insight 是降级产出（``degraded=True`` / ``fallback_tier>0``）→ 返回 ``DAMPING_FACTOR``。

    与 ``engine._apply_insight`` 的 ``min(factor, DAMPING_FACTOR)`` **同源同值**：审计报出的
    权重必须是**实际生效**的权重，否则审计本身就成了新的漂移面。
    """
    degraded = _field(insight, "degraded", None)
    tier = _field(insight, "fallback_tier", 0)
    if degraded is not True and not (isinstance(tier, int) and not isinstance(tier, bool) and tier > 0):
        return None
    from alphabee.orchestrator.services.degradation import (
        DAMPING_FACTOR,  # 函数内 import：避免 agents→orchestrator 的模块级依赖
    )

    return float(DAMPING_FACTOR)


def _edge_applicable(edge: str, thesis: Any, insight: Any, signals: Any, conflicts: Any) -> bool:
    """边是否有可审计输入（自动选边的准入谓词；显式 ``edge=`` 不受此限）。"""
    if edge == "insight->thesis":
        return insight is not None
    if edge == "anomaly->fact_values->signal":
        return bool(_anomaly_votes(thesis)[0])
    if edge == "verified_conflict->dimension_score":
        return any(
            str(_field(hypothesis, "status", "") or "").strip().lower() in ("verified", "partial")
            for conflict in _conflict_items(conflicts)
            for hypothesis in _hypotheses(conflict)
        )
    if edge == "insight->report":
        return insight is not None
    return False


def audit_amplification(
    thesis: InvestmentThesis | None,
    insight: Any | None,
    signals: Any | None,
    conflicts: Any | None,
    *,
    use_llm: bool = False,
    edge: str | None = None,
) -> AmplificationAudit | None:
    """§8 加权边审计（§14.4-B）：给一条 WEIGHTED 边做**确定性**方向一致性判读。

    :param thesis: ``InvestmentThesis``（或其 ``ThesisArtifact`` 包裹 / 裸 dict 载荷）。
    :param insight: 上游 insight 产物（``InsightArtifact`` / dict / ``None``）。
    :param signals: 上游 signal 产物（``SignalAnalysisArtifact`` / ``{"results": ...}`` / ``None``）。
    :param conflicts: 冲突结算产物（``ConflictAnalysisResult`` / ``{"conflicts": [...]}`` / ``None``）。
    :param use_llm: v1 为**显式非目标**（§16 反模式"不做 LLM 检测器"）：本函数在任何取值下都不调用
        LLM；``True`` 时在 rationale 末尾追加 ``[L2 未启用]`` 标记，避免读者误以为做过 L2 叠加。
    :param edge: 显式指定审计的边（§8.1 四条之一）。``None``（缺省）→ **自动选边**：
        按 §8.1 表序（``insight->thesis`` → ``anomaly->fact_values->signal`` →
        ``verified_conflict->dimension_score`` → ``insight->report``）取**第一条判为"不一致"的可用边**；
        若全部一致，则取第一条可用边。这样一次调用最多报告一条真实不一致，且不会把
        可审计的边静默略过。

    :returns: 审计结论；**无任何可用边**（输入缺失）时返回 ``None`` —— 不构造"看起来一致"的空审计，
        也不虚报偏离。``ThesisReview.amplification_audit`` 的 ``| None`` 正是为这两种空态预留的。

    :raises KeyError: ``edge`` 指定的边不在 §8.1 四条内（调用方显式给错 key 属编程错误，
        必须立刻暴露，而不是静默回落）。
    """
    if edge is not None:
        branch = _EDGE_AUDITORS[edge]
        audit = branch(thesis, insight, signals, conflicts)
        return _with_llm_marker(audit) if use_llm else audit

    first_applicable: AmplificationAudit | None = None
    for candidate in _EDGE_PRIORITY:
        if not _edge_applicable(candidate, thesis, insight, signals, conflicts):
            continue
        audit = _EDGE_AUDITORS[candidate](thesis, insight, signals, conflicts)
        if first_applicable is None:
            first_applicable = audit
        if not audit.direction_consistent:
            return _with_llm_marker(audit) if use_llm else audit
    if first_applicable is None:
        return None
    return _with_llm_marker(first_applicable) if use_llm else first_applicable


def _with_llm_marker(audit: AmplificationAudit) -> AmplificationAudit:
    """``use_llm=True`` 的显式留痕（v1 的 L2 是未启用项，不得让人误读为"已叠加 LLM 判读"）。"""
    return AmplificationAudit(
        edge=audit.edge,
        upstream_artifact=audit.upstream_artifact,
        weight=audit.weight,
        direction_consistent=audit.direction_consistent,
        rationale=f"{audit.rationale} [L2 未启用：v1 的 L2 LLM 判读为显式非目标，本结论全部来自 L1 确定性比对]",
    )


def _audit_with_context(
    thesis: InvestmentThesis | None,
    context: AmplificationContext | None,
) -> AmplificationAudit | None:
    """fail-open 核心：``context`` 非 None 时跑 §8 审计，异常只 warning 并返回 ``None``。

    唯一实现点（``_build_review`` 与 :func:`attach_amplification_audit` 共用），保证
    "review() 挂钩"与"节点侧补挂"两条路径产出的结论**逐字段相同**（F3 测试钉住）。
    """
    if context is None or context.is_empty():
        return None
    try:
        return audit_amplification(
            thesis,
            context.insight,
            context.signals,
            context.conflicts,
            use_llm=False,
            edge=context.edge,
        )
    except Exception as exc:  # noqa: BLE001 - 审计必须可失败，绝不打断 run（§14.0 fail-open）
        logger.warning("amplification_audit_failed (fail-open): %s", exc)
        return None


def attach_amplification_audit(
    review: ThesisReview,
    thesis: InvestmentThesis | None,
    context: AmplificationContext | None,
) -> AmplificationAudit | None:
    """把 §8 审计结论挂到已构造好的 ``review.amplification_audit``（append-only 字段）。

    **定位：库级 API，不是生产发射点（F3-2 结转，t64）。** 生产路径**唯一** =
    ``review_thesis`` 节点经 :meth:`ThesisReviewer.review` 的 ``amplification=`` 参数**一次成文**：
    t61 起该关键字由节点**硬传**（开关关闭时传 ``None``），签名不匹配会立刻 ``TypeError`` ——
    早期那套"探测签名、不支持则回退到本函数补挂"的路径**已删除**，不得再引入。本函数因此
    不再承担任何生产分支，保留它只有两个用途：

    1. **等价性钉住**：``test_hook_path_and_attach_path_produce_identical_audits`` 用它产出与
       ``review()`` 挂钩路径**逐字段相同**的结论，防止两条路径口径漂移；
    2. **调用方自行补挂**：不走 ``review_thesis`` 节点的库使用者，拿到 ``ThesisReview`` 后可显式
       把审计挂上去，无需重新实现判读逻辑。

    * ``context is None``（或空 context）→ **严格 no-op**（返回 ``None``，不改 ``review`` 任何字段）；
    * 审计自身异常 → fail-open（见 :func:`_audit_with_context`）。

    :returns: 挂上的审计结论（或 ``None``）。
    """
    audit = _audit_with_context(thesis, context)
    if audit is not None:
        review.amplification_audit = audit
    return audit
