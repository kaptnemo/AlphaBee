"""resolve_driver_profile 节点测试（DOMAIN_CONTEXT P0 第 5 步）。"""

import asyncio

from alphabee.company_track.contracts import CompanyTrackArtifact
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
    state = _state(
        symbol="603979.SH",
        artifacts=[
            _industry_artifact(industry="建筑装饰", sub_industry="采掘服务", sw_code="801720.SI"),
            _track_artifact(track_label="矿业服务", business_model="integrator"),
        ],
    )
    result = asyncio.run(node.resolve_driver_profile(state, {}))

    profile = _find_driver_profile(result)
    assert profile.playbook == "mining_services"
    assert profile.fallback is False


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
