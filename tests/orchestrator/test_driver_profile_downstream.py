"""驱动画像 → 冲突探索 / 假设验证 / 洞察合成的下游接线（D2 §9）。

契约（逐条对应设计稿 §9 的 conflicts / verify / insights 三行）：

* ``generate_explore_conflicts_prompt``：``research_agenda``（仅 open）→ 框架层冲突假设、
  ``key_conflicts`` → 分歧模板、``candidates`` 非空 → 框架竞争假设 A/B/C；
* ``build_verify_context``：``recommended_verification_order`` 首次接线 + ``research_agenda`` 按
  ``priority``（critical → high → medium）排序，决定先验证什么；
* ``INSIGHT_AGENT_SYSTEM_PROMPT`` 原则 9：引用 ``driver_hypotheses`` 必须保留 evidence、
  ``unverified_drivers`` 不得写成结论；
* **无 driver_profile（缺失 / 空画像）时三个出口与旧版逐字一致**（不回归）；
* 框架竞争复用既有 ``ConflictAnalysisResult`` / ``VerificationResultItem``，未新建数据模型。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from alphabee.agents.explore_conflicts import prompts as conflicts_prompts
from alphabee.agents.insights import prompts as insights_prompts
from alphabee.agents.schemas import ConflictAnalysisResult, VerificationResultItem, VerificationResultList
from alphabee.agents.verify_hypotheses import prompts as verify_prompts
from alphabee.core import Artifact, ArtifactType
from alphabee.domain_context.contracts import (
    DriverEvidence,
    DriverHypothesis,
    DriverProfile,
    ResearchQuestion,
)
from alphabee.orchestrator.services import payload_builders

SYMBOL = "600519.SH"


# ── fixtures ──────────────────────────────────────────────────────────────


def _driver_profile(*, agenda: list[ResearchQuestion] | None = None, **overrides: Any) -> DriverProfile:
    base: dict[str, Any] = {
        "symbol": SYMBOL,
        "playbook": "hog_cycle",
        "playbook_version": 1,
        "primary_drivers": ["猪价", "完全成本"],
        "provenance": "hybrid",
        "anchor_strength": "weak",
        "key_conflicts": ["猪价上行 vs 出栏量下滑", "成本改善 vs 价格回落"],
        "recommended_verification_order": ["完全成本口径", "能繁母猪存栏"],
        "research_agenda": agenda
        if agenda is not None
        else [
            ResearchQuestion(
                question="公司自称 AI 服务器龙头，但该分部收入占比仅 8%——口径是否含代工？",
                why_matters="口径决定报告主线是否切换",
                decisive_evidence=["分产品收入明细的原始口径（按产品还是按客户）"],
                preferred_sources=["query_financial_report 第三节分产品收入明细"],
                priority="critical",
            ),
            ResearchQuestion(question="完全成本是否已见底？", priority="medium"),
            ResearchQuestion(question="资本开支周期何时反转？", priority="high"),
            ResearchQuestion(question="这条已回答，不再是冲突来源", priority="high", status="answered"),
        ],
        "candidates": ["mining_services"],
        "unverified_drivers": ["铜箔-树脂价差"],
        "driver_hypotheses": [
            DriverHypothesis(
                variable="高频高速 CCL 收入占比 × 铜箔-树脂价差",
                role="primary",
                evidence=[DriverEvidence(kind="annual_report", ref="2025 年报 第三节", quote="高速覆铜板收入占比提升")],
            )
        ],
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


def _state(artifacts: list[Artifact] | None = None) -> dict[str, Any]:
    return {"artifacts": artifacts or [], "issues": []}


def _prompt_payload(prompt: str) -> dict[str, Any]:
    """取出 conflicts prompt 里那段 ```json 输入包（断言"真的进了 prompt"而非只看源码）。"""
    marker = "```json\n"
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n```", start)
    return json.loads(prompt[start:end])


# ── ① conflicts：agenda / key_conflicts / candidates 进 prompt ────────────


def test_conflicts_prompt_carries_open_agenda_key_conflicts_and_candidates() -> None:
    prompt = payload_builders.generate_explore_conflicts_prompt(_state([_profile_artifact()]), "分析贵州茅台", SYMBOL)
    payload = _prompt_payload(prompt)
    driver_profile = payload["driver_profile"]

    # research_agenda：仅 open 项进入，且带 why_matters / decisive_evidence / preferred_sources / priority
    questions = [item["question"] for item in driver_profile["research_agenda"]]
    assert "公司自称 AI 服务器龙头，但该分部收入占比仅 8%——口径是否含代工？" in questions
    assert "这条已回答，不再是冲突来源" not in questions
    critical = driver_profile["research_agenda"][0]
    assert critical["priority"] == "critical"
    assert critical["why_matters"] == "口径决定报告主线是否切换"
    assert critical["decisive_evidence"] == ["分产品收入明细的原始口径（按产品还是按客户）"]
    assert critical["preferred_sources"] == ["query_financial_report 第三节分产品收入明细"]

    # key_conflicts：框架级分歧模板
    assert driver_profile["key_conflicts"] == ["猪价上行 vs 出栏量下滑", "成本改善 vs 价格回落"]

    # candidates 非空 ⇒ 框架竞争假设 A/B/C 的输入 + 指引
    assert driver_profile["framework_candidates"] == ["mining_services"]
    assert "framework_candidates" in prompt
    assert "A/B/C" in prompt
    assert "框架层冲突" in prompt
    assert "research_agenda" in prompt
    assert "key_conflicts" in prompt


def test_conflicts_prompt_keeps_legacy_fields_and_contract() -> None:
    """新增字段不得挤掉旧字段，也不得换掉既有输出契约（ConflictAnalysisResult）。"""
    prompt = payload_builders.generate_explore_conflicts_prompt(_state([_profile_artifact()]), "分析贵州茅台", SYMBOL)
    payload = _prompt_payload(prompt)
    assert set(payload) == {
        "symbol",
        "query",
        "latest_snapshot",
        "market_valuation",
        "key_signals",
        "key_derived_facts",
        "anomaly",
        "disputed_candidates",
        "company_track",
        "driver_profile",
    }
    assert "ConflictAnalysisResult" in prompt
    assert "这是探索阶段（provisional）" in prompt
    # 指引插在正文与 JSON 输入包之间（不破坏既有的“正文 + 输入包”结构）
    assert prompt.index("框架层冲突") < prompt.index("```json")


