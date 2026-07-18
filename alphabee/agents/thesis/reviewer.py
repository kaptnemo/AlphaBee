"""ThesisReviewer — deterministic + optional LLM review of InvestmentThesis quality.

Evaluates each dimension for evidence sufficiency, signal consistency,
context appropriateness, and missing checks.
"""

from __future__ import annotations

import json

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from alphabee.agents.thesis.models import (
    CompanyContext,
    DimensionVerdict,
    InvestmentThesis,
    ThesisReview,
)
from alphabee.agents.thesis.prompts import REVIEWER_SYSTEM_PROMPT, REVIEWER_USER_TEMPLATE
from alphabee.utils import create_chat_model
from alphabee.utils.pipeline import extract_text, parse_json

logger = structlog.get_logger(__name__)

_STATUS_RANK = {"contested": 3, "insufficient": 2, "qualified": 1, "confirmed": 0}


class ThesisReviewer:
    """Two-layer thesis quality auditor.

    Layer 1 (deterministic, always runs): rule-based checks for obvious
    issues like zero-confidence dimensions, single-evidence verdicts, and
    directionally conflicting signals.

    Layer 2 (LLM, opt-in): qualitative review covering evidence sufficiency,
    signal consistency, context appropriateness, and missing checks.
    """

    def __init__(self):
        self._model = None

    @property
    def _llm(self):
        if self._model is None:
            self._model = create_chat_model("agent.thesis.reviewer")
        return self._model

    # ── Public API ──────────────────────────────────────────────────────────

    def review(
        self,
        thesis: InvestmentThesis,
        signal_results: dict[str, dict] | None = None,
        company_context: CompanyContext | None = None,
        use_llm: bool = False,
    ) -> ThesisReview:
        """Audit thesis quality and produce a ``ThesisReview``.

        Args:
            thesis: ``InvestmentThesis`` from ``ThesisEngine``.
            signal_results: Raw signal evaluation results for detail access.
            company_context: Optional industry / lifecycle context.
            use_llm: When True, run Layer 2 LLM review in addition to
                deterministic checks.

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

        return self._build_review(thesis, verdicts, use_llm)

    # ── Layer 1 — deterministic checks ──────────────────────────────────────

    def _layer1_check(
        self,
        dim_id: str,
        dim,
        signal_results: dict,
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
        _HIGH_LEVERAGE_INDUSTRIES = {"银行", "证券", "保险", "房地产", "建筑装饰"}
        _HIGH_RD_INDUSTRIES = {"医药", "半导体", "芯片", "计算机", "通信", "电子"}
        _FINANCIAL_INDUSTRIES = {"银行", "证券", "保险"}

        if dim_id == "credit_risk":
            if not ctx.industry:
                issues.append("缺少行业负债率基准，杠杆判断可能需要校准")
            elif ctx.industry in _HIGH_LEVERAGE_INDUSTRIES:
                issues.append(f"{ctx.industry}行业天然高杠杆，负债率较高可能属于正常经营特征")

        if dim_id == "earnings_quality" and ctx.industry:
            if ctx.industry in _HIGH_RD_INDUSTRIES and (judgment in ("negative", "strong_negative")):
                issues.append(f"{ctx.industry}行业研发投入高，短期盈利弱化可能是战略投入而非经营恶化")
                if status == "contested":
                    status = "qualified"
                    suggested_action = "reconsider_with_context"

        if dim_id == "financial_quality" and ctx.industry:
            if ctx.industry in _FINANCIAL_INDUSTRIES:
                issues.append(f"{ctx.industry}行业财务报表结构与一般企业不同，部分通用财务指标适用性有限")
            if ctx.lifecycle_stage == "growth" and (judgment in ("negative", "strong_negative")):
                issues.append("成长期公司盈利指标偏低可能是加速扩张的正常代价")
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
        signal_results: dict,
        ctx: CompanyContext,
    ) -> dict:
        thesis_json = json.dumps(thesis.to_dict(), ensure_ascii=False, indent=2)

        signal_details: dict = {}
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
        llm_data: dict,
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
    ) -> ThesisReview:
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
        )

    # ── Text / JSON helpers ──────────────────────────────────────────────────

    def _extract_text(self, content) -> str:
        return extract_text(content)

    def _parse_json(self, text: str) -> dict:
        return parse_json(text)
