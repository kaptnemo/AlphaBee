"""Payload builders shared by conflict, verification, and analysis nodes."""

from __future__ import annotations

import json as _json

from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.orchestrator.collectors import _find_artifact
from alphabee.orchestrator.contracts import (
    AnomalyReportArtifact,
    DerivedFactsArtifact,
    FactCollectionArtifact,
    ReportAnomalyPayload,
    ReportCompanyPayload,
    ReportConflictAnalysisPayload,
    ReportConflictHypothesisPayload,
    ReportConflictItemPayload,
    ReportGenerationPayload,
    ReportIssuePayload,
    ReportMetricEntry,
    ReportMetricsPayload,
    ReportSignalEntry,
    ReportSignalsPayload,
    SignalAnalysisArtifact,
    ThesisArtifact,
    VerificationArtifact,
    coerce_anomaly_report,
    coerce_conflicts_result,
    coerce_derived_facts,
    coerce_signal_analysis,
    coerce_verification_artifact,
    find_artifact_model,
)
from alphabee.orchestrator.state import OrchestratorState


def default_anomaly_fact_values() -> dict[str, float]:
    """Return neutral anomaly facts so anomaly signal rules can evaluate."""
    from alphabee.agents.anomaly.registry import ANOMALY_PATTERNS, ensure_loaded

    ensure_loaded()
    # 即使本轮没有足够历史数据跑出 anomaly_report，
    # 也要补一组“中性异常事实”，这样依赖异常字段的 signal rules 仍能稳定执行，
    # 而不是因为字段缺失把整条规则链打断。
    values = {
        "anomaly_triggered_count": 0.0,
        "anomaly_pattern_count": 0.0,
        "anomaly_max_zscore": 0.0,
        "anomaly_high_count": 0.0,
    }
    for pattern_id in ANOMALY_PATTERNS:
        values[f"anomaly_pattern_{pattern_id}"] = 0.0
    return values


def _build_key_signals(signal_analysis: dict) -> list[dict]:
    key = []
    for sig_id, result in signal_analysis.items():
        level = result.get("level", "")
        if level not in ("none", "unknown", ""):
            # 冲突探索只需要“有信息量的信号”，
            # 没命中的信号不带入 prompt，避免 agent 被大量无效规则噪声淹没。
            key.append({
                "signal_id": sig_id,
                "level": level,
                "interpretation": (result.get("interpretation") or "")[:200],
                "thesis_impact": result.get("thesis_impact", {}),
            })
    return key


def _build_key_derived(derived_facts: dict) -> dict:
    result = {}
    for name, item in derived_facts.items():
        level = item.get("level", "")
        val = item.get(name)
        if level not in ("none", "") or val is not None:
            # 与其把全部 derived facts 机械传给下游，
            # 不如只保留“有值或有明显等级判断”的关键指标，提高 prompt 密度。
            result[name] = {
                "value": round(float(val), 3) if isinstance(val, (int, float)) else val,
                "level": level,
                "interpretation": (item.get("interpretation") or "")[:120],
            }
    return result


