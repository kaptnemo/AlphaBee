"""Analysis and research nodes — engines, conflict exploration, hypothesis verification, thesis generation.

Pipelines (called after data collection):
1. run_analysis_engines — deterministic engines: DerivedFacts, Signal, Anomaly
2. explore_conflicts    — LLM-based conflict exploration from structured data
3. verify_hypotheses    — LLM-based hypothesis verification (one agent per conflict)
4. run_thesis           — ThesisEngine + optional LLM enhancement
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.derived_facts.engine import Engine as DerivedFactsEngine
from alphabee.agents.derived_facts.registry import RULES, load_rules
from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.agents.facts.tools.company_profile import get_company_profile
from alphabee.agents.facts.tools.industry_fact import get_industry_fact
from alphabee.agents.signal.engine import SignalEngine
from alphabee.agents.signal.registry import SIGNAL_RULES, load_signal_rules
from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.enhancer import ThesisEnhancer
from alphabee.agents.thesis.models import CompanyContext
from alphabee.core import (
    Artifact,
    Issue,
    IssueSeverity,
    Step,
    StepStatus,
)
from alphabee.orchestrator.collectors import (  # shared utilities
    _build_conflict_data,
    _extract_final_text,
    _finalize_step,
    _find_artifact,
    _make_id,
)
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import parse_json


# ── company context helpers ────────────────────────────────────────────


def _keyword_extract_industry(text: str) -> str:
    """Fallback: extract industry from free text using keyword matching."""
    industry_keywords: list[tuple[str, str]] = [
        ("白酒", "白酒"), ("银行", "银行"), ("证券", "证券"),
        ("保险", "保险"), ("房地产", "房地产"),
        ("半导体", "半导体"), ("芯片", "半导体"),
        ("新能源汽车", "新能源汽车"), ("光伏", "光伏"),
        ("医药", "医药"), ("消费电子", "消费电子"),
        ("钢铁", "钢铁"), ("煤炭", "煤炭"), ("电力", "电力"),
        ("化工", "化工"), ("机械", "机械"), ("军工", "军工"),
        ("农林", "农林牧渔"), ("食品", "食品饮料"), ("家电", "家电"),
        ("纺织", "纺织服装"), ("建材", "建材"), ("建筑", "建筑装饰"),
        ("传媒", "传媒"), ("计算机", "计算机"), ("通信", "通信"),
        ("环保", "环保"), ("公用", "公用事业"), ("交通", "交通运输"),
    ]
    for kw, industry in industry_keywords:
        if kw in text:
            return industry
    return ""


def _detect_market_cap(
    fact_text: str,
    market_facts: MarketFacts | None = None,
) -> str:
    """Detect market cap category from structured data or text hints."""
    text = fact_text.lower()
    if "大盘" in text or "蓝筹" in text or "白马" in text:
        return "large"
    if "中小盘" in text or "中盘" in text:
        return "mid"
    if "小盘" in text or "创业板" in text or "微盘" in text:
        return "small"
    if market_facts is not None and market_facts.market_cap is not None:
        mv = market_facts.market_cap / 1e8
        if mv >= 500:
            return "large"
        if mv >= 100:
            return "mid"
        return "small"
    return ""


def _detect_lifecycle(
    fact_text: str,
    financial_facts: FinancialFacts | None = None,
) -> str:
    """Detect lifecycle stage from text hints."""
    text = fact_text.lower()
    if "成熟" in text or "稳定" in text:
        return "mature"
    if "成长" in text or "高增长" in text:
        return "growth"
    if financial_facts is not None and financial_facts.snapshots:
        yoy = financial_facts.snapshots[0].revenue_yoy or 0
        if yoy >= 20:
            return "growth"
        if yoy >= 5:
            return "mature"
    return ""


def _build_company_context(
    symbol: str | None,
    fact_text: str,
    *,
    financial_facts: FinancialFacts | None = None,
    market_facts: MarketFacts | None = None,
) -> CompanyContext:
    """Build a ``CompanyContext`` from structured data sources.

    Uses authoritative Tushare data (company_profile + industry_fact) as the
    primary source for industry classification.  Falls back to keyword
    matching only when structured data is unavailable.
    """
    ctx = CompanyContext(symbol=symbol or "")
    if not symbol:
        return ctx

    ctx.name = symbol

    try:
        profile = get_company_profile(symbol)
        basic = profile.get("basic", {})
        if basic:
            tushare_industry = basic.get("industry", {})
            if isinstance(tushare_industry, dict):
                val = tushare_industry.get(0, "")
                if val:
                    ctx.industry = str(val)
    except Exception:
        pass

    try:
        ind_fact = get_industry_fact(symbol)
        if not ctx.industry:
            ctx.industry = ind_fact.get("industry", "")
        ctx.sub_industry = ind_fact.get("sw_code", "") or ""
        sw_daily = ind_fact.get("sw_daily", [])
        if sw_daily and isinstance(sw_daily[0], dict):
            item = sw_daily[0]
            ctx.business_model_summary = (
                f"行业PE(TTM): {item.get('industry_pe_ttm', 'N/A')}, "
                f"行业PB: {item.get('industry_pb', 'N/A')}"
            )
    except Exception:
        pass

    if not ctx.industry:
        ctx.industry = _keyword_extract_industry(fact_text.lower())

    ctx.market_cap_category = _detect_market_cap(fact_text, market_facts)
    ctx.lifecycle_stage = _detect_lifecycle(fact_text, financial_facts)

    try:
        company = profile.get("company", {})
        if not ctx.business_model_summary and company:
            main_biz = company.get("main_business", {})
            if isinstance(main_biz, dict):
                biz_val = main_biz.get(0, "")
                if biz_val:
                    ctx.business_model_summary = str(biz_val)[:300]
    except Exception:
        pass

    return ctx


# ── node 1: run_analysis_engines ──────────────────────────────────────


async def run_analysis_engines(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Run deterministic analysis engines on the structured fact values.

    Engines (each isolated with try/except — one failure does not block others):
    - DerivedFactsEngine  (21 YAML rules)
    - AnomalyEngine        (statistical cross-check, injects anomaly facts)
    - SignalEngine         (signal rules, including anomaly-aware rules)
    """
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    fact_values: dict[str, float] = dict(state.get("fact_values") or {})
    financial_facts = state.get("financial_facts")

    step = Step(
        id="run_analysis_engines",
        kind="run_analysis_engines",
        inputs={"symbol": symbol, "fact_values_count": len(fact_values)},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    if not fact_values:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.CRITICAL,
                category="missing_data",
                message="No fact_values available — skipping all analysis engines.",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    # ── DerivedFacts Engine ───────────────────────────────────────────
    derived_facts: dict[str, dict] = {}
    try:
        load_rules()
        df_engine = DerivedFactsEngine()
        all_rule_names = list(RULES.keys())
        derived_facts = df_engine.run(all_rule_names, fact_values)
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type="derived_facts",
                producer_step=step.id,
                value={"results": derived_facts, "rule_count": len(all_rule_names)},
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"DerivedFacts engine failed: {exc}",
                related_step=step.id,
            )
        )

    # ── AnomalyEngine ─────────────────────────────────────────────────
    anomaly_report = None
    anomaly_fact_values: dict[str, float] = _default_anomaly_fact_values()
    if financial_facts is not None and len(financial_facts.snapshots) >= 2:
        try:
            from alphabee.agents.anomaly.engine import AnomalyEngine

            extra_vals: dict[str, float] = {}
            try:
                profile = get_company_profile(symbol)
                company_data = profile.get("company", {}) if profile else {}
                employees_raw = company_data.get("employees", {})
                if isinstance(employees_raw, dict):
                    employees_val = employees_raw.get(0)
                    if employees_val is not None:
                        extra_vals["employees"] = float(employees_val)
            except Exception:
                pass

            anomaly_engine = AnomalyEngine()
            anomaly_report = anomaly_engine.run(
                financial_facts, extra_values=extra_vals or None,
            )
            anomaly_fact_values.update(anomaly_report.to_fact_values())
            new_artifacts.append(
                Artifact(
                    id=_make_id("artifact"),
                    type="anomaly_report",
                    producer_step=step.id,
                    value=anomaly_report.to_dict(),
                )
            )
        except Exception as exc:
            new_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="subagent_failure",
                    message=f"AnomalyEngine failed: {exc}",
                    related_step=step.id,
                )
            )

    fact_values.update(anomaly_fact_values)

    # ── SignalEngine ──────────────────────────────────────────────────
    signal_analysis: dict[str, dict] = {}
    try:
        load_signal_rules()
        signal_engine = SignalEngine()
        all_signal_names = list(SIGNAL_RULES.keys())
        signal_analysis = signal_engine.run(all_signal_names, fact_values)
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type="signal_analysis",
                producer_step=step.id,
                value={"results": signal_analysis, "rule_count": len(all_signal_names)},
            )
        )

        # Record data-unavailable signals to the failure database
        _record_signal_data_gaps(signal_analysis, fact_values, symbol)

    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"SignalEngine failed: {exc}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "issues": state.get("issues", []) + new_issues,
        "fact_values": fact_values,
        "derived_facts": derived_facts,
        "signal_analysis": signal_analysis,
        "anomaly_report": anomaly_report.to_dict() if anomaly_report else None,
    }


