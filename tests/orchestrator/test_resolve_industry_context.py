"""resolve_industry_context 节点测试（industry-context Phase 0 垂直切片）。

契约：
- 行业识别成功 + 成分股基准可得 → artifact（degraded=false）+ 基准注入 fact_values；
- 行业识别失败/未知 → industry_context_missing issue，无 artifact，step SKIPPED；
- 行业已知但成分股基准不可得 → artifact（degraded=true）+ industry_benchmarks_missing issue。
"""

import asyncio

from alphabee.core import ArtifactType, IssueSeverity, Run, RunStatus
from alphabee.orchestrator.contracts import IndustryContextArtifact, find_artifact_model
from alphabee.orchestrator.nodes import resolve_industry_context as node


def _run():
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "query": "分析贵州茅台"},
    )


def _state():
    return {"run": _run(), "steps": [], "artifacts": [], "issues": [], "decisions": []}


def _patch_industry_fact(monkeypatch, ind_fact):
    import alphabee.agents.facts.tools.industry_fact as industry_fact_module

    monkeypatch.setattr(industry_fact_module, "get_industry_fact", lambda symbol: ind_fact)


def _patch_peers(monkeypatch, records, error=None):
    import alphabee.industry.data as data_module

    monkeypatch.setattr(data_module, "fetch_peer_financials", lambda *a, **k: (records, error))


def _run_node(monkeypatch, ind_fact, records, error=None) -> dict:
    _patch_industry_fact(monkeypatch, ind_fact)
    _patch_peers(monkeypatch, records, error)
    return asyncio.run(node.resolve_industry_context(_state(), {}))


def _ind_fact(industry="白酒", sw_code="801120.SI", pe=None, pb=None):
    sw_daily = []
    if pe is not None or pb is not None:
        sw_daily = [{"industry_pe_ttm": pe, "industry_pb": pb}]
    return {"industry": industry, "sector": "消费", "sw_code": sw_code, "sw_daily": sw_daily}


# 成分股行：**源单位（百分比）**输入键（adapter 重命名后的列名：revenue_yoy / roe /
# debt_to_assets / gross_margin）——节点内部经 normalize 统一为 canonical（RATIO 口径），
# 见 docs/industry-context-phase1-design.md §2.1 的单位契约。
def _source_unit_peers():
    return [
        {"revenue_yoy": 10.0, "roe": 12.0, "debt_to_assets": 40.0, "gross_margin": 30.0},
        {"revenue_yoy": 20.0, "roe": 16.0, "debt_to_assets": 60.0, "gross_margin": 35.0},
        {"revenue_yoy": 30.0, "roe": 20.0, "debt_to_assets": 80.0, "gross_margin": 40.0},
    ]


# ── 成功路径 ───────────────────────────────────────────────────────────────


