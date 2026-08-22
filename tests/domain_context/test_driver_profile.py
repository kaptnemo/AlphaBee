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