def test_conflicts_prompt_reports_no_framework_competition_without_candidates() -> None:
    prompt = payload_builders.generate_explore_conflicts_prompt(
        _state([_profile_artifact(_driver_profile(candidates=[]))]), "分析贵州茅台", SYMBOL
    )
    assert _prompt_payload(prompt)["driver_profile"]["framework_candidates"] == []


# ── ② verify context：priority 顺序 + recommended_verification_order ─────


def test_verify_context_orders_agenda_by_priority_and_exposes_recommended_order() -> None:
    context = payload_builders.build_verify_context(_state([_profile_artifact()]), SYMBOL)
    driver_profile = context["driver_profile"]

    # recommended_verification_order（此前零消费）首次接线
    assert driver_profile["recommended_verification_order"] == ["完全成本口径", "能繁母猪存栏"]
    # priority 决定验证顺序：critical → high → medium（输入顺序是 critical/high/medium 乱序的）
    assert [item["priority"] for item in driver_profile["research_agenda"]] == [
        "critical",
        "high",
        "medium",
    ]
    assert driver_profile["verification_sequence"] == [
        "公司自称 AI 服务器龙头，但该分部收入占比仅 8%——口径是否含代工？",
        "资本开支周期何时反转？",
        "完全成本是否已见底？",
    ]
    # 已回答的议程不参与排序
    assert "这条已回答，不再是冲突来源" not in driver_profile["verification_sequence"]
    # 决定论据 / 取证来源一并传给验证代理
    critical = driver_profile["research_agenda"][0]
    assert critical["decisive_evidence"] == ["分产品收入明细的原始口径（按产品还是按客户）"]
    assert critical["preferred_sources"] == ["query_financial_report 第三节分产品收入明细"]


