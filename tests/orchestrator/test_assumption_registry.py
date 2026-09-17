"""从 F1(C2) 的 tests/orchestrator/test_node_contracts.py 移出的 4 个跨期用例。

来源：engineer-impl 在 captain 裁决「F1c 移出 F1」时，从 C2 版测试文件中删除的用例。
原因（R2-1 blocker）：C2 树不含 gates.py / verification.py（二者属 F1c/t21），
这 4 个用例 import 了其中的 build_invalidated_assumption_issues / build_assumption_registry
→ 在预期 C2 树上必然 ImportError/AttributeError（实测 4 failed）。

**归属：t21(F1c)** —— 请把它们并入 `tests/orchestrator/test_assumption_registry.py`
（该文件在 t21 的 inScope 内），随 F1c 的生产者/消费者一起提交。
本文件由会话记录重建（原文件删除发生在本指令之前、工作区已无法导出），
依赖的辅助函数（_ctx/_artifact/_step/_with_contract/_registry_artifact/_replace 等）
需按 t21 的测试文件重新提供。
"""

from __future__ import annotations

import pytest

from alphabee.core.schemas import Artifact, ArtifactType


def test_verification_registry_producer_is_switch_gated(monkeypatch: pytest.MonkeyPatch):
    """生产者受 ``deviation.detection.enabled`` 控制：关闭 → 不产 registry artifact（纯增量）。"""
    from alphabee.agents.schemas import ConflictAnalysisResult, ConflictItem, HypothesisItem
    from alphabee.core.schemas import ArtifactType
    from alphabee.orchestrator.nodes import verification as verification_node
    from alphabee.orchestrator.services import detection

    conflict = ConflictItem(
        id="c1",
        theme="应收质量",
        description="利润增长没有被现金流验证。",
        related_dimensions=["earnings_quality"],
        severity="high",
        confidence=0.9,
        hypotheses=[
            HypothesisItem(
                id="h1",
                conflict_id="c1",
                explanation="应收增长源于结算周期而非恶化",
                predictions=["经营现金流/净利润持续低于1"],
                required_evidence=["financial_facts"],
                score=0.8,
            )
        ],
    )
    result = ConflictAnalysisResult.model_validate({"conflicts": [conflict.model_dump(mode="json")]})
    hypotheses = list(result.conflicts[0].hypotheses)

    monkeypatch.setattr(detection, "detection_switches", lambda: False)
    assert verification_node.build_assumption_registry(result, hypotheses, {}, "verify_hypotheses") is None

    monkeypatch.setattr(detection, "detection_switches", lambda: True)
    artifact = verification_node.build_assumption_registry(result, hypotheses, {}, "verify_hypotheses")
    assert artifact is not None
    assert artifact.type == ArtifactType.ASSUMPTION_REGISTRY
    assert artifact.value["entries"][0]["id"] == "h1"
    assert artifact.value["entries"][0]["status"] == "active"


def test_gate_assumption_check_is_strict_noop_without_registry():
    """★ C2：登记簿缺失 → gate 检查不产 issue、不改返回值。"""
    from alphabee.orchestrator.gates import build_invalidated_assumption_issues

    assert build_invalidated_assumption_issues({}, "review_report") == []
    assert build_invalidated_assumption_issues({"artifacts": []}, "review_report") == []


def test_gate_assumption_check_is_strict_noop_without_invalidated_entries():
    from alphabee.core.schemas import ArtifactType
    from alphabee.orchestrator.contracts import AssumptionEntry, AssumptionRegistryArtifact
    from alphabee.orchestrator.gates import build_invalidated_assumption_issues

    artifact = Artifact(
        id="artifact-reg",
        type=ArtifactType.ASSUMPTION_REGISTRY,
        producer_step="verify_hypotheses",
        value=AssumptionRegistryArtifact(entries=[AssumptionEntry(id="h1", statement="x")]).model_dump(mode="json"),
    )
    assert build_invalidated_assumption_issues({"artifacts": [artifact]}, "review_report") == []


def test_gate_assumption_check_fires_on_invalidated_entry():
    from alphabee.core.schemas import ArtifactType
    from alphabee.orchestrator.contracts import AssumptionEntry, AssumptionRegistryArtifact
    from alphabee.orchestrator.gates import build_invalidated_assumption_issues

    artifact = Artifact(
        id="artifact-reg",
        type=ArtifactType.ASSUMPTION_REGISTRY,
        producer_step="verify_hypotheses",
        value=AssumptionRegistryArtifact(
            entries=[AssumptionEntry(id="h1", statement="应收增长源于结算周期", status="invalidated")]
        ).model_dump(mode="json"),
    )
    issues = build_invalidated_assumption_issues({"artifacts": [artifact]}, "review_report")
    assert len(issues) == 1
    assert issues[0].category == "assumption_based_claim"


