"""F1b 检测器测试：6 个确定性检测器的"触发 / 不触发"双例与 fail-open（§14.2-B、§14.7）。

覆盖要点：

* 每个检测器**触发与不触发**两侧（含 ``derived_facts_nonempty`` 的"真无数据 vs 引擎故障"分支）；
* **注册表缺失 = 严格 no-op**：``assumption_still_valid`` 在无登记簿时判 pass（不误报 D4，C2）；
* 检测器**只读**：执行后 ``ctx`` 与入参逐字段一致（不得修改 state/update）；
* 单个检测器抛异常 → ``logger.warning`` + 跳过，**不影响同节点其他检测器**（fail-open，C3）；
* 无 LLM 检测器（v1）：注册表里 6 个名字全为确定性函数。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from alphabee.core.schemas import (
    Artifact,
    ArtifactType,
    DeviationClass,
    IssueSeverity,
    Step,
    StepStatus,
)
from alphabee.orchestrator import detectors as det
from alphabee.orchestrator.contracts import (
    AssumptionEntry,
    AssumptionRegistryArtifact,
)
from alphabee.orchestrator.detectors import DETECTORS, NodeContext, run_detectors


def _artifact(artifact_type: ArtifactType, value: dict, artifact_id: str = "artifact-1") -> Artifact:
    return Artifact(id=artifact_id, type=artifact_type, producer_step="step-1", value=value)


def _step(node_id: str = "run_analysis_engines") -> Step:
    return Step(id=node_id, kind=node_id, status=StepStatus.RUNNING)


def _ctx(
    node_id: str = "run_analysis_engines",
    *,
    new_artifacts: list[Artifact] | None = None,
    new_decisions: list | None = None,
    view: dict | None = None,
) -> NodeContext:
    return NodeContext(
        node_id=node_id,
        step=_step(node_id),
        new_artifacts=list(new_artifacts or []),
        new_decisions=list(new_decisions or []),
        view=dict(view or {}),
    )


def _contract(names: list[str]):
    from alphabee.orchestrator.node_contracts import NodeContract

    return NodeContract(node_id="synthetic_node", detectors=names)


# ── derived_facts_nonempty（§6.2 双重检查模板） ─────────────────────────────


def test_derived_facts_nonempty_passes_when_results_present():
    ctx = _ctx(new_artifacts=[_artifact(ArtifactType.DERIVED_FACTS, {"results": {"roe": {"roe": 0.12}}})])
    result = det.derived_facts_nonempty(ctx)
    assert result.passed is True


def test_derived_facts_nonempty_true_no_data_is_low_severity():
    """artifact 空 + fact_values 空 → 判 D1 但 severity=low（"可能真无数据"）。"""
    ctx = _ctx(new_artifacts=[_artifact(ArtifactType.DERIVED_FACTS, {"results": {}})], view={"fact_values": {}})
    result = det.derived_facts_nonempty(ctx)
    assert result.passed is False
    assert result.deviation_class is DeviationClass.D1_DATA
    assert result.severity is IssueSeverity.LOW
    assert "可能确实无可用数据" in result.message


def test_derived_facts_nonempty_engine_fault_is_high_severity():
    """artifact 空但 fact_values 非空 → D1 high（引擎故障）。"""
    ctx = _ctx(
        new_artifacts=[_artifact(ArtifactType.DERIVED_FACTS, {"results": {}})],
        view={"fact_values": {"roe": 0.12, "gross_margin": 0.3}},
    )
    result = det.derived_facts_nonempty(ctx)
    assert result.passed is False
    assert result.severity is IssueSeverity.HIGH
    assert "引擎故障" in result.message


def test_derived_facts_nonempty_passes_when_artifact_absent():
    """本节点没产 DERIVED_FACTS 就不是本检测器的职责（不误报）。"""
    assert det.derived_facts_nonempty(_ctx()).passed is True


# ── artifact_schema_valid ───────────────────────────────────────────────────


def test_artifact_schema_valid_passes_on_valid_payload():
    ctx = _ctx(
        new_artifacts=[_artifact(ArtifactType.DERIVED_FACTS, {"results": {"roe": {"roe": 1.0}}, "rule_count": 1})]
    )
    assert det.artifact_schema_valid(ctx).passed is True


def test_artifact_schema_valid_detects_invalid_payload():
    """``rule_count`` 必须是 int；字符串会触发校验失败。"""
    ctx = _ctx(new_artifacts=[_artifact(ArtifactType.DERIVED_FACTS, {"rule_count": "not-an-int"})])
    result = det.artifact_schema_valid(ctx)
    assert result.passed is False
    assert result.deviation_class is DeviationClass.D2_STRUCTURE
    assert result.related_artifact == "artifact-1"


def test_artifact_schema_valid_skips_unregistered_types():
    """未登记 typed 契约的类型必须跳过（否则会把正常 artifact 误报 D2）。"""
    ctx = _ctx(new_artifacts=[_artifact(ArtifactType.FACT_COLLECTION if False else ArtifactType.EVALUATION_REPORT, {})])
    assert det.artifact_schema_valid(ctx).passed is True


# ── insight_artifacts_present ───────────────────────────────────────────────


def test_insight_artifacts_present_passes_with_core_view():
    ctx = _ctx(
        node_id="synthesize_insights",
        new_artifacts=[_artifact(ArtifactType.INSIGHT_ANALYSIS, {"core_view": "公司基本面稳健"})],
    )
    assert det.insight_artifacts_present(ctx).passed is True


def test_insight_artifacts_present_detects_missing_and_empty():
    missing = det.insight_artifacts_present(_ctx(node_id="synthesize_insights"))
    assert missing.passed is False
    assert "未产出 insight artifact" in missing.message

    empty = det.insight_artifacts_present(
        _ctx(
            node_id="synthesize_insights", new_artifacts=[_artifact(ArtifactType.INSIGHT_ANALYSIS, {"core_view": "  "})]
        )
    )
    assert empty.passed is False
    assert "core_view 为空" in empty.message


# ── evidence_refs_present ───────────────────────────────────────────────────


def _decision(maker: str, *, based_on=None, evidence_refs=None):
    from alphabee.core.schemas import Decision, EvidenceRef

    return Decision(
        id=f"decision-{maker}",
        maker=maker,
        rationale="r",
        confidence=0.8,
        based_on=list(based_on or []),
        # 注意：``evidence_refs`` 的模型类型是 ``list[EvidenceRef]``，**不接受裸字符串**；
        # 本助手把传入的 id 字符串转成 ``EvidenceRef(ref_id=..., ref_type="decision")``。
        evidence_refs=[
            ref if isinstance(ref, EvidenceRef) else EvidenceRef(ref_id=str(ref), ref_type="decision")
            for ref in (evidence_refs or [])
        ],
    )


def test_evidence_refs_present_passes_when_verdicts_have_evidence():
    """正例：**本节点新增**的 verdict 带 ``based_on`` → 不报（F3 起扫描 ``ctx.new_decisions``）。"""
    ctx = _ctx(
        node_id="run_thesis",
        new_decisions=[_decision("thesis_reviewer", based_on=["artifact-1"])],
    )
    assert det.evidence_refs_present(ctx).passed is True


def test_evidence_refs_present_detects_verdict_without_evidence():
    """反例（F3 明确保留的正例面）：**本节点新增**的无证据 verdict 仍必须报 D3。"""
    ctx = _ctx(node_id="run_thesis", new_decisions=[_decision("thesis_reviewer")])
    result = det.evidence_refs_present(ctx)
    assert result.passed is False
    assert result.deviation_class is DeviationClass.D3_ARGUMENT
    assert "缺少证据引用" in result.message


def test_evidence_refs_present_ignores_non_verdict_decisions():
    """普通中间结论（假设已排除、市场分）不是维度 verdict，不得误报 D3。"""
    ctx = _ctx(
        node_id="run_thesis",
        new_decisions=[_decision("conflict_verifier"), _decision("market_score_engine")],
    )
    assert det.evidence_refs_present(ctx).passed is True


# ── downstream_inputs_present ───────────────────────────────────────────────


def test_downstream_inputs_present_detects_missing_sections():
    """空 state → 报告载荷四段全空 → D1 偏离（要求显式降级分支）。"""
    result = det.downstream_inputs_present(_ctx(node_id="generate_report", view={}))
    assert result.passed is False
    assert result.deviation_class is DeviationClass.D1_DATA
    assert "缺少下游输入段" in result.message


def test_downstream_inputs_present_passes_with_full_payload():
    view = {
        "artifacts": [
            _artifact(ArtifactType.THESIS_ANALYSIS, {"verdict": "bullish", "dimensions": {"growth": "high"}}),
            _artifact(ArtifactType.INSIGHT_ANALYSIS, {"core_view": "稳健"}, artifact_id="artifact-2"),
            _artifact(ArtifactType.ANOMALY_REPORT, {"anomaly_count": 1}, artifact_id="artifact-3"),
            _artifact(ArtifactType.CONFLICTS_RESULT, {"conflicts": [{"theme": "t"}]}, artifact_id="artifact-4"),
        ]
    }
    assert det.downstream_inputs_present(_ctx(node_id="generate_report", view=view)).passed is True


# ── assumption_still_valid（注册表缺失 = 严格 no-op） ──────────────────────


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _referencing_artifact(assumption_id: str, *, artifact_id: str = "artifact-ref") -> Artifact:
    """本节点新增的、**引用了某假设 id** 的载荷（§14.2-B「被本节点引用」的判定输入）。"""
    return _artifact(
        ArtifactType.THESIS_ANALYSIS,
        {"based_on": [assumption_id], "verdict": "ok"},
        artifact_id=artifact_id,
    )


def _registry_artifact(entries: list[AssumptionEntry]) -> Artifact:
    return _artifact(
        ArtifactType.ASSUMPTION_REGISTRY,
        AssumptionRegistryArtifact(entries=entries).model_dump(mode="json"),
    )


def test_assumption_still_valid_noop_without_registry():
    """★ C2：注册表 artifact 不存在时必须判 pass（否则 F1 上线会全链路误报 D4）。"""
    assert det.assumption_still_valid(_ctx(node_id="run_thesis", view={})).passed is True
    assert det.assumption_still_valid(_ctx(node_id="run_thesis", view={"artifacts": []})).passed is True


def test_assumption_still_valid_noop_when_no_invalidated_entry():
    """注册表存在但无 invalidated 假设 → 同样 no-op。"""
    ctx = _ctx(
        node_id="run_thesis",
        view={"artifacts": [_registry_artifact([AssumptionEntry(id="h1", statement="x")])]},
    )
    assert det.assumption_still_valid(ctx).passed is True


def test_assumption_still_valid_triggers_on_invalidated_entry():
    """★ 证明本检测器**是活的**（生产者顺延至 F1c，但本逻辑不依赖生产者到位）。

    F1c 之前的 run 不会有登记簿 artifact（生产者顺延，见 ``detectors`` 模块「期次说明」），
    故这里**手工构造**含 ``status="invalidated"`` 条目的注册表来驱动 D4 分支——
    否则该检测器在 F1 期间会被误判为死代码。
    """
    ctx = _ctx(
        node_id="run_thesis",
        new_artifacts=[_referencing_artifact("h1")],
        view={
            "artifacts": [
                _registry_artifact([AssumptionEntry(id="h1", statement="应收增长源于结算周期", status="invalidated")])
            ]
        },
    )
    result = det.assumption_still_valid(ctx)
    assert result.passed is False
    assert result.deviation_class is DeviationClass.D4_STATE
    assert "已证伪假设" in result.message


def test_assumption_still_valid_noop_when_payload_unparsable():
    """登记簿存在但载荷不可解析 → 判 pass（交给 artifact_schema_valid，不越权）。"""
    ctx = _ctx(node_id="run_thesis", view={"artifacts": [_artifact(ArtifactType.ASSUMPTION_REGISTRY, {"bad": 1})]})
    assert det.assumption_still_valid(ctx).passed is True


def test_assumption_still_valid_noop_when_invalidated_not_referenced_by_node():
    """★ R2-4 反例（R2 审查构造的正是此情形）：注册表含 invalidated，但本节点新增载荷
    **未引用**该假设 id → **不报 D4**（§14.2-B「且被本节点引用」；原实现只判"含 invalidated
    即报"，属 §16 反模式的误报）。"""
    ctx = _ctx(
        node_id="run_thesis",
        new_artifacts=[_referencing_artifact("some-other-id")],
        view={"artifacts": [_registry_artifact([AssumptionEntry(id="h1", statement="x", status="invalidated")])]},
    )
    assert det.assumption_still_valid(ctx).passed is True