def generate_explore_conflicts_prompt(
    state: OrchestratorState, query: str, symbol: str | None
) -> str:
    financial_facts: FinancialFacts | None = state.get("financial_facts")
    market_facts: MarketFacts | None = state.get("market_facts")
    derived_facts = coerce_derived_facts(state.get("derived_facts")) or DerivedFactsArtifact()
    signal_analysis = coerce_signal_analysis(state.get("signal_analysis")) or SignalAnalysisArtifact()

    anomaly_report = coerce_anomaly_report(state.get("anomaly_report"))
    if anomaly_report is None:
        anomaly_report = find_artifact_model(
            state.get("artifacts", []),
            "anomaly_report",
            AnomalyReportArtifact,
        )

    snapshot_summary: dict = {}
    if financial_facts and financial_facts.snapshots:
        snapshot = financial_facts.snapshots[0]
        snapshot_summary = {
            "period": getattr(snapshot, "period", ""),
            "revenue_yoy": getattr(snapshot, "revenue_yoy", None),
            "net_profit_yoy": getattr(snapshot, "net_profit_yoy", None),
            "gross_margin": getattr(snapshot, "gross_margin", None),
            "roe": getattr(snapshot, "roe", None),
            "operating_cashflow_ratio": getattr(snapshot, "operating_cashflow_ratio", None),
        }

    market_summary: dict = {}
    if market_facts:
        market_summary = {
            "pe_ttm": getattr(market_facts, "pe_ttm", None),
            "pb_ratio": getattr(market_facts, "pb_ratio", None),
            "pe_ttm_5y_avg": getattr(market_facts, "pe_ttm_5y_avg", None),
        }

    anomaly_summary: dict = {}
    if anomaly_report:
        anomaly_summary = {
            "anomaly_count": anomaly_report.anomaly_count,
            "pattern_count": anomaly_report.pattern_count,
            "top_anomalies": [
                {"name": item.get("metric"), "level": item.get("level"), "z_score": item.get("z_score")}
                for item in anomaly_report.anomalies
                if item.get("level") != "none"
            ][:5],
            "pattern_matches": [
                {"name": item.get("pattern_name"), "severity": item.get("severity")}
                for item in anomaly_report.pattern_matches
            ][:3],
        }

    # 冲突探索 prompt 只携带最能暴露背离关系的摘要层信息：
    # 最新财务快照、估值、关键衍生指标、风险信号、异常模式。
    # 这样 agent 会优先寻找“逻辑打架”的点，而不是泛泛复述公司概况。
    payload = {
        "symbol": symbol or "unknown",
        "query": query,
        "latest_snapshot": snapshot_summary,
        "market_valuation": market_summary,
        "key_signals": _build_key_signals(signal_analysis.results),
        "key_derived_facts": _build_key_derived(derived_facts.results),
        "anomaly": anomaly_summary,
    }

    return (
        "请对以下数据进行冲突探索分析，识别背离和矛盾，输出结构化的 ConflictAnalysisResult。\n\n"
        f"```json\n{_json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


def build_verify_context(state: OrchestratorState, symbol: str | None) -> dict:
    financial_facts: FinancialFacts | None = state.get("financial_facts")
    market_facts: MarketFacts | None = state.get("market_facts")

    # 验证阶段比冲突探索更强调“证据链”，因此会给更多期历史快照，
    # 让 agent 判断某个怀疑点到底是单期噪声还是持续模式。
    snapshots_summary = []
    if financial_facts and financial_facts.snapshots:
        for snapshot in financial_facts.snapshots[:4]:
            snapshots_summary.append({
                "period": getattr(snapshot, "period", ""),
                "revenue_yoy": getattr(snapshot, "revenue_yoy", None),
                "net_profit_yoy": getattr(snapshot, "net_profit_yoy", None),
                "gross_margin": getattr(snapshot, "gross_margin", None),
                "roe": getattr(snapshot, "roe", None),
                "operating_cashflow_ratio": getattr(snapshot, "operating_cashflow_ratio", None),
                "accounts_receivable_days": getattr(snapshot, "accounts_receivable_days", None),
                "inventory_days": getattr(snapshot, "inventory_days", None),
                "debt_ratio": getattr(snapshot, "debt_ratio", None),
            })

    market_summary = {}
    if market_facts:
        market_summary = {
            "pe_ttm": getattr(market_facts, "pe_ttm", None),
            "pb_ratio": getattr(market_facts, "pb_ratio", None),
            "pe_ttm_5y_avg": getattr(market_facts, "pe_ttm_5y_avg", None),
            "market_cap": getattr(market_facts, "market_cap", None),
        }

    anomaly_report = coerce_anomaly_report(state.get("anomaly_report")) or find_artifact_model(
        state.get("artifacts", []),
        "anomaly_report",
        AnomalyReportArtifact,
    )

    return {
        "symbol": symbol or "unknown",
        "financial_snapshots": snapshots_summary,
        "market": market_summary,
        "anomaly": {
            "anomalies": [
                {"metric": item.get("metric"), "level": item.get("level"), "z_score": item.get("z_score")}
                for item in (anomaly_report.anomalies if anomaly_report else [])
                if item.get("level") != "none"
            ][:8],
        } if anomaly_report else {},
    }


def build_report_generation_payload(state: OrchestratorState) -> ReportGenerationPayload:
    """Assemble all structured node outputs into a typed report-generation payload."""

    artifacts = state.get("artifacts", [])
    issues = state.get("issues", [])

    payload = ReportGenerationPayload()

    fact_val = find_artifact_model(artifacts, "fact_collection", FactCollectionArtifact)
    if fact_val:
        payload.company = ReportCompanyPayload(
            symbol=fact_val.symbol or "",
            query=fact_val.query,
            raw_response=(fact_val.raw_response or "")[:2000],
        )

    derived_val = find_artifact_model(artifacts, "derived_facts", DerivedFactsArtifact)
    if derived_val:
        top_metrics: list[ReportMetricEntry] = []
        for name, result in derived_val.results.items():
            value = result.get(name)
            if value is None:
                continue
            top_metrics.append(
                ReportMetricEntry(
                    name=name,
                    value=round(float(value), 3),
                    level=str(result.get("level", "")),
                    interpretation=str(result.get("interpretation", "")),
                )
            )
        payload.metrics = ReportMetricsPayload(
            rule_count=derived_val.rule_count,
            top_metrics=top_metrics[:10],
        )

    signal_val = find_artifact_model(artifacts, "signal_analysis", SignalAnalysisArtifact)
    if signal_val:
        signal_list = [
            ReportSignalEntry(
                signal_id=sig_id,
                level=str(result.get("level", "unknown")),
                interpretation=str(result.get("interpretation", "")),
                thesis_impact=result.get("thesis_impact", {}),
                error=str(result.get("error", "")),
            )
            for sig_id, result in signal_val.results.items()
        ]
        level_order = {"blocked": -2, "missing_fact": -1, "high": 3, "medium": 2, "low": 1, "none": 0}
        signal_list.sort(key=lambda item: level_order.get(item.level, 0), reverse=True)
        payload.signals = ReportSignalsPayload(
            rule_count=signal_val.rule_count,
            signals=signal_list,
        )

    thesis_val = find_artifact_model(artifacts, "thesis_analysis", ThesisArtifact)
    if thesis_val:
        payload.thesis = dict(thesis_val.thesis)
        enhanced = thesis_val.enhanced or {}
        if enhanced.get("enhancement_applied"):
            payload.thesis["enhanced"] = {
                "cross_signal_patterns": enhanced.get("cross_signal_patterns", []),
                "context_notes": enhanced.get("context_notes", ""),
            }

    review_val = _find_artifact(artifacts, "thesis_review")
    if review_val:
        payload.review = review_val

    anomaly_val = find_artifact_model(artifacts, "anomaly_report", AnomalyReportArtifact)
    if anomaly_val:
        payload.anomaly = ReportAnomalyPayload(
            anomaly_count=anomaly_val.anomaly_count,
            pattern_count=anomaly_val.pattern_count,
            anomalies=[
                anomaly for anomaly in anomaly_val.anomalies
                if anomaly.get("level") != "none"
            ],
            pattern_matches=list(anomaly_val.pattern_matches),
        )

    conflicts_result = coerce_conflicts_result(state.get("conflicts_result"))
    verification_artifact = (
        coerce_verification_artifact(state.get("verification_results"))
        or VerificationArtifact()
    )
    if conflicts_result:
        verify_by_hid = {
            result.hypothesis_id: result
            for result in verification_artifact.results
            if result.hypothesis_id
        }

        enriched_conflicts: list[ReportConflictItemPayload] = []
        for conflict in conflicts_result.conflicts:
            enriched_hypotheses: list[ReportConflictHypothesisPayload] = []
            for hypothesis in conflict.hypotheses:
                verification = verify_by_hid.get(hypothesis.id)
                enriched_hypotheses.append(
                    ReportConflictHypothesisPayload(
                        explanation=hypothesis.explanation,
                        predictions=list(hypothesis.predictions),
                        verification_status=(
                            verification.status
                            if verification is not None
                            else hypothesis.status
                        ),
                        support_score=(
                            verification.support_score if verification is not None else None
                        ),
                        contradiction_score=(
                            verification.contradiction_score if verification is not None else None
                        ),
                        confidence=(
                            verification.confidence if verification is not None else None
                        ),
                        supporting_evidence=(
                            list(verification.supporting_evidence)
                            if verification is not None else []
                        ),
                        refuting_evidence=(
                            list(verification.refuting_evidence)
                            if verification is not None else []
                        ),
                        gaps=list(verification.gaps) if verification is not None else [],
                        summary=verification.summary if verification is not None else "",
                    )
                )

            enriched_conflicts.append(
                ReportConflictItemPayload(
                    theme=conflict.theme,
                    severity=conflict.severity,
                    description=conflict.description,
                    confidence=conflict.confidence,
                    related_dimensions=list(conflict.related_dimensions),
                    hypotheses=enriched_hypotheses,
                )
            )

        payload.conflict_analysis = ReportConflictAnalysisPayload(
            conflict_count=len(enriched_conflicts),
            verified_count=sum(
                1
                for conflict in enriched_conflicts
                for hypothesis in conflict.hypotheses
                if hypothesis.verification_status in ("verified", "partial")
            ),
            rejected_count=sum(
                1
                for conflict in enriched_conflicts
                for hypothesis in conflict.hypotheses
                if hypothesis.verification_status == "rejected"
            ),
            conflicts=enriched_conflicts,
        )

    payload.issues = [
        ReportIssuePayload(
            id=issue.id,
            severity=issue.severity.value,
            category=issue.category,
            message=issue.message,
        )
        for issue in issues
    ]
    payload.required_issue_disclosures = [
        issue for issue in payload.issues
        if issue.severity in {"high", "critical"}
    ]

    return payload