def test_conflicts_provisional_assumptions_are_switch_gated(monkeypatch: pytest.MonkeyPatch):
    """探索阶段生产者（§14.2-D 生产者之一）：开关关闭 → 不产 artifact；开启 → active 条目。

    与 verification 的结算登记簿用**同一个假设 id**，保证下游按 id 一一对应。
    （本条由 engineer-impl 按会话记录重建：原用例随 conflicts.py 的 F1c 代码一并移出 C2，
    归档首版漏收此条，经 reviewer 指出后补入。）
    """
    from alphabee.agents.schemas import ConflictAnalysisResult, ConflictItem, HypothesisItem
    from alphabee.core.schemas import ArtifactType
    from alphabee.orchestrator.nodes import conflicts as conflicts_node
    from alphabee.orchestrator.services import detection

    conflict = ConflictItem(
        id="c1",
        theme="应收质量",
        description="利润增长没有被现金流验证。",
        related_dimensions=["earnings_quality"],
        severity="high",
        confidence=0.9,
        hypotheses=[
            HypothesisItem(
                id="h1",
                conflict_id="c1",
                explanation="应收增长源于结算周期而非恶化",
                predictions=["经营现金流/净利润持续低于1"],
                required_evidence=["financial_facts"],
                score=0.8,
            )
        ],
    )
    result = ConflictAnalysisResult.model_validate({"conflicts": [conflict.model_dump(mode="json")]})

    monkeypatch.setattr(detection, "detection_switches", lambda: False)
    assert conflicts_node.build_provisional_assumptions(result, "explore_conflicts") is None

    monkeypatch.setattr(detection, "detection_switches", lambda: True)
    artifact = conflicts_node.build_provisional_assumptions(result, "explore_conflicts")
    assert artifact is not None
    assert artifact.type == ArtifactType.ASSUMPTION_REGISTRY
    assert artifact.value["entries"][0] == {
        "id": "h1",
        "statement": "应收增长源于结算周期而非恶化",
        "status": "active",
        "source_artifact": "conflicts_result",
        "invalidated_by": "",
    }


# ── t21 追加的 4 项验收（R3-3 判定 / R2-5 语义 / 探索⊆结算 / 双 registry）──────


def _conflicts(specs: list[tuple[str, str]] | None = None):
    """构造一份含假设的 ConflictAnalysisResult（生产者入参）。"""
    from alphabee.agents.schemas import ConflictAnalysisResult, ConflictItem, HypothesisItem

    pairs = specs or [("h1", "应收增长源于结算周期而非恶化")]
    conflict = ConflictItem(
        id="c1",
        theme="应收质量",
        description="利润增长没有被现金流验证。",
        related_dimensions=["earnings_quality"],
        severity="high",
        confidence=0.9,
        hypotheses=[
            HypothesisItem(
                id=hid,
                conflict_id="c1",
                explanation=statement,
                predictions=["经营现金流/净利润持续低于1"],
                required_evidence=["financial_facts"],
                score=0.8,
            )
            for hid, statement in pairs
        ],
    )
    return ConflictAnalysisResult.model_validate({"conflicts": [conflict.model_dump(mode="json")]})


def _verif(hypothesis_id: str, status: str):
    """构造 VerificationResultItem（结算期状态来源）。"""
    from alphabee.agents.schemas import VerificationResultItem

    return VerificationResultItem(
        id=f"v-{hypothesis_id}",
        hypothesis_id=hypothesis_id,
        status=status,
        support_score=0.1,
        contradiction_score=0.9,
        confidence=0.8,
        summary="反证充分",
    )


def test_producer_ids_are_non_containing_by_construction():
    """★ R3-3 判定（t21 前置检查）：生产端 id 由 ``_make_id`` 生成，形态为
    ``<prefix>-<uuid4().hex[:12]>`` —— **定长 12 位十六进制**。同前缀 id 等长 ⇒ 任一 id
    都不可能是另一个 id 的子串 ⇒ ``_node_payloads_referencing`` 的"序列化文本包含"匹配在
    生产形态下**不可触发子串碰撞**（R3-3 记为"生产形态下不可触发"关闭）。

    本用例以真实生成器采样钉住该形态，防将来改成短 id / 递增 id 后静默回归。
    """
    import re

    from alphabee.orchestrator.collectors import _make_id

    ids = [_make_id("hypothesis") for _ in range(200)]
    assert all(re.fullmatch(r"hypothesis-[0-9a-f]{12}", value) for value in ids)
    assert len(set(ids)) == len(ids), "id 必须唯一"
    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            assert first not in second and second not in first, f"id 互为子串：{first} / {second}"