def test_assumption_still_valid_ignores_registry_artifact_self_hit():
    """★ 防自命中：本节点**新增的登记簿 artifact 自身**含该 id → 不得判为"引用"。

    若不排除 ``ASSUMPTION_REGISTRY`` 自身载荷，id 包含判定会自命中、把"登记"误判成"引用"。
    """
    registry = _registry_artifact([AssumptionEntry(id="h1", statement="x", status="invalidated")])
    ctx = _ctx(node_id="run_thesis", new_artifacts=[registry], view={"artifacts": [registry]})
    assert det.assumption_still_valid(ctx).passed is True


def test_assumption_still_valid_reports_only_referenced_entries():
    """注册表含两条 invalidated，本节点只引用其中一条 → 只报那一条（不整表连坐）。"""
    ctx = _ctx(
        node_id="run_thesis",
        new_artifacts=[_referencing_artifact("h2")],
        view={
            "artifacts": [
                _registry_artifact(
                    [
                        AssumptionEntry(id="h1", statement="甲", status="invalidated"),
                        AssumptionEntry(id="h2", statement="乙", status="invalidated"),
                    ]
                )
            ]
        },
    )
    result = det.assumption_still_valid(ctx)
    assert result.passed is False
    assert "乙" in result.message
    assert "甲" not in result.message


