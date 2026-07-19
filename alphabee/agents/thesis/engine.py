"""ThesisEngine — 将 SignalEngine 输出聚合为投资论点各维度判断。

执行流程：
1. 读取 SignalEngine 返回的 signal_results（每条信号的 level + thesis_impact）
2. 按照 thesis_impact key（如 financial_quality / earnings_quality / credit_risk）
   将有效信号分组到各维度
3. 对每个维度计算加权综合评分：
   score = mean(signal_level_score × impact_direction_score)
   其中 signal_level_score ∈ [0, 1]，impact_direction_score ∈ [-1, 1]
4. 将 score 映射到判断档位（strong_positive / positive / neutral / negative / strong_negative）
5. 从 YAML 维度定义取解释文字
6. 计算置信度（基于可用信号数 / 全部维度信号总数）
"""

from __future__ import annotations

from typing import Any

import structlog

from alphabee.agents.thesis.models import (
    IMPACT_TO_DIRECTION,
    JUDGMENT_LABELS,
    SIGNAL_LEVEL_TO_SCORE,
    EvidenceItem,
    InvestmentThesis,
    ThesisDimension,
    score_to_judgment,
)
from alphabee.agents.thesis.registry import (
    DIMENSION_DEFS,
    ThesisDimensionDef,
    ensure_loaded,
)

logger = structlog.get_logger(__name__)

# 触发信号（level != "none"）对应的最小触发强度阈值，低于此值视为无显著贡献
_TRIGGER_THRESHOLD = 0.05
_SEVERITY_TO_SCORE: dict[str, float] = {
    "critical": 1.0,
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
}
_CONFLICT_PENALTY: dict[str, float] = {
    "critical": 0.8,
    "high": 0.55,
    "medium": 0.25,
    "low": 0.1,
}
_POSITIVE_ANOMALY_PATTERNS = {"efficiency_gain"}
_FINANCIAL_INDUSTRIES = {"银行", "证券", "保险"}
_PROJECT_BASED_KEYWORDS = ("项目", "验收", "军工", "工程", "软件", "集成", "to_b")


