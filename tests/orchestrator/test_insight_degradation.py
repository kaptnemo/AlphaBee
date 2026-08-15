"""Insight degradation tests (ROADMAP 0.4, see docs/INSIGHT_DEGRADATION_DESIGN.md).

四级降级阶梯契约：
- Tier 0: 严格解析成功 → degraded=false，无 issue
- Tier 1: lenient_parse 宽松救援（修结构不补内容）→ degraded=true, fallback_tier=1
- Tier 2: build_fallback_insight 确定性兜底（只转述 context）→ tier=2, confidence="low"
- Tier 3: 最小骨架（agent 失败 / 空响应 / 上下文为空）→ tier=3

节点级契约：任何失败模式下 INSIGHT_ANALYSIS artifact 必然存在。
"""

import asyncio
import json as _json

from alphabee.agents.insights.rescue import build_fallback_insight, build_minimal_insight, lenient_parse
from alphabee.core import ArtifactType, Run, RunStatus
from alphabee.orchestrator.contracts import InsightArtifact, find_artifact_model
from alphabee.orchestrator.nodes import insights as insights_node
from alphabee.orchestrator.services import payload_builders

# ── fixtures ──────────────────────────────────────────────────────────────


def _base_run():
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "query": "分析贵州茅台"},
    )


def _state() -> dict:
    return {
        "run": _base_run(),
        "steps": [],
        "artifacts": [],
        "issues": [],
        "decisions": [],
    }


def _fake_agent_with_content(content: str):
    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [type("Msg", (), {"content": content})()]}

    return FakeAgent()


def _patch_agent(monkeypatch, content: str, context: dict | None = None):
    monkeypatch.setattr(insights_node, "build_insight_context", lambda state, symbol: context or {})
    monkeypatch.setattr(
        __import__("alphabee.agents.insights.agent", fromlist=["insight_agent_factory"]),
        "insight_agent_factory",
        lambda: _fake_agent_with_content(content),
    )


def _run_node(monkeypatch, content: str, context: dict | None = None) -> dict:
    _patch_agent(monkeypatch, content, context)
    return asyncio.run(insights_node.synthesize_insights(_state(), {}))


def _insight_from(result: dict) -> InsightArtifact:
    artifact = find_artifact_model(result["artifacts"], ArtifactType.INSIGHT_ANALYSIS, InsightArtifact)
    assert artifact is not None, "INSIGHT_ANALYSIS artifact must exist"
    return artifact


# ── Tier 0：严格解析 ──────────────────────────────────────────────────────


def test_lenient_parse_full_valid_payload_is_not_degraded():
    """合法完整 payload 不应触发任何修补（修复点为空 → 仍可解析）。"""
    raw = _json.dumps(
        {
            "core_view": "增长质量下降，估值承压",
            "central_tension": "高成长定价 vs 财务质量恶化",
            "main_driver": "应收账款回收",
            "supporting_evidence": [{"statement": "应收增速高于收入", "source": "signal:receivable_quality"}],
            "counter_evidence": [{"statement": "毛利率稳定", "source": "derived_fact:gross_margin"}],
            "what_would_change_my_mind": ["若应收账龄改善则推翻负面判断"],
            "confidence": "medium",
        },
        ensure_ascii=False,
    )
    rescued = lenient_parse(raw)
    assert rescued is not None
    output, reason = rescued
    assert output.confidence == "medium"
    assert len(output.supporting_evidence) == 1
    assert output.supporting_evidence[0].statement == "应收增速高于收入"


# ── Tier 1：宽松救援（单元级） ────────────────────────────────────────────