def test_verify_prompt_renders_driver_profile_ordering_guidance() -> None:
    """context → prompt：验证模板能渲染（花括号未破坏 format）且带顺序指引与议程问题。"""
    context = payload_builders.build_verify_context(_state([_profile_artifact()]), SYMBOL)
    prompt = verify_prompts.VERIFY_HYPOTHESES_USER_TEMPLATE.format(
        hypotheses_json="[]",
        context_json=json.dumps(context, ensure_ascii=False, indent=2),
    )
    assert "recommended_verification_order" in prompt
    assert "资本开支周期何时反转？" in prompt
    assert "critical → high → medium" in prompt
    assert "先按 `recommended_verification_order`" in prompt


def test_verify_system_prompt_has_ordering_principle() -> None:
    system_prompt = verify_prompts.VERIFY_HYPOTHESES_PROMPT
    assert "按驱动画像决定验证顺序" in system_prompt
    assert "recommended_verification_order" in system_prompt
    assert "critical → high → medium" in system_prompt


# ── ③ insights 原则 9 增补 ───────────────────────────────────────────────


def test_insights_principle9_requires_evidence_and_quarantines_unverified() -> None:
    prompt = insights_prompts.INSIGHT_AGENT_SYSTEM_PROMPT
    assert "引用 `driver_hypotheses` 时必须保留其 evidence" in prompt
    assert "不得只搬 `variable` 而丢掉证据" in prompt
    assert "**`unverified_drivers` 不得写成结论**" in prompt
    assert "what_would_change_my_mind" in prompt


# ── ④ 无 driver_profile：与旧版一致（不回归） ─────────────────────────────


def test_conflicts_prompt_without_driver_profile_matches_legacy_shape() -> None:
    prompt = payload_builders.generate_explore_conflicts_prompt(_state(), "分析贵州茅台", SYMBOL)
    payload = _prompt_payload(prompt)
    # 旧字段一字不少、一字不多；不注入 driver_profile，也不追加框架层指引
    assert set(payload) == {
        "symbol",
        "query",
        "latest_snapshot",
        "market_valuation",
        "key_signals",
        "key_derived_facts",
        "anomaly",
        "disputed_candidates",
        "company_track",
    }
    assert "driver_profile" not in prompt
    assert "框架层冲突" not in prompt
    assert "A/B/C" not in prompt
    # 无画像 vs 空画像（议程/模板/候选全空）必须完全一致：空画像不产生任何注入
    empty_profile_prompt = payload_builders.generate_explore_conflicts_prompt(
        _state([_profile_artifact(_driver_profile(agenda=[], key_conflicts=[], candidates=[]))]),
        "分析贵州茅台",
        SYMBOL,
    )
    assert empty_profile_prompt == prompt


def test_verify_context_without_driver_profile_matches_legacy_shape() -> None:
    context = payload_builders.build_verify_context(_state(), SYMBOL)
    assert set(context) == {"symbol", "company_track", "financial_snapshots", "market", "anomaly"}
    assert "driver_profile" not in context
    # 空画像（无议程、无推荐顺序）同样不注入
    empty_context = payload_builders.build_verify_context(
        _state([_profile_artifact(_driver_profile(agenda=[], recommended_verification_order=[]))]), SYMBOL
    )
    assert empty_context == context


# ── ⑤ 复用既有契约，未新建数据模型 ───────────────────────────────────────


def test_no_new_pydantic_models_introduced_by_downstream_wiring() -> None:
    """框架竞争仍走既有 ``ConflictAnalysisResult`` / ``VerificationResultItem``。"""
    modules = [
        payload_builders,
        conflicts_prompts,
        verify_prompts,
        insights_prompts,
    ]
    for module in modules:
        locally_defined = [
            name
            for name, obj in vars(module).items()
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == module.__name__
        ]
        assert locally_defined == [], f"{module.__name__} 不应新增数据模型：{locally_defined}"

    # 既有契约未被替换/新增：冲突列表与验证结果仍是原模型
    assert "conflicts" in ConflictAnalysisResult.model_fields
    assert "status" in VerificationResultItem.model_fields
    assert "results" in VerificationResultList.model_fields