# ── run_detectors：fail-open、只读、注册表 ─────────────────────────────────


def test_run_detectors_is_read_only():
    """检测器只读：执行前后 ctx 上的 artifacts/decisions 逐字段一致。"""
    payload = {"results": {}}
    artifact = _artifact(ArtifactType.DERIVED_FACTS, payload)
    ctx = _ctx(new_artifacts=[artifact], view={"fact_values": {"roe": 1.0}})
    before_artifacts = [item.model_dump(mode="json") for item in ctx.new_artifacts]
    before_view = {key: value for key, value in ctx.view.items()}

    issues = run_detectors(_contract(["derived_facts_nonempty"]), ctx)

    assert len(issues) == 1  # 引擎故障分支
    assert [item.model_dump(mode="json") for item in ctx.new_artifacts] == before_artifacts
    assert ctx.view == before_view


def test_run_detectors_single_failure_does_not_block_others(monkeypatch: pytest.MonkeyPatch, caplog):
    """★ C3：一个检测器抛异常 → warning + 跳过，同节点另一个检测器照常执行。"""

    def _boom(_ctx: NodeContext):
        raise RuntimeError("detector blew up")

    monkeypatch.setitem(DETECTORS, "derived_facts_nonempty", _boom)
    ctx = _ctx(node_id="synthesize_insights", new_artifacts=[])
    with caplog.at_level(logging.WARNING):
        issues = run_detectors(_contract(["derived_facts_nonempty", "insight_artifacts_present"]), ctx)
    assert len(issues) == 1  # 只有 insight_artifacts_present 报偏离
    assert "insight artifact" in issues[0].message
    assert any("fail-open" in record.message for record in caplog.records)


