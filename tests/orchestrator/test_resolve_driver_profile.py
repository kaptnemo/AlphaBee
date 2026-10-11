"""resolve_driver_profile 节点测试（DOMAIN_CONTEXT P0 第 5 步 + D1 Tier B 接线）。"""

import asyncio
import json as _json
import sys
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from alphabee.agents.driver_profile.output import (
    DriverResearchOutput,
    ResearchEvidence,
    ResearchHypothesis,
)
from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.config import DomainContextResearchSettings
from alphabee.core import Artifact, ArtifactType, IssueSeverity, Run, RunStatus
from alphabee.domain_context import DriverProfile, RouterInput, build_driver_profile
from alphabee.domain_context import research as research_module
from alphabee.orchestrator.contracts import IndustryContextArtifact
from alphabee.orchestrator.nodes import resolve_driver_profile as node
from alphabee.orchestrator.services import degradation as degradation_module
from alphabee.orchestrator.state import OrchestratorState


def _run(symbol="002714.SZ"):
    return Run(
        id="run-1",
        goal="分析牧原股份",
        status=RunStatus.RUNNING,
        context={"symbol": symbol, "query": "分析牧原股份"},
    )


def _state(symbol="002714.SZ", artifacts=None):
    return {
        "run": _run(symbol),
        "steps": [],
        "artifacts": artifacts or [],
        "issues": [],
        "decisions": [],
    }


def _industry_artifact(industry="农林牧渔", sub_industry="养殖业", sw_code="801010.SI"):
    return Artifact(
        id="a-ind",
        type=ArtifactType.INDUSTRY_CONTEXT,
        producer_step="resolve_industry_context",
        value=IndustryContextArtifact(
            industry=industry,
            sub_industry=sub_industry,
            sw_code=sw_code,
        ).model_dump(mode="json"),
    )


def _track_artifact(track_label="生猪养殖", business_model="other"):
    return Artifact(
        id="a-track",
        type=ArtifactType.COMPANY_TRACK,
        producer_step="resolve_company_track",
        value=CompanyTrackArtifact(
            symbol="002714.SZ",
            track_label=track_label,
            business_model=business_model,
        ).model_dump(mode="json"),
    )


def _find_driver_profile(result):
    for artifact in result.get("artifacts", []):
        if artifact.type == ArtifactType.DRIVER_PROFILE:
            return DriverProfile.model_validate(artifact.value)
    return None


def test_muyuan_routes_to_hog_cycle():
    state = _state(artifacts=[_industry_artifact(), _track_artifact()])
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile is not None
    assert profile.playbook == "hog_cycle"
    assert profile.fallback is False
    assert profile.degraded is False
    assert profile.primary_drivers == ["猪价", "能繁母猪存栏", "完全成本"]
    # 正常命中：无 issue
    assert not result.get("issues")


def test_jinchengxin_routes_to_mining_services():
    # 生产形状：sub_industry 恒为空串，命中只能来自 track_label（夹具保真，G-2）
    state = _state(
        symbol="603979.SH",
        artifacts=[
            _industry_artifact(industry="建筑装饰", sub_industry="", sw_code="801720.SI"),
            _track_artifact(track_label="矿业服务", business_model="integrator"),
        ],
    )
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile.playbook == "mining_services"
    assert profile.fallback is False
    assert profile.why_selected == ["track_label_match"]
    assert profile.anchor_strength == "strong"


def test_no_identity_signals_degrades():
    # 上游无 INDUSTRY_CONTEXT / COMPANY_TRACK → 身份信号全空 → 降级 + fallback
    state = _state(symbol="000001.SZ", artifacts=[])
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile is not None
    assert profile.playbook == "generic_fundamental"
    assert profile.fallback is True
    assert profile.degraded is True
    issues = [i for i in result.get("issues", []) if i.category == "driver_profile_degraded"]
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM


