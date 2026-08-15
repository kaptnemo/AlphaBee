"""行业研究工作流端到端测试（industry-context Phase 1，collect 全 mock）。"""

from datetime import date, timedelta

import pytest

from alphabee.industry import IndustryContextWorkflow, IndustryProfileStore
from alphabee.industry.contracts import IndustryTarget


def _ind_fact(industry="白酒", sw_code="801120.SI", pe=25.0, pb=6.0):
    sw_daily = []
    if pe is not None or pb is not None:
        sw_daily = [{"industry_pe_ttm": pe, "industry_pb": pb, "trade_date": "2026-08-15"}]
    return {"industry": industry, "sector": "消费", "sw_code": sw_code, "sw_daily": sw_daily}


def _peer_rows(count=6, end_date="20251231"):
    """源单位行（百分比）：revenue_yoy 10..20，roe 10..20%，debt 40..60%，gross 30..40%；
    估值（已是 RATIO）：pe_ttm 20..，pb_ratio 3..（供中位数估值推导）。"""
    rows = []
    for i in range(count):
        rows.append(
            {
                "revenue_yoy": 10.0 + 2.0 * i,
                "roe": 10.0 + 2.0 * i,
                "debt_to_assets": 40.0 + 4.0 * i,
                "gross_margin": 30.0 + 2.0 * i,
                "pe_ttm": 20.0 + 2.0 * i,
                "pb_ratio": 3.0 + i,
                "period": end_date,
                "stock_code": f"60000{i}.SH",
            }
        )
    return rows


def _patch_collect(monkeypatch, ind_fact=None, rows=None, error=None):
    if ind_fact is not None:
        import alphabee.agents.facts.tools.industry_fact as industry_fact_module

        monkeypatch.setattr(industry_fact_module, "get_industry_fact", lambda symbol: ind_fact)
    if rows is not None or error is not None:
        import alphabee.industry.data as data_module

        codes = [row.get("stock_code") for row in (rows or [])]
        monkeypatch.setattr(
            data_module,
            "fetch_industry_peers",
            lambda *a, **k: (rows or [], codes, error),
        )


def _workflow(tmp_path):
    return IndustryContextWorkflow(store=IndustryProfileStore(root=tmp_path))


# ── 成功路径 ───────────────────────────────────────────────────────────────


def test_workflow_success_persists_artifact(tmp_path, monkeypatch):
    _patch_collect(monkeypatch, _ind_fact(), _peer_rows(6))
    result = _workflow(tmp_path).run(IndustryTarget(symbol="600519.SH"), as_of_date="2026-08-15")

    artifact = result.artifact
    assert artifact is not None
    assert artifact.industry == "白酒"
    assert artifact.classification_standard == "sw_l1"
    assert artifact.industry_code == "801120.SI"
    assert artifact.degraded is False
    assert artifact.stale is False
    assert artifact.peer_count == 6
    assert len(artifact.peer_universe) == 6  # 成分股代码可复现

    # 单位契约：roe/debt/gross 为 RATIO，revenue_yoy 为百分比（百分点）
    assert artifact.financial_benchmarks["industry_avg_roe"] == pytest.approx(0.15)
    assert artifact.financial_benchmarks["industry_avg_debt_ratio"] == pytest.approx(0.50)
    assert artifact.financial_benchmarks["industry_avg_gross_margin"] == pytest.approx(0.35)
    assert artifact.growth_benchmarks["industry_revenue_yoy"] == pytest.approx(15.0)
    # 估值：成分股中位数优先（pe 20..30 → 25.0；pb 3..8 → 5.5），快照仅兜底
    assert artifact.valuation_benchmarks["industry_pe_ttm"] == pytest.approx(25.0)
    assert artifact.valuation_benchmarks["industry_pb"] == pytest.approx(5.5)
    assert any("valuation:peer_median" in ref for ref in artifact.source_refs)

    # 审核：无 note → approved，confidence 0.8，stale_after 取最早到期（估值 30d）
    assert result.review.status == "approved"
    assert result.review.confidence == pytest.approx(0.8)
    assert artifact.review_status == "approved"
    assert artifact.stale_after == (date(2026, 8, 15) + timedelta(days=30)).isoformat()

    # 已落盘且可读回
    assert result.persist_path is not None
    loaded = IndustryProfileStore(root=tmp_path).load("sw_l1", "801120.SI")
    assert loaded is not None
    assert loaded == artifact


def test_workflow_string_target_equivalent(tmp_path, monkeypatch):
    _patch_collect(monkeypatch, _ind_fact(), _peer_rows(6))
    result = _workflow(tmp_path).run("600519.SH")
    assert result.artifact is not None
    assert result.artifact.industry == "白酒"