def test_run_detectors_unknown_name_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        issues = run_detectors(_contract(["not_registered"]), _ctx())
    assert issues == []
    assert any("未在 DETECTORS 注册表" in record.message for record in caplog.records)


def test_issues_carry_classification_and_detection_latency_fields():
    """产出的 Issue 必须带 F0 的分类字段（deviation_class / detected_at_step，§5.1）。"""
    ctx = _ctx(node_id="generate_report", view={})
    issues = run_detectors(_contract(["downstream_inputs_present"]), ctx)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.deviation_class is DeviationClass.D1_DATA
    assert issue.detected_at_step == "generate_report"
    assert issue.related_step == "generate_report"
    assert issue.category == "report_input_missing"


def test_detector_registry_contains_only_deterministic_functions():
    """v1 无 LLM 检测器：注册表里 6 个名字都必须是普通同步函数。"""
    import inspect

    assert set(DETECTORS) == {
        "derived_facts_nonempty",
        "artifact_schema_valid",
        "insight_artifacts_present",
        "evidence_refs_present",
        "downstream_inputs_present",
        "assumption_still_valid",
    }
    for name, func in DETECTORS.items():
        assert not inspect.iscoroutinefunction(func), f"{name} 不应是 async（v1 无 LLM 检测器）"


def test_declared_contract_detectors_are_all_registered():
    """契约声明的检测器名必须在 DETECTORS 中（§14.2-A 断言 4 的本地复证）。"""
    from alphabee.orchestrator.node_contracts import NODE_CONTRACTS

    declared = {name for contract in NODE_CONTRACTS.values() for name in contract.detectors}
    assert declared <= set(DETECTORS)