def test_normal_no_match_is_fallback_not_degraded(monkeypatch):
    # 有身份信号但命中不了专用框架（白酒）→ fallback，但不降级、不产 issue
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=False))
    state = _state(
        symbol="600519.SH",
        artifacts=[
            _industry_artifact(industry="食品饮料", sub_industry="白酒Ⅱ", sw_code="801120.SI"),
            _track_artifact(track_label="白酒", business_model="brand"),
        ],
    )
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile.playbook == "generic_fundamental"
    assert profile.fallback is True
    assert profile.degraded is False
    assert not result.get("issues")


def test_no_symbol_skips():
    result = asyncio.run(node.resolve_driver_profile(_state(symbol=None), {}))
    assert result["steps"][0].status.value == "skipped"
    assert not result.get("artifacts")


def test_build_driver_profile_summary():
    from alphabee.orchestrator.services.payload_builders import _build_driver_profile_summary

    profile = build_driver_profile(
        "002714.SZ",
        RouterInput(track_label="生猪养殖", industry="农林牧渔", sub_industry="养殖业"),
    )
    artifacts = [
        Artifact(
            id="a-dp",
            type=ArtifactType.DRIVER_PROFILE,
            producer_step="resolve_driver_profile",
            value=profile.model_dump(mode="json"),
        )
    ]
    summary = _build_driver_profile_summary(artifacts)
    assert summary["playbook"] == "hog_cycle"
    assert summary["fallback"] is False
    assert summary["primary_drivers"] == ["猪价", "能繁母猪存栏", "完全成本"]
    assert len(summary["activated_primitives"]) == 4
    # 无 DRIVER_PROFILE artifact → 空 dict（不抛异常）
    assert _build_driver_profile_summary([]) == {}


# ── 结构信号组装（sw_code / 分部结构 / 财务结构进 RouterInput）──────────


def _track_artifact_with_segments(
    track_label: str = "白酒",
    business_model: str = "brand",
) -> Artifact:
    """带分部结构的 COMPANY_TRACK（占比/同比齐备，含非最新期以验证只取最新期）。"""
    track = CompanyTrackArtifact(
        symbol="600519.SH",
        track_label=track_label,
        business_model=business_model,
        dominant_segment="高端白酒",
        fastest_segment="次高端",
        segments=[
            SegmentSnapshot(
                report_date="20241231", segment_name="高端白酒", revenue=150.0, revenue_share=60.0, revenue_yoy=5.0
            ),
            SegmentSnapshot(
                report_date="20251231", segment_name="高端白酒", revenue=200.0, revenue_share=42.0, revenue_yoy=12.0
            ),
            SegmentSnapshot(
                report_date="20251231", segment_name="次高端", revenue=100.0, revenue_share=21.0, revenue_yoy=58.0
            ),
        ],
    )
    return Artifact(
        id="a-track",
        type=ArtifactType.COMPANY_TRACK,
        producer_step="resolve_company_track",
        value=track.model_dump(mode="json"),
    )


def test_node_assembles_structure_signals_into_router_input(monkeypatch):
    captured: dict[str, RouterInput] = {}
    real_build = node.build_driver_profile

    def _spy(symbol, router_input, **kwargs):
        captured["inp"] = router_input
        return real_build(symbol, router_input, **kwargs)

    monkeypatch.setattr(node, "build_driver_profile", _spy)

    state = _state(
        symbol="600519.SH",
        artifacts=[
            _industry_artifact(industry="食品饮料", sub_industry="", sw_code="801120.SI"),
            _track_artifact_with_segments(),
        ],
    )
    state["fact_values"] = {
        "gross_margin": 35.0,
        "roe": 12.0,
        # canonical 基准键 → 进 RouterInput 的 peer_gross_margin（RATIO → ×100 归一为 PERCENT）
        "peer_avg_gross_margin": 0.30,
        # 外部/非 canonical 键名必须被丢弃（不泄漏数据源字段名）
        "grossprofit_margin": 999.0,
    }
    asyncio.run(node.resolve_driver_profile(state, {}))

    inp = captured["inp"]
    assert inp.sw_code == "801120.SI"
    assert inp.dominant_segment == "高端白酒"
    assert inp.dominant_share == 42.0
    assert inp.fastest_segment == "次高端"
    assert inp.fastest_yoy == 58.0
    # 只取最新报告期，且按占比降序
    assert inp.segment_summary == ["高端白酒 42%(+12%)", "次高端 21%(+58%)"]
    assert inp.financial_structure == {
        "gross_margin": 35.0,
        "roe": 12.0,
        "peer_gross_margin": 30.0,
    }