def _default_anomaly_fact_values() -> dict[str, float]:
    """Return neutral anomaly facts so anomaly signal rules can evaluate."""
    return {
        "anomaly_triggered_count": 0.0,
        "anomaly_pattern_count": 0.0,
        "anomaly_max_zscore": 0.0,
        "anomaly_high_count": 0.0,
    }


# ── node 2: explore_conflicts ─────────────────────────────────────────


def _build_key_signals(signal_analysis: dict) -> list[dict]:
    key = []
    for sig_id, r in signal_analysis.items():
        level = r.get("level", "")
        if level not in ("none", "unknown", ""):
            key.append({
                "signal_id": sig_id,
                "level": level,
                "interpretation": (r.get("interpretation") or "")[:200],
                "thesis_impact": r.get("thesis_impact", {}),
            })
    return key


def _build_key_derived(derived_facts: dict) -> dict:
    result = {}
    for name, r in derived_facts.items():
        level = r.get("level", "")
        val = r.get(name)
        if level not in ("none", "") or val is not None:
            result[name] = {
                "value": round(float(val), 3) if isinstance(val, (int, float)) else val,
                "level": level,
                "interpretation": (r.get("interpretation") or "")[:120],
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
        s = financial_facts.snapshots[0]
        snapshot_summary = {
            "period": getattr(s, "period", ""),
            "revenue_yoy": getattr(s, "revenue_yoy", None),
            "net_profit_yoy": getattr(s, "net_profit_yoy", None),
            "gross_margin": getattr(s, "gross_margin", None),
            "roe": getattr(s, "roe", None),
            "operating_cashflow_ratio": getattr(s, "operating_cashflow_ratio", None),
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
                {"name": a.get("metric"), "level": a.get("level"), "z_score": a.get("z_score")}
                for a in anomaly_report.get("anomalies", [])
                if a.get("level") != "none"
            ][:5],
            "pattern_matches": [
                {"name": p.get("pattern_name"), "severity": p.get("severity")}
                for p in anomaly_report.get("pattern_matches", [])
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
        f"请对以下数据进行冲突探索分析，识别背离和矛盾，输出结构化的 ConflictAnalysisResult。\n\n"
        f"```json\n{_json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


async def explore_conflicts(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Run the ConflictExplorer agent to identify gaps and conflicts.

    Output is parsed into ConflictAnalysisResult and stored in both the
    artifacts list and state["conflicts_result"] for downstream consumption.
    """
    from alphabee.agents.explore_conflicts.agent import explore_conflicts_agent_factory
    from alphabee.agents.schemas import ConflictAnalysisResult

    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    query = run.context.get("query", "") if run else ""

    step = Step(
        id="explore_conflicts",
        kind="explore_conflicts",
        inputs={"symbol": symbol, "query": query},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    raw_result: dict | None = None
    conflicts_result: ConflictAnalysisResult | None = None
    raw_text = ""

    try:
        content = generate_explore_conflicts_prompt(state, query, symbol)
        agent = explore_conflicts_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=content)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        new_issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.HIGH,
            category="subagent_failure",
            message=f"ExploreConflicts agent failed: {exc}",
            related_step=step.id,
        ))
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    parse_error: str | None = None
    if raw_text:
        try:
            parsed_dict = parse_json(raw_text)
            conflicts_result = ConflictAnalysisResult.model_validate(parsed_dict)
        except Exception as exc:
            parse_error = str(exc)
            new_issues.append(Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="parse_error",
                message=f"ConflictAnalysisResult parse failed: {exc} — raw_text saved in artifact",
                related_step=step.id,
            ))

    artifact_value: dict = {
        "symbol": symbol,
        "raw_text": raw_text[:4000] if raw_text else "",
    }
    if conflicts_result is not None:
        artifact_value["conflicts"] = conflicts_result.model_dump()
        artifact_value["conflict_count"] = len(conflicts_result.conflicts)
        hypothesis_count = sum(len(c.hypotheses) for c in conflicts_result.conflicts)
        artifact_value["hypothesis_count"] = hypothesis_count
    else:
        artifact_value["parse_error"] = parse_error or "unknown"

    new_artifacts.append(Artifact(
        id=_make_id("artifact"),
        type="conflict_analysis",
        producer_step=step.id,
        value=artifact_value,
    ))

    if conflicts_result:
        for conflict in conflicts_result.conflicts:
            if conflict.severity in ("high", "critical"):
                new_issues.append(Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH if conflict.severity == "high" else IssueSeverity.CRITICAL,
                    category="conflict",
                    message=f"[冲突] {conflict.theme}: {conflict.description}",
                    related_step=step.id,
                ))

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "issues": state.get("issues", []) + new_issues,
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "conflicts_result": conflicts_result.model_dump() if conflicts_result else None,
    }


# ── node 3: verify_hypotheses ─────────────────────────────────────────


def _build_verify_context(state: OrchestratorState, symbol: str | None) -> dict:
    financial_facts: FinancialFacts | None = state.get("financial_facts")
    market_facts: MarketFacts | None = state.get("market_facts")

    snapshots_summary = []
    if financial_facts and financial_facts.snapshots:
        for s in financial_facts.snapshots[:4]:
            snapshots_summary.append({
                "period": getattr(s, "period", ""),
                "revenue_yoy": getattr(s, "revenue_yoy", None),
                "net_profit_yoy": getattr(s, "net_profit_yoy", None),
                "gross_margin": getattr(s, "gross_margin", None),
                "roe": getattr(s, "roe", None),
                "operating_cashflow_ratio": getattr(s, "operating_cashflow_ratio", None),
                "accounts_receivable_days": getattr(s, "accounts_receivable_days", None),
                "inventory_days": getattr(s, "inventory_days", None),
                "debt_ratio": getattr(s, "debt_ratio", None),
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
                {"metric": a.get("metric"), "level": a.get("level"), "z_score": a.get("z_score")}
                for a in (anomaly_report or {}).get("anomalies", [])
                if a.get("level") != "none"
            ][:8],
        } if anomaly_report else {},
    }


async def _verify_single_conflict(
    conflict: "ConflictItem",
    shared_context: dict,
    step_id: str,
    config: "RunnableConfig",
) -> "tuple[list[VerificationResultItem], list[Issue]]":
    """Verify all hypotheses for one conflict using a dedicated agent instance.

    Returns (results, issues).  Never raises — errors are returned as Issues.
    """
    from alphabee.agents.schemas import VerificationResultList, VerificationResultItem
    from alphabee.agents.verify_hypotheses.agent import verify_hypotheses_agent_factory
    from alphabee.agents.verify_hypotheses.prompts import VERIFY_HYPOTHESES_USER_TEMPLATE

    issues: list[Issue] = []

    if not conflict.hypotheses:
        return [], issues

    hypotheses_json = _json.dumps(
        [h.model_dump() for h in conflict.hypotheses],
        ensure_ascii=False,
        indent=2,
    )
    ctx = {**shared_context, "conflict_theme": conflict.theme, "conflict_severity": conflict.severity}
    context_json = _json.dumps(ctx, ensure_ascii=False, indent=2)
    user_msg = VERIFY_HYPOTHESES_USER_TEMPLATE.format(
        hypotheses_json=hypotheses_json,
        context_json=context_json,
    )

    raw_text = ""
    try:
        agent = verify_hypotheses_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_msg)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.HIGH,
            category="subagent_failure",
            message=f"VerifyHypotheses agent failed for conflict '{conflict.theme}': {exc}",
            related_step=step_id,
        ))
        return [], issues

    if not raw_text:
        return [], issues

    try:
        parsed = parse_json(raw_text)
        if isinstance(parsed, list):
            parsed = {"results": parsed}
        vlist = VerificationResultList.model_validate(parsed)
        return vlist.results, issues
    except Exception as exc:
        issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.MEDIUM,
            category="parse_error",
            message=f"VerificationResultList parse failed for conflict '{conflict.theme}': {exc}",
            related_step=step_id,
        ))
        return [], issues


async def verify_hypotheses(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Verify hypotheses from explore_conflicts in parallel — one agent per conflict.

    Each ConflictItem gets its own VerifyHypothesesAgent instance running concurrently
    via asyncio.gather.  Results are merged and written back into conflicts_result.
    """
    from alphabee.agents.schemas import ConflictAnalysisResult, VerificationResultItem

    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="verify_hypotheses",
        kind="verify_hypotheses",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )
    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    conflicts_raw = state.get("conflicts_result")
    if not conflicts_raw:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {**state, "steps": state.get("steps", []) + [completed_step]}

    try:
        conflicts_result = ConflictAnalysisResult.model_validate(conflicts_raw)
    except Exception as exc:
        new_issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.MEDIUM,
            category="parse_error",
            message=f"verify_hypotheses: conflicts_result parse failed: {exc}",
            related_step=step.id,
        ))
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    all_hypotheses = [h for c in conflicts_result.conflicts for h in c.hypotheses]
    if not all_hypotheses:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {**state, "steps": state.get("steps", []) + [completed_step]}

    shared_context = _build_verify_context(state, symbol)
    tasks = [
        _verify_single_conflict(conflict, shared_context, step.id, config)
        for conflict in conflicts_result.conflicts
        if conflict.hypotheses
    ]
    task_results: list[tuple[list[VerificationResultItem], list[Issue]]] = (
        await asyncio.gather(*tasks)
    )

    all_results: list[VerificationResultItem] = []
    for results, issues in task_results:
        all_results.extend(results)
        new_issues.extend(issues)

    result_by_hid = {r.hypothesis_id: r for r in all_results}

    for conflict in conflicts_result.conflicts:
        for hyp in conflict.hypotheses:
            if hyp.id in result_by_hid:
                hyp.status = result_by_hid[hyp.id].status

    verified_ids = {hid for hid, r in result_by_hid.items() if r.status in ("verified", "partial")}
    rejected_ids = {hid for hid, r in result_by_hid.items() if r.status == "rejected"}

    new_artifacts.append(Artifact(
        id=_make_id("artifact"),
        type="verification_results",
        producer_step=step.id,
        value={
            "symbol": symbol,
            "results": [r.model_dump() for r in all_results],
            "verified_count": len(verified_ids),
            "rejected_count": len(rejected_ids),
            "unknown_count": len(all_hypotheses) - len(verified_ids) - len(rejected_ids),
        },
    ))

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "issues": state.get("issues", []) + new_issues,
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "conflicts_result": conflicts_result.model_dump(),
        "verification_results": [r.model_dump() for r in all_results],
    }