class ThesisEngine:
    """将 SignalEngine 评估结果聚合为 InvestmentThesis。

    用法::

        engine = ThesisEngine()
        thesis = engine.run(
            symbol="600519.SH",
            period="2023年报",
            signal_results={
                "revenue_quality_risk": {
                    "level": "high",
                    "interpretation": "...",
                    "thesis_impact": {"financial_quality": "negative", "earnings_quality": "negative"},
                },
                ...
            },
        )
    """

    def __init__(self, dimension_defs: dict[str, ThesisDimensionDef] | None = None):
        ensure_loaded()
        self.dimension_defs: dict[str, ThesisDimensionDef] = (
            dict(dimension_defs) if dimension_defs is not None else dict(DIMENSION_DEFS)
        )

    # ── 主入口 ────────────────────────────────────────────────────────────

    def run(
        self,
        symbol: str,
        period: str,
        signal_results: dict[str, dict],
        anomaly_report: dict[str, Any] | None = None,
        conflict_analysis: dict[str, Any] | None = None,
        verification_results: list[dict[str, Any]] | None = None,
        company_context: dict[str, Any] | None = None,
        insight: dict[str, Any] | None = None,
    ) -> InvestmentThesis:
        """根据信号评估结果生成 InvestmentThesis。

        Args:
            symbol: 股票代码，如 "600519.SH"。
            period: 分析周期，如 "2023年报"。
            signal_results: SignalEngine.run() 的返回值，格式为
                {signal_id: {"level": str, "interpretation": str,
                             "thesis_impact": {dim_key: impact_str}, ...}}

        Returns:
            InvestmentThesis，包含各维度判断、主要风险和 Critic 追问。
        """
        # ── 1. 统计信号基础数据 ────────────────────────────────────────
        total_signals = len(signal_results)
        triggered_signals = sum(
            1 for r in signal_results.values() if r.get("level") in SIGNAL_LEVEL_TO_SCORE and r["level"] != "none"
        )

        # ── 2. 按维度分组信号证据 ──────────────────────────────────────
        # dim_key → list of (signal_id, level_score, impact_direction, evidence)
        dim_contributions: dict[str, list[tuple[float, float, EvidenceItem]]] = {
            dim_def.signal_dimension_key: [] for dim_def in self.dimension_defs.values()
        }

        for signal_id, result in signal_results.items():
            level = result.get("level", "")
            level_score = SIGNAL_LEVEL_TO_SCORE.get(level)
            if level_score is None:
                # blocked / missing_fact / invalid — 跳过，不参与 thesis 计算
                continue

            thesis_impact: dict[str, str] = result.get("thesis_impact") or {}
            interpretation = result.get("interpretation", "")

            for dim_key, impact_str in thesis_impact.items():
                self._append_contribution(
                    dim_contributions=dim_contributions,
                    dim_key=dim_key,
                    source_id=signal_id,
                    source_name=signal_id,
                    level=level,
                    impact=impact_str,
                    interpretation=interpretation,
                    source_type="signal",
                )

        # 二阶 anomaly 直接生成 thesis evidence，并以较低权重参与打分
        self._consume_anomaly_report(dim_contributions, anomaly_report)

        # ── 3. 计算各维度综合评分并构建 ThesisDimension ───────────────
        dimensions: dict[str, ThesisDimension] = {}

        for dim_def in self.dimension_defs.values():
            dim_key = dim_def.signal_dimension_key
            contribs = dim_contributions.get(dim_key, [])

            if not contribs:
                # 无任何信号覆盖，维度评分默认中性，置信度为 0
                score = 0.0
                judgment = "neutral"
                evidence_list: list[EvidenceItem] = []
                confidence = 0.0
            else:
                # 加权平均：每个信号的贡献 = level_score × impact_direction
                effective_scores = [ls * d for ls, d, _ in contribs]
                score = sum(effective_scores) / len(effective_scores)
                judgment = score_to_judgment(score)
                evidence_list = [e for _, _, e in contribs]
                confidence = min(1.0, len(contribs) / max(1, total_signals))

            interpretation = dim_def.get_interpretation(judgment)

            dimensions[dim_def.id] = ThesisDimension(
                id=dim_def.id,
                name=dim_def.name,
                judgment=judgment,
                score=score,
                evidence=evidence_list,
                counter_evidence=[],
                missing_evidence=[],
                context_notes=[],
                interpretation=interpretation,
                confidence=confidence,
            )

        # ── 4. 显式消费 conflict / verification / company_context ───────
        self._apply_conflict_analysis(
            dimensions=dimensions,
            conflict_analysis=conflict_analysis,
            verification_results=verification_results,
        )
        self._apply_company_context(
            dimensions=dimensions,
            company_context=company_context,
        )
        self._apply_insight(
            dimensions=dimensions,
            insight=insight,
        )
        self._refresh_dimensions(dimensions)

        # ── 4. 汇总整体判断 ────────────────────────────────────────────
        overall_judgment, overall_score = self._compute_overall(dimensions)

        # ── 5. 提取主要风险 ────────────────────────────────────────────
        primary_risks = self._extract_primary_risks(
            dimensions,
            signal_results,
            conflict_analysis=conflict_analysis,
            verification_results=verification_results,
        )

        thesis = InvestmentThesis(
            symbol=symbol,
            period=period,
            dimensions=dimensions,
            primary_risks=primary_risks,
            overall_judgment=overall_judgment,
            overall_score=overall_score,
            signal_count=total_signals,
            triggered_signal_count=triggered_signals,
        )

        logger.info(
            "thesis_generated",
            symbol=symbol,
            period=period,
            overall_judgment=overall_judgment,
            overall_score=round(overall_score, 3),
            dimensions=list(dimensions.keys()),
        )

        return thesis

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _compute_overall(
        self,
        dimensions: dict[str, ThesisDimension],
    ) -> tuple[str, float]:
        """等权平均各维度评分，计算整体判断。"""
        scored = [d for d in dimensions.values() if d.confidence > 0]
        if not scored:
            return "neutral", 0.0
        overall_score = sum(d.score for d in scored) / len(scored)
        return score_to_judgment(overall_score), overall_score

    def _extract_primary_risks(
        self,
        dimensions: dict[str, ThesisDimension],
        signal_results: dict[str, dict],
        conflict_analysis: dict[str, Any] | None = None,
        verification_results: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """提取主要风险：level=high 的信号解释 + negative/strong_negative 维度名称。"""
        risks: list[str] = []

        # 高风险信号的解释
        for signal_id, result in signal_results.items():
            if result.get("level") == "high":
                interp = result.get("interpretation", "")
                if interp:
                    risks.append(" ".join(interp.split()))

        # 负面维度名称
        for dim in dimensions.values():
            if dim.judgment in ("negative", "strong_negative") and dim.confidence > 0:
                label = JUDGMENT_LABELS.get(dim.judgment, dim.judgment)
                risks.append(f"{dim.name}评估：{label}")

        if conflict_analysis:
            verify_by_hid = {
                item.get("hypothesis_id", ""): item
                for item in ((verification_results or []) or (conflict_analysis.get("verification_results") or []))
                if item.get("hypothesis_id")
            }
            for conflict in conflict_analysis.get("conflicts", []):
                severity = conflict.get("severity", "")
                if severity not in ("high", "critical"):
                    continue
                for hypothesis in conflict.get("hypotheses", []):
                    status = self._resolve_hypothesis_status(hypothesis, verify_by_hid)
                    if status not in ("verified", "partial"):
                        continue
                    summary = self._resolve_hypothesis_summary(hypothesis, verify_by_hid)
                    if summary:
                        risks.append(summary)
                        break

        return risks

    def _append_contribution(
        self,
        *,
        dim_contributions: dict[str, list[tuple[float, float, EvidenceItem]]],
        dim_key: str,
        source_id: str,
        source_name: str,
        level: str,
        impact: str,
        interpretation: str,
        source_type: str,
        level_scale: float = 1.0,
        source_label: str = "",
    ) -> None:
        if dim_key not in dim_contributions:
            return
        level_score = SIGNAL_LEVEL_TO_SCORE.get(level)
        if level_score is None:
            level_score = _SEVERITY_TO_SCORE.get(level)
        if level_score is None:
            return
        direction = IMPACT_TO_DIRECTION.get(impact, 0.0)
        evidence = EvidenceItem(
            signal_id=source_id,
            signal_name=source_name,
            level=level,
            impact=impact,
            interpretation=" ".join(interpretation.split()) if interpretation else "",
            source_type=source_type,
            source_label=source_label,
        )
        dim_contributions[dim_key].append((level_score * level_scale, direction, evidence))

    def _consume_anomaly_report(
        self,
        dim_contributions: dict[str, list[tuple[float, float, EvidenceItem]]],
        anomaly_report: dict[str, Any] | None,
    ) -> None:
        if not anomaly_report:
            return
        for pattern in anomaly_report.get("pattern_matches", []):
            dim_key = pattern.get("risk_dimension", "")
            if dim_key not in dim_contributions:
                continue
            pattern_id = pattern.get("pattern_id", "")
            severity = pattern.get("severity", "medium")
            if pattern_id in _POSITIVE_ANOMALY_PATTERNS:
                impact = "slightly_positive" if severity == "low" else "positive"
            else:
                impact = "negative" if severity in ("high", "critical") else "slightly_negative"
            self._append_contribution(
                dim_contributions=dim_contributions,
                dim_key=dim_key,
                source_id=f"anomaly_pattern:{pattern_id}",
                source_name=pattern.get("pattern_name", pattern_id),
                level=severity,
                impact=impact,
                interpretation=pattern.get("explanation", ""),
                source_type="anomaly",
                source_label="anomaly_pattern",
                level_scale=0.45,
            )

    def _apply_conflict_analysis(
        self,
        *,
        dimensions: dict[str, ThesisDimension],
        conflict_analysis: dict[str, Any] | None,
        verification_results: list[dict[str, Any]] | None,
    ) -> None:
        if not conflict_analysis:
            return

        verify_items = verification_results or conflict_analysis.get("verification_results") or []
        verify_by_hid = {item.get("hypothesis_id", ""): item for item in verify_items if item.get("hypothesis_id")}

        for conflict in conflict_analysis.get("conflicts", []):
            dim_ids = self._map_conflict_dimensions(conflict)
            if not dim_ids:
                continue
            severity = conflict.get("severity", "medium")
            penalty = _CONFLICT_PENALTY.get(severity, 0.0)
            for hypothesis in conflict.get("hypotheses", []):
                status = self._resolve_hypothesis_status(hypothesis, verify_by_hid)
                summary = self._resolve_hypothesis_summary(hypothesis, verify_by_hid)
                gaps = self._resolve_hypothesis_gaps(hypothesis, verify_by_hid)

                if status in ("verified", "partial"):
                    if severity in ("high", "critical"):
                        self._apply_verified_conflict(
                            dimensions=dimensions,
                            dim_ids=dim_ids,
                            conflict=conflict,
                            hypothesis=hypothesis,
                            summary=summary,
                            penalty=penalty * (0.7 if status == "partial" else 1.0),
                        )
                    if gaps:
                        for dim_id in dim_ids:
                            dim = dimensions.get(dim_id)
                            if dim is None:
                                continue
                            dim.missing_evidence.extend(gaps[:3])
                            dim.confidence = max(0.0, dim.confidence - 0.05)
                elif status == "rejected":
                    message = summary or hypothesis.get("explanation", "")
                    if message:
                        for dim_id in dim_ids:
                            dim = dimensions.get(dim_id)
                            if dim is None:
                                continue
                            dim.counter_evidence.append(message)
                elif status in ("unknown", "pending"):
                    missing = gaps or ([summary] if summary else [])
                    if not missing:
                        missing = [hypothesis.get("explanation", "关键假设仍待验证")]
                    for dim_id in dim_ids:
                        dim = dimensions.get(dim_id)
                        if dim is None:
                            continue
                        dim.missing_evidence.extend(missing[:3])
                        dim.confidence = max(0.0, dim.confidence - 0.1)

        for dim in dimensions.values():
            dim.counter_evidence = self._dedupe_preserve_order(dim.counter_evidence)
            dim.missing_evidence = self._dedupe_preserve_order(dim.missing_evidence)

    def _apply_verified_conflict(
        self,
        *,
        dimensions: dict[str, ThesisDimension],
        dim_ids: list[str],
        conflict: dict[str, Any],
        hypothesis: dict[str, Any],
        summary: str,
        penalty: float,
    ) -> None:
        theme = conflict.get("theme", "")
        severity = conflict.get("severity", "medium")
        description = conflict.get("description", "")
        explanation = hypothesis.get("explanation", "")
        text = summary or explanation or description or theme
        for dim_id in dim_ids:
            dim = dimensions.get(dim_id)
            if dim is None:
                continue
            dim.evidence.append(
                EvidenceItem(
                    signal_id=f"conflict:{conflict.get('id', theme)}",
                    signal_name=theme or "verified_conflict",
                    level=severity,
                    impact="negative",
                    interpretation=text,
                    source_type="conflict",
                    source_label="verified_conflict",
                )
            )
            dim.score = max(-1.0, min(1.0, dim.score - penalty))
            dim.confidence = min(1.0, dim.confidence + 0.1)

    def _apply_company_context(
        self,
        *,
        dimensions: dict[str, ThesisDimension],
        company_context: dict[str, Any] | None,
    ) -> None:
        ctx = self._coerce_context_dict(company_context)
        if not ctx:
            return

        lifecycle = str(ctx.get("lifecycle_stage", "") or "")
        industry = str(ctx.get("industry", "") or "")
        business_model = str(ctx.get("business_model_summary", "") or "")

        if lifecycle == "growth":
            for dim_id in ("growth_quality", "capital_efficiency", "operational_stability"):
                self._temper_negative_dimension(
                    dimensions,
                    dim_id,
                    factor=0.85,
                    note="成长期公司扩张会放大阶段性波动，相关负面判断已按生命周期做折减。",
                )

        if industry in _FINANCIAL_INDUSTRIES:
            for dim_id in ("financial_quality", "credit_risk", "capital_efficiency"):
                self._temper_negative_dimension(
                    dimensions,
                    dim_id,
                    factor=0.85,
                    note=f"{industry}行业报表结构与一般工商企业不同，相关维度已按行业语境做折减。",
                )

        if any(keyword in business_model for keyword in _PROJECT_BASED_KEYWORDS):
            for dim_id in ("financial_quality", "operational_stability"):
                dim = dimensions.get(dim_id)
                if dim is None or dim.score >= 0:
                    continue
                if any(
                    keyword in ((item.signal_id or "") + (item.interpretation or ""))
                    for item in dim.evidence
                    for keyword in ("应收", "receivable", "现金流", "cashflow", "回款")
                ):
                    dim.score *= 0.9
                    dim.context_notes.append("项目制/验收型业务会天然拉长回款节奏，相关异常已按商业模式做小幅折减。")

        for dim in dimensions.values():
            dim.context_notes = self._dedupe_preserve_order(dim.context_notes)

    def _temper_negative_dimension(
        self,
        dimensions: dict[str, ThesisDimension],
        dim_id: str,
        *,
        factor: float,
        note: str,
    ) -> None:
        dim = dimensions.get(dim_id)
        if dim is None or dim.score >= 0:
            return
        dim.score *= factor
        dim.context_notes.append(note)

    def _apply_insight(
        self,
        *,
        dimensions: dict[str, ThesisDimension],
        insight: dict[str, Any] | None,
    ) -> None:
        """Apply InsightAgent output as qualitative context on top of deterministic scores.

        InsightAgent produces an LLM-generated opinion document.  We use it to:
        - inject counter-evidence (so the thesis does not read as one-sided)
        - attach the central tension as a context note on every dimension
        - temper overall confidence when the insight itself is low-confidence

        The deterministic signal-based scores are never overridden; insight only
        adds qualitative annotations that the report generator can surface.
        """
        if not insight:
            return

        central_tension = str(insight.get("central_tension", "") or "")
        counter_evidence_items: list[dict] = insight.get("counter_evidence") or []
        insight_confidence = str(insight.get("confidence", "medium") or "medium")

        # ── Attach central tension to every dimension as a context note ──
        if central_tension:
            for dim in dimensions.values():
                if central_tension not in dim.context_notes:
                    dim.context_notes.append(f"洞察-中心矛盾：{central_tension}")

        # ── Inject counter-evidence as counter_evidence on all dimensions ──
        # These are LLM-identified facts that cut against the core view.
        # Rather than guessing which dimension each counter-evidence item
        # belongs to, we attach all of them to every dimension — the report
        # generator will surface the most relevant ones per dimension.
        for item in counter_evidence_items:
            statement = str(item.get("statement", "") or "")
            if not statement:
                continue
            source = str(item.get("source", "insight") or "insight")
            text = f"[洞察反证] {statement} (来源: {source})"
            for dim in dimensions.values():
                dim.counter_evidence.append(text)

        # ── Deduplicate counter_evidence per dimension ──
        for dim in dimensions.values():
            dim.counter_evidence = self._dedupe_preserve_order(dim.counter_evidence)

        # ── Temper confidence when insight itself is low ──
        confidence_factor = {"high": 1.0, "medium": 0.95, "low": 0.85}
        factor = confidence_factor.get(insight_confidence, 0.95)
        if factor < 1.0:
            for dim in dimensions.values():
                dim.confidence = round(max(0.0, dim.confidence * factor), 3)

    def _refresh_dimensions(self, dimensions: dict[str, ThesisDimension]) -> None:
        for dim_id, dim in dimensions.items():
            dim_def = self.dimension_defs.get(dim_id)
            if dim_def is None:
                continue
            dim.score = max(-1.0, min(1.0, dim.score))
            dim.judgment = score_to_judgment(dim.score)
            interpretation = dim_def.get_interpretation(dim.judgment)
            if dim.context_notes:
                interpretation = f"{interpretation} 语境校准：" + " ".join(
                    self._dedupe_preserve_order(dim.context_notes)
                )
            dim.interpretation = interpretation

    def _map_conflict_dimensions(self, conflict: dict[str, Any]) -> list[str]:
        related_dimensions = conflict.get("related_dimensions") or []
        return self._dedupe_preserve_order(
            [str(dim_id) for dim_id in related_dimensions if isinstance(dim_id, str) and dim_id in self.dimension_defs]
        )

    def _resolve_hypothesis_status(
        self,
        hypothesis: dict[str, Any],
        verify_by_hid: dict[str, dict[str, Any]],
    ) -> str:
        hid = hypothesis.get("id", "")
        verification = verify_by_hid.get(hid, {})
        return str(verification.get("status") or hypothesis.get("status") or "pending")

    def _resolve_hypothesis_summary(
        self,
        hypothesis: dict[str, Any],
        verify_by_hid: dict[str, dict[str, Any]],
    ) -> str:
        hid = hypothesis.get("id", "")
        verification = verify_by_hid.get(hid, {})
        return str(verification.get("summary") or hypothesis.get("summary") or hypothesis.get("explanation") or "")

    def _resolve_hypothesis_gaps(
        self,
        hypothesis: dict[str, Any],
        verify_by_hid: dict[str, dict[str, Any]],
    ) -> list[str]:
        hid = hypothesis.get("id", "")
        verification = verify_by_hid.get(hid, {})
        gaps = verification.get("gaps") or hypothesis.get("gaps") or []
        return [str(item) for item in gaps if item]

    def _coerce_context_dict(self, company_context: Any) -> dict[str, Any]:
        if company_context is None:
            return {}
        if isinstance(company_context, dict):
            return company_context
        if hasattr(company_context, "to_dict"):
            try:
                data = company_context.to_dict()
                if isinstance(data, dict):
                    return data
            except Exception:
                return {}
        return {}

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result