def test_node_sw_code_upgrades_anchor_to_strong():
    # 赛道标签匹配不上、宽 L1 行业名只算弱锚；靠 INDUSTRY_CONTEXT.sw_code 才升为强锚
    state = _state(
        artifacts=[
            _industry_artifact(industry="农林牧渔", sub_industry="", sw_code="801010.SI"),
            _track_artifact(track_label="其他赛道"),
        ]
    )
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile.playbook == "hog_cycle"
    assert profile.anchor_strength == "strong"
    assert "sw_code_match" in profile.why_selected


def test_node_structure_facts_drive_fallback_primary_drivers(monkeypatch):
    # 白酒命中不了专用框架 → 兜底；主驱动由分部结构 + 毛利率差（含 canonical 基准）确定性推导
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=False))
    state = _state(
        symbol="600519.SH",
        artifacts=[
            _industry_artifact(industry="食品饮料", sub_industry="", sw_code="801120.SI"),
            _track_artifact_with_segments(),
        ],
    )
    state["fact_values"] = {"gross_margin": 35.0, "peer_avg_gross_margin": 0.30}
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile.fallback is True
    assert profile.degraded is False
    assert profile.primary_drivers == [
        "主力分部 高端白酒 收入占比 42%",
        "次高端 同比 58%（快于主力）",
        "毛利率 35.0%（行业基准 30.0%，差 +5.0pp）",
    ]


# ── G-10：画像构建失败不得抛穿 LangGraph ────────────────────────────────


def test_primitive_load_failure_degrades_without_raising(monkeypatch):
    import alphabee.domain_context.driver_profile as driver_profile_module

    def _boom(*args, **kwargs):
        raise RuntimeError("primitive 目录缺失")

    monkeypatch.setattr(driver_profile_module, "load_primitives", _boom)

    state = _state(artifacts=[_industry_artifact(), _track_artifact()])
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile is not None
    assert profile.playbook == "generic_fundamental"
    assert profile.fallback is True
    assert profile.degraded is True
    assert profile.degraded_reason.startswith("driver_profile_build_failed")

    issues = [i for i in result.get("issues", []) if i.category == "driver_profile_degraded"]
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM
    # 有 issue + artifact → PARTIAL（不抛穿、也不静默）
    assert result["steps"][0].status.value == "partial"


# ── payload summary 补全（G-6）─────────────────────────────────────────


