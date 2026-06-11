"""Data collection node — FactCollector Agent + DerivedFacts Engine + SignalEngine.

Runs the full fact pipeline:
1. FactCollector LLM agent (comprehensive data gathering + narrative)
2. Direct structured extraction via get_financial_facts_model / get_market_facts_model
3. DerivedFacts Engine (deterministic, 21 YAML rules)
4. SignalEngine (deterministic, 3 signal rules)
5. ThesisEngine (deterministic, with optional LLM enhancement)

Steps 1-2 run concurrently; 3-5 run sequentially on the merged fact_values.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.derived_facts.engine import Engine as DerivedFactsEngine
from alphabee.agents.derived_facts.registry import RULES, load_rules
from alphabee.agents.facts.agent import fact_collector_agent_factory
from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.agents.facts.tools.company_profile import get_company_profile
from alphabee.agents.facts.tools.financial_fact import get_financial_facts_model
from alphabee.agents.facts.tools.industry_fact import get_industry_fact
from alphabee.agents.facts.tools.market_fact import get_market_facts_model
from alphabee.agents.signal.engine import SignalEngine
from alphabee.agents.signal.registry import SIGNAL_RULES, load_signal_rules
from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.enhancer import ThesisEnhancer
from alphabee.agents.thesis.models import CompanyContext
from alphabee.core import (
    Artifact,
    Issue,
    IssueSeverity,
    Run,
    RunStatus,
    Step,
    StepStatus,
)
from alphabee.orchestrator.state import OrchestratorState
from alphabee.tools.common import extract_symbols_from_query

# ── helpers ──────────────────────────────────────────────────────────


def _make_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
                parts.append(block.get("text", ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _latest_query(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            text = _extract_text(message.content).strip()
            if text:
                return text
    return _extract_text(messages[-1].content).strip() if messages else ""


def _extract_final_text(result: dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if not messages:
        return ""
    return _extract_text(messages[-1].content).strip()


def _first_symbol(query: str) -> str | None:
    """Extract the first stock symbol from a query string."""
    symbols = extract_symbols_from_query(query)
    if symbols:
        return list(symbols.values())[0]
    return None


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
    # Heuristic from market cap value
    if market_facts is not None and market_facts.market_cap is not None:
        mv = market_facts.market_cap / 1e8  # 亿元
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

    # ── 1. Industry from structured sources ──
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

    # ── 2. Sub-industry / SW classification ──
    try:
        ind_fact = get_industry_fact(symbol)
        if not ctx.industry:
            ctx.industry = ind_fact.get("industry", "")
        ctx.sub_industry = ind_fact.get("sw_code", "") or ""
        # Store industry PE/PB info for downstream consumers
        sw_daily = ind_fact.get("sw_daily", [])
        if sw_daily and isinstance(sw_daily[0], dict):
            item = sw_daily[0]
            ctx.business_model_summary = (
                f"行业PE(TTM): {item.get('industry_pe_ttm', 'N/A')}, "
                f"行业PB: {item.get('industry_pb', 'N/A')}"
            )
    except Exception:
        pass

    # ── 3. Keyword fallback ──
    if not ctx.industry:
        ctx.industry = _keyword_extract_industry(fact_text.lower())

    # ── 4. Market cap category ──
    ctx.market_cap_category = _detect_market_cap(fact_text, market_facts)

    # ── 5. Lifecycle stage ──
    ctx.lifecycle_stage = _detect_lifecycle(fact_text, financial_facts)

    # ── 6. Business model summary ──
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


# ── main collector ───────────────────────────────────────────────────


async def collect_facts(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Run the full data pipeline and populate the state for the harness.

    1. FactCollector LLM agent (narrative + comprehensive data)
    2. Direct structured extraction (FinancialFacts + MarketFacts models)
    3. DerivedFacts Engine (deterministic, 21 rules)
    4. SignalEngine (deterministic, 3 signal rules)
    5. ThesisEngine (deterministic, with optional LLM enhancement)
    """
    query = _latest_query(state.get("messages", []))
    symbol = _first_symbol(query)

    run = Run(
        id=_make_id("orch-run"),
        goal=query or "investment analysis",
        status=RunStatus.RUNNING,
        context={"query": query, "symbol": symbol},
        started_at=datetime.now(),
    )

    step = Step(
        id="collect_facts",
        kind="collect_facts",
        inputs={"query": query, "symbol": symbol},
        status=StepStatus.RUNNING,
    )

    artifacts: list[Artifact] = []
    issues: list[Issue] = []

    # ── Step 1 & 2 (concurrent): LLM agent + structured models ──────
    fact_text: str = ""
    financial_facts: FinancialFacts | None = None
    market_facts: MarketFacts | None = None

    async def _run_fact_agent() -> str:
        try:
            agent = fact_collector_agent_factory()
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=query)]},
                config=config,
            )
            return _extract_final_text(result)
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="subagent_failure",
                    message=f"FactCollector agent failed: {exc}",
                    related_step=step.id,
                )
            )
            return ""

    async def _run_structured_models() -> None:
        nonlocal financial_facts, market_facts
        if not symbol:
            return
        try:
            financial_facts = await asyncio.to_thread(
                get_financial_facts_model, symbol,
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="missing_data",
                    message=f"Financial facts unavailable for {symbol}: {exc}",
                    related_step=step.id,
                )
            )
        try:
            market_facts = await asyncio.to_thread(
                get_market_facts_model, symbol,
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="missing_data",
                    message=f"Market facts unavailable for {symbol}: {exc}",
                    related_step=step.id,
                )
            )

    fact_text, _ = await asyncio.gather(
        _run_fact_agent(),
        _run_structured_models(),
    )

    # ── Package fact_collection artifact ─────────────────────────────
    artifacts.append(
        Artifact(
            id=_make_id("artifact"),
            type="fact_collection",
            producer_step=step.id,
            value={
                "agent": "FactCollector",
                "query": query,
                "symbol": symbol,
                "raw_response": fact_text,
            },
        )
    )

    # ── Step 3, 4 & 5: DerivedFacts + Signal + Thesis engines ────────
    derived_results: dict = {}
    signal_results: dict = {}

    if financial_facts is not None or market_facts is not None:
        # Merge fact_values from both models
        fact_values: dict[str, float] = {}
        if financial_facts is not None:
            fact_values.update(financial_facts.to_fact_values())
        if market_facts is not None:
            fact_values.update(market_facts.to_fact_values())

        # DerivedFacts Engine
        try:
            load_rules()
            df_engine = DerivedFactsEngine()
            all_rule_names = list(RULES.keys())
            derived_results = df_engine.run(all_rule_names, fact_values)

            artifacts.append(
                Artifact(
                    id=_make_id("artifact"),
                    type="derived_facts",
                    producer_step=step.id,
                    value={
                        "results": derived_results,
                        "rule_count": len(all_rule_names),
                    },
                )
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="subagent_failure",
                    message=f"DerivedFacts engine failed: {exc}",
                    related_step=step.id,
                )
            )

        # SignalEngine
        try:
            load_signal_rules()
            signal_engine = SignalEngine()
            all_signal_names = list(SIGNAL_RULES.keys())
            signal_results = signal_engine.run(all_signal_names, fact_values)

            artifacts.append(
                Artifact(
                    id=_make_id("artifact"),
                    type="signal_analysis",
                    producer_step=step.id,
                    value={
                        "results": signal_results,
                        "rule_count": len(all_signal_names),
                    },
                )
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="subagent_failure",
                    message=f"SignalEngine failed: {exc}",
                    related_step=step.id,
                )
            )

        # ── Step 4.5: AnomalyEngine（勾稽关系异常检测）─────────────
        anomaly_report_dict: dict = {}
        if financial_facts is not None and len(financial_facts.snapshots) >= 2:
            try:
                from alphabee.agents.anomaly.engine import AnomalyEngine

                # 提取 employees（跨来源引用：company_profile）
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
                anomaly_report_dict = anomaly_report.to_dict()

                # 注入 fact_values 供下游信号规则引用
                anomaly_fv = anomaly_report.to_fact_values()
                fact_values.update(anomaly_fv)

                artifacts.append(
                    Artifact(
                        id=_make_id("artifact"),
                        type="anomaly_report",
                        producer_step=step.id,
                        value=anomaly_report_dict,
                    )
                )
            except Exception as exc:
                issues.append(
                    Issue(
                        id=_make_id("issue"),
                        severity=IssueSeverity.MEDIUM,
                        category="subagent_failure",
                        message=f"AnomalyEngine failed: {exc}",
                        related_step=step.id,
                    )
                )

        # ── Step 5: ThesisEngine ───────────────────────────────────────
        if signal_results:
            try:
                thesis_engine = ThesisEngine()
                thesis = thesis_engine.run(
                    symbol=symbol or "unknown",
                    period="latest",
                    signal_results=signal_results,
                )

                # Extract company context from fact_collection text + structured models
                company_ctx = _build_company_context(
                    symbol=symbol,
                    fact_text=fact_text,
                    financial_facts=financial_facts,
                    market_facts=market_facts,
                )

                # Optionally enhance with LLM when explicitly enabled
                enhanced = None
                if state.get("enhance"):
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
                        pass  # fall through to plain thesis

                artifacts.append(
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
                            "anomaly_data": {
                                "anomaly_count": anomaly_report_dict.get("anomaly_count", 0),
                                "pattern_count": anomaly_report_dict.get("pattern_count", 0),
                                "anomalies": [
                                    a for a in anomaly_report_dict.get("anomalies", [])
                                    if a.get("level") != "none"
                                ],
                                "pattern_matches": anomaly_report_dict.get("pattern_matches", []),
                            },
                        },
                    )
                )
            except Exception as exc:
                issues.append(
                    Issue(
                        id=_make_id("issue"),
                        severity=IssueSeverity.HIGH,
                        category="subagent_failure",
                        message=f"ThesisEngine failed: {exc}",
                        related_step=step.id,
                    )
                )
    else:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.CRITICAL,
                category="missing_data",
                message=(
                    "No structured data available for derived facts or signal "
                    "computation. Ensure the stock symbol is recognized."
                ),
                related_step=step.id,
            )
        )

    # ── Finalize step status ─────────────────────────────────────────
    if issues and not artifacts:
        step_status = StepStatus.FAILED
    elif issues:
        step_status = StepStatus.PARTIAL
    else:
        step_status = StepStatus.SUCCEEDED

    completed_step = step.model_copy(
        update={
            "status": step_status,
            "outputs": [a.id for a in artifacts],
        }
    )

    return {
        "messages": state.get("messages", []),
        "run": run,
        "steps": [completed_step],
        "artifacts": artifacts,
        "observations": [],
        "decisions": [],
        "issues": issues,
        "final_artifact_id": None,
        "evaluation_artifact_id": None,
        "supplement_round": 0,
        "max_supplement_rounds": 1,
    }