def test_lenient_rescue_missing_field_and_string_evidence():
    raw = _json.dumps(
        {
            "core_view": "观点A",
            "central_tension": "矛盾B",
            # main_driver 缺失
            "supporting_evidence": ["证据字符串1", "证据字符串2"],
            "counter_evidence": [{"statement": "反证", "source": "s1", "weight": "significant"}],
            "materiality_rank": [{"variable": "毛利率", "importance": "major", "reasoning": "定价权"}],
            "what_would_change_my_mind": [{"condition": "若毛利率下滑则改变判断"}],
            "confidence": "moderate",
        },
        ensure_ascii=False,
    )
    rescued = lenient_parse(raw)
    assert rescued is not None
    output, reason = rescued
    assert output.core_view == "观点A"
    assert output.central_tension == "矛盾B"
    assert output.main_driver == ""  # 只修结构不补内容
    assert len(output.supporting_evidence) == 2
    assert output.supporting_evidence[0].source == "insight:raw"
    assert output.counter_evidence[0].weight == "strong"  # significant → strong
    assert output.materiality_rank[0].importance == "high"  # major → high
    assert output.what_would_change_my_mind == ["若毛利率下滑则改变判断"]
    assert output.confidence == "medium"  # moderate → medium
    assert "lenient_rescue" in reason


def test_lenient_rescue_unwraps_nested_payload():
    raw = _json.dumps(
        {"insight": {"core_view": "观点", "central_tension": "矛盾", "main_driver": "驱动"}},
        ensure_ascii=False,
    )
    rescued = lenient_parse(raw)
    assert rescued is not None
    output, _ = rescued
    assert output.core_view == "观点"
    assert output.main_driver == "驱动"


def test_lenient_rescue_garbage_returns_none():
    assert lenient_parse("这不是 JSON") is None
    assert lenient_parse('["也是列表"]') is None
    assert lenient_parse('{"core_view": 123}') is not None  # 数字文本可强转，仍可救援


# ── Tier 2：确定性兜底（单元级） ──────────────────────────────────────────


def _rich_context() -> dict:
    return {
        "symbol": "600519.SH",
        "company": {
            "industry": "白酒",
            "sub_industry": "白酒",
            "market_cap_category": "large",
            "lifecycle_stage": "成熟期",
        },
        "latest_snapshot": {
            "period": "2024Q4",
            "revenue_yoy": 0.15,
            "net_profit_yoy": 0.12,
            "gross_margin": 0.9,
        },
        "market_valuation": {"pe_ttm": 30.0},
        "key_signals": [
            {"signal_id": "cashflow_quality", "level": "high", "interpretation": "经营现金流持续低于净利润"},
            {"signal_id": "receivable_quality", "level": "medium", "interpretation": "应收增速快于收入"},
        ],
        "key_derived_facts": {
            "operating_cashflow_ratio": {"value": 0.6, "level": "high", "interpretation": "现金流覆盖率不足"},
            "gross_margin_yoy": {"value": 0.01, "level": "medium", "interpretation": "毛利率微升"},
        },
        "anomaly": {
            "anomaly_count": 1,
            "pattern_count": 1,
            "top_anomalies": [{"metric": "ocf_ratio", "level": "high", "z_score": 3.1}],
            "pattern_matches": [{"name": "现金流与利润背离", "severity": "high"}],
        },
        "conflicts": [
            {
                "theme": "盈利增长但现金流恶化",
                "severity": "high",
                "description": "利润增长没有被现金流验证。",
                "related_dimensions": ["earnings_quality"],
                "hypotheses": [
                    {
                        "id": "h1",
                        "explanation": "收入确认前置",
                        "status": "verified",
                        "summary": "现金流未能验证利润增长",
                        "gaps": ["缺少同行数据"],
                        "predictions": ["经营现金流/净利润持续低于1"],
                    }
                ],
            }
        ],
        "verified_count": 1,
        "rejected_count": 0,
    }


def test_fallback_synthesizes_from_context():
    output = build_fallback_insight(_rich_context(), "600519.SH")
    assert output.confidence == "low"  # H3
    assert "盈利增长但现金流恶化" in output.central_tension  # verified 冲突主题
    assert output.main_driver == "operating_cashflow_ratio"  # 最高等级衍生指标
    assert output.supporting_evidence[0].source == "signal:cashflow_quality"
    assert output.counter_evidence[0].source == "conflict:盈利增长但现金流恶化"
    # verified 假设的 predictions 是可证伪条件来源
    assert any("经营现金流/净利润持续低于1" in c for c in output.what_would_change_my_mind)
    assert output.bull_case == "" and output.bear_case == ""  # H2：不虚构情景
    assert "行业: 白酒" in output.business_model_context
    assert "高风险信号" in output.core_view


