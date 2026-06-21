"""TaskRecorder — 从最终 JSON payload 抽取 TaskRecord。

零侵入设计：不依赖 pipeline 内部状态，只消费 finalize_message 产出的
AIMessage JSON payload。
"""

from __future__ import annotations

import time
from typing import Any

from alphabee.task_records.models import (
    AnomalyResult,
    DimensionVerdictSummary,
    IssueRecord,
    SignalResult,
    StageTiming,
    TaskRecord,
)


class TaskRecorder:
    """从最终 payload 构建 TaskRecord。"""

    # ═══════════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════════

    def capture(
        self,
        *,
        query: str,
        symbol: str | None,
        flags: dict[str, bool],
        payload: dict[str, Any],
        artifacts: list[dict] | None = None,
        start_ts: float = 0.0,
    ) -> TaskRecord:
        """从最终 payload 构建 TaskRecord。

        Args:
            query: 用户查询文本。
            symbol: 目标股票代码。
            flags: {enhance, llm_review}。
            payload: finalize_message 的 JSON dict。
            artifacts: payload 中的 "artifacts" 列表（可选，用于提取阶段元信息）。
            start_ts: 主流程开始时间戳 (time.monotonic())。
        """
        now_ts = time.monotonic()
        report = payload.get("final_report") or {}
        thesis_val = self._find_artifact(artifacts or [], "thesis_analysis")
        anomaly_val = self._find_artifact(artifacts or [], "anomaly_report")
        review_val = self._find_artifact(artifacts or [], "thesis_review")
        signal_val = self._find_artifact(artifacts or [], "signal_analysis")
        derived_val = self._find_artifact(artifacts or [], "derived_facts")
        fact_val = self._find_artifact(artifacts or [], "fact_collection")
        industry_ctx = (thesis_val or {}).get("industry_context", {}) if thesis_val else {}

        record = TaskRecord(
            query=query,
            symbol=symbol,
            flags=flags,
            total_duration_s=round(now_ts - start_ts, 1) if start_ts else 0.0,
            # ── 事实层 ──
            fact_value_count=self._count_fact_values(fact_val),
            derived_fact_count=(derived_val or {}).get("rule_count", 0),
            derived_blocked_count=self._count_blocked(derived_val),
            # ── 信号层 ──
            signal_count=(signal_val or {}).get("rule_count", 0),
            signal_results=self._extract_signals(signal_val),
            # ── 异常层 ──
            anomaly_triggered_count=self._count_triggered_anomalies(anomaly_val),
            anomaly_pattern_count=(anomaly_val or {}).get("pattern_count", 0),
            anomaly_details=self._extract_anomalies(anomaly_val),
            # ── 论点层 ──
            thesis_dimensions=self._extract_thesis_dims(thesis_val),
            overall_judgment=self._extract_overall_judgment(thesis_val),
            # ── 审查层 ──
            review_overall_status=(review_val or {}).get("overall_status", ""),
            review_dimension_verdicts=self._extract_review_dims(review_val),
            # ── 问题 ──
            issues=self._extract_issues(payload.get("issues", [])),
            # ── 报告元信息 ──
            overall_confidence=report.get("overall_confidence", ""),
            risk_count=report.get("risk_count", {}),
            report_raw=report,
            # ── 标的上下文 ──
            company_industry=industry_ctx.get("industry", ""),
            company_lifecycle=industry_ctx.get("lifecycle_stage", ""),
            company_market_cap=industry_ctx.get("market_cap_category", ""),
        )
        return record

    # ═══════════════════════════════════════════════════════════════
    # 提取器
    # ═══════════════════════════════════════════════════════════════

    def _find_artifact(self, artifacts: list[dict], atype: str) -> dict | None:
        for a in reversed(artifacts):
            if a.get("type") == atype and isinstance(a.get("value"), dict):
                return a["value"]
        return None

    def _count_fact_values(self, fact_val: dict | None) -> int:
        if not fact_val:
            return 0
        raw = fact_val.get("raw_response", "")
        return len(raw) if isinstance(raw, str) else 0

    def _count_blocked(self, derived_val: dict | None) -> int:
        if not derived_val:
            return 0
        results = derived_val.get("results", {})
        return sum(1 for r in results.values() if r.get("level") == "blocked")

    def _extract_signals(self, signal_val: dict | None) -> list[SignalResult]:
        if not signal_val:
            return []
        results = signal_val.get("results", {})
        return [
            SignalResult(
                signal_id=sid,
                level=r.get("level", "unknown"),
                interpretation=r.get("interpretation", ""),
            )
            for sid, r in results.items()
        ]

    def _count_triggered_anomalies(self, anomaly_val: dict | None) -> int:
        if not anomaly_val:
            return 0
        return sum(
            1 for a in anomaly_val.get("anomalies", [])
            if a.get("level") != "none"
        )

    def _extract_anomalies(self, anomaly_val: dict | None) -> list[AnomalyResult]:
        if not anomaly_val:
            return []
        pattern_matches = anomaly_val.get("pattern_matches", [])
        rule_to_patterns: dict[str, list[str]] = {}
        for pm in pattern_matches:
            for rid in pm.get("triggering_rules", []):
                rule_to_patterns.setdefault(rid, []).append(pm.get("pattern_id", ""))

        return [
            AnomalyResult(
                rule_id=a.get("rule_id", ""),
                z_score=a.get("z_score", 0.0),
                level=a.get("level", "none"),
                pattern_ids=rule_to_patterns.get(a.get("rule_id", ""), []),
            )
            for a in anomaly_val.get("anomalies", [])
            if a.get("level") != "none"
        ]

    def _extract_thesis_dims(self, thesis_val: dict | None) -> list[DimensionVerdictSummary]:
        if not thesis_val:
            return []
        thesis_data = thesis_val.get("thesis", {})
        dims = thesis_data.get("dimensions", {})
        return [
            DimensionVerdictSummary(
                dim_id=dim_id,
                dim_name=d.get("name", dim_id),
                judgment=d.get("judgment", ""),
                score=d.get("score", 0.0),
                confidence=d.get("confidence", 0.0),
                evidence_count=len(d.get("evidence", [])),
            )
            for dim_id, d in dims.items()
        ]

    def _extract_overall_judgment(self, thesis_val: dict | None) -> str:
        if not thesis_val:
            return ""
        thesis_data = thesis_val.get("thesis", {})
        return thesis_data.get("overall_judgment", "")

    def _extract_review_dims(self, review_val: dict | None) -> list[DimensionVerdictSummary]:
        if not review_val:
            return []
        verdicts = review_val.get("dimension_verdicts", {})
        return [
            DimensionVerdictSummary(
                dim_id=dim_id,
                dim_name=v.get("dimension_name", dim_id),
                status=v.get("status", ""),
                evidence_count=v.get("evidence_count", 0),
                judgment=v.get("original_judgment", ""),
                score=v.get("original_score", 0.0),
            )
            for dim_id, v in verdicts.items()
        ]

    def _extract_issues(self, issues: list[dict]) -> list[IssueRecord]:
        return [
            IssueRecord(
                severity=i.get("severity", ""),
                category=i.get("category", ""),
                message=i.get("message", ""),
                related_step=i.get("related_step", ""),
            )
            for i in issues
        ]
