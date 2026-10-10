"""resolve_driver_profile 节点测试（DOMAIN_CONTEXT P0 第 5 步）。"""

import asyncio

from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.core import Artifact, ArtifactType, IssueSeverity, Run, RunStatus
from alphabee.domain_context import DriverProfile, RouterInput, build_driver_profile
from alphabee.orchestrator.contracts import IndustryContextArtifact
from alphabee.orchestrator.nodes import resolve_driver_profile as node


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


def test_normal_no_match_is_fallback_not_degraded():
    # 有身份信号但命中不了专用框架（白酒）→ fallback，但不降级、不产 issue
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
        # canonical 基准键 → 进 RouterInput 的 peer_gross_margin
        "peer_avg_gross_margin": 30.0,
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


def test_node_structure_facts_drive_fallback_primary_drivers():
    # 白酒命中不了专用框架 → 兜底；主驱动由分部结构 + 毛利率差（含 canonical 基准）确定性推导
    state = _state(
        symbol="600519.SH",
        artifacts=[
            _industry_artifact(industry="食品饮料", sub_industry="", sw_code="801120.SI"),
            _track_artifact_with_segments(),
        ],
    )
    state["fact_values"] = {"gross_margin": 35.0, "peer_avg_gross_margin": 30.0}
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
