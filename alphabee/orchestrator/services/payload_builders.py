"""Payload builders shared by conflict, verification, and analysis nodes."""

from __future__ import annotations

import json as _json

from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.orchestrator.collectors import _find_artifact
from alphabee.orchestrator.state import OrchestratorState


def default_anomaly_fact_values() -> dict[str, float]:
    """Return neutral anomaly facts so anomaly signal rules can evaluate."""
    from alphabee.agents.anomaly.registry import ANOMALY_PATTERNS, ensure_loaded

    ensure_loaded()
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
    derived_facts: dict[str, dict] = state.get("derived_facts") or {}
    signal_analysis: dict[str, dict] = state.get("signal_analysis") or {}

    anomaly_report: dict | None = state.get("anomaly_report")
    if anomaly_report is None:
        anomaly_report = _find_artifact(state.get("artifacts", []), "anomaly_report")

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
            "anomaly_count": anomaly_report.get("anomaly_count", 0),
            "pattern_count": anomaly_report.get("pattern_count", 0),
            "top_anomalies": [
                {"name": item.get("metric"), "level": item.get("level"), "z_score": item.get("z_score")}
                for item in anomaly_report.get("anomalies", [])
                if item.get("level") != "none"
            ][:5],
            "pattern_matches": [
                {"name": item.get("pattern_name"), "severity": item.get("severity")}
                for item in anomaly_report.get("pattern_matches", [])
            ][:3],
        }

    payload = {
        "symbol": symbol or "unknown",
        "query": query,
        "latest_snapshot": snapshot_summary,
        "market_valuation": market_summary,
        "key_signals": _build_key_signals(signal_analysis),
        "key_derived_facts": _build_key_derived(derived_facts),
        "anomaly": anomaly_summary,
    }

    return (
        "请对以下数据进行冲突探索分析，识别背离和矛盾，输出结构化的 ConflictAnalysisResult。\n\n"
        f"```json\n{_json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


def build_verify_context(state: OrchestratorState, symbol: str | None) -> dict:
    financial_facts: FinancialFacts | None = state.get("financial_facts")
    market_facts: MarketFacts | None = state.get("market_facts")

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

    anomaly_report = state.get("anomaly_report") or _find_artifact(
        state.get("artifacts", []), "anomaly_report"
    )

    return {
        "symbol": symbol or "unknown",
        "financial_snapshots": snapshots_summary,
        "market": market_summary,
        "anomaly": {
            "anomalies": [
                {"metric": item.get("metric"), "level": item.get("level"), "z_score": item.get("z_score")}
                for item in (anomaly_report or {}).get("anomalies", [])
                if item.get("level") != "none"
            ][:8],
        } if anomaly_report else {},
    }