def test_produced_entries_have_non_empty_statement():
    """★ R2-5 语义要求落在**生产者侧**：产出的每条 ``entry.statement`` 必须非空
    （``AssumptionEntry.statement`` 保留默认 ``""`` 是 append-only 兼容需要，
    语义上的"必填"由生产者保证）。"""
    from alphabee.orchestrator.nodes import conflicts as conflicts_node
    from alphabee.orchestrator.nodes import verification as verification_node

    conflicts_result = _conflicts([("h1", "甲假设"), ("h2", "乙假设")])
    hypotheses = list(conflicts_result.conflicts[0].hypotheses)
    provisional = conflicts_node.build_provisional_assumptions(conflicts_result, "explore_conflicts")
    settled = verification_node.build_assumption_registry(
        conflicts_result, hypotheses, {"h1": _verif("h1", "rejected")}, "verify_hypotheses"
    )
    assert provisional is not None and settled is not None
    for artifact in (provisional, settled):
        assert artifact.value["entries"], "登记簿不应为空"
        assert all(entry["statement"].strip() for entry in artifact.value["entries"])


def test_settlement_registry_covers_all_exploration_ids():
    """★ 探索期登记的 id 集合必须被结算期重建集合覆盖 —— 否则某假设的 ``invalidated``
    状态永远不会被检测器看到（静默漏报；因消费侧只取**最新**一份 registry）。"""
    from alphabee.orchestrator.nodes import conflicts as conflicts_node
    from alphabee.orchestrator.nodes import verification as verification_node

    conflicts_result = _conflicts([("h1", "甲假设"), ("h2", "乙假设")])
    hypotheses = list(conflicts_result.conflicts[0].hypotheses)
    provisional = conflicts_node.build_provisional_assumptions(conflicts_result, "explore_conflicts")
    settled = verification_node.build_assumption_registry(
        conflicts_result,
        hypotheses,
        {},
        "verify_hypotheses",  # 无结算结果 → 全 active
    )
    assert provisional is not None and settled is not None
    exploration_ids = {entry["id"] for entry in provisional.value["entries"]}
    settlement_ids = {entry["id"] for entry in settled.value["entries"]}
    assert exploration_ids <= settlement_ids, f"结算期未覆盖探索期 id：{exploration_ids - settlement_ids}"


def test_detector_reads_latest_registry_and_catches_d4():
    """★ 双 registry 用例：探索期先产一份**全 active** 的 registry，结算期再产一份含
    ``invalidated`` 的重建版；消费侧 ``find_artifact_model`` 取**最新**一份 ⇒ 只要本节点
    引用了该假设 id，检测器仍捕获 D4（§14.2-B + §6.3 的超集语义）。"""
    from alphabee.core.schemas import Artifact as SchemaArtifact
    from alphabee.orchestrator.detectors import NodeContext, assumption_still_valid
    from alphabee.orchestrator.nodes import conflicts as conflicts_node
    from alphabee.orchestrator.nodes import verification as verification_node

    conflicts_result = _conflicts([("h1", "甲假设")])
    hypotheses = list(conflicts_result.conflicts[0].hypotheses)
    hid = hypotheses[0].id

    early = conflicts_node.build_provisional_assumptions(conflicts_result, "explore_conflicts")
    late = verification_node.build_assumption_registry(
        conflicts_result, hypotheses, {hid: _verif(hid, "rejected")}, "verify_hypotheses"
    )
    assert early is not None and late is not None
    assert all(entry["status"] == "active" for entry in early.value["entries"])
    assert late.value["entries"][0]["status"] == "invalidated"

    referencing = SchemaArtifact(
        id="artifact-ref",
        type=ArtifactType.THESIS_ANALYSIS,
        producer_step="run_thesis",
        value={"based_on": [hid]},
    )
    ctx = NodeContext(
        node_id="run_thesis",
        step=None,
        new_artifacts=[referencing],
        view={"artifacts": [early, late]},
    )
    result = assumption_still_valid(ctx)
    assert result.passed is False, "结算期重建的 invalidated 必须被检测到（消费最新一份）"
    assert result.deviation_class is not None and result.deviation_class.value == "d4_state"