def test_fallback_honesty_no_fabrication():
    """H1：supporting/counter evidence 的每条陈述必须能在 context 中找到来源。"""
    context = _rich_context()
    output = build_fallback_insight(context, "600519.SH")
    all_context_values: list[str] = []

    def collect(obj):
        if isinstance(obj, str):
            all_context_values.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                collect(v)
        elif isinstance(obj, list):
            for v in obj:
                collect(v)

    collect(context)
    # 拼接成一个大字符串（含子串匹配）
    blob = "\n".join(all_context_values)

    for item in output.supporting_evidence:
        assert item.statement in blob, f"虚构来源: {item.statement}"
    for item in output.counter_evidence:
        assert item.statement in blob, f"虚构来源: {item.statement}"


def test_fallback_empty_context_is_minimal():
    output = build_fallback_insight({}, "600519.SH")
    assert output.confidence == "low"
    # 数据缺失 ≠ 数据健康：空上下文不输出任何断言
    assert output.core_view == ""
    assert output.central_tension == ""
    assert output.supporting_evidence == []
    assert output.bull_case == ""


# ── Tier 3：最小骨架 ──────────────────────────────────────────────────────


def test_minimal_skeleton():
    output = build_minimal_insight("600519.SH", "empty_context")
    assert output.core_view == ""
    assert output.confidence == "low"
    assert output.what_would_change_my_mind == []


# ── 节点级：任何失败模式 artifact 必然存在 ────────────────────────────────


def test_node_tier0_success(monkeypatch):
    payload = {
        "core_view": "观点",
        "central_tension": "矛盾",
        "main_driver": "驱动",
        "what_would_change_my_mind": ["条件1"],
        "confidence": "high",
    }
    result = _run_node(monkeypatch, _json.dumps(payload, ensure_ascii=False), _rich_context())
    insight = _insight_from(result)
    assert insight.degraded is False
    assert insight.fallback_tier == 0
    assert insight.core_view == "观点"
    assert not [i for i in result["issues"] if i.category == "insight_degraded"]


def test_node_tier1_rescues_bad_json(monkeypatch):
    raw = _json.dumps(
        {
            "core_view": "观点",
            "central_tension": "矛盾",
            "supporting_evidence": ["字符串证据"],
            "what_would_change_my_mind": [{"condition": "条件X"}],
        },
        ensure_ascii=False,
    )
    result = _run_node(monkeypatch, raw, _rich_context())
    insight = _insight_from(result)
    assert insight.degraded is True
    assert insight.fallback_tier == 1
    assert insight.main_driver == ""  # 不补内容
    assert insight.supporting_evidence[0]["source"] == "insight:raw"
    assert [i for i in result["issues"] if i.category == "insight_degraded"]


def test_node_tier2_deterministic_fallback(monkeypatch):
    # agent 返回完全不可解析的内容 → 确定性兜底
    result = _run_node(monkeypatch, "这不是 JSON，也不是任何结构", _rich_context())
    insight = _insight_from(result)
    assert insight.degraded is True
    assert insight.fallback_tier == 2
    assert insight.confidence == "low"
    assert "盈利增长但现金流恶化" in insight.central_tension
    assert [i for i in result["issues"] if i.category == "insight_degraded"]


def test_node_tier2_fallback_feeds_report_payload(monkeypatch):
    """降级 artifact 必须能被 report payload 消费（degraded 透传）。"""
    result = _run_node(monkeypatch, "garbage", _rich_context())
    state = {**_state(), "artifacts": result["artifacts"]}
    payload = payload_builders.build_report_generation_payload(state)
    assert payload.insight is not None
    assert payload.insight.degraded is True
    assert payload.insight.core_view != ""


def test_node_tier3_empty_context(monkeypatch):
    result = _run_node(monkeypatch, "garbage", {})
    insight = _insight_from(result)
    assert insight.degraded is True
    assert insight.fallback_tier == 3
    assert insight.core_view == ""
    assert [i for i in result["issues"] if i.category == "insight_degraded"]


def test_node_tier3_empty_response(monkeypatch):
    result = _run_node(monkeypatch, "", _rich_context())
    insight = _insight_from(result)
    assert insight.degraded is True
    assert insight.fallback_tier == 3
    assert insight.degradation_reason == "empty_response"
