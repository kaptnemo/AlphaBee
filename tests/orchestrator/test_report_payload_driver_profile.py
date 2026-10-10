"""报告生成载荷接入驱动画像（D2 §9 / G-5）。

契约：

* ``ReportGenerationPayload.driver_profile`` 默认空 dict（旧 artifact / 无画像不回归）；
* ``build_report_generation_payload`` 真实填入 D0 已补全的画像摘要（含 report_questions）；
* 报告/论点提示词写明：main_driver 取 ``driver_hypotheses[role=primary].variable``、
  central_tension 围绕 ``research_agenda[priority=critical]``、report_questions 必须回应、
  ``unverified_drivers`` 不得写成结论；
* 无画像（缺失）时 payload 仍可构造且 ``model_dump_json`` 不报错。
"""

from __future__ import annotations

import json
from typing import Any

from alphabee.agents.thesis.prompts import THESIS_AGENT_PROMPT
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Run, RunStatus
from alphabee.domain_context.contracts import (
    DriverEvidence,
    DriverHypothesis,
    DriverProfile,
    ResearchQuestion,
)
from alphabee.orchestrator.contracts import ReportGenerationPayload
from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT
from alphabee.orchestrator.services.payload_builders import build_report_generation_payload

SYMBOL = "002916.SZ"


def _driver_profile(**overrides: Any) -> DriverProfile:
    base: dict[str, Any] = {
        "symbol": SYMBOL,
        "playbook": "generic_fundamental",
        "playbook_version": 1,
        "primary_drivers": ["高频高速 CCL 收入占比 × 铜箔-树脂价差"],
        "secondary_drivers": ["产能利用率"],
        "provenance": "hybrid",
        "anchor_strength": "weak",
        "report_questions": ["高速覆铜板收入占比提升是否可持续？", "铜箔-树脂价差何时反转？"],
        "driver_hypotheses": [
            DriverHypothesis(
                variable="高频高速 CCL 收入占比 × 铜箔-树脂价差",
                role="primary",
                mechanism="AI 服务器放量 → 高毛利产品结构改善",
                evidence=[DriverEvidence(kind="annual_report", ref="2025 年报 第三节", quote="收入占比提升")],
            ),
            DriverHypothesis(variable="产能利用率", role="secondary"),
        ],
        "research_agenda": [
            ResearchQuestion(
                question="该分部收入占比口径是否含代工？",
                why_matters="口径决定主线",
                decisive_evidence=["分产品收入明细原始口径"],
                priority="critical",
            ),
            ResearchQuestion(question="在建工程何时转固？", priority="high"),
        ],
        "unverified_drivers": ["铜箔-树脂价差"],
        "novel_drivers": ["AI 服务器高速 CCL 结构占比"],
    }
    base.update(overrides)
    return DriverProfile(**base)


def _profile_artifact(profile: DriverProfile | None = None) -> Artifact:
    return Artifact(
        id="a-driver-profile",
        type=ArtifactType.DRIVER_PROFILE,
        producer_step="resolve_driver_profile",
        value=(profile or _driver_profile()).model_dump(mode="json"),
    )


def _state(artifacts: list[Artifact] | None = None, issues: list[Issue] | None = None) -> dict[str, Any]:
    return {
        "run": Run(
            id="run-1",
            goal="分析深南电路",
            status=RunStatus.RUNNING,
            context={"symbol": SYMBOL, "query": "分析深南电路"},
        ),
        "steps": [],
        "artifacts": artifacts or [],
        "issues": issues or [],
    }


# ── ① 契约：默认空 dict，既有消费方不回归 ────────────────────────────────


def test_report_payload_defaults_driver_profile_to_empty_dict() -> None:
    payload = ReportGenerationPayload()
    assert payload.driver_profile == {}
    # 既有字段全部保持默认（新增字段不改变旧构造路径）
    assert payload.company.symbol == ""
    assert payload.insight is None
    assert payload.company_track is None
    assert payload.issues == []
    # 旧 payload dict（无 driver_profile 键）仍可反序列化 ⇒ 旧 artifact/缓存不炸
    revived = ReportGenerationPayload.model_validate({"thesis": {"overall": "neutral"}})
    assert revived.driver_profile == {}
    # dump 一定包含该键（消费方按 key 读取，不会 KeyError）
    assert "driver_profile" in payload.model_dump()


# ── ② 真实填入（复用 D0 摘要，G-5 有通路）────────────────────────────────


