"""Tier B/C 单测：触发分支 / 输入包 / 收敛 C1–C6 / 降级阶梯 / 缓存预算超时重试。

全部 mock 级（不调 LLM、不读盘、不联网）：

- Tier B 的 LLM 调用经 ``run_driver_research(agent_factory=...)`` 的注入缝替换成 FakeAgent；
- Tier C 全部是纯函数，直接用构造好的 ``DriverResearchResult`` 驱动；
- 本地年报片段经 ``report_fragments=`` 注入，或在测试里 patch 源模块的
  ``fetch_local_report_fragments``（范式 A：patch 源模块属性）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from alphabee.agents.driver_profile.output import (
    DriverResearchOutput,
    ResearchAgendaItem,
    ResearchEvidence,
    ResearchHypothesis,
)
from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.config import (
    DomainContextResearchSettings,
    DomainContextSettings,
    LLMConfig,
    Settings,
)
from alphabee.domain_context import research
from alphabee.domain_context.context_router import RouterInput
from alphabee.domain_context.contracts import ActivatedPrimitive, DriverProfile

# ══════════════════════════════════════════════════════════════════════════
# fixtures / helpers
# ══════════════════════════════════════════════════════════════════════════


def _settings(tmp_path: Path, **overrides: Any) -> DomainContextResearchSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "cache_dir": str(tmp_path / "cache"),
        "wall_timeout_s": 5.0,
        "cache_ttl_days": 90,
    }
    base.update(overrides)
    return DomainContextResearchSettings(**base)


def _stub_settings(*, enabled: bool) -> Settings:
    """构造一个只关心开关的 Settings 桩（不依赖 config.yaml 的本地内容）。"""
    return Settings(
        llm=LLMConfig(api_key="sk-test", base_url="http://example.invalid", model="test-model"),
        domain_context=DomainContextSettings(research=DomainContextResearchSettings(enabled=enabled)),
    )


def _profile(**overrides: Any) -> DriverProfile:
    base: dict[str, Any] = {
        "symbol": "002916.SZ",
        "playbook": "generic_fundamental",
        "playbook_version": 1,
        "anchor_strength": "none",
        "fallback": True,
        "provenance": "rule",
        "primary_drivers": ["毛利率（Tier A 兜底）"],
    }
    base.update(overrides)
    return DriverProfile(**base)


def _capacity_primitive() -> ActivatedPrimitive:
    return ActivatedPrimitive(
        id="capacity_cycle",
        description="产能周期",
        key_variables=["产能利用率", "行业新增产能"],
        disconfirming_signals=["在建工程转固后产能利用率仍低于 70%"],
        causal_paths=["新增产能投放 → 供给过剩 → 价格下行 → 毛利收缩"],
    )


def _hypothesis(
    variable: str,
    *,
    evidence: bool = True,
    ref: str = "2025 年报 第三节",
    confidence: float = 0.7,
    role: str = "primary",
    observables: list[str] | None = None,
    company_form: str = "",
    mechanism: str = "",
) -> ResearchHypothesis:
    return ResearchHypothesis(
        variable=variable,
        role=role,
        mechanism=mechanism,
        company_form=company_form,
        observables=list(observables or []),
        evidence=[ResearchEvidence(kind="annual_report", ref=ref, quote="原文片段")] if evidence else [],
        confidence=confidence,
    )


def _output(
    hypotheses: list[ResearchHypothesis] | None = None,
    agenda: list[ResearchAgendaItem] | None = None,
    **kwargs: Any,
) -> DriverResearchOutput:
    return DriverResearchOutput(
        driver_hypotheses=list(hypotheses or []),
        research_agenda=list(agenda or []),
        **kwargs,
    )


def _result(output: DriverResearchOutput, meta: dict[str, Any] | None = None) -> research.DriverResearchResult:
    return research.DriverResearchResult(output=output, meta=dict(meta or {}))


def _output_json(**kwargs: Any) -> str:
    return json.dumps(_output(**kwargs).model_dump(mode="json"), ensure_ascii=False)


def _track() -> CompanyTrackArtifact:
    return CompanyTrackArtifact(
        symbol="002916.SZ",
        sw_industry="电子",
        sw_code="801080.SI",
        as_of_date="20251231",
        segments=[
            SegmentSnapshot(
                report_date="20251231",
                segment_name="高频高速覆铜板",
                revenue=4.2e9,
                revenue_share=42.0,
                revenue_yoy=58.0,
                gross_margin=28.5,
                source="em",
            ),
            SegmentSnapshot(
                report_date="20251231",
                segment_name="常规覆铜板",
                revenue=5.8e9,
                revenue_share=58.0,
                revenue_yoy=6.0,
                source="em",
            ),
            SegmentSnapshot(
                report_date="20241231",
                segment_name="高频高速覆铜板",
                revenue=2.1e9,
                revenue_share=24.0,
                source="em",
            ),
        ],
        review_notes=["赛道标签从「常规覆铜板」迁移到「高频高速覆铜板」"],
    )


class FakeAgent:
    """Mock 级研究代理：按顺序返回预设回复，记录调用次数/config。"""

    def __init__(self, replies: list[Any], *, delay: float = 0.0) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.calls: list[Any] = []
        self.configs: list[Any] = []

    async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
        self.calls.append(payload)
        self.configs.append(config)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0)
        if isinstance(reply, str):
            return {"messages": [AIMessage(content=reply)]}
        return reply  # type: ignore[no-any-return]


# ══════════════════════════════════════════════════════════════════════════
# D1.5 触发条件 + flag 门控
# ══════════════════════════════════════════════════════════════════════════


def test_should_research_no_anchor_branch() -> None:
    inp = RouterInput(symbol="600519.SH", track_label="白酒")
    assert research.should_research(inp, "none", None, False) == (True, "no_anchor")


def test_should_research_no_anchor_without_identity_signals_is_skipped() -> None:
    """身份信号全空 = 输入缺失：不是"稀疏区域"，不该烧 LLM（走 degraded 兜底）。"""
    assert research.should_research(RouterInput(symbol="600519.SH"), "none", None, False) == (False, "")


def test_should_research_weak_anchor_branch() -> None:
    inp = RouterInput(symbol="600519.SH", business_model="brand")
    assert research.should_research(inp, "weak", 0.9, False) == (True, "weak_anchor")


def test_should_research_multi_segment_branch() -> None:
    inp = RouterInput(symbol="002594.SZ", track_label="新能源车")
    assert research.should_research(inp, "strong", 0.42, False) == (True, "multi_segment")


def test_should_research_identity_drift_branch() -> None:
    inp = RouterInput(symbol="002916.SZ", track_label="覆铜板")
    assert research.should_research(inp, "strong", 0.80, True) == (True, "identity_drift")


def test_should_research_strong_anchor_with_clear_structure_is_skipped() -> None:
    """强锚 + 单一主线 + 无漂移 ⇒ 跳过 Tier B（保护既有 golden + 省成本）。"""
    inp = RouterInput(symbol="002714.SZ", track_label="生猪养殖")
    assert research.should_research(inp, "strong", 0.80, False) == (False, "")


def test_research_switch_defaults_off_and_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DomainContextResearchSettings().enabled is False
    assert research.research_enabled(DomainContextResearchSettings()) is False

    monkeypatch.setattr(research, "get_settings", lambda: _stub_settings(enabled=False))
    assert research.research_enabled() is False

    monkeypatch.setattr(research, "get_settings", lambda: _stub_settings(enabled=True))
    assert research.research_enabled() is True


def test_research_settings_reads_domain_context_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "get_settings", lambda: _stub_settings(enabled=True))
    assert research.research_settings().enabled is True
    assert research.research_settings().max_tool_calls == 20


def test_config_example_registers_every_research_field_with_defaults() -> None:
    """``config.yaml.example`` 的 ``domain_context.research`` 与代码默认值同源（防文档漂移）。"""
    import yaml

    from alphabee import PROJECT_ROOT

    raw = yaml.safe_load((PROJECT_ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    section = raw["domain_context"]["research"]
    assert set(section) == set(DomainContextResearchSettings.model_fields)
    for key, field in DomainContextResearchSettings.model_fields.items():
        assert section[key] == field.default, key
    assert section["enabled"] is False


# ══════════════════════════════════════════════════════════════════════════
# D1.6 输入包
# ══════════════════════════════════════════════════════════════════════════


def test_build_research_input_carries_multi_period_segments_and_benchmarks() -> None:
    pack = research.build_research_input(
        "002916.SZ",
        RouterInput(symbol="002916.SZ", track_label="覆铜板", industry="电子", sw_code="801080.SI"),
        track=_track(),
        fact_values={"gross_margin": 22.5, "peer_avg_gross_margin": 19.8, "revenue": 1.0e10},
        profile=_profile(),
        report_fragments=["管理层讨论与分析：高速覆铜板收入占比提升。"],
    )
    assert [block["report_date"] for block in pack["segments"]] == ["20251231", "20241231"]
    assert pack["latest_report_date"] == "20251231"
    # 最新期分项按占比降序（常规覆铜板 58% > 高频高速覆铜板 42%）
    assert [item["segment_name"] for item in pack["latest_segments"]] == ["常规覆铜板", "高频高速覆铜板"]
    assert pack["latest_segments"][1]["revenue_share"] == 42.0
    assert pack["latest_segments"][1]["gross_margin"] == 28.5
    assert pack["financials"]["gross_margin"] == 22.5
    assert pack["financials"]["peer_avg_gross_margin"] == 19.8
    assert "管理层讨论与分析" in pack["report_fragments"]
    assert pack["review_notes"] == ["赛道标签从「常规覆铜板」迁移到「高频高速覆铜板」"]
    assert pack["identity"]["sw_code"] == "801080.SI"


def test_build_research_input_consumes_weak_anchor_primitive_knowledge() -> None:
    """Tier A 的 key_variables/causal_paths/disconfirming_signals 首次真正被消费（G-7）。"""
    pack = research.build_research_input(
        "002916.SZ",
        RouterInput(symbol="002916.SZ"),
        profile=_profile(anchor_strength="weak", activated_primitives=[_capacity_primitive()]),
        report_fragments=[],
    )
    primitives = pack["anchor_primitives"]
    assert primitives == [
        {
            "id": "capacity_cycle",
            "key_variables": ["产能利用率", "行业新增产能"],
            "causal_paths": ["新增产能投放 → 供给过剩 → 价格下行 → 毛利收缩"],
            "disconfirming_signals": ["在建工程转固后产能利用率仍低于 70%"],
        }
    ]


def test_build_research_input_truncates_report_fragments_to_12000_chars() -> None:
    pack = research.build_research_input(
        "002916.SZ",
        RouterInput(symbol="002916.SZ"),
        report_fragments=["x" * 20_000],
    )
    assert len(pack["report_fragments"]) == research.MAX_REPORT_FRAGMENT_CHARS == 12_000


def test_build_research_input_reads_local_report_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_fetch(symbol: str, *, max_chars: int = 12_000) -> tuple[list[str], dict[str, Any]]:
        calls.append((symbol, max_chars))
        return ["一手材料：报告期内公司从事的主要业务。"], {"report_period": "2025年年度报告"}

    monkeypatch.setattr(
        __import__("alphabee.company_track.peer_report", fromlist=["fetch_local_report_fragments"]),
        "fetch_local_report_fragments",
        fake_fetch,
    )
    pack = research.build_research_input("002916.SZ", RouterInput(symbol="002916.SZ"))
    assert calls == [("002916.SZ", 12_000)]
    assert "一手材料" in pack["report_fragments"]


# ══════════════════════════════════════════════════════════════════════════
# Tier C：C1–C6
# ══════════════════════════════════════════════════════════════════════════


def test_converge_c1_drops_illegal_single_item(tmp_path: Path) -> None:
    """C1：单条非法假设丢弃并计数，不影响合法条目（全丢才走降级链）。"""
    output = DriverResearchOutput.model_construct(
        driver_hypotheses=[
            {"mechanism": "缺少 variable，schema 不合法"},
            {"variable": "   ", "evidence": []},
            _hypothesis("产能利用率").model_dump(mode="json"),
        ]
    )
    converged = research.converge(
        _profile(), _result(output), settings=_settings(tmp_path), research_input={"latest_segments": []}
    )
    assert converged.research_meta["schema_dropped"] == 2
    assert [item.variable for item in converged.driver_hypotheses] == ["产能利用率"]


def test_converge_c2_evidence_is_required_else_unverified(tmp_path: Path) -> None:
    """C2（钉住）：证据为空 **或 ref 为空** ⇒ 进 unverified_drivers，绝不进 primary_drivers。"""
    output = _output(
        hypotheses=[
            _hypothesis("产能利用率", evidence=True, confidence=0.8),
            _hypothesis("无证据驱动", evidence=False, confidence=0.9),
            _hypothesis("空 ref 驱动", evidence=True, ref="  ", confidence=0.9),
        ]
    )
    converged = research.converge(_profile(), _result(output), settings=_settings(tmp_path))

    assert "无证据驱动" in converged.unverified_drivers
    assert "空 ref 驱动" in converged.unverified_drivers
    assert "无证据驱动" not in converged.primary_drivers
    assert "空 ref 驱动" not in converged.primary_drivers
    assert [item.variable for item in converged.driver_hypotheses] == ["产能利用率"]
    assert converged.research_meta["unverified_count"] == 2


def test_converge_c3_segment_share_conflict_downgrades_and_records_issue(tmp_path: Path) -> None:
    """C3：observable 声称的分部占比与实测相差超容差 ⇒ conf ≤ 0.3 + MEDIUM issue。"""
    output = _output(
        hypotheses=[
            _hypothesis(
                "AI 服务器高速覆铜板占比",
                confidence=0.9,
                company_form="高速覆铜板收入占比 8%",
                observables=["高速覆铜板 收入占比 8%（来源：annual_report:第三节）"],
            )
        ]
    )
    research_input = {"latest_segments": [{"segment_name": "高速覆铜板", "revenue_share": 42.0}]}
    converged = research.converge(
        _profile(), _result(output), settings=_settings(tmp_path), research_input=research_input
    )

    hypothesis = converged.driver_hypotheses[0]
    assert hypothesis.confidence <= research._CONTRADICTION_MAX_CONFIDENCE
    issues = converged.research_meta["issues"]
    assert any(
        issue["category"] == "driver_profile_research_fact_conflict" and issue["severity"] == "medium"
        for issue in issues
    )


def test_converge_c3_without_structural_facts_does_not_fabricate_conflict(tmp_path: Path) -> None:
    output = _output(hypotheses=[_hypothesis("毛利率结构", company_form="毛利率 55%", confidence=0.9)])
    converged = research.converge(_profile(), _result(output), settings=_settings(tmp_path), research_input={})
    assert converged.driver_hypotheses[0].confidence == 0.9
    assert not [issue for issue in converged.research_meta["issues"] if issue["category"].endswith("fact_conflict")]


def test_converge_c4_matches_primitive_and_flags_novel_driver(tmp_path: Path) -> None:
    """C4：对齐 key_variables ⇒ matched_primitive + 继承证伪信号；对不上 ⇒ novel_drivers。"""
    output = _output(
        hypotheses=[
            _hypothesis("产能利用率", confidence=0.8),
            _hypothesis("铜箔-树脂价差", confidence=0.7),
        ]
    )
    profile = _profile(anchor_strength="weak", activated_primitives=[_capacity_primitive()])
    converged = research.converge(profile, _result(output), settings=_settings(tmp_path))

    matched = {item.variable: item for item in converged.driver_hypotheses}
    assert matched["产能利用率"].matched_primitive == "capacity_cycle"
    assert "在建工程转固后产能利用率仍低于 70%" in matched["产能利用率"].falsifiers
    assert matched["产能利用率"].mechanism == "新增产能投放 → 供给过剩 → 价格下行 → 毛利收缩"
    assert matched["铜箔-树脂价差"].matched_primitive == ""
    assert "铜箔-树脂价差" in converged.novel_drivers
    assert converged.research_meta["matched_primitives"] == ["capacity_cycle"]


def test_converge_c4_appends_primitive_causal_paths_after_llm_mechanism(tmp_path: Path) -> None:
    """R2：对齐即**无条件继承**原语 causal_paths——LLM 自写的 mechanism 在前，
    原语因果链去重后以"；"追加（不是"只在 mechanism 为空时才用"的条件分支）。"""
    output = _output(hypotheses=[_hypothesis("产能利用率", confidence=0.8, mechanism="产能利用率回升 → 单位成本摊薄")])
    profile = _profile(anchor_strength="weak", activated_primitives=[_capacity_primitive()])
    converged = research.converge(profile, _result(output), settings=_settings(tmp_path))

    hypothesis = converged.driver_hypotheses[0]
    assert hypothesis.matched_primitive == "capacity_cycle"
    assert hypothesis.mechanism == ("产能利用率回升 → 单位成本摊薄；新增产能投放 → 供给过剩 → 价格下行 → 毛利收缩")
    assert "在建工程转固后产能利用率仍低于 70%" in hypothesis.falsifiers


def test_converge_c4_dedups_causal_path_already_present_in_mechanism(tmp_path: Path) -> None:
    """原语因果链若已含在 mechanism 里，不再重复追加（去重）。"""
    path = "新增产能投放 → 供给过剩 → 价格下行 → 毛利收缩"
    output = _output(hypotheses=[_hypothesis("产能利用率", confidence=0.8, mechanism=path)])
    profile = _profile(anchor_strength="weak", activated_primitives=[_capacity_primitive()])
    converged = research.converge(profile, _result(output), settings=_settings(tmp_path))
    assert converged.driver_hypotheses[0].mechanism == path


def test_converge_c5_truncates_by_confidence_and_caps_primary_count(tmp_path: Path) -> None:
    output = _output(
        hypotheses=[
            _hypothesis("驱动A", confidence=0.9),
            _hypothesis("驱动B", confidence=0.8),
            _hypothesis("驱动C", confidence=0.7),
            _hypothesis("驱动D", confidence=0.6),
            _hypothesis("驱动E", confidence=0.1),
        ]
    )
    converged = research.converge(_profile(), _result(output), settings=_settings(tmp_path, max_hypotheses=4))
    variables = [item.variable for item in converged.driver_hypotheses]
    assert variables == ["驱动A", "驱动B", "驱动C", "驱动D"]
    primaries = [item for item in converged.driver_hypotheses if item.role == "primary"]
    assert len(primaries) == 3
    assert [item.variable for item in primaries] == ["驱动A", "驱动B", "驱动C"]
    assert converged.driver_hypotheses[3].role == "secondary"


def test_converge_c5_promotes_highest_confidence_when_no_primary(tmp_path: Path) -> None:
    output = _output(
        hypotheses=[
            _hypothesis("驱动A", confidence=0.6, role="secondary"),
            _hypothesis("驱动B", confidence=0.9, role="risk"),
        ]
    )
    converged = research.converge(_profile(), _result(output), settings=_settings(tmp_path))
    assert converged.driver_hypotheses[0].role == "primary"
    assert converged.primary_drivers == ["驱动B"]


def test_converge_c6_derives_drivers_agenda_and_provenance(tmp_path: Path) -> None:
    output = _output(
        hypotheses=[
            _hypothesis(
                "产能利用率",
                confidence=0.8,
                observables=["产能利用率（来源：tushare:income）"],
            ),
            _hypothesis("行业新增产能", confidence=0.6, role="secondary"),
        ],
        agenda=[
            ResearchAgendaItem(question="在建工程何时转固？", priority="medium"),
            ResearchAgendaItem(question="产能利用率是否见底？", priority="critical"),
        ],
        novel_concepts=["铜箔-树脂价差"],
        open_questions=["公司未披露的自研/代工拆分"],
        candidates=["capacity_cycle"],
    )
    profile = _profile(anchor_strength="weak", activated_primitives=[_capacity_primitive()])
    converged = research.converge(profile, _result(output), settings=_settings(tmp_path))

    assert converged.primary_drivers == ["产能利用率"]
    assert converged.secondary_drivers == ["行业新增产能"]
    # P1-4：契约 novel_drivers 的唯一来源是 C4 推导（本用例两条假设都对齐了 capacity_cycle
    # ⇒ 推导为空）；LLM 自述的 novel_concepts 只落 research_meta，不混进契约字段。
    assert converged.novel_drivers == []
    assert converged.research_meta["novel_concepts"] == ["铜箔-树脂价差"]
    assert converged.candidates == ["capacity_cycle"]
    # P2-7：open_questions（认知缺口）与 unverified_drivers（C2 隔离的驱动变量）分列。
    assert converged.unverified_drivers == []
    assert converged.open_questions == ["公司未披露的自研/代工拆分"]
    assert converged.provenance == "hybrid"
    assert converged.research_confidence == pytest.approx(0.7)
    # agenda：按 priority 排序后截断（critical 在前）
    assert [item.question for item in converged.research_agenda] == [
        "产能利用率是否见底？",
        "在建工程何时转固？",
    ]
    assert converged.research_agenda[0].priority == "critical"
    assert converged.research_agenda[0].status == "open"
    # observable 的「指标名（来源：X）」被拆成结构化口径
    assert converged.driver_hypotheses[0].observables[0].name == "产能利用率"
    assert converged.driver_hypotheses[0].observables[0].source == "tushare:income"


def test_converge_c6_keeps_tier_a_drivers_when_no_usable_hypothesis(tmp_path: Path) -> None:
    """研究层不给力时不能把规则主线抹掉（primary_drivers 覆盖率契约）。

    R3：无可用假设 ⇒ 内容全部来自规则，``provenance`` 必须如实为 ``"rule"``（否则 D2 的
    ``driver_profile_evidence_present`` 会把规则产物误判成 LLM 产物）；「跑了但没产出可溯驱动」
    走 artifact 载荷内 issue + ``research_meta["llm_attempted"]`` 显式留痕。
    """
    output = _output(hypotheses=[_hypothesis("无证据驱动", evidence=False)])
    converged = research.converge(_profile(), _result(output), settings=_settings(tmp_path))
    assert converged.driver_hypotheses == []
    assert converged.primary_drivers == ["毛利率（Tier A 兜底）"]
    assert converged.provenance == "rule"
    assert converged.research_meta["llm_attempted"] is True
    assert converged.research_meta["unverified_count"] == 1
    assert converged.research_meta["hypotheses_count"] == 0
    assert converged.research_confidence == 0.0
    issues = converged.research_meta["issues"]
    assert any(
        issue["category"] == "driver_profile_research_evidence_insufficient"
        and issue["severity"] == "medium"
        and "无证据假设" in issue["message"]
        for issue in issues
    )


def test_converge_records_insufficient_evidence_issue_when_output_has_no_hypotheses(tmp_path: Path) -> None:
    """LLM 一条假设都没给 ⇒ 同样落"证据不足"载荷内 issue（四类失败之一），provenance 为 rule。"""
    converged = research.converge(_profile(), _result(_output()), settings=_settings(tmp_path))
    assert converged.provenance == "rule"
    assert converged.research_meta["llm_attempted"] is True
    assert any(
        issue["category"] == "driver_profile_research_evidence_insufficient"
        and "未产出可用驱动假设" in issue["message"]
        for issue in converged.research_meta["issues"]
    )


def test_converge_marks_llm_not_attempted_on_cache_hit(tmp_path: Path) -> None:
    """缓存命中时 Tier B 并未真跑：``llm_attempted`` 必须为 False（留痕不得失真）。"""
    output = _output(hypotheses=[_hypothesis("产能利用率")])
    converged = research.converge(_profile(), _result(output, meta={"cached": True}), settings=_settings(tmp_path))
    assert converged.research_meta["llm_attempted"] is False
    assert converged.provenance == "llm"


def test_converge_caps_agenda_items_by_priority(tmp_path: Path) -> None:
    output = _output(
        agenda=[
            ResearchAgendaItem(question="问题1", priority="high"),
            ResearchAgendaItem(question="问题2", priority="critical"),
            ResearchAgendaItem(question="问题3", priority="medium"),
        ]
    )
    converged = research.converge(_profile(), _result(output), settings=_settings(tmp_path, max_agenda_items=2))
    assert [item.question for item in converged.research_agenda] == ["问题2", "问题1"]


# ══════════════════════════════════════════════════════════════════════════
# D1.9 降级阶梯 Tier 0–3
# ══════════════════════════════════════════════════════════════════════════


def test_finalize_tier0_not_degraded_and_no_issue() -> None:
    converged = _profile(
        provenance="hybrid",
        driver_hypotheses=[],
        research_meta={"trigger": "weak_anchor"},
    )
    payload = research.finalize_research_artifact(converged, tier=0)
    assert payload["degraded"] is False
    assert payload["degradation_reason"] == ""
    assert payload["research_meta"]["fallback_tier"] == 0
    assert payload["provenance"] == "hybrid"
    assert payload["research_meta"]["trigger"] == "weak_anchor"
    assert not [
        issue for issue in payload["research_meta"].get("issues", []) if issue.get("category", "").endswith("degraded")
    ]


def test_finalize_tier1_marks_degraded_and_keeps_content() -> None:
    profile = _profile(provenance="llm", primary_drivers=["派生驱动"])
    payload = research.finalize_research_artifact(profile, tier=1, reason="schema_invalid_salvaged:1")
    assert payload["degraded"] is True
    assert payload["degradation_reason"] == "schema_invalid_salvaged:1"
    assert payload["research_meta"]["fallback_tier"] == 1
    assert payload["primary_drivers"] == ["派生驱动"]
    issues = payload["research_meta"]["issues"]
    assert any(
        issue["category"] == "driver_profile_research_degraded" and issue["severity"] == "medium" for issue in issues
    )


def test_finalize_tier2_reverts_to_tier_a_rule_drivers() -> None:
    profile = _profile(
        provenance="llm",
        anchor_strength="none",
        primary_drivers=["Tier A 兜底", "LLM 驱动"],
        driver_hypotheses=[],
        candidates=["capacity_cycle"],
        novel_drivers=["新驱动"],
        open_questions=["未解的认知缺口"],
        proposed_frameworks=["产能爬坡型CDMO"],
        research_confidence=0.8,
    )
    payload = research.finalize_research_artifact(profile, tier=2, reason="agent_failure:OSError")
    assert payload["provenance"] == "rule"
    assert payload["novel_drivers"] == []
    assert payload["candidates"] == []
    # §6.3：降级塑形的清空清单补两键（P2-7 open_questions / P1-3 proposed_frameworks）
    assert payload["open_questions"] == []
    assert payload["proposed_frameworks"] == []
    assert payload["research_confidence"] == 0.0
    assert payload["degraded"] is True
    assert payload["research_meta"]["fallback_tier"] == 2
    # Tier A + D0 结构性兜底驱动保留（降级不丢主线）
    assert payload["primary_drivers"] == ["Tier A 兜底", "LLM 驱动"]


def test_finalize_tier3_minimal_skeleton_keeps_playbook_with_empty_drivers() -> None:
    profile = _profile(
        playbook="hog_cycle",
        playbook_version=1,
        anchor_strength="none",
        activated_primitives=[_capacity_primitive()],
        primary_drivers=["Tier A 兜底"],
        open_questions=["未解的认知缺口"],
        proposed_frameworks=["产能爬坡型CDMO"],
    )
    payload = research.finalize_research_artifact(profile, tier=3, reason="empty_context")
    assert payload["playbook"] == "hog_cycle"
    assert payload["primary_drivers"] == []
    assert payload["secondary_drivers"] == []
    assert payload["activated_primitives"] == []
    assert payload["driver_hypotheses"] == []
    assert payload["research_agenda"] == []
    # §6.3：Tier 3 最小骨架同样清空 P2-7 / P1-3 两键
    assert payload["open_questions"] == []
    assert payload["proposed_frameworks"] == []
    assert payload["degraded"] is True
    assert payload["degradation_reason"] == "empty_context"
    assert payload["research_meta"]["fallback_tier"] == 3


def test_finalize_writes_degradation_fields_only_through_apply_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``degraded``/``degradation_reason`` 的唯一写入点 = apply_degradation（threshold=1）。"""
    from alphabee.orchestrator.services import degradation as degradation_module

    calls: list[tuple[int, str, int]] = []
    real = degradation_module.apply_degradation

    def spy(payload: dict[str, Any], tier: int, reason: str, *, threshold: int = 2) -> dict[str, Any]:
        calls.append((tier, reason, threshold))
        return real(payload, tier, reason, threshold=threshold)

    monkeypatch.setattr(degradation_module, "apply_degradation", spy)

    research.finalize_research_artifact(_profile(), tier=0)
    research.finalize_research_artifact(_profile(), tier=2, reason="boom")
    assert calls == [(0, "", 1), (2, "boom", 1)]