def test_driver_profile_summary_exposes_primitives_and_research_fields():
    from alphabee.domain_context.contracts import DriverHypothesis, ResearchQuestion
    from alphabee.orchestrator.services.payload_builders import _build_driver_profile_summary

    profile = build_driver_profile("002714.SZ", RouterInput(track_label="生猪养殖"))
    profile = profile.model_copy(
        update={
            "driver_hypotheses": [DriverHypothesis(variable=f"v{i}") for i in range(10)],
            "research_agenda": [ResearchQuestion(question=f"q{i}") for i in range(8)],
            "novel_drivers": ["候选驱动A"],
            "unverified_drivers": ["未验证驱动B"],
        }
    )
    artifacts = [
        Artifact(
            id="a-dp",
            type=ArtifactType.DRIVER_PROFILE,
            producer_step="resolve_driver_profile",
            value=profile.model_dump(mode="json"),
        )
    ]
    summary = _build_driver_profile_summary(artifacts)

    # 原语内容（此前只传 id/priority_questions/report_angles）
    primitive = summary["activated_primitives"][0]
    assert primitive["key_variables"]
    assert primitive["causal_paths"]
    assert primitive["disconfirming_signals"]
    assert primitive["preferred_sources"]
    assert primitive["priority_questions"]
    assert primitive["report_angles"]
    # 顶层：来源 / 锚强度 / 命中依据
    assert summary["provenance"] == "rule"
    assert summary["anchor_strength"] == "strong"
    assert summary["why_selected"] == ["track_label_match"]
    # 研究层字段：截断 8 / 6，只取下游要用的键
    assert len(summary["driver_hypotheses"]) == 8
    assert summary["driver_hypotheses"][0] == {"variable": "v0", "role": "primary"}
    assert len(summary["research_agenda"]) == 6
    assert summary["research_agenda"][0] == {"question": "q0", "priority": "high"}
    assert summary["novel_drivers"] == ["候选驱动A"]
    assert summary["unverified_drivers"] == ["未验证驱动B"]


# ══════════════════════════════════════════════════════════════════════════
# Tier B 研究层接线（开关门控 / 输入组装 / 研究失败与降级留痕）
# ══════════════════════════════════════════════════════════════════════════


#: 白酒（有身份信号、命中不了专用框架）⇒ Tier B 的 no_anchor 触发分支。
def _research_trigger_state(state=None):
    """白酒：命中不了专用框架（fallback + anchor=none）⇒ should_research 为 no_anchor。"""
    state = state or _state(
        symbol="600519.SH",
        artifacts=[
            _industry_artifact(industry="食品饮料", sub_industry="", sw_code="801120.SI"),
            _track_artifact_with_segments(),
        ],
    )
    return state


def _config_stub(*, enabled: bool):
    """最小 Settings 桩（只供节点读 domain_context.research.enabled）。"""
    return SimpleNamespace(domain_context=SimpleNamespace(research=DomainContextResearchSettings(enabled=enabled)))


def _driver_profile_payload(result):
    for artifact in result.get("artifacts", []):
        if artifact.type == ArtifactType.DRIVER_PROFILE:
            return artifact.value
    raise AssertionError("DRIVER_PROFILE artifact 必须恒产出")


def _research_output_json(
    variable="高频高速覆铜板收入占比 × 铜箔-树脂价差",
    *,
    evidence=True,
    company_form="高速覆铜板收入占比 42%",
):
    hypothesis = ResearchHypothesis(
        variable=variable,
        role="primary",
        mechanism="AI 服务器放量 → 高毛利产品结构改善",
        company_form=company_form,
        observables=[f"{variable}（来源：annual_report:第三节）"],
        evidence=[ResearchEvidence(kind="annual_report", ref="2025 年报 第三节", quote="原文片段")] if evidence else [],
        confidence=0.8,
    )
    return _json.dumps(
        DriverResearchOutput(
            profit_formula="收入 = 销量 × 单价",
            driver_hypotheses=[hypothesis],
        ).model_dump(mode="json"),
        ensure_ascii=False,
    )


