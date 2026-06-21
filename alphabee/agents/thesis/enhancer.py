"""Thesis enhancer — LLM-driven cross-signal pattern recognition and context-aware analysis.

Takes the deterministic ``InvestmentThesis`` (from ``ThesisEngine`` + ``CriticEngine``)
and enriches it with qualitative insights the rule-based engines cannot produce:

1. Cross-signal pattern discovery
2. Industry/lifecycle context-aware calibration
3. User-intent-adapted summarisation
4. Confidence disclaimers for LLM-generated content

Usage::

    enhancer = ThesisEnhancer()
    enhanced = enhancer.enhance(
        thesis=thesis,
        signal_results=signal_results,
        company_context=CompanyContext(industry="白酒", lifecycle_stage="mature"),
        user_intent="长期投资价值评估",
    )
"""

from __future__ import annotations

import json

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from alphabee.agents.thesis.models import (
    CompanyContext,
    CrossSignalPattern,
    EnhancedThesis,
    InvestmentThesis,
)
from alphabee.agents.thesis.prompts import ENHANCER_SYSTEM_PROMPT, ENHANCER_USER_TEMPLATE
from alphabee.utils import create_chat_model
from alphabee.utils.pipeline import extract_text, parse_json

logger = structlog.get_logger(__name__)


class ThesisEnhancer:
    """LLM-based post-processor for deterministic thesis output.

    The enhancer is purely additive — it never alters the deterministic
    judgments or evidence maps.  LLM failures fall back gracefully to an
    empty ``EnhancedThesis`` with a note explaining the failure.
    """

    def __init__(self):
        self._model = None

    @property
    def _llm(self):
        if self._model is None:
            self._model = create_chat_model("agent.thesis.enhancer")
        return self._model

    # ── Public API ─────────────────────────────────────────────────────

    def enhance(
        self,
        *,
        thesis: InvestmentThesis,
        signal_results: dict[str, dict] | None = None,
        company_context: CompanyContext | None = None,
        user_intent: str = "",
        fact_summary: str = "",
    ) -> EnhancedThesis:
        """Enrich deterministic thesis with LLM-driven analysis.

        Args:
            thesis: Deterministic ``InvestmentThesis`` from ``ThesisEngine``.
            signal_results: Raw signal evaluation results.
            company_context: Industry / lifecycle / market-cap information.
            user_intent: The user's analytical goal, e.g. "长期投资价值".
            fact_summary: Free-text summary of key factual data.

        Returns:
            ``EnhancedThesis`` wrapping the original thesis + LLM additions.
        """
        ctx = company_context or CompanyContext()
        signals = signal_results or {}

        try:
            result = self._call_llm(
                thesis=thesis,
                signal_details=self._summarise_signals(signals),
                company_context=ctx,
                user_intent=user_intent,
                fact_summary=fact_summary,
            )
        except Exception as exc:
            logger.warning("thesis_enhancer_llm_failed", error=str(exc))
            return EnhancedThesis(
                deterministic_thesis=thesis,
                context_notes="LLM 增强层调用失败，以下仅包含确定性分析结论。",
                intent_adjusted_summary="增强失败，请参考确定性结论。",
                llm_confidence_note=f"LLM 增强未执行：{exc}",
            )

        patterns = [
            CrossSignalPattern(
                pattern_name=p.get("pattern_name", "未知模式"),
                signals_involved=p.get("signals_involved", []),
                narrative=p.get("narrative", ""),
                severity_modifier=p.get("severity_modifier", "unchanged"),
            )
            for p in result.get("cross_signal_patterns", [])
        ]

        return EnhancedThesis(
            deterministic_thesis=thesis,
            cross_signal_patterns=patterns,
            context_notes=result.get("context_notes", ""),
            intent_adjusted_summary=result.get("intent_adjusted_summary", ""),
            llm_confidence_note=result.get("llm_confidence_note", ""),
            enhancement_applied=True,
        )

    # ── Private helpers ─────────────────────────────────────────────────

    def _summarise_signals(self, signal_results: dict[str, dict]) -> dict:
        return {
            sig_id: {
                "level": r.get("level", "unknown"),
                "interpretation": r.get("interpretation", ""),
                "thesis_impact": r.get("thesis_impact", {}),
            }
            for sig_id, r in signal_results.items()
        }

    def _call_llm(
        self,
        *,
        thesis: InvestmentThesis,
        signal_details: dict,
        company_context: CompanyContext,
        user_intent: str,
        fact_summary: str,
    ) -> dict:
        thesis_json = json.dumps(thesis.to_dict(), ensure_ascii=False, indent=2)
        signal_json = json.dumps(signal_details, ensure_ascii=False, indent=2)
        context_json = json.dumps(
            company_context.to_dict(), ensure_ascii=False, indent=2
        )

        prompt = ENHANCER_USER_TEMPLATE.format(
            thesis_json=thesis_json,
            signal_details_json=signal_json,
            company_context_json=context_json,
            user_intent=user_intent or "未指定",
            fact_summary=fact_summary or "无",
        )

        response = self._llm.invoke([
            SystemMessage(content=ENHANCER_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        raw_text = self._extract_text(response.content)
        return self._parse_json(raw_text)

    def _extract_text(self, content) -> str:
        return extract_text(content)

    def _parse_json(self, text: str) -> dict:
        return parse_json(text)