def test_build_report_generation_payload_fills_driver_profile_summary() -> None:
    payload = build_report_generation_payload(_state([_profile_artifact()]))
    summary = payload.driver_profile

    assert summary["playbook"] == "generic_fundamental"
    assert summary["provenance"] == "hybrid"
    assert summary["anchor_strength"] == "weak"
    assert summary["primary_drivers"] == ["高频高速 CCL 收入占比 × 铜箔-树脂价差"]
    assert summary["secondary_drivers"] == ["产能利用率"]
    # D1 研究层字段：驱动假设（含 role）+ 议程（含 priority）+ 报告问题 + 未验证驱动
    assert summary["driver_hypotheses"] == [
        {"variable": "高频高速 CCL 收入占比 × 铜箔-树脂价差", "role": "primary"},
        {"variable": "产能利用率", "role": "secondary"},
    ]
    assert summary["research_agenda"] == [
        {"question": "该分部收入占比口径是否含代工？", "priority": "critical"},
        {"question": "在建工程何时转固？", "priority": "high"},
    ]
    assert summary["report_questions"] == ["高速覆铜板收入占比提升是否可持续？", "铜箔-树脂价差何时反转？"]
    assert summary["unverified_drivers"] == ["铜箔-树脂价差"]
    assert summary["novel_drivers"] == ["AI 服务器高速 CCL 结构占比"]


def test_report_prompt_json_really_carries_driver_profile() -> None:
    """报告 prompt 用 ``payload.model_dump_json()``：画像必须真的出现在那段 JSON 里。"""
    payload = build_report_generation_payload(_state([_profile_artifact()]))
    prompt_json = json.loads(payload.model_dump_json(indent=2))
    assert prompt_json["driver_profile"]["driver_hypotheses"][0]["role"] == "primary"
    assert "该分部收入占比口径是否含代工？" in payload.model_dump_json()
    assert "高速覆铜板收入占比提升是否可持续？" in payload.model_dump_json()


def test_driver_profile_summary_tolerates_missing_and_legacy_artifacts() -> None:
    # 无画像 ⇒ 空 dict，且 payload 与旧行为一致（其余字段照常）
    payload = build_report_generation_payload(_state())
    assert payload.driver_profile == {}
    assert payload.model_dump_json()  # 不报错

    # 旧 artifact（只有 playbook/primary_drivers，无 D1 字段）⇒ 仍能读出摘要，D1 键为空列表
    legacy = Artifact(
        id="a-legacy",
        type=ArtifactType.DRIVER_PROFILE,
        producer_step="resolve_driver_profile",
        value={"symbol": SYMBOL, "playbook": "hog_cycle", "primary_drivers": ["猪价"]},
    )
    legacy_payload = build_report_generation_payload(_state([legacy]))
    assert legacy_payload.driver_profile["playbook"] == "hog_cycle"
    assert legacy_payload.driver_profile["driver_hypotheses"] == []
    assert legacy_payload.driver_profile["research_agenda"] == []
    assert legacy_payload.driver_profile["report_questions"] == []


def test_report_payload_keeps_issue_disclosures_intact() -> None:
    """加字段不得影响既有 issue 披露路径（既有消费方不回归）。"""
    issue = Issue(
        id="issue-1",
        severity=IssueSeverity.HIGH,
        category="test",
        message="必须披露",
        related_step="resolve_driver_profile",
    )
    payload = build_report_generation_payload(_state([_profile_artifact()], issues=[issue]))
    assert [item.id for item in payload.issues] == ["issue-1"]
    assert [item.id for item in payload.required_issue_disclosures] == ["issue-1"]


# ── ③ 提示词指引 ─────────────────────────────────────────────────────────


def test_report_prompt_guides_main_driver_and_central_tension_from_driver_profile() -> None:
    prompt = REPORT_GENERATOR_PROMPT
    assert "driver_profile" in prompt
    assert 'role == "primary"' in prompt
    assert "driver_hypotheses" in prompt
    assert 'priority == "critical"' in prompt
    assert "research_agenda" in prompt
    assert "main_driver" in prompt
    assert "central_tension" in prompt
    # 报告问题必须逐条回应 + 未验证驱动不得写成结论
    assert "report_questions" in prompt and "逐条得到回应" in prompt
    assert "unverified_drivers" in prompt and "不得写成结论" in prompt
    # 无画像时的降级分支写明（不回归）
    assert "driver_profile 为空 dict 时" in prompt


def test_thesis_prompt_aligns_with_driver_profile_when_present() -> None:
    prompt = THESIS_AGENT_PROMPT
    assert "驱动画像对齐" in prompt
    assert 'role == "primary"' in prompt
    assert 'priority == "critical"' in prompt
    assert "unverified_drivers" in prompt
    assert "若输入没有 driver_profile 字段" in prompt