def test_workflow_direct_target(tmp_path, monkeypatch):
    import alphabee.providers.industry as providers_module

    monkeypatch.setattr(
        providers_module,
        "get_industry_daily",
        lambda sw_code, industry: type(
            "R",
            (),
            {
                "daily": [{"industry_pe_ttm": 22.0, "industry_pb": 3.0, "trade_date": "2026-08-15"}],
                "source": "sw_daily",
            },
        )(),
    )
    _patch_collect(monkeypatch, rows=_peer_rows(5))
    result = _workflow(tmp_path).run(
        IndustryTarget(
            classification_standard="sw_l1",
            industry_code="801120.SI",
            industry_name="白酒",
        ),
        as_of_date="2026-08-15",
    )
    artifact = result.artifact
    assert artifact is not None
    assert artifact.industry == "白酒"
    # 直接目标：估值快照 22.0 存在但成分股中位数优先（5 只 pe 20..28 → 24.0）
    assert artifact.valuation_benchmarks["industry_pe_ttm"] == pytest.approx(24.0)


# ── 降级路径 ───────────────────────────────────────────────────────────────


def test_workflow_identity_failure_rejected_no_artifact(tmp_path, monkeypatch):
    _patch_collect(monkeypatch, {"industry": "", "sw_code": None, "sw_daily": []})
    result = _workflow(tmp_path).run(IndustryTarget(symbol="600519.SH"))

    assert result.review.status == "rejected"
    assert result.artifact is None
    assert result.persist_path is None
    assert result.degraded is True
    assert IndustryProfileStore(root=tmp_path).load("sw_l1", "801120.SI") is None


def test_workflow_peers_failure_degraded_artifact(tmp_path, monkeypatch):
    _patch_collect(monkeypatch, _ind_fact(pe=25.0, pb=6.0), [], error="index_member 空成分列表")
    result = _workflow(tmp_path).run(IndustryTarget(symbol="600519.SH"), as_of_date="2026-08-15")

    artifact = result.artifact
    assert artifact is not None  # 仍产出（估值快照有效）
    assert artifact.degraded is True
    assert "index_member" in artifact.degraded_reason
    assert artifact.valuation_benchmarks["industry_pe_ttm"] == pytest.approx(25.0)
    assert artifact.peer_count is None
    assert result.review.status == "needs_review"
    assert result.review.confidence <= 0.6


def test_workflow_mixed_periods_blocks_growth(tmp_path, monkeypatch):
    rows = _peer_rows(4, end_date="20251231") + _peer_rows(4, end_date="20250630")
    _patch_collect(monkeypatch, _ind_fact(), rows)
    result = _workflow(tmp_path).run(IndustryTarget(symbol="600519.SH"), as_of_date="2026-08-15")

    artifact = result.artifact
    assert artifact is not None
    # B3 严格：报告期 mixed → growth 基准置空 → 注入时被跳过 → 下游 market_share_change 回到 blocked
    assert artifact.growth_benchmarks["industry_revenue_yoy"] is None
    assert "industry_revenue_yoy" not in artifact.benchmark_fact_values()
    assert any("报告期" in note or "口径" in note for note in artifact.review_notes)
    assert artifact.review_status == "needs_review"


def test_workflow_qualitative_llm_failure_falls_back_empty(tmp_path, monkeypatch):
    import alphabee.utils.llm as llm_module

    def boom(component, **kwargs):
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(llm_module, "create_chat_model", boom)
    _patch_collect(monkeypatch, _ind_fact(), _peer_rows(6))
    result = _workflow(tmp_path).run(
        IndustryTarget(symbol="600519.SH"),
        qualitative_mode="llm",
    )

    artifact = result.artifact
    assert artifact is not None
    assert result.qualitative.business_model_summary == ""  # 回退空块
    assert result.qualitative.synthesized_by == "none"
    assert any("LLM" in note for note in result.qualitative.synthesis_notes)


# ── 定性默认关闭（v1 划界）────────────────────────────────────────────────


def test_workflow_qualitative_default_off(tmp_path, monkeypatch):
    _patch_collect(monkeypatch, _ind_fact(), _peer_rows(6))
    result = _workflow(tmp_path).run(IndustryTarget(symbol="600519.SH"))

    assert result.qualitative.business_model_summary == ""
    assert result.qualitative.key_drivers == []
    assert result.qualitative.risk_factors == []
    assert result.qualitative.industry_chain == {}
    # 生命周期启发式仍在确定性路径产出
    assert result.qualitative.lifecycle_stage == "成长期"
    assert any("默认关闭" in note for note in result.qualitative.synthesis_notes)