# ── node 4: run_thesis ────────────────────────────────────────────────


async def run_thesis(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Run ThesisEngine on signal results, with optional LLM enhancement.

    Reads ``signal_analysis`` artifact from state; skips gracefully when absent.
    """
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    query = run.context.get("query", "") if run else ""
    financial_facts = state.get("financial_facts")
    market_facts = state.get("market_facts")
    enhance = state.get("enhance", False)

    step = Step(
        id="run_thesis",
        kind="run_thesis",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    signal_av = _find_artifact(state.get("artifacts", []), "signal_analysis")
    signal_results: dict = signal_av.get("results", {}) if signal_av else {}

    fc_av = _find_artifact(state.get("artifacts", []), "fact_collection")
    fact_text: str = fc_av.get("raw_response", "") if fc_av else ""

    anomaly_av = _find_artifact(state.get("artifacts", []), "anomaly_report")
    anomaly_data: dict = {}
    if anomaly_av:
        anomaly_data = {
            "anomaly_count": anomaly_av.get("anomaly_count", 0),
            "pattern_count": anomaly_av.get("pattern_count", 0),
            "anomalies": [
                a for a in anomaly_av.get("anomalies", [])
                if a.get("level") != "none"
            ],
            "pattern_matches": anomaly_av.get("pattern_matches", []),
        }

    if not signal_results:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="missing_data",
                message="No signal results available — skipping ThesisEngine.",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    try:
        period = "latest"
        if financial_facts is not None and financial_facts.snapshots:
            snap_period = financial_facts.snapshots[0].period
            if snap_period:
                period = snap_period

        thesis_engine = ThesisEngine()
        thesis = thesis_engine.run(
            symbol=symbol or "unknown",
            period=period,
            signal_results=signal_results,
        )

        company_ctx = _build_company_context(
            symbol=symbol,
            fact_text=fact_text,
            financial_facts=financial_facts,
            market_facts=market_facts,
        )

        enhanced = None
        if enhance:
            try:
                enhancer = ThesisEnhancer()
                enhanced = enhancer.enhance(
                    thesis=thesis,
                    signal_results=signal_results,
                    company_context=company_ctx,
                    user_intent=query,
                    fact_summary=fact_text[:2000] if fact_text else "",
                )
            except Exception:
                pass

        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type="thesis_analysis",
                producer_step=step.id,
                value={
                    "thesis": thesis.to_dict(),
                    "enhanced": enhanced.to_dict() if enhanced else None,
                    "industry_context": {
                        "industry": company_ctx.industry,
                        "sub_industry": company_ctx.sub_industry,
                        "market_cap_category": company_ctx.market_cap_category,
                        "lifecycle_stage": company_ctx.lifecycle_stage,
                        "business_model_summary": (
                            company_ctx.business_model_summary[:300]
                            if company_ctx.business_model_summary
                            else ""
                        ),
                    },
                    "anomaly_data": anomaly_data,
                    "conflict_data": _build_conflict_data(state),
                },
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"ThesisEngine failed: {exc}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "issues": state.get("issues", []) + new_issues,
    }


# ── helper: record signal data gaps to failure database ────────────────


def _record_signal_data_gaps(
    signal_analysis: dict[str, dict],
    fact_values: dict[str, float],
    symbol: str | None,
) -> None:
    """Record blocked / missing_fact / invalid signals as failure events.

    Signals with these levels indicate upstream data was unavailable —
    the data source couldn't provide required canonical fields, so the
    signal rule couldn't evaluate.  Recording them closes the loop:
    the same auto-fix pipeline that handles API failures can also
    handle missing-field gaps.
    """
    _DATA_UNAVAILABLE_LEVELS = {"blocked", "missing_fact", "invalid"}

    for signal_id, result in signal_analysis.items():
        level = result.get("level", "")
        if level not in _DATA_UNAVAILABLE_LEVELS:
            continue

        error_msg = result.get("error", "")
        rule = SIGNAL_RULES.get(signal_id)

        # Collect all required fields declared by this signal rule
        declared_fields: list[str] = []
        if rule is not None:
            declared_fields = list(rule.required_facts or []) + list(
                rule.required_derived_facts or []
            )

        # Determine which declared fields are actually absent
        missing = [f for f in declared_fields if f not in fact_values]

        # For "blocked" signals, the blocked_by list names the derived
        # facts that failed upstream
        blocked_by = result.get("blocked_by", [])

        # Map level to error_type for the failure record
        if level == "missing_fact":
            et = "missing_field"
        elif level == "blocked":
            et = "missing_field"
        else:
            et = "parse_error"  # invalid signals failed during formula eval

        try:
            from alphabee.data_fetch.recorder import record_failure

            record_failure(
                provider="signal_engine",
                api_name=signal_id,
                symbol=symbol,
                error_type=et,
                error_message=error_msg,
                severity="medium" if level == "blocked" else "low",
                missing_fields=missing or blocked_by or None,
            )
        except Exception:
            pass  # never let failure recording break the signal pipeline
