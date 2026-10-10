"""DriverProfile 组装与契约测试（DOMAIN_CONTEXT_ROADMAP P0 第 4 步）。"""

from alphabee.core import ArtifactRoleGroup, ArtifactType
from alphabee.domain_context import DriverProfile, RouterInput, build_driver_profile


def test_muyuan_driver_profile_expands_primitives():
    profile = build_driver_profile(
        "002714.SZ",
        RouterInput(track_label="生猪养殖", industry="农林牧渔", sub_industry="养殖业"),
    )
    assert profile.symbol == "002714.SZ"
    assert profile.playbook == "hog_cycle"
    assert profile.fallback is False
    assert profile.degraded is False
    # 展开后的原语（含完整内容）
    ids = {p.id for p in profile.activated_primitives}
    assert ids == {"commodity_cycle", "biological_inventory", "cost_curve", "capacity_cycle"}
    # 每个激活原语都带上了 key_variables（不再只是 id）
    for p in profile.activated_primitives:
        assert p.key_variables
        assert p.priority_questions
    # 主驱动变量（变量名，用于报告主线）
    assert profile.primary_drivers == ["猪价", "能繁母猪存栏", "完全成本"]


def test_generic_fallback_profile_not_degraded():
    profile = build_driver_profile(
        "600519.SH",
        RouterInput(track_label="白酒", industry="食品饮料", sub_industry="白酒Ⅱ", business_model="brand"),
    )
    assert profile.playbook == "generic_fundamental"
    assert profile.fallback is True
    assert profile.degraded is False


def test_empty_input_profile_degraded():
    profile = build_driver_profile("", RouterInput())
    assert profile.fallback is True
    assert profile.degraded is True
    assert profile.degraded_reason == "identity_signals_missing"


def test_artifact_type_registered():
    from alphabee.core.schemas import _ARTIFACT_TYPE_TO_ROLE_GROUP

    assert ArtifactType.DRIVER_PROFILE.value == "driver_profile"
    assert _ARTIFACT_TYPE_TO_ROLE_GROUP[ArtifactType.DRIVER_PROFILE] == ArtifactRoleGroup.DATA


def test_coerce_driver_profile_roundtrip():
    from alphabee.orchestrator.contracts import coerce_driver_profile

    profile = build_driver_profile(
        "002714.SZ",
        RouterInput(track_label="生猪养殖", sub_industry="养殖业"),
    )
    # 已是实例 → 原样返回
    assert coerce_driver_profile(profile) is profile
    # dict → 校验重建
    coerced = coerce_driver_profile(profile.model_dump())
    assert isinstance(coerced, DriverProfile)
    assert coerced is not None
    assert coerced.playbook == "hog_cycle"
    assert len(coerced.activated_primitives) == 4
    # None → None
    assert coerce_driver_profile(None) is None


# ── v2 契约：只增不减、旧 artifact 向后兼容 ────────────────────────────


def _legacy_v1_payload() -> dict:
    """schema_version="1" 时期的画像 dict（无任何 v2 字段、原语也无新字段）。"""
    return {
        "schema_version": "1",
        "symbol": "002714.SZ",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "playbook": "hog_cycle",
        "playbook_version": 1,
        "activated_primitives": [
            {
                "id": "commodity_cycle",
                "score": 1.0,
                "trend": "stable",
                "description": "商品价格周期",
                "key_variables": ["猪价"],
                "priority_questions": ["当前处于周期哪个阶段？"],
                "disconfirming_signals": ["价格下行仍在扩产"],
                "preferred_sources": ["现货价格"],
                "report_angles": ["周期定位"],
            }
        ],
        "primary_drivers": ["猪价"],
        "secondary_drivers": [],
        "why_selected": ["track_label_match"],
        "fallback": False,
        "degraded": False,
        "degraded_reason": "",
    }


def test_legacy_v1_artifact_validates_with_v2_defaults():
    profile = DriverProfile.model_validate(_legacy_v1_payload())

    # 旧数据保留自己的版本号，不被强行改写
    assert profile.schema_version == "1"
    assert profile.playbook == "hog_cycle"
    # 新增字段全部取默认值
    assert profile.provenance == "rule"
    assert profile.anchor_strength == ""
    assert profile.key_conflicts == []
    assert profile.recommended_verification_order == []
    assert profile.report_questions == []
    assert profile.driver_hypotheses == []
    assert profile.research_agenda == []
    assert profile.novel_drivers == []
    assert profile.candidates == []
    assert profile.unverified_drivers == []
    assert profile.research_confidence == 0.0
    assert profile.research_meta == {}
    # ActivatedPrimitive 也只增字段
    primitive = profile.activated_primitives[0]
    assert primitive.causal_paths == []
    assert primitive.when_to_activate == []


def test_new_profile_carries_anchor_strength_and_playbook_knowledge():
    profile = build_driver_profile(
        "002714.SZ",
        RouterInput(track_label="生猪养殖", sub_industry="养殖业"),
    )
    assert profile.schema_version == "2"
    assert profile.provenance == "rule"
    assert profile.anchor_strength == "strong"
    # playbook 级框架知识进入快照（此前只在 YAML 里）
    assert profile.key_conflicts
    assert profile.recommended_verification_order
    assert profile.report_questions


def test_profile_passes_through_causal_paths_and_when_to_activate():
    profile = build_driver_profile("002714.SZ", RouterInput(track_label="生猪养殖"))
    for primitive in profile.activated_primitives:
        assert primitive.causal_paths
        assert primitive.when_to_activate


def test_driver_hypotheses_and_research_agenda_roundtrip():
    from alphabee.domain_context.contracts import (
        DriverEvidence,
        DriverHypothesis,
        DriverObservable,
        ResearchQuestion,
    )

    profile = build_driver_profile("002714.SZ", RouterInput(track_label="生猪养殖"))
    profile = profile.model_copy(
        update={
            "schema_version": "2",
            "driver_hypotheses": [
                DriverHypothesis(
                    variable="猪价",
                    role="primary",
                    mechanism="猪价上行 → 售价抬升 → 头均利润修复",
                    company_form="商品猪出栏均价",
                    observables=[DriverObservable(name="生猪均价", source="tushare:index_classify", cadence="周")],
                    falsifiers=["猪价下行而产能继续扩张"],
                    evidence=[
                        DriverEvidence(kind="结构化事实", ref="artifact:market_facts", quote="均价 15.2 元/公斤")
                    ],
                    confidence=0.6,
                    matched_primitive="commodity_cycle",
                )
            ],
            "research_agenda": [
                ResearchQuestion(
                    question="猪价上行是周期反转还是反弹？",
                    why_matters="决定盈利修复的持续性与赔率",
                    decisive_evidence=["能繁母猪存栏趋势"],
                    preferred_sources=["tushare"],
                    priority="critical",
                    status="open",
                )
            ],
        }
    )

    dumped = profile.model_dump(mode="json")
    restored = DriverProfile.model_validate(dumped)

    assert restored.driver_hypotheses == profile.driver_hypotheses
    assert restored.research_agenda == profile.research_agenda
    assert restored.driver_hypotheses[0].observables[0].name == "生猪均价"
    assert restored.research_agenda[0].priority == "critical"
