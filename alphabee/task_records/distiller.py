"""RuleDistiller — 基于 TaskAnalyzer 统计结果的 LLM 蒸馏建议。"""

from __future__ import annotations

import json

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from alphabee.task_records.analyzer import TaskAnalyzer
from alphabee.task_records.prompts import (
    DISTILL_CALIBRATION_PROMPT,
    DISTILL_SIGNALS_PROMPT,
    DISTILL_SUMMARY_PROMPT,
    DISTILL_THRESHOLDS_PROMPT,
)
from alphabee.task_records.store import TaskStore
from alphabee.utils import create_chat_model

logger = structlog.get_logger(__name__)


class RuleDistiller:
    """LLM 驱动的规则蒸馏分析。

    用法::

        store = TaskStore()
        distiller = RuleDistiller(store)
        report = distiller.generate_full_report()
        print(report)
    """

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.analyzer = TaskAnalyzer(store)

    @property
    def _model(self):
        return create_chat_model("agent.distiller")

    # ── 单项蒸馏 ──────────────────────────────────────────────────

    def suggest_signals(self) -> str:
        """基于'证据单薄'维度，让 LLM 提出新信号 YAML 规则建议。"""
        stats = {
            "single_evidence_dims": self.analyzer.single_evidence_dimensions(),
            "signal_trigger_rates": self.analyzer.signal_trigger_rates(),
            "available_derived_facts": [
                "roe_level", "gross_margin_trend", "revenue_growth",
                "profit_leverage", "debt_ratio", "current_ratio",
                "cashflow_quality", "accounts_receivable_yoy",
                "receivable_growth_gap", "interest_coverage",
                "goodwill_risk", "capex_intensity", "valuation_compression",
                "peg_ratio", "pb_roe_match", "inventory_pressure",
                "asset_turnover", "receivable_pressure", "dividend_coverage",
                "market_share_change", "accounts_receivable_growth",
            ],
        }
        return self._call_llm(
            DISTILL_SIGNALS_PROMPT,
            json.dumps(stats, ensure_ascii=False, indent=2),
        )

    def suggest_calibration(self) -> str:
        """基于'语境不适配'行业，让 LLM 提出行业校准方案。"""
        context_gaps = self.analyzer.context_gap_industries()
        # 收集典型 issue
        sample_issues: list[str] = []
        for r in self.store.load_all():
            for i in r.issues:
                if "语境不适配" in i.message and len(sample_issues) < 10:
                    sample_issues.append(i.message)

        stats = {
            "context_gap_industries": context_gaps,
            "sample_issues": sample_issues,
            "current_calibration_rules": [
                "银行/证券/保险: financial_quality 提示财报结构差异",
                "医药/半导体/芯片/计算机/通信/电子: earnings_quality + negative → 提示高研发投入",
                "成长期 + negative → 提示扩张代价",
            ],
        }
        return self._call_llm(
            DISTILL_CALIBRATION_PROMPT,
            json.dumps(stats, ensure_ascii=False, indent=2),
        )

    def suggest_thresholds(self) -> str:
        """基于触发率统计和 z-score 分布，建议动态阈值调整。"""
        stats = {
            "high_zscore_rules": self.analyzer.high_zscore_rules(),
            "signal_trigger_rates": self.analyzer.signal_trigger_rates(),
            "anomaly_pattern_frequencies": self.analyzer.anomaly_pattern_frequencies(),
        }
        return self._call_llm(
            DISTILL_THRESHOLDS_PROMPT,
            json.dumps(stats, ensure_ascii=False, indent=2),
        )

    # ── 综合报告 ──────────────────────────────────────────────────

    def generate_full_report(self) -> str:
        """综合蒸馏报告：所有统计数据 → LLM 分析 → Markdown 报告。"""
        summary = self.analyzer.summary()
        stats_json = json.dumps(summary, ensure_ascii=False, indent=2)
        prompt = DISTILL_SUMMARY_PROMPT.format(stats_json=stats_json)
        return self._call_llm(prompt, "")

    # ── LLM 调用 ──────────────────────────────────────────────────

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        try:
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt or "请开始分析"),
            ]
            response = self._model.invoke(messages)
            text = self._extract_text(response.content)
            logger.info("distiller_llm_done", text_length=len(text))
            return text
        except Exception as exc:
            logger.warning("distiller_llm_failed", error=str(exc))
            return f"❌ LLM 分析失败: {exc}"

    @staticmethod
    def _extract_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "") if isinstance(block, dict) and block.get("type") in ("text", "thinking")
                else (str(block) if isinstance(block, str) else "")
                for block in content
            ]
            return "\n".join(p for p in parts if p)
        return str(content)


# ── 便捷函数 ──────────────────────────────────────────────────────


def distill(store_dir: str | None = None) -> str:
    """一键蒸馏入口。"""
    store = TaskStore(store_dir)
    if store.count() == 0:
        return "暂无运行记录，请先执行一些分析任务。"
    distiller = RuleDistiller(store)
    return distiller.generate_full_report()
