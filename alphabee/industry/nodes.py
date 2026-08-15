"""行业研究工作流节点（industry-context Phase 1）。

六节点流水线（主计划 1.1）：

    collect_industry_facts → normalize_industry_schema → derive_industry_benchmarks
    → synthesize_industry_context → review_industry_context → persist_industry_profile

节点签名统一 ``(state: IndustryWorkflowState, options: WorkflowOptions) -> IndustryWorkflowState``，
每个节点可独立单测。外部字段只存在于 collect 节点（经 adapter/采集层），下游一律 canonical。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from alphabee.industry.benchmarks import derive_benchmarks
from alphabee.industry.contracts import (
    IndustryContextArtifact,
    IndustryQualitative,
    IndustryReview,
    IndustryWorkflowState,
    WorkflowOptions,
)
from alphabee.industry.normalize import assess_period_alignment, normalize_industry_records
from alphabee.industry.persistence import IndustryProfileStore, suggest_stale_after


def _safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── 1. collect_industry_facts ─────────────────────────────────────────────


def collect_industry_facts(
    state: IndustryWorkflowState,
    options: WorkflowOptions,
) -> IndustryWorkflowState:
    """采集行业身份、估值快照与成分股财务行（best-effort，任一部分失败不中断）。"""
    target = state.target
    errors: list[str] = []
    identity: dict | None = None
    valuation: dict = {
        "industry_pe_ttm": None,
        "industry_pb": None,
        "trade_date": "",
        "source": "none",
    }

    # ── identity + 估值快照 ─────────────────────────────────────────
    if target.symbol:
        try:
            from alphabee.agents.facts.tools.industry_fact import get_industry_fact

            ind_fact = get_industry_fact(target.symbol) or {}
            industry = str(ind_fact.get("industry") or "").strip()
            sw_code = str(ind_fact.get("sw_code") or "") or None
            sw_daily = ind_fact.get("sw_daily") or []
            pe = pb = None
            trade_date = ""
            if sw_daily and isinstance(sw_daily[0], dict):
                pe = _safe_float(sw_daily[0].get("industry_pe_ttm"))
                pb = _safe_float(sw_daily[0].get("industry_pb"))
                trade_date = str(sw_daily[0].get("trade_date") or "")
            if not industry and not sw_code:
                errors.append("行业识别失败：symbol 无法解析行业")
            else:
                identity = {
                    "industry": industry or target.industry_name,
                    "sub_industry": target.sub_industry,
                    "classification_standard": "sw_l1" if sw_code else "custom",
                    "industry_code": sw_code or "",
                    "sw_code": sw_code,
                    "sector": str(ind_fact.get("sector") or ""),
                }
                valuation = {
                    "industry_pe_ttm": pe,
                    "industry_pb": pb,
                    "trade_date": trade_date,
                    "source": "get_industry_fact",
                }
        except Exception as exc:
            errors.append(f"行业识别失败: {exc}")
    elif target.is_direct():
        sw_code = target.industry_code if target.classification_standard in ("sw_l1", "sw_l2") else None
        identity = {
            "industry": target.industry_name,
            "sub_industry": target.sub_industry,
            "classification_standard": target.classification_standard,
            "industry_code": target.industry_code,
            "sw_code": sw_code,
            "sector": "",
        }
        if sw_code:
            try:
                from alphabee.providers.industry import get_industry_daily

                result = get_industry_daily(sw_code=sw_code, industry=target.industry_name)
                if result.daily and isinstance(result.daily[0], dict):
                    row = result.daily[0]
                    valuation = {
                        "industry_pe_ttm": _safe_float(row.get("industry_pe_ttm")),
                        "industry_pb": _safe_float(row.get("industry_pb")),
                        "trade_date": str(row.get("trade_date") or ""),
                        "source": result.source or "industry_daily",
                    }
            except Exception as exc:
                errors.append(f"行业估值获取失败: {exc}")
    else:
        errors.append("目标未指定：需要 symbol 或 classification_standard + industry_code")

    # ── 成分股财务行（源单位行，normalize 节点负责转换）────────────
    peers_block: dict = {"rows": [], "peer_codes": [], "source": "", "fetch_error": None}
    sw_code = (identity or {}).get("sw_code")
    if identity and sw_code:
        try:
            from alphabee.industry.data import fetch_industry_peers

            rows, peer_codes, error = fetch_industry_peers(sw_code, limit=options.peer_limit)
            peers_block = {
                "rows": rows,
                "peer_codes": peer_codes,
                "source": f"tushare:index_member({sw_code})+fina_indicator",
                "fetch_error": error,
            }
            if error:
                errors.append(f"成分股取数失败: {error}")
        except Exception as exc:
            peers_block["fetch_error"] = str(exc)
            errors.append(f"成分股取数失败: {exc}")
    elif identity is None:
        peers_block["fetch_error"] = "无行业身份，跳过成分股取数"
    else:
        peers_block["fetch_error"] = "sw_code 缺失，无法取行业成分股"

    state.raw_facts = {"identity": identity, "valuation": valuation, "peers": peers_block}
    state.errors = errors
    if identity is None:
        state.degraded = True
        state.degraded_reason = "; ".join(errors) or "行业身份不可得"
    return state


# ── 2. normalize_industry_schema ──────────────────────────────────────────


def normalize_industry_schema(
    state: IndustryWorkflowState,
    options: WorkflowOptions,
) -> IndustryWorkflowState:
    """把源单位行归一化为 canonical 记录 + 评估报告期对齐（B3）。"""
    del options
    peers = state.raw_facts.get("peers") or {}
    rows = peers.get("rows") or []
    state.canonical_records = normalize_industry_records(rows, source="tushare")
    state.period_alignment = assess_period_alignment(state.canonical_records)
    return state


# ── 3. derive_industry_benchmarks ─────────────────────────────────────────


def derive_industry_benchmarks(
    state: IndustryWorkflowState,
    options: WorkflowOptions,
) -> IndustryWorkflowState:
    """从 canonical 记录推导行业基准（中位数，复用 Phase 0 纯函数）。"""
    identity = state.raw_facts.get("identity") or {}
    valuation = state.raw_facts.get("valuation") or {}
    peers = state.raw_facts.get("peers") or {}
    as_of_date = options.as_of_date or date.today().isoformat()

    source_refs: list[str] = []
    if peers.get("source"):
        source_refs.append(peers["source"])
    if valuation.get("source") and valuation["source"] != "none":
        source_refs.append(f"valuation:{valuation['source']}")

    state.benchmarks = derive_benchmarks(
        state.canonical_records,
        industry=identity.get("industry") or "",
        sw_code=identity.get("sw_code"),
        as_of_date=as_of_date,
        pe_ttm=valuation.get("industry_pe_ttm"),
        pb=valuation.get("industry_pb"),
        source_refs=source_refs,
    )
    return state


# ── 4. synthesize_industry_context ────────────────────────────────────────


def _infer_lifecycle_stage(benchmarks) -> str | None:
    """确定性生命周期启发（轻量；无数据返回 None）。"""
    if benchmarks is None:
        return None
    growth = benchmarks.revenue_yoy
    if growth is None:
        return None
    if growth < 0:
        return "衰退期"
    pe = benchmarks.pe_ttm
    if growth >= 20 and (pe is None or pe >= 30):
        return "成长期"
    if growth < 10 and (pe is None or pe < 20):
        return "成熟期"
    return "成长期" if growth >= 10 else "成熟期"


def _synthesize_with_llm(
    state: IndustryWorkflowState,
    qualitative: IndustryQualitative,
) -> IndustryQualitative | None:
    """LLM 轻量定性合成（agent.industry_research）；任何失败返回 None（回退空块）。"""
    try:
        from alphabee.utils.llm import create_chat_model
        from alphabee.utils.pipeline import parse_json

        benchmarks = state.benchmarks
        identity = state.raw_facts.get("identity") or {}
        benchmark_groups = benchmarks.to_category_dicts() if benchmarks is not None else ({}, {}, {})
        summary = {
            "行业": identity.get("industry") or "",
            "成分股数": len(state.canonical_records),
            "估值基准": benchmark_groups[0],
            "财务基准": benchmark_groups[1],
            "成长基准": benchmark_groups[2],
            "生命周期": qualitative.lifecycle_stage,
        }
        prompt = (
            "你是行业研究员。基于以下数值基准，用 JSON 输出行业定性摘要（全部字段必填，"
            "business_model_summary 不超过 120 字，key_drivers/risk_factors 各不超过 5 条，"
            "industry_chain 为 {上游:[], 中游:[], 下游:[]}）。只输出 JSON：\n"
            f"{summary}\n"
        )
        model = create_chat_model("agent.industry_research")
        raw = model.invoke(prompt).content
        parsed = parse_json(raw)
        if not isinstance(parsed, dict):
            return None
        return IndustryQualitative(
            lifecycle_stage=qualitative.lifecycle_stage,
            business_model_summary=str(parsed.get("business_model_summary") or "").strip(),
            industry_chain={
                k: [str(item) for item in (v or [])] for k, v in (parsed.get("industry_chain") or {}).items()
            },
            key_drivers=[str(item) for item in (parsed.get("key_drivers") or [])],
            risk_factors=[str(item) for item in (parsed.get("risk_factors") or [])],
            synthesized_by="llm",
        )
    except Exception:
        return None


def synthesize_industry_context(
    state: IndustryWorkflowState,
    options: WorkflowOptions,
) -> IndustryWorkflowState:
    """定性合成（v1 默认空块；``qualitative_mode="llm"`` 时可选 LLM 生成）。"""
    qualitative = IndustryQualitative()
    notes: list[str] = []

    lifecycle = _infer_lifecycle_stage(state.benchmarks)
    if lifecycle:
        qualitative.lifecycle_stage = lifecycle

    if options.qualitative_mode == "llm":
        generated = _synthesize_with_llm(state, qualitative)
        if generated is not None:
            qualitative = generated
            notes.append("定性块由 LLM 生成（agent.industry_research），需人工复核证据")
        else:
            notes.append("LLM 定性合成失败，回退空块（v1 保持轻量）")
    else:
        notes.append("定性合成默认关闭（v1 保持轻量，见 DOMAIN_CONTEXT_ROADMAP 划界）")

    qualitative.synthesis_notes = notes
    state.qualitative = qualitative
    return state


# ── 5. review_industry_context ────────────────────────────────────────────


def review_industry_context(
    state: IndustryWorkflowState,
    options: WorkflowOptions,
) -> IndustryWorkflowState:
    """确定性审核检查（新鲜度/成分覆盖/基准覆盖/口径对齐/证据支撑）+ 置信度 + 过期建议。"""
    peers = state.raw_facts.get("peers") or {}
    benchmarks = state.benchmarks
    qualitative = state.qualitative
    as_of_date = options.as_of_date or date.today().isoformat()

    notes: list[str] = []
    degraded = state.degraded
    reason = state.degraded_reason
    growth_blocked = False

    # 0. 成分股取数失败（identity 已有但 peers 不可得）→ 部分降级（估值仍有效）
    if peers.get("fetch_error"):
        degraded = True
        reason = reason or peers["fetch_error"]
        notes.append(f"成分股财务取数失败：{peers['fetch_error']}")

    # 1. 成分覆盖
    peer_count = len(state.canonical_records)
    if peer_count < 5:
        notes.append(f"成分股覆盖不足（peer_count={peer_count} < 5），基准代表性有限")

    # 2. 基准覆盖（全缺失 → degraded）
    valuation, financial, growth = benchmarks.to_category_dicts() if benchmarks is not None else ({}, {}, {})
    has_any_benchmark = any(any(v is not None for v in group.values()) for group in (valuation, financial, growth))
    if not has_any_benchmark:
        degraded = True
        reason = reason or "无任何可用基准（估值/财务/成长全缺失）"
        notes.append("无任何可用基准（估值/财务/成长全缺失）")

    # 3. 口径对齐（B3 严格：mixed → growth 置空）
    alignment = state.period_alignment
    if alignment is not None and not alignment.growth_usable():
        growth_blocked = True
        if alignment.status == "mixed" and not alignment.period_counts:
            notes.append("成分股缺少报告期信息，无法确认口径对齐，growth 基准置空（B3）")
        else:
            notes.append(
                f"报告期对齐为 {alignment.status}（主导期 {alignment.dominant_period}），"
                "无法保证统一口径，growth 基准置空（B3）"
            )
    elif alignment is not None and alignment.status == "mostly_aligned":
        notes.append(f"报告期 mostly_aligned（主导期 {alignment.dominant_period}），growth 基准按近似口径保留")

    # 4. 证据支撑（定性非空但无来源 → 提示复核）
    if not qualitative.is_empty() and qualitative.synthesized_by == "llm" and not peers.get("source"):
        notes.append("定性块无对应数据来源，需人工复核")

    # 5. 置信度启发式（0.8 起扣减，下限 0.3）
    confidence = 0.8
    if degraded:
        confidence -= 0.15
    if peer_count < 5:
        confidence -= 0.2
    if growth_blocked:
        confidence -= 0.1
    if not any(v is not None for v in valuation.values()):
        confidence -= 0.1
    confidence = max(0.3, round(confidence, 2))

    # 6. stale_after（按实际存在的类别，取最早到期）
    present: set[str] = set()
    if any(v is not None for v in valuation.values()):
        present.add("valuation")
    if any(v is not None for v in financial.values()):
        present.add("financial")
    if any(v is not None for v in growth.values()) and not growth_blocked:
        present.add("growth")
    if not qualitative.is_empty() or qualitative.lifecycle_stage:
        present.add("qualitative")
    stale_after = suggest_stale_after(as_of_date, present)

    status = "needs_review" if notes else "approved"
    state.review = IndustryReview(
        status=status,
        notes=notes,
        confidence=confidence,
        stale_after=stale_after,
        reviewed_at=_now_iso(),
    )
    state.degraded = degraded
    state.degraded_reason = reason
    state.growth_blocked = growth_blocked
    return state


# ── 6. persist_industry_profile ───────────────────────────────────────────


def persist_industry_profile(
    state: IndustryWorkflowState,
    options: WorkflowOptions,
) -> IndustryWorkflowState:
    """组装 v2 artifact 并原子写入 JSON 快照（latest-wins）。"""
    identity = state.raw_facts.get("identity") or {}
    peers = state.raw_facts.get("peers") or {}
    valuation = state.raw_facts.get("valuation") or {}
    benchmarks = state.benchmarks

    v_bench, f_bench, g_bench = benchmarks.to_category_dicts() if benchmarks is not None else ({}, {}, {})
    if state.growth_blocked:
        # 置空 growth 基准（保留键、值 None）→ 注入时被跳过 → 下游规则回到 blocked（B3）
        g_bench = {key: None for key in g_bench}

    source_refs: list[str] = []
    if peers.get("source"):
        source_refs.append(peers["source"])
    if valuation.get("source") and valuation["source"] != "none":
        source_refs.append(f"valuation:{valuation['source']}")

    artifact = IndustryContextArtifact(
        schema_version="2",
        industry=identity.get("industry") or "",
        sub_industry=identity.get("sub_industry") or "",
        classification_standard=identity.get("classification_standard") or "",
        industry_code=identity.get("industry_code") or "",
        sw_code=identity.get("sw_code"),
        as_of_date=options.as_of_date or date.today().isoformat(),
        generated_at=_now_iso(),
        stale_after=state.review.stale_after,
        source_refs=source_refs,
        confidence=state.review.confidence,
        lifecycle_stage=state.qualitative.lifecycle_stage,
        business_model_summary=state.qualitative.business_model_summary,
        industry_chain=state.qualitative.industry_chain,
        key_drivers=state.qualitative.key_drivers,
        risk_factors=state.qualitative.risk_factors,
        valuation_benchmarks=v_bench,
        financial_benchmarks=f_bench,
        growth_benchmarks=g_bench,
        peer_universe=list(peers.get("peer_codes") or []),
        peer_count=len(state.canonical_records) or None,
        review_status=state.review.status,
        review_notes=state.review.notes,
        degraded=state.degraded,
        degraded_reason=state.degraded_reason,
    )
    state.artifact = artifact

    store = options.store or IndustryProfileStore()
    state.persist_path = str(store.save(artifact))
    return state