def test_success_injects_benchmarks_and_artifact(monkeypatch):
    result = _run_node(monkeypatch, _ind_fact(pe=25.0, pb=6.0), _source_unit_peers())

    artifact = find_artifact_model(result["artifacts"], ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    assert artifact is not None
    assert artifact.industry == "白酒"
    assert artifact.degraded is False
    assert artifact.peer_count == 3
    # v2 形状：三组基准字典（canonical 键，RATIO 口径）
    assert artifact.financial_benchmarks["industry_avg_debt_ratio"] == 0.60
    assert artifact.financial_benchmarks["industry_avg_roe"] == 0.16
    assert artifact.valuation_benchmarks["industry_pe_ttm"] == 25.0
    assert artifact.growth_benchmarks["industry_revenue_yoy"] == 20.0  # 百分比（百分点）口径

    # 数值基准注入 fact_values（供 derived facts / signals 引用）
    assert result["fact_values"]["industry_revenue_yoy"] == 20.0
    assert result["fact_values"]["industry_avg_roe"] == 0.16
    assert result["fact_values"]["industry_avg_debt_ratio"] == 0.60
    assert result["fact_values"]["industry_pe_ttm"] == 25.0
    assert not [i for i in result["issues"] if "industry" in i.category]


def test_success_step_status_succeeded(monkeypatch):
    result = _run_node(monkeypatch, _ind_fact(), [{"roe": 0.1}])
    step = result["steps"][0]
    assert step.id == "resolve_industry_context"
    assert step.outputs  # 已 finalize


def test_peer_median_valuation_overrides_snapshot(monkeypatch):
    # 成分股中位数估值优先于指数快照（pe [20,30,40] → 30.0，pb [3,5,7] → 5.0）
    peers = [
        {
            "revenue_yoy": 10.0,
            "roe": 12.0,
            "debt_to_assets": 40.0,
            "gross_margin": 30.0,
            "pe_ttm": 20.0,
            "pb_ratio": 3.0,
        },
        {
            "revenue_yoy": 20.0,
            "roe": 16.0,
            "debt_to_assets": 60.0,
            "gross_margin": 35.0,
            "pe_ttm": 30.0,
            "pb_ratio": 5.0,
        },
        {
            "revenue_yoy": 30.0,
            "roe": 20.0,
            "debt_to_assets": 80.0,
            "gross_margin": 40.0,
            "pe_ttm": 40.0,
            "pb_ratio": 7.0,
        },
    ]
    result = _run_node(monkeypatch, _ind_fact(pe=25.0, pb=6.0), peers)

    artifact = find_artifact_model(result["artifacts"], ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    assert artifact.valuation_benchmarks["industry_pe_ttm"] == 30.0
    assert artifact.valuation_benchmarks["industry_pb"] == 5.0
    assert result["fact_values"]["industry_pe_ttm"] == 30.0
    assert any("peer_median" in ref for ref in artifact.source_refs)


# ── 降级路径 ───────────────────────────────────────────────────────────────


def test_industry_unknown_degrades_with_issue(monkeypatch):
    result = _run_node(monkeypatch, {"industry": "", "sw_code": None, "sw_daily": []}, [])

    step = result["steps"][0]
    assert step.status.value == "skipped"
    artifacts = result.get("artifacts", [])
    assert find_artifact_model(artifacts, ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact) is None
    issues = [i for i in result["issues"] if i.category == "industry_context_missing"]
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM


def test_industry_fact_failure_degrades_with_issue(monkeypatch):
    import alphabee.agents.facts.tools.industry_fact as industry_fact_module

    def boom(symbol):
        raise RuntimeError("tushare down")

    monkeypatch.setattr(industry_fact_module, "get_industry_fact", boom)
    result = asyncio.run(node.resolve_industry_context(_state(), {}))

    issues = [i for i in result["issues"] if i.category == "industry_context_missing"]
    assert len(issues) == 1
    assert "tushare down" in issues[0].message
    assert result["steps"][0].status.value == "skipped"


def test_peer_fetch_failure_marks_degraded_artifact(monkeypatch):
    # 拿不到成分股财务 → artifact 仍产出（估值快照有效）但 degraded=true
    result = _run_node(monkeypatch, _ind_fact(pe=25.0, pb=6.0), [], error="index_member 空成分列表")

    artifact = find_artifact_model(result["artifacts"], ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    assert artifact is not None
    assert artifact.degraded is True
    assert "index_member" in artifact.degraded_reason
    # 估值快照仍透传（artifact + fact_values）
    assert artifact.valuation_benchmarks["industry_pe_ttm"] == 25.0
    assert result["fact_values"]["industry_pe_ttm"] == 25.0
    # 财务基准不注入
    assert "industry_avg_debt_ratio" not in result["fact_values"]
    issues = [i for i in result["issues"] if i.category == "industry_benchmarks_missing"]
    assert len(issues) == 1


def test_peers_without_useful_fields_marks_degraded(monkeypatch):
    peers = [{"foo": 1}, {"bar": 2}]  # 没有可用财务字段
    result = _run_node(monkeypatch, _ind_fact(), peers)

    artifact = find_artifact_model(result["artifacts"], ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    assert artifact.degraded is True
    assert result["fact_values"] == {}  # 无可注入基准
