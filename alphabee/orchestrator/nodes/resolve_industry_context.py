"""resolve_industry_context node — industry-context-injection Phase 0 垂直切片。

职责（只解析 + 注入，不做完整行业研究，见 docs/industry/industry-context-injection-plan.md）：

1. 行业识别：复用 ``get_industry_fact()``（申万分类 + 行业指数估值 PE/PB）。
2. 财务/成长基准：从行业成分股财务指标推导中位数（``alphabee/industry/``）。
3. 注入：完整上下文写 ``INDUSTRY_CONTEXT`` artifact，数值基准注入 ``fact_values``
   （供 derived facts / signals 规则引用，如 market_share_change 复活）。

降级契约：
- 行业识别失败/未知 → 不产 artifact，发 ``industry_context_missing``（MEDIUM），管道继续；
- 行业已知但成分股基准不可得 → 仍产出 artifact（含估值快照）但 ``degraded=True``，
  发 ``industry_benchmarks_missing``（MEDIUM），下游规则回退默认阈值。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.contracts import IndustryContextArtifact
from alphabee.orchestrator.state import OrchestratorState


def _safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


async def resolve_industry_context(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """解析股票所属行业并注入行业基准（数值基准层）。"""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="resolve_industry_context",
        kind="resolve_industry_context",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    # ── 1. 行业识别 ─────────────────────────────────────────────────
    try:
        from alphabee.agents.facts.tools.industry_fact import get_industry_fact

        ind_fact = get_industry_fact(symbol) if symbol else None
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="industry_context_missing",
                message=f"行业识别失败，管道回退默认阈值: {exc}",
                related_step=step.id,
            )
        )
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed_step], "issues": new_issues}
    industry = str((ind_fact or {}).get("industry") or "").strip()
    if not industry:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="industry_context_missing",
                message="无法解析股票所属行业，管道回退默认阈值",
                related_step=step.id,
            )
        )
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed_step], "issues": new_issues}

    sw_code = str(ind_fact.get("sw_code") or "") or None
    sw_level = str(ind_fact.get("sw_level") or "").strip() or None
    sw_daily = ind_fact.get("sw_daily") or []

    # 估值基准：行业指数快照最新一行
    pe_ttm: float | None = None
    pb: float | None = None
    if sw_daily and isinstance(sw_daily[0], dict):
        pe_ttm = _safe_float(sw_daily[0].get("industry_pe_ttm"))
        pb = _safe_float(sw_daily[0].get("industry_pb"))

    # ── 2. 财务/成长基准（成分股中位数，best-effort）────────────────
    peer_records: list[dict] = []
    fetch_error: str | None = None
    try:
        from alphabee.industry.data import fetch_peer_financials
        from alphabee.industry.normalize import normalize_industry_records

        # fetch 返回源单位行（百分比），先经 normalize 统一为 canonical（RATIO 口径），
        # 修复 Phase 0 单位错配（见 docs/industry/industry-context-phase1-design.md §2.1）
        raw_records, fetch_error = fetch_peer_financials(symbol or "", industry, sw_code)
        peer_records = normalize_industry_records(raw_records, source="tushare")
    except Exception as exc:
        fetch_error = str(exc)

    from alphabee.industry.benchmarks import (
        IndustryBenchmarks,
        derive_benchmarks,
    )

    degraded = False
    degraded_reason = ""
    if peer_records:
        benchmarks = derive_benchmarks(
            peer_records,
            industry=industry,
            sw_code=sw_code,
            as_of_date=date.today().isoformat(),
            pe_ttm=pe_ttm,
            pb=pb,
            source_refs=[f"tushare:index_member({sw_code})+fina_indicator"],
        )
        if not benchmarks.has_financial_benchmarks():
            degraded = True
            degraded_reason = "成分股记录存在但无可用的财务基准字段"
    else:
        # 即便拿不到成分股财务，估值快照（若有）仍然有效：保留 pe/pb，标记部分降级
        benchmarks = IndustryBenchmarks(
            industry=industry,
            sw_code=sw_code,
            as_of_date=date.today().isoformat(),
            pe_ttm=pe_ttm,
            pb=pb,
        )
        degraded = True
        degraded_reason = fetch_error or "无成分股财务数据"

    valuation, financial, growth = benchmarks.to_category_dicts()
    fact_deltas = benchmarks.to_fact_values()

    artifact_value = IndustryContextArtifact(
        industry=industry,
        sub_industry="",
        classification_standard=(f"sw_{sw_level.lower()}" if sw_level else ("sw_l1" if sw_code else "custom")),
        industry_code=sw_code or "",
        sw_code=sw_code,
        as_of_date=date.today().isoformat(),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        source_refs=benchmarks.source_refs,
        valuation_benchmarks=valuation,
        financial_benchmarks=financial,
        growth_benchmarks=growth,
        peer_count=benchmarks.peer_count,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )
    new_artifacts.append(
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.INDUSTRY_CONTEXT,
            producer_step=step.id,
            value=artifact_value.model_dump(mode="json"),
        )
    )

    if degraded:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="industry_benchmarks_missing",
                message=f"行业基准部分缺失（degraded）: {degraded_reason[:200]}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed_step],
        "artifacts": new_artifacts,
        "issues": new_issues,
        "fact_values": fact_deltas,
    }