def test_finalize_keeps_tier_a_input_degradation_on_research_success() -> None:
    """D0 的"输入缺失"降级与"研究档位"是两条轴：研究成功不得抹掉前者（仍只经唯一写入点）。"""
    profile = _profile(degraded=True, degraded_reason="identity_signals_missing")
    payload = research.finalize_research_artifact(profile, tier=0)
    assert payload["degraded"] is True
    assert payload["degradation_reason"] == "identity_signals_missing"
    assert payload["research_meta"]["fallback_tier"] == 0


def test_finalize_merges_extra_meta_into_research_meta() -> None:
    payload = research.finalize_research_artifact(
        _profile(), tier=0, meta={"cached": True, "tool_calls": 0, "trigger": "no_anchor"}
    )
    assert payload["research_meta"]["cached"] is True
    assert payload["research_meta"]["trigger"] == "no_anchor"
    assert payload["research_meta"]["fallback_tier"] == 0


# ══════════════════════════════════════════════════════════════════════════
# D1.8 缓存 / 预算 / 超时 / 重试
# ══════════════════════════════════════════════════════════════════════════


def test_research_cache_key_covers_its_four_components() -> None:
    key = research.research_cache_key("002916.SZ", "20251231", "覆铜板", 2)
    assert key == "002916.SZ|20251231|覆铜板|2"
    for other in (
        research.research_cache_key("002916.SZ", "20241231", "覆铜板", 2),
        research.research_cache_key("002916.SZ", "20251231", "常规覆铜板", 2),
        research.research_cache_key("002916.SZ", "20251231", "覆铜板", 3),
    ):
        assert other != key