# ── R2-3：maker 完备性守卫 + 检测器 category 运行时断言 ──────────────────────


def _repo_maker_literals() -> set[str]:
    """AST 扫全仓 ``maker=`` **关键字实参**（含 ``alphabee/`` 下全部模块）。

    **必须用 AST 而非文本 grep**：仓库里存在非 ``maker=`` 位置的同名文本
    （``amplification_audit`` 同时是列表元素与说明字符串），文本扫描会把它们误认为产出点，
    从而使"已归类"断言假绿。
    """
    found: set[str] = set()
    for path in sorted((_REPO_ROOT / "alphabee").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg == "maker" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
    return found


def _unclassified_makers(
    literals: set[str],
    *,
    verdict: frozenset[str] | None = None,
    non_verdict: frozenset[str] | None = None,
) -> set[str]:
    """未归类的 maker 字面量（守卫核心，抽出以便**变异测试**证明其非真空）。

    **单向断言**：只要求 ``literal ⇒ classified``。**不得**反向要求
    ``classified ⇒ literal`` —— 前瞻登记项（``amplification_audit``，F3 才有生产者）
    本就没有字面量，反向断言会让守卫开局即红。
    """
    classified = (det.VERDICT_MAKERS if verdict is None else verdict) | (
        det.NON_VERDICT_MAKERS if non_verdict is None else non_verdict
    )
    return {literal for literal in literals if literal not in classified}


def test_every_repo_maker_literal_is_classified():
    """★ R2-3 承重守卫：全仓每个 ``maker=`` 字面量都必须显式归类为 verdict / 非 verdict。"""
    literals = _repo_maker_literals()
    assert literals, "未能扫到任何 maker= 字面量，扫描口径需与代码同步"
    assert _unclassified_makers(literals) == set(), (
        f"以下 maker 未被归类（新增 verdict 产出者时必须同步 VERDICT_MAKERS / NON_VERDICT_MAKERS）："
        f"{sorted(_unclassified_makers(literals))}"
    )


def test_evidence_refs_present_known_limitation_cross_node_overreport():
    """★ R2-8 关闭（F3 翻转）：扫描面改为 ``ctx.new_decisions`` ⇒ **别的节点**产出的无证据 verdict
    不再在**本节点**被判 D3（跨节点误归属 / 过报已消除）。

    历史：本用例曾**刻意断言过报**以钉住视图级扫描的缺陷（``ctx.view["decisions"]``）；t36 把它
    登记为「F3 门验收项」；F3（本改动）把检测器切到 ``new_decisions``，本用例随之**翻转为断言不报**。

    两个方向都被钉住，缺一不可：
    * 本用例（反例面）：视图里有、**本节点新增里没有** → 不报（不误归属）；
    * ``test_evidence_refs_present_detects_verdict_without_evidence``（正例面）：本节点新增里没有
      证据的 verdict → 仍报 D3（不漏报）。
    """
    other_node_verdict = _decision("thesis_reviewer")  # 无 based_on / evidence_refs
    ctx = _ctx(node_id="synthesize_insights", view={"decisions": [other_node_verdict]})
    result = det.evidence_refs_present(ctx)
    assert result.passed is True, "视图级 Decision 不得再被判成本节点的 D3（F3 已切换扫描面）"


def test_evidence_refs_present_ignores_view_when_new_decisions_empty():
    """★ 变异证据（证明上面的"不报"不是真空：把同一条 Decision 放进 ``new_decisions`` 就立刻报 D3）。"""
    verdict = _decision("thesis_reviewer")  # 无 based_on / evidence_refs
    nothing_new = _ctx(node_id="synthesize_insights", view={"decisions": [verdict]})
    mine = _ctx(node_id="synthesize_insights", new_decisions=[verdict], view={"decisions": [verdict]})
    assert det.evidence_refs_present(nothing_new).passed is True
    assert det.evidence_refs_present(mine).passed is False


def test_maker_guard_is_not_vacuous():
    """★ 变异证据（证明守卫**真能捕获违规**，而非"常量存在但不生效"）。

    ① 把已归类的 ``thesis_reviewer`` 从 VERDICT_MAKERS 移除 → 其现网字面量必须被判未归类；
    ② 引入一个未归类字面量 → 必须被判未归类；
    ③ 未变异对照 → 空集（正例，防守卫恒红）。
    """
    literals = _repo_maker_literals()
    assert "thesis_reviewer" in literals, "现网应存在 thesis_reviewer 字面量，否则本变异无效"

    mutated = det.VERDICT_MAKERS - {"thesis_reviewer"}
    assert _unclassified_makers(literals, verdict=mutated) == {"thesis_reviewer"}
    assert _unclassified_makers(literals | {"brand_new_verdict_maker"}) == {"brand_new_verdict_maker"}
    assert _unclassified_makers(literals) == set()  # 未变异对照


def test_verdict_and_non_verdict_makers_are_disjoint():
    """两侧分类不得重叠（重叠会让"归类"语义含糊、R2 无法客观核）。"""
    assert not (det.VERDICT_MAKERS & det.NON_VERDICT_MAKERS)


def test_detector_categories_are_all_registered():
    """★ R2-3 运行时断言：注册表内**每个**检测器的 category 都必须在 CLASS_BY_CATEGORY 登记。

    与静态字面量扫描互补：运行时不受"category 是否写成字面量"影响，任何注册项都跑不掉。
    """
    from alphabee.orchestrator.services.deviation import CLASS_BY_CATEGORY

    assert set(det._DETECTOR_CATEGORIES) == set(DETECTORS), "每个注册检测器都必须声明 category"
    unregistered = {n: c for n, c in det._DETECTOR_CATEGORIES.items() if c not in CLASS_BY_CATEGORY}
    assert not unregistered, f"检测器 category 未在 CLASS_BY_CATEGORY 登记（会全部回落 D2）：{unregistered}"


# ── 承重断言：注册表 category 必须已登记（captain 设计②；F1 侧承载） ──────────
# 落位理由：本文件是 F1 测试，可以 import detectors；而 F0 测试（锚 fb3bced）**禁止** import F1 模块，
# 否则在 fb3bced 上重跑 F0 认证会失败、破坏双锚点记账。故"运行时"断言只能放在这里。


def test_every_registered_detector_declares_a_category():
    """每个注册进 ``DETECTORS`` 的检测器都必须绑定 category（装饰器第二个位置参数）。"""
    from alphabee.orchestrator.detectors import DETECTOR_CATEGORIES as categories

    assert categories is not None and categories, "name→category 视图为空"
    assert set(categories) == set(DETECTORS), (
        f"注册项与 category 视图不一致；缺 category 的：{sorted(set(DETECTORS) - set(categories))}"
    )


def test_every_registered_detector_category_is_registered_in_classification():
    """★ 任何注册进注册表的 category 都必须登记在 ``CLASS_BY_CATEGORY``。

    不依赖字面量、也不在测试里复制映射（复制等于自证）：数据源是权威视图
    ``detectors.DETECTOR_CATEGORIES``（与装饰器写入的私有名同一对象）。
    未登记 → 该检测器的偏离会回落 D2 保守兜底、系统性污染 per-class 画像（F0 finding A 同类）。
    """
    from alphabee.orchestrator.detectors import DETECTOR_CATEGORIES as categories
    from alphabee.orchestrator.services.deviation import CLASS_BY_CATEGORY

    unmapped = {name: category for name, category in categories.items() if category not in CLASS_BY_CATEGORY}
    assert not unmapped, f"以下检测器的 category 未登记（会回落 D2）：{unmapped}"


def test_registered_categories_are_visible_to_source_scan():
    """注册表视图与源码 AST 扫描必须互相印证（防"声明了但没注册"或扫描面脱节）。

    **本用例自建 AST 扫描**（**不** import F0 测试模块 —— 那会引入跨期耦合、破坏 F0 自包含性），
    并与注册表公开视图 ``DETECTOR_CATEGORIES`` 断言**集合相等**（而非仅交集非空）。
    """
    import ast
    from pathlib import Path

    import alphabee.orchestrator.detectors as det_module
    from alphabee.orchestrator.detectors import DETECTOR_CATEGORIES as categories

    source = Path(det_module.__file__).read_text(encoding="utf-8")
    scanned: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or len(decorator.args) < 2:
                    continue
                func = decorator.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "detector" and isinstance(decorator.args[1], ast.Constant):
                    if isinstance(decorator.args[1].value, str):
                        scanned.add(decorator.args[1].value)

    assert scanned == set(categories.values()), (
        f"源码里的 @detector 声明 {sorted(scanned)} != 注册表视图 {sorted(set(categories.values()))}"
    )


def test_f0_test_file_stays_self_contained():
    """★ 防回归：F0 测试文件（锚 fb3bced）**不得** import 任何 F1 模块。

    F0 认证需在 ``fb3bced`` 上复现——那时的代码树里没有 ``orchestrator.detectors``；
    若 F0 测试文件引入该 import，F0 认证将无法复现、双锚点记账失效。
    """
    import ast
    from pathlib import Path

    f0_test = Path(__file__).with_name("test_deviation_service.py")
    tree = ast.parse(f0_test.read_text(encoding="utf-8"))
    f1_modules = {"alphabee.orchestrator.detectors", "alphabee.orchestrator.services.detection"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in f1_modules:
            offenders.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"line {node.lineno}: import {alias.name}" for alias in node.names if alias.name in f1_modules
            )
    assert not offenders, f"F0 测试文件引入了 F1 模块依赖（破坏 F0 自包含）：{offenders}"


# ── F1 结转：R2-4 第 ② 类载荷（Decision）+ 词边界匹配加固 ────────────────────


def _ctx_with_decisions(decisions, *, new_artifacts=None, view=None) -> det.NodeContext:
    """直接构造 NodeContext（含第 ② 类载荷 new_decisions）。"""
    return det.NodeContext(
        node_id="run_thesis",
        step=None,
        new_artifacts=list(new_artifacts or []),
        new_decisions=list(decisions or []),
        view=dict(view or {}),
    )


def _invalidated_registry(assumption_id: str = "h1") -> list:
    return [_registry_artifact([AssumptionEntry(id=assumption_id, statement="甲", status="invalidated")])]


def test_assumption_still_valid_detects_decision_reference():
    """★ R2-4 第 ② 类载荷：本节点新增 ``Decision`` 的 ``based_on`` 引用 invalidated 假设 → D4。"""
    ctx = _ctx_with_decisions(
        [_decision("thesis_reviewer", based_on=["h1"])],
        view={"artifacts": _invalidated_registry("h1")},
    )
    result = det.assumption_still_valid(ctx)
    assert result.passed is False
    assert result.deviation_class == DeviationClass.D4_STATE


def test_assumption_still_valid_detects_decision_evidence_refs_reference():
    """★ 同上，走 ``evidence_refs`` 字段（引用 id 在 ``EvidenceRef.ref_id``，第 ② 类并行入口）。

    该字段是**结构化对象**而非裸字符串，故两条构造路径都要钉住：① 结构化对象原样透传；
    ② 测试助手 ``_decision(evidence_refs=["h1"])`` 的字符串→``EvidenceRef`` 转换分支
    （真实生产者也可能先拿到 id 字符串）。二者都必须能触发 D4。
    """
    from alphabee.core.schemas import Decision, EvidenceRef

    structured = Decision(
        id="decision-evidence-1",
        maker="thesis_reviewer",
        rationale="r",
        confidence=0.8,
        evidence_refs=[EvidenceRef(ref_id="h1", ref_type="artifact")],
    )
    via_helper = _decision("thesis_reviewer", evidence_refs=["h1"])

    for decision in (structured, via_helper):
        ctx = _ctx_with_decisions([decision], view={"artifacts": _invalidated_registry("h1")})
        assert det.assumption_still_valid(ctx).passed is False, f"{type(decision).__name__} 路径未触发"


def test_assumption_still_valid_noop_when_decision_references_other_id():
    """② 类载荷反例：``Decision`` 引用**别的** id → 不报（节点未依赖该已证伪假设）。"""
    ctx = _ctx_with_decisions(
        [_decision("thesis_reviewer", based_on=["h9"])],
        view={"artifacts": _invalidated_registry("h1")},
    )
    assert det.assumption_still_valid(ctx).passed is True


def test_assumption_still_valid_noop_when_no_decision_payload():
    """③ 无 Decision 载荷、也无 artifact 引用 → 不报。"""
    ctx = _ctx_with_decisions([], view={"artifacts": _invalidated_registry("h1")})
    assert det.assumption_still_valid(ctx).passed is True


def test_referencing_match_is_word_bounded():
    """★ 词边界加固：假设 id 为 ``h1``、载荷只出现 ``h10`` → **不得**判为引用（子串碰撞消除）。

    生产 id 为**LLM 自由字符串**（`HypothesisItem.id`，常见 ``h1``/``h10`` 形态），此加固使
    判定与 id 形态无关。完整边界矩阵见 ``test_id_occurs_boundary_matrix``。
    """
    ctx = _ctx_with_decisions(
        [_decision("thesis_reviewer", based_on=["h10"])],
        new_artifacts=[_referencing_artifact("h10")],
        view={"artifacts": _invalidated_registry("h1")},
    )
    assert det.assumption_still_valid(ctx).passed is True


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        # ── 必须捕获：真实引用形态（漏报方向更危险 —— 已证伪假设会静默通过）──
        ("h1", True),
        ('"h1"', True),
        ('{"based_on": ["h1"]}', True),
        ("基于h1推断", True),
        ("x_h1", True),  # ← T31-6：复合键 / 下划线分隔，初版边界类含 "_" 时会漏报
        ("h1_x", True),  # ← 同上
        ("h1_notes", True),  # ← 同上
        ("h1_a", True),  # ← T35-2：残余过报面（id `h1` 命中 `h1_a`）显式钉住，见下 docstring
        # ── 必须排除：字母数字相邻的子串碰撞（过报方向）──
        ("h10", False),
        ("abch1", False),
        ("h1x", False),
    ],
)
def test_id_occurs_boundary_matrix(blob: str, expected: bool):
    """★ T31-6 边界矩阵 + T35-2 残余项：``_id_occurs("h1", blob)`` 在两个方向上都被钉住。

    矩阵由 t31 的 reviewer 压测设计、captain 落地。**注意**：本矩阵刻意断言 ``h1_notes`` 与
    ``h1_a`` 为命中——这是把 ``_`` 移出边界类后的**有意取舍**（捕获面不小于改造前）。其残余
    （id 对 ``h1`` vs ``h1_a`` 在下划线相连时分不开）已在 ``_id_occurs`` docstring 披露：
    它是"序列化文本包含判定"的固有极限，F3（t60）已把 ``evidence_refs_present`` 切到结构化
    扫描面（``ctx.new_decisions``），而本函数仍服务 ``assumption_still_valid`` 的文本面。
    """
    assert det._id_occurs(blob, "h1") is expected, f"blob={blob!r} 期望 {expected}"