class _FakeAgent:
    """Mock 级研究代理：按顺序返回预设回复（字符串 → AIMessage，dict → 原样返回）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, str):
            return {"messages": [AIMessage(content=reply)]}
        return reply


def _patch_agent_factory(monkeypatch, agent):
    """范式 A：patch **源模块**的工厂属性（节点内延迟 import 只认源模块）。"""
    module = __import__("alphabee.agents.driver_profile.agent", fromlist=["driver_profile_research_agent_factory"])
    built = {"count": 0}

    def factory():
        built["count"] += 1
        return agent

    monkeypatch.setattr(module, "driver_profile_research_agent_factory", factory)
    return built


def _patch_research_settings(monkeypatch, tmp_path, **overrides):
    base = {
        "enabled": True,
        "cache_dir": str(tmp_path / "research_cache"),
        "wall_timeout_s": 5.0,
        "cache_ttl_days": 90,
    }
    base.update(overrides)
    cfg = DomainContextResearchSettings(**base)
    monkeypatch.setattr(research_module, "research_settings", lambda: cfg)
    return cfg


def _research_issues(result, category):
    return [issue for issue in result.get("issues", []) if issue.category == category]


def test_research_switch_not_declared_in_orchestrator_state():
    """开关走 state 的隐式键：不得为此新增 OrchestratorState 声明字段。"""
    assert "driver_research" not in OrchestratorState.__annotations__


def test_research_switches_config_default_off_and_state_override(monkeypatch):
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=False))
    assert node._research_switches({}) == {"enabled": False}
    # state 显式携带 → 覆盖 config（可只对某次 run 打开）
    assert node._research_switches({"driver_research": {"enabled": True}}) == {"enabled": True}

    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))
    assert node._research_switches({}) == {"enabled": True}
    assert node._research_switches({"driver_research": {"enabled": False}}) == {"enabled": False}

    def _boom():
        raise OSError("config.yaml 缺失")

    monkeypatch.setattr("alphabee.config.get_settings", _boom)
    # 配置不可读 ⇒ 保持默认关（研究层是可选增强，不该影响主链路）
    assert node._research_switches({}) == {"enabled": False}


def test_research_flag_off_is_strict_noop_matching_d0(monkeypatch):
    """flag 关：不 import 研究模块、不构造 agent、产物与 D0 逐字段一致。"""

    class _ExplodingResearchModule:
        def __getattr__(self, name):
            raise AssertionError(f"flag 关时不得导入/访问研究层：{name}")

    monkeypatch.setitem(sys.modules, "alphabee.domain_context.research", _ExplodingResearchModule())
    built = _patch_agent_factory(monkeypatch, _FakeAgent([_research_output_json()]))
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=False))

    result = asyncio.run(node.resolve_driver_profile(_state(artifacts=[_industry_artifact(), _track_artifact()]), {}))
    payload = _driver_profile_payload(result)

    # 与 D0 的纯组装逐字段一致（同一 RouterInput + 同一 generated_at）
    expected = build_driver_profile(
        "002714.SZ",
        RouterInput(
            track_label="生猪养殖",
            industry="农林牧渔",
            sub_industry="养殖业",
            business_model="other",
            sw_code="801010.SI",
        ),
        generated_at=payload["generated_at"],
    ).model_dump(mode="json")
    assert payload == expected
    assert payload["provenance"] == "rule"
    assert payload["research_meta"] == {}  # 无任何研究留痕（字段恒存在，故判空表）
    assert payload["driver_hypotheses"] == []
    assert payload["research_agenda"] == []
    assert built["count"] == 0
    assert not result.get("issues")
    assert result["steps"][0].status.value == "succeeded"


def test_research_flag_on_runs_should_research_to_converge(monkeypatch, tmp_path):
    """flag 开（state 覆盖 config）：should_research → run_driver_research → converge 链路接通。"""
    _patch_research_settings(monkeypatch, tmp_path)
    agent = _FakeAgent([_research_output_json()])
    built = _patch_agent_factory(monkeypatch, agent)
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=False))

    state = _research_trigger_state()
    state["driver_research"] = {"enabled": True}
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    payload = _driver_profile_payload(result)
    assert built["count"] == 1
    assert agent.calls == 1
    assert payload["provenance"] == "llm"
    assert payload["research_meta"]["trigger"] == "no_anchor"
    assert payload["research_meta"]["cached"] is False
    assert payload["research_meta"]["fallback_tier"] == 0
    assert payload["degraded"] is False
    assert [item["variable"] for item in payload["driver_hypotheses"]] == ["高频高速覆铜板收入占比 × 铜箔-树脂价差"]
    assert payload["primary_drivers"] == ["高频高速覆铜板收入占比 × 铜箔-树脂价差"]
    assert not result.get("issues")


def test_research_flag_on_without_trigger_keeps_d0_artifact(monkeypatch, tmp_path):
    """flag 开但未触发（强锚 + 单一主线 + 无漂移）⇒ 零 LLM 成本，产物仍是 D0。"""
    _patch_research_settings(monkeypatch, tmp_path)
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))
    calls = {"n": 0}

    async def _boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("未触发时不得调用研究层")

    monkeypatch.setattr(research_module, "run_driver_research", _boom)

    result = asyncio.run(node.resolve_driver_profile(_state(artifacts=[_industry_artifact(), _track_artifact()]), {}))
    payload = _driver_profile_payload(result)
    assert calls["n"] == 0
    assert payload["provenance"] == "rule"
    assert payload["research_meta"] == {}  # 无任何研究留痕（字段恒存在，故判空表）
    assert not result.get("issues")


def test_research_failure_degrades_with_issue_and_partial_step(monkeypatch, tmp_path):
    """研究失败（重试后仍解析失败）→ apply_degradation 降级 + MEDIUM issue + PARTIAL，不抛穿。"""
    _patch_research_settings(monkeypatch, tmp_path)
    agent = _FakeAgent(["我先说说研究思路", "仍然是自然语言"])
    _patch_agent_factory(monkeypatch, agent)
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    calls: list[tuple[int, str, int]] = []
    real_apply = degradation_module.apply_degradation

    def spy(payload, tier, reason, *, threshold=2):
        calls.append((tier, reason[:12], threshold))
        return real_apply(payload, tier, reason, threshold=threshold)

    monkeypatch.setattr(degradation_module, "apply_degradation", spy)

    result = asyncio.run(node.resolve_driver_profile(_research_trigger_state(), {}))
    payload = _driver_profile_payload(result)

    assert agent.calls == 2  # 重试一次后放弃
    assert payload["degraded"] is True
    assert payload["degradation_reason"].startswith("parse_failed")
    assert payload["research_meta"]["fallback_tier"] == 2
    assert payload["provenance"] == "rule"
    assert payload["primary_drivers"]  # Tier A（含 D0 结构性兜底）主线仍在
    assert payload["driver_hypotheses"] == []

    issues = _research_issues(result, "driver_profile_research_degraded")
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM
    assert payload["research_meta"]["issues"][0]["category"] == "driver_profile_research_degraded"

    assert len(calls) == 1  # 降级唯一写入点
    assert calls[0][0] == 2 and calls[0][1].startswith("parse_failed") and calls[0][2] == 1  # threshold=1
    assert result["steps"][0].status.value == "partial"


def test_research_truncation_leaves_low_issue_and_skips_cache(monkeypatch, tmp_path):
    """截断（工具预算累计超限）→ LOW issue + truncated 留痕 + 不落缓存，仍按已有输出收口。"""
    _patch_research_settings(monkeypatch, tmp_path, max_tool_calls=0)
    reply = {
        "messages": [
            ToolMessage(content="tushare 返回", tool_call_id="t1"),
            AIMessage(content=_research_output_json()),
        ]
    }
    _patch_agent_factory(monkeypatch, _FakeAgent([reply]))
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    result = asyncio.run(node.resolve_driver_profile(_research_trigger_state(), {}))
    payload = _driver_profile_payload(result)

    assert payload["research_meta"]["truncated"] is True
    assert payload["driver_hypotheses"]  # 已收口的输出仍被采用
    assert payload["degraded"] is False
    issues = _research_issues(result, "driver_profile_research_truncated")
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.LOW
    assert not list((tmp_path / "research_cache").glob("*.json"))


def test_research_evidence_insufficient_leaves_medium_issue(monkeypatch, tmp_path):
    """证据不足（无 evidence）→ 进 unverified_drivers + MEDIUM issue，provenance 如实回退 rule。"""
    _patch_research_settings(monkeypatch, tmp_path)
    _patch_agent_factory(monkeypatch, _FakeAgent([_research_output_json(evidence=False)]))
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    result = asyncio.run(node.resolve_driver_profile(_research_trigger_state(), {}))
    payload = _driver_profile_payload(result)

    assert payload["driver_hypotheses"] == []
    assert payload["unverified_drivers"]
    assert payload["provenance"] == "rule"
    assert payload["research_meta"]["llm_attempted"] is True
    assert payload["primary_drivers"]  # 规则主线仍在

    issues = _research_issues(result, "driver_profile_research_evidence_insufficient")
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM


def test_research_fact_conflict_leaves_medium_issue_and_downgrades(monkeypatch, tmp_path):
    """事实矛盾（声称占比与实测差超容差）→ 降权 conf≤0.3 + MEDIUM issue。"""
    _patch_research_settings(monkeypatch, tmp_path)
    _patch_agent_factory(
        monkeypatch,
        _FakeAgent([_research_output_json(company_form="高端白酒收入占比 5%")]),  # 实测 42%
    )
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    result = asyncio.run(node.resolve_driver_profile(_research_trigger_state(), {}))
    payload = _driver_profile_payload(result)

    assert payload["driver_hypotheses"][0]["confidence"] <= 0.3
    issues = _research_issues(result, "driver_profile_research_fact_conflict")
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM


# ══════════════════════════════════════════════════════════════════════════
# 修复回归：百分数/分数口径边界、研究段统一防护、Tier≥1 可读原因
# ══════════════════════════════════════════════════════════════════════════


def _track_multi_segment_artifact() -> Artifact:
    """真实百分数口径的双主线 COMPANY_TRACK：主力分部占比 42%（strong 锚不变）。

    与 ``_track_artifact()`` 的区别是**带了分部结构**：RouterInput.dominant_share 于是拿到
    42.0（canonical 百分数），用于钉住 node → should_research 的单位边界。
    """
    track = CompanyTrackArtifact(
        symbol="002594.SZ",
        track_label="生猪养殖",  # 命中 hog_cycle ⇒ strong 锚（不是 no_anchor / weak_anchor）
        business_model="integrator",
        dominant_segment="生猪养殖",
        fastest_segment="屠宰",
        segments=[
            SegmentSnapshot(
                report_date="20251231",
                segment_name="生猪养殖",
                revenue=42.0,
                revenue_share=42.0,
                revenue_yoy=8.0,
            ),
            SegmentSnapshot(
                report_date="20251231",
                segment_name="屠宰",
                revenue=58.0,
                revenue_share=58.0,
                revenue_yoy=35.0,
            ),
        ],
    )
    return Artifact(
        id="a-track-multi",
        type=ArtifactType.COMPANY_TRACK,
        producer_step="resolve_company_track",
        value=track.model_dump(mode="json"),
    )


def _salvageable_output_json() -> str:
    """一条非法假设 + 一条合法假设 ⇒ Tier 1 宽松救援（丢弃非法、保留合法）。"""
    valid = ResearchHypothesis(
        variable="产能利用率",
        role="primary",
        evidence=[ResearchEvidence(kind="annual_report", ref="2025 年报 第三节", quote="原文片段")],
        confidence=0.7,
    ).model_dump(mode="json")
    return _json.dumps(
        {
            "profit_formula": "收入 = 销量 × 单价",
            "driver_hypotheses": [{"mechanism": "缺少 variable，schema 非法"}, valid],
        },
        ensure_ascii=False,
    )


def test_research_multi_segment_trigger_uses_percent_share(monkeypatch, tmp_path):
    """F1 端到端：主力分部 42%（canonical 百分数）+ strong 锚 ⇒ trigger=multi_segment 且真的跑了研究层。

    若 node 不把百分数归一化成 0~1 分数，`42.0 < 0.50` 恒为假 ⇒ 研究层不会被调用（本用例失败），
    两处口径因此不再能静默错开。
    """
    _patch_research_settings(monkeypatch, tmp_path)
    agent = _FakeAgent([_research_output_json()])
    built = _patch_agent_factory(monkeypatch, agent)
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    state = _state(
        symbol="002594.SZ",
        artifacts=[
            _industry_artifact(industry="汽车", sub_industry="", sw_code="801880.SI"),
            _track_multi_segment_artifact(),
        ],
    )
    result = asyncio.run(node.resolve_driver_profile(state, {}))
    payload = _driver_profile_payload(result)

    assert payload["anchor_strength"] == "strong"  # 触发不是 no_anchor / weak_anchor
    assert payload["research_meta"]["trigger"] == "multi_segment"
    assert built["count"] == 1
    assert agent.calls == 1
    assert payload["provenance"] == "hybrid"  # 有 Tier A 锚
    assert payload["primary_drivers"] == ["高频高速覆铜板收入占比 × 铜箔-树脂价差"]


def test_research_should_research_failure_does_not_break_node(monkeypatch, tmp_path):
    """F2：触发判定自身抛错也被同一防护兜住——不抛穿、DRIVER_PROFILE 恒产出、有 MEDIUM issue。"""
    _patch_research_settings(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("触发判定炸了")

    monkeypatch.setattr(research_module, "should_research", _boom)
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    result = asyncio.run(node.resolve_driver_profile(_research_trigger_state(), {}))
    payload = _driver_profile_payload(result)

    assert payload["degraded"] is True
    assert payload["degradation_reason"].startswith("research_failed:RuntimeError")
    assert payload["research_meta"]["fallback_tier"] == 2
    assert payload["provenance"] == "rule"
    issues = _research_issues(result, "driver_profile_research_degraded")
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM
    assert result["steps"][0].status.value == "partial"


def test_research_module_import_failure_is_guarded(monkeypatch):
    """F2：连研究层 import 都失败（假模块）也不抛穿，且仍带降级元数据 + MEDIUM issue。"""

    class _ExplodingResearchModule:
        def __getattr__(self, name):
            raise AssertionError(f"研究层不可用：{name}")

    monkeypatch.setitem(sys.modules, "alphabee.domain_context.research", _ExplodingResearchModule())
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    result = asyncio.run(node.resolve_driver_profile(_research_trigger_state(), {}))
    payload = _driver_profile_payload(result)

    assert payload["provenance"] == "rule"
    assert payload["degraded"] is True
    assert payload["degradation_reason"].startswith("research_failed:AssertionError")
    assert payload["research_meta"]["fallback_tier"] == 2
    assert payload["primary_drivers"]  # Tier A 结构性兜底驱动仍在
    issues = _research_issues(result, "driver_profile_research_degraded")
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM


def test_research_tier1_salvage_carries_readable_reason(monkeypatch, tmp_path):
    """F3：Tier 1 宽松救援走成功分支，也必须带可读 degradation_reason（issue message 不含 unknown）。"""
    _patch_research_settings(monkeypatch, tmp_path)
    _patch_agent_factory(monkeypatch, _FakeAgent([_salvageable_output_json()]))
    monkeypatch.setattr("alphabee.config.get_settings", lambda: _config_stub(enabled=True))

    result = asyncio.run(node.resolve_driver_profile(_research_trigger_state(), {}))
    payload = _driver_profile_payload(result)

    assert payload["research_meta"]["fallback_tier"] == 1
    assert payload["degraded"] is True
    assert payload["degradation_reason"] == "research_schema_salvaged"
    assert [item["variable"] for item in payload["driver_hypotheses"]] == ["产能利用率"]  # 合法条目保留
    issues = _research_issues(result, "driver_profile_research_degraded")
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.MEDIUM
    assert "unknown" not in issues[0].message
    assert payload["research_meta"]["issues"][0]["message"].endswith("research_schema_salvaged")