async def test_cache_hit_skips_agent_factory(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    key = research.research_cache_key("002916.SZ", "20251231", "覆铜板", 1)
    written = research.save_cached_research(
        cfg.cache_dir,
        "002916.SZ",
        cache_key=key,
        output=_output(hypotheses=[_hypothesis("产能利用率")]),
        track_label="覆铜板",
        report_period="20251231",
        playbook_version=1,
    )
    assert written is not None

    built: list[int] = []

    def factory() -> Any:
        built.append(1)
        raise AssertionError("缓存命中不得构造 agent（即不得调 LLM）")

    result = await research.run_driver_research(
        "002916.SZ",
        {"symbol": "002916.SZ"},
        settings=cfg,
        cache_dir=cfg.cache_dir,
        track_label="覆铜板",
        report_period="20251231",
        playbook_version=1,
        trigger="weak_anchor",
        agent_factory=factory,
    )
    assert built == []
    assert result.meta["cached"] is True
    assert result.meta["cache_key"] == key
    assert result.output.driver_hypotheses[0].variable == "产能利用率"
    # research_meta.cached=true 落进画像（节点据此跳过 Tier B 的留痕）
    converged = research.converge(_profile(), result, settings=cfg, research_input={}, trigger="weak_anchor")
    assert converged.research_meta["cached"] is True
    assert converged.research_meta["trigger"] == "weak_anchor"


async def test_cache_miss_runs_agent_then_hits_on_second_call(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    agent = FakeAgent([_output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")])])
    first = await research.run_driver_research(
        "002916.SZ",
        {"symbol": "002916.SZ"},
        settings=cfg,
        cache_dir=cfg.cache_dir,
        track_label="覆铜板",
        report_period="20251231",
        playbook_version=1,
        agent_factory=lambda: agent,
    )
    assert first.meta["cached"] is False
    assert len(agent.calls) == 1
    assert Path(cfg.cache_dir).joinpath("002916.SZ.json").exists()

    def factory() -> Any:
        raise AssertionError("第二次调用应命中缓存")

    second = await research.run_driver_research(
        "002916.SZ",
        {"symbol": "002916.SZ"},
        settings=cfg,
        cache_dir=cfg.cache_dir,
        track_label="覆铜板",
        report_period="20251231",
        playbook_version=1,
        agent_factory=factory,
    )
    assert second.meta["cached"] is True


async def test_cache_key_change_forces_new_llm_call(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    agent = FakeAgent(
        [
            _output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")]),
            _output_json(hypotheses=[_hypothesis("新报告期驱动").model_dump(mode="json")]),
        ]
    )
    await research.run_driver_research(
        "002916.SZ",
        {},
        settings=cfg,
        cache_dir=cfg.cache_dir,
        track_label="覆铜板",
        report_period="20251231",
        agent_factory=lambda: agent,
    )
    second = await research.run_driver_research(
        "002916.SZ",
        {},
        settings=cfg,
        cache_dir=cfg.cache_dir,
        track_label="覆铜板",
        report_period="20260630",
        agent_factory=lambda: agent,
    )
    assert second.meta["cached"] is False
    assert len(agent.calls) == 2
    assert second.output.driver_hypotheses[0].variable == "新报告期驱动"


async def test_expired_cache_is_a_miss(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    path = Path(cfg.cache_dir) / "002916.SZ.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    key = research.research_cache_key("002916.SZ", "20251231", "", 1)
    path.write_text(
        json.dumps(
            {
                "cache_key": key,
                "created_at": "2020-01-01T00:00:00+00:00",
                "output": _output().model_dump(mode="json"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    agent = FakeAgent([_output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")])])
    result = await research.run_driver_research(
        "002916.SZ",
        {},
        settings=cfg,
        cache_dir=cfg.cache_dir,
        report_period="20251231",
        agent_factory=lambda: agent,
    )
    assert result.meta["cached"] is False
    assert len(agent.calls) == 1


def test_cache_read_fails_open_on_corrupt_file(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    path = Path(cfg.cache_dir) / "002916.SZ.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert research.load_cached_research(cfg.cache_dir, "002916.SZ", cache_key="whatever") is None


async def test_tool_budget_overrun_truncates_and_records_low_issue(tmp_path: Path) -> None:
    cfg = _settings(tmp_path, max_tool_calls=1)
    reply = {
        "messages": [
            ToolMessage(content="tushare 返回", tool_call_id="1"),
            ToolMessage(content="年报返回", tool_call_id="2"),
            AIMessage(content=_output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")])),
        ]
    }
    result = await research.run_driver_research(
        "002916.SZ",
        {},
        settings=cfg,
        cache_dir=cfg.cache_dir,
        agent_factory=lambda: FakeAgent([reply]),
    )
    assert result.meta["truncated"] is True
    assert result.meta["tool_calls"] == 2
    assert any(
        issue["severity"] == "low" and issue["category"] == "driver_profile_research_truncated"
        for issue in result.meta["issues"]
    )
    assert result.output.driver_hypotheses  # 按已有输出收口，不整条丢弃


async def test_explicit_recursion_limit_is_passed_to_agent(tmp_path: Path) -> None:
    cfg = _settings(tmp_path, recursion_limit=17)
    agent = FakeAgent([_output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")])])
    await research.run_driver_research(
        "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: agent
    )
    assert agent.configs[0] == {"recursion_limit": 17}


async def test_wall_timeout_raises_driver_research_error(tmp_path: Path) -> None:
    cfg = _settings(tmp_path, wall_timeout_s=0.01)
    agent = FakeAgent([_output_json()], delay=0.2)
    with pytest.raises(research.DriverResearchError) as excinfo:
        await research.run_driver_research(
            "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: agent
        )
    assert "wall_timeout" in str(excinfo.value)
    assert excinfo.value.truncated is True
    assert excinfo.value.tier == 2


async def test_parse_failure_retries_once_with_error_feedback(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    agent = FakeAgent(
        [
            "我先解释一下我的研究思路：这家公司的驱动是……",
            _output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")]),
        ]
    )
    result = await research.run_driver_research(
        "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: agent
    )
    assert len(agent.calls) == 2
    assert result.meta["retry_count"] == 1
    assert result.meta["model_attempts"] == 2
    assert result.output.driver_hypotheses[0].variable == "产能利用率"
    # 第二次调用带上了"附解析错误"的回灌指令
    assert "无法被解析为合法 JSON" in agent.calls[1]["messages"][-1].content


async def test_parse_failure_after_retry_raises_for_degradation_ladder(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    agent = FakeAgent(["还是自然语言", "仍然是自然语言"])
    with pytest.raises(research.DriverResearchError) as excinfo:
        await research.run_driver_research(
            "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: agent
        )
    assert len(agent.calls) == 2  # 最多重试一次
    assert "parse_failed" in str(excinfo.value)
    assert excinfo.value.tier == 2


async def test_truncated_output_is_not_retried(tmp_path: Path) -> None:
    """预算已尽 ⇒ 直接收口，不再 re-ask（重试只会继续烧工具调用）。"""
    cfg = _settings(tmp_path, max_tool_calls=0)
    reply = {
        "messages": [
            ToolMessage(content="tushare 返回", tool_call_id="1"),
            AIMessage(content="无法解析的输出"),
        ]
    }
    agent = FakeAgent([reply])
    with pytest.raises(research.DriverResearchError) as excinfo:
        await research.run_driver_research(
            "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: agent
        )
    assert len(agent.calls) == 1
    assert excinfo.value.truncated is True


async def test_salvaged_tier1_result_is_not_cached(tmp_path: Path) -> None:
    """只有"严格解析成功且未截断"的结果才落缓存（半截/救援结果缓存 90 天会固化噪声）。"""
    cfg = _settings(tmp_path)
    raw = json.dumps(
        {
            "driver_hypotheses": [
                {"mechanism": "缺少 variable"},
                _hypothesis("产能利用率").model_dump(mode="json"),
            ]
        },
        ensure_ascii=False,
    )
    result = await research.run_driver_research(
        "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: FakeAgent([raw])
    )
    assert result.meta["tier"] == 1
    assert not list(Path(cfg.cache_dir).glob("*.json"))


async def test_tool_budget_counts_across_retry_attempts_and_skips_cache(tmp_path: Path) -> None:
    """R1-a：重试也吃同一份工具预算——两次各 15 次调用（累计 30 > 20）⇒ truncated + LOW issue，
    且**本 attempt 解析成功也不落缓存**。"""
    cfg = _settings(tmp_path, max_tool_calls=20)
    first = {
        "messages": [
            *[ToolMessage(content=f"t{i}", tool_call_id=f"a{i}") for i in range(15)],
            AIMessage(content="第一次输出不是 JSON"),
        ]
    }
    second = {
        "messages": [
            *[ToolMessage(content=f"t{i}", tool_call_id=f"b{i}") for i in range(15)],
            AIMessage(content=_output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")])),
        ]
    }
    agent = FakeAgent([first, second])
    result = await research.run_driver_research(
        "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: agent
    )

    assert len(agent.calls) == 2
    assert result.meta["tool_calls"] == 30  # 累计（不是取 max）
    assert result.meta["truncated"] is True
    assert result.output.driver_hypotheses  # 仍然按已有输出收口
    truncated_issues = [
        issue
        for issue in result.meta["issues"]
        if issue["category"] == "driver_profile_research_truncated" and issue["severity"] == "low"
    ]
    assert len(truncated_issues) == 1  # 只记一条，不因每次 attempt 超限重复记账
    assert "30" in truncated_issues[0]["message"]
    assert not list(Path(cfg.cache_dir).glob("*.json"))


async def test_no_second_attempt_when_wall_budget_already_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-b：单标的墙钟预算只算一次——第一次 attempt 已吃掉全部预算后不再发起第二次。"""

    class FakeClock:
        """只替换 research 模块内的 time.monotonic（不动真实 time 模块，避免干扰事件循环）。"""

        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = FakeClock()
    monkeypatch.setattr(research, "time", clock)
    cfg = _settings(tmp_path, wall_timeout_s=10.0)

    class BudgetEatingAgent(FakeAgent):
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            result = await super().ainvoke(payload, config)
            clock.now += 11.0  # 第一次 attempt 结束后，单标的预算已用尽
            return result

    agent = BudgetEatingAgent(["第一次输出也不是 JSON", "第二次 attempt 不该被发起"])
    with pytest.raises(research.DriverResearchError) as excinfo:
        await research.run_driver_research(
            "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: agent
        )

    assert len(agent.calls) == 1
    assert "wall_timeout" in str(excinfo.value)
    assert excinfo.value.tier == 2
    assert excinfo.value.truncated is True


async def test_agent_exception_becomes_driver_research_error(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)

    class Boom:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            raise OSError("tushare 挂了")

    with pytest.raises(research.DriverResearchError) as excinfo:
        await research.run_driver_research("002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=Boom)
    assert "agent_failure:OSError" in str(excinfo.value)


async def test_truncated_result_is_not_cached(tmp_path: Path) -> None:
    cfg = _settings(tmp_path, max_tool_calls=0)
    reply = {
        "messages": [
            ToolMessage(content="x", tool_call_id="1"),
            AIMessage(content=_output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")])),
        ]
    }
    await research.run_driver_research(
        "002916.SZ", {}, settings=cfg, cache_dir=cfg.cache_dir, agent_factory=lambda: FakeAgent([reply])
    )
    assert not list(Path(cfg.cache_dir).glob("*.json"))


# ══════════════════════════════════════════════════════════════════════════
# 输出解析（Tier 0 / Tier 1）与提取函数
# ══════════════════════════════════════════════════════════════════════════


def test_parse_research_output_tier0_strict() -> None:
    output, tier, error = research.parse_research_output(
        _output_json(hypotheses=[_hypothesis("产能利用率").model_dump(mode="json")])
    )
    assert output is not None
    assert tier == 0
    assert error == ""


def test_parse_research_output_tier1_salvages_legal_items() -> None:
    raw = json.dumps(
        {
            "profit_formula": "收入 = 销量 × 单价",
            "driver_hypotheses": [
                {"mechanism": "缺少 variable"},
                _hypothesis("产能利用率").model_dump(mode="json"),
            ],
            "research_agenda": [{"why_matters": "缺少 question"}, {"question": "在建工程何时转固？"}],
        },
        ensure_ascii=False,
    )
    output, tier, error = research.parse_research_output(raw)
    assert output is not None
    assert tier == 1
    assert "schema_invalid_salvaged:2" in error
    assert [item.variable for item in output.driver_hypotheses] == ["产能利用率"]
    assert [item.question for item in output.research_agenda] == ["在建工程何时转固？"]


def test_parse_research_output_all_items_invalid_goes_to_ladder() -> None:
    raw = json.dumps({"driver_hypotheses": [{"mechanism": "缺少 variable"}], "research_agenda": []})
    output, tier, error = research.parse_research_output(raw)
    assert output is None
    assert tier == 2
    assert "schema_invalid" in error


def test_salvage_research_output_rejects_empty_shell() -> None:
    assert research.salvage_research_output({"driver_hypotheses": [], "research_agenda": []}) is None
    assert research.salvage_research_output("不是对象") is None


def test_salvage_research_output_syncs_renamed_and_new_fields() -> None:
    """P1-3/P1-4：宽松救援路径同步契约字段名——``proposed_frameworks`` / ``novel_concepts``
    原样救回；更名前的旧键 ``novel_drivers`` **不再**被读取（无双读、无静默兼容）。"""
    rescued = research.salvage_research_output(
        {
            "proposed_frameworks": ["产能爬坡型CDMO"],
            "novel_concepts": ["概念X"],
            "open_questions": ["缺口Y"],
        }
    )
    assert rescued is not None
    output, dropped = rescued
    assert dropped == 0
    assert output.proposed_frameworks == ["产能爬坡型CDMO"]
    assert output.novel_concepts == ["概念X"]
    assert output.open_questions == ["缺口Y"]
    # 只有旧键的载荷视为空壳 ⇒ 救援失败（否则等于把更名当没发生，静默带回旧语义）
    assert research.salvage_research_output({"novel_drivers": ["旧概念"]}) is None


def test_parse_research_output_empty_text() -> None:
    output, tier, error = research.parse_research_output("   ")
    assert output is None
    assert tier == 2
    assert error == "empty_response"


def test_extract_research_text_takes_only_text_blocks() -> None:
    result = {
        "messages": [
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "这是思考内容，不得进正文"},
                    {"type": "text", "text": '{"profit_formula": "x"}'},
                ]
            )
        ]
    }
    assert research.extract_research_text(result) == '{"profit_formula": "x"}'


def test_extract_research_text_handles_plain_string_and_empty_result() -> None:
    assert research.extract_research_text({"messages": [AIMessage(content="纯文本")]}) == "纯文本"
    assert research.extract_research_text({}) == ""
    assert research.extract_research_text({"messages": []}) == ""


def test_count_tool_calls_counts_tool_messages() -> None:
    result = {
        "messages": [
            AIMessage(content=""),
            ToolMessage(content="a", tool_call_id="1"),
            ToolMessage(content="b", tool_call_id="2"),
        ]
    }
    assert research.count_tool_calls(result) == 2
    assert research.count_tool_calls({}) == 0
