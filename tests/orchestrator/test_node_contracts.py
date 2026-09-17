"""F1a 契约测试：``NODE_CONTRACTS`` 全登记 + ``validate_contracts`` 断言（§14.2-A / §14.7）。

覆盖（对照代码级验收 §15.7）：

* 契约键集 == ``NODE_ORDER`` 且与 ``agent.py`` 图 builder 实装一致（读源码 AST，
  **不调** ``get_graph()``：编译图会剪掉源节点暂不可达的边，例如
  ``review_report → record_deviations``——用编译图断言会漏掉这条边）；
* §7.1 恢复阶梯 / §8.1 放大标注逐节点对照文档值（期望值在测试里独立书写，防止"实现即期望"）；
* ``max_retries>0`` 只允许 ``collect_raw_facts`` / ``generate_report``，且与
  ``OrchestratorState`` 的 ``max_*_rounds`` **逐字段一致**（真值来自 state 模块，不是常量自证）；
* 每条 ``WEIGHTED`` 边在审计覆盖表中有独立 review 审计节点（≠ 消费方）；
* ``detectors`` 名与 ``detectors.py::DETECTORS`` 注册表一致（F1b 落地后强制）；
* ``validate_contracts()`` 空违规，且各类违规都能被单独注入捕获（负例）。

不 import ``agent``（避免 import 期 ``StateGraph`` 构图与 tushare token 副作用），
但会 import ``OrchestratorState`` 取计数器真值（与 agent 无关，仅 langgraph/langchain 依赖）。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from alphabee.orchestrator import node_contracts as nc
from alphabee.orchestrator.node_contracts import (
    AMPLIFICATION_AUDIT,
    NODE_BUDGETS,
    NODE_CONTRACTS,
    RECOVERY_TIERS,
    AmplificationAuditBinding,
    AmplificationMode,
    NodeBudget,
    NodeContract,
    get_contract,
    validate_contracts,
)
from alphabee.orchestrator.services.deviation import NODE_ORDER
from alphabee.orchestrator.state import OrchestratorState

_AGENT_PATH = Path(nc.__file__).with_name("agent.py")

# §7.1「各节点阶梯配置」+ 回环预算列（含 F0b 后追加的 run 尾部账本 sink）。
_EXPECTED_LADDERS: dict[str, tuple[int, ...]] = {
    "collect_raw_facts": (0, 4, 2),
    "resolve_industry_context": (0, 2, 3),
    "resolve_company_track": (0, 2, 3),
    "resolve_driver_profile": (0, 2, 3),
    "run_analysis_engines": (0, 2, 3),
    "explore_conflicts": (0, 2, 3),
    "verify_hypotheses": (0, 1, 2),
    "synthesize_insights": (0, 1, 2, 3),
    "run_thesis": (0, 1, 2),
    "review_thesis": (0, 1, 2),
    "resolve_midterm_decision": (0, 5),  # §7.1 宏观环行动建议：只允许 0 或 5
    "midterm_decision_reporter": (0, 5),
    "generate_report": (0, 2, 4),
    "review_report": (0, 2),
    "record_deviations": (0,),  # F0b：账本 sink 失败只记 Step，不降级产物
    "finalize_message": (0,),
}

# §7.1 回环预算列 + §14.2-A 断言 2：节点 → (计数器, 硬上限字段)
_EXPECTED_RETRY_BUDGETS: dict[str, tuple[str, str]] = {
    "collect_raw_facts": ("supplement_round", "max_supplement_rounds"),
    "generate_report": ("report_review_round", "max_report_review_rounds"),
}

# §8.1 表：每条高风险 WEIGHTED 边的权重要点/审计节点/审计动作（期望值独立书写）
_EXPECTED_WEIGHTED_EDGES: tuple[str, ...] = (
    "insight->thesis",
    "anomaly->fact_values->signal",
    "verified_conflict->dimension_score",
    "insight->report",
)
_EXPECTED_AMPLIFICATION_LABELS: dict[str, dict[str, AmplificationMode]] = {
    # 加权消费方（α>1 的乘法发生地）
    "run_analysis_engines": {"anomaly->fact_values->signal": AmplificationMode.WEIGHTED},
    "run_thesis": {
        "insight->thesis": AmplificationMode.WEIGHTED,
        "verified_conflict->dimension_score": AmplificationMode.WEIGHTED,
    },
    "generate_report": {
        "insight->report": AmplificationMode.WEIGHTED,
        "thesis->report": AmplificationMode.VERBATIM,
        "conflict->report": AmplificationMode.VERBATIM,
    },
    # 裁决类（可推翻上游）
    "review_thesis": {"insight->thesis": AmplificationMode.OVERRIDE},
    "review_report": {
        "report->final_report": AmplificationMode.OVERRIDE,
        "report_gate_verdict->final_report": AmplificationMode.OVERRIDE,
    },
}

# §8.1「审计要求」列规定的审计节点（必须是 review 节点，不能是消费方自审）
_EXPECTED_AUDITORS: dict[str, str] = {
    "insight->thesis": "review_thesis",
    "anomaly->fact_values->signal": "review_report",
    "verified_conflict->dimension_score": "review_thesis",
    "insight->report": "review_report",
}

# §6.2「首批检测器」表：检测器 → 节点
_EXPECTED_DETECTORS: dict[str, tuple[str, ...]] = {
    "run_analysis_engines": ("derived_facts_nonempty", "artifact_schema_valid"),
    "resolve_industry_context": ("artifact_schema_valid",),
    "resolve_company_track": ("artifact_schema_valid",),
    "resolve_driver_profile": ("artifact_schema_valid",),
    "explore_conflicts": ("artifact_schema_valid",),
    "verify_hypotheses": ("artifact_schema_valid",),
    "synthesize_insights": ("insight_artifacts_present", "artifact_schema_valid", "assumption_still_valid"),
    "run_thesis": ("evidence_refs_present", "artifact_schema_valid", "assumption_still_valid"),
    "review_thesis": ("evidence_refs_present", "artifact_schema_valid"),
    "resolve_midterm_decision": ("artifact_schema_valid",),
    "generate_report": ("downstream_inputs_present", "artifact_schema_valid"),
    "review_report": ("artifact_schema_valid",),
}


# ── 测试辅助（不复用 F0 测试模块，避免测试间耦合） ───────────────────────────


def _graph_nodes_from_source() -> list[str]:
    """从 agent.py 源码按书写顺序取 ``_graph.add_node("name", ...)`` 的节点名。"""
    tree = ast.parse(_AGENT_PATH.read_text(encoding="utf-8"))
    names: list[str] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_node":
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "_graph"):
            continue
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            names.append(call.args[0].value)
    return names


def _graph_edges_from_source() -> list[tuple[str, str]]:
    """从 agent.py 源码取**生效的**边（含 conditional_edges 映射；跳过注释掉的边）。"""
    tree = ast.parse(_AGENT_PATH.read_text(encoding="utf-8"))
    edges: list[tuple[str, str]] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        func = call.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "_graph"):
            continue
        if func.attr == "add_edge" and len(call.args) >= 2:
            first, second = call.args[0], call.args[1]
            if isinstance(first, ast.Constant) and isinstance(second, ast.Constant):
                if isinstance(first.value, str) and isinstance(second.value, str):
                    edges.append((first.value, second.value))
            continue
        if func.attr != "add_conditional_edges" or len(call.args) < 3:
            continue
        source, mapping = call.args[0], call.args[2]
        if not (isinstance(source, ast.Constant) and isinstance(source.value, str)):
            continue
        if not isinstance(mapping, ast.Dict):
            continue
        for target in mapping.values:
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                edges.append((source.value, target.value))
    return edges


def _with_contract(contract: NodeContract, **updates: object) -> dict[str, NodeContract]:
    """复制真实契约表并替换一个节点契约（保留其余节点合法，精确定位单条违规）。"""
    custom = dict(NODE_CONTRACTS)
    custom[contract.node_id] = contract.model_copy(update=updates)
    return custom


def _replace(
    audit: dict[str, AmplificationAuditBinding], edge: str, **updates: object
) -> dict[str, AmplificationAuditBinding]:
    custom = dict(audit)
    custom[edge] = custom[edge].__class__(**{**vars(custom[edge]), **updates})
    return custom


# ── 契约登记表（§14.2-A） ───────────────────────────────────────────────────


def test_node_contracts_cover_every_pipeline_node():
    """契约键集 == NODE_ORDER（16 节点，F0b 后含 ``record_deviations``）。"""
    assert set(NODE_CONTRACTS) == set(NODE_ORDER)
    assert len(NODE_CONTRACTS) == len(NODE_ORDER) == 16
    assert len(NODE_CONTRACTS) != 15, "文档 §14.2-A 的 15 节点是 F0b 之前的口径"
    for node_id, contract in NODE_CONTRACTS.items():
        assert contract.node_id == node_id


def test_contracts_match_graph_builder_registration():
    """契约与图 builder 实装一致（读源码 AST，不经 get_graph()）。"""
    registered = _graph_nodes_from_source()
    assert registered, f"未能从 {_AGENT_PATH} 解析出节点注册（注册方式变更需同步本测试）"
    assert set(registered) == set(NODE_CONTRACTS)
    for source, target in _graph_edges_from_source():
        assert source in NODE_CONTRACTS, f"边 {source} -> {target} 的源节点未登记契约"
        assert target in NODE_CONTRACTS, f"边 {source} -> {target} 的目标节点未登记契约"


def test_graph_edge_reader_sees_edges_pruned_by_compiled_graph():
    """契约校验必须用 builder 的边：编译图会剪掉源不可达的边（本条证明该选择有实效）。"""
    builder_edges = set(_graph_edges_from_source())
    # §14.1-D 记在案的坑：review_report 当前不可达，它到 sink 的边只存在于 builder 声明里
    assert ("review_report", "record_deviations") in builder_edges


def test_recovery_ladders_match_documented_table():
    """§7.1：逐节点阶梯值对照文档（含「行动类只允许 0 或 5」）。"""
    for node_id, expected in _EXPECTED_LADDERS.items():
        assert NODE_CONTRACTS[node_id].recovery_ladder == expected, node_id
    assert set(_EXPECTED_LADDERS) == set(NODE_CONTRACTS)
    for ladder in _EXPECTED_LADDERS.values():
        assert set(ladder) <= set(RECOVERY_TIERS)
    # 行动类节点不得声明降级/修复档
    for node_id in ("resolve_midterm_decision", "midterm_decision_reporter"):
        assert set(NODE_CONTRACTS[node_id].recovery_ladder) <= {0, 5}


def test_amplification_labels_match_documented_edges():
    """§8.1：有放大标注的节点必须逐条对齐文档，未标注节点保持空（不做隐性加权）。"""
    for node_id, contract in NODE_CONTRACTS.items():
        assert contract.amplification_labels == _EXPECTED_AMPLIFICATION_LABELS.get(node_id, {}), node_id


def test_detectors_match_documented_first_batch():
    """§6.2：六个首批检测器的挂载节点与文档一致。"""
    declared = {
        node_id: tuple(contract.detectors) for node_id, contract in NODE_CONTRACTS.items() if contract.detectors
    }
    assert declared == _EXPECTED_DETECTORS
    assert sorted({name for names in declared.values() for name in names}) == [
        "artifact_schema_valid",
        "assumption_still_valid",
        "derived_facts_nonempty",
        "downstream_inputs_present",
        "evidence_refs_present",
        "insight_artifacts_present",
    ]


def test_contracts_declare_pre_and_postconditions():
    """契约必须真的声明前后置条件（否则契约只是空壳，§6.1）。"""
    for node_id, contract in NODE_CONTRACTS.items():
        assert contract.preconditions, f"{node_id} 缺前置条件"
        assert contract.postconditions, f"{node_id} 缺后置条件"
        assert all(isinstance(item, str) and item.strip() for item in contract.preconditions + contract.postconditions)


def test_amplification_mode_values():
    """§14.2-A：三态值域固定为 verbatim/weighted/override。"""
    assert {mode.value for mode in AmplificationMode} == {"verbatim", "weighted", "override"}
    assert AmplificationMode("weighted") is AmplificationMode.WEIGHTED
    # StrEnum：可直接与字符串比较（序列化/契约比较口径）
    assert NODE_CONTRACTS["run_thesis"].amplification_labels["insight->thesis"] == "weighted"


def test_get_contract_lookup_and_missing():
    assert get_contract("collect_raw_facts") is NODE_CONTRACTS["collect_raw_facts"]
    assert get_contract("no_such_node") is None
    assert get_contract(None) is None
    assert get_contract("") is None
    assert get_contract("__start__") is None  # 图哨兵不是节点


# ── CI 断言：空违规 ─────────────────────────────────────────────────────────


def test_validate_contracts_has_no_violation():
    """§15.7：契约自检必须空违规（负例见下方各专项测试）。"""
    assert validate_contracts() == []


def test_detector_names_are_registered_in_detectors_registry():
    """§14.2-A 断言 4：``detectors`` 名与 ``detectors.py::DETECTORS`` 注册表一致。

    该断言需要 F1b 的 ``detectors.py``：本 PR（F1a）落地前无检测器实现可核对，
    此时由 ``validate_contracts()`` 的 "DETECTORS 注册表为空" 违规兜底（见下一条测试）；
    F1b 一旦创建该模块，本测试自动生效——名字拼错、漏注册都会失败。
    """
    if importlib.util.find_spec(nc._DETECTORS_MODULE) is None:
        pytest.skip(f"{nc._DETECTORS_MODULE} 尚未落地（F1b 生效该断言）")
    from alphabee.orchestrator.detectors import DETECTORS

    declared = {name for contract in NODE_CONTRACTS.values() for name in contract.detectors}
    assert declared, "契约必须声明检测器（§6.2）"
    assert declared <= set(DETECTORS)


def test_validate_contracts_uses_injected_detector_registry():
    """注册表可用时，契约里的检测器名必须逐个命中（真实注册表路径，§14.2-A 断言 4）。"""
    stub = ModuleType(nc._DETECTORS_MODULE)
    declared = {name for contract in NODE_CONTRACTS.values() for name in contract.detectors}
    stub.DETECTORS = {name: object() for name in declared}  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {nc._DETECTORS_MODULE: stub}):
        nc._detector_registry.cache_clear()
        try:
            assert validate_contracts() == []
        finally:
            nc._detector_registry.cache_clear()


def test_validate_contracts_catches_unregistered_detector_name():
    custom = _with_contract(
        NODE_CONTRACTS["generate_report"], detectors=["downstream_inputs_present", "not_a_detector"]
    )
    violations = validate_contracts(
        contracts=custom,
        detectors={"downstream_inputs_present": object(), "artifact_schema_valid": object()},
    )
    assert any("not_a_detector" in violation for violation in violations)


def test_validate_contracts_reports_unusable_detector_registry(monkeypatch: pytest.MonkeyPatch):
    """F1b 落地后 ``detectors.py`` 存在但注册表为空 → 必须报红（不是静默通过）。

    用 monkeypatch 模拟「模块已存在、注册装饰器没生效」这一真实故障态，
    避免测试结果依赖 F1a/F1b 的落地顺序。
    """
    monkeypatch.setattr(nc, "_detector_registry", lambda: {})
    monkeypatch.setattr(nc, "_detector_module_available", lambda: True)
    violations = validate_contracts()
    assert len(violations) == 1
    assert "DETECTORS 注册表为空" in violations[0]


def test_validate_contracts_detector_check_is_not_vacuous():
    """★ 反向证明断言 4 真的生效（非真空通过）：摘掉一个真实检测器名 → 必须报红。

    F1b 落地前该断言会因 ``detectors.py`` 缺失而整体延后；落地后这条用例证明
    "空违规"来自"确实无违规"，而不是"根本没检查"。
    """
    from alphabee.orchestrator.detectors import DETECTORS

    declared = {name for contract in NODE_CONTRACTS.values() for name in contract.detectors}
    assert "derived_facts_nonempty" in declared and "derived_facts_nonempty" in DETECTORS
    mutated = {name: object() for name in declared - {"derived_facts_nonempty"}}

    violations = validate_contracts(detectors=mutated)

    assert any("derived_facts_nonempty" in violation for violation in violations)


# ── F1b：假设登记簿与报告 gate 的严格 no-op（C2） ───────────────────────────


def test_assumption_registry_artifact_type_is_registered():
    """§14.2-D：``ArtifactType.ASSUMPTION_REGISTRY`` 必须登记（含 role_group 自动推断）。"""
    from alphabee.core.schemas import Artifact, ArtifactType

    assert ArtifactType.ASSUMPTION_REGISTRY == "assumption_registry"
    artifact = Artifact(
        id="artifact-x",
        type=ArtifactType.ASSUMPTION_REGISTRY,
        producer_step="verify_hypotheses",
        value={"entries": []},
    )
    assert artifact.role_group is not None


def test_detector_check_is_deferred_before_f1b(monkeypatch: pytest.MonkeyPatch):
    """F1a 阶段（``detectors.py`` 尚未落地）检测器名核对整体延后，不产生违规。"""
    monkeypatch.setattr(nc, "_detector_registry", lambda: {})
    monkeypatch.setattr(nc, "_detector_module_available", lambda: False)
    assert validate_contracts() == []


# ── CI 断言：节点登记 ───────────────────────────────────────────────────────


def test_validate_contracts_catches_unregistered_node():
    custom = {node_id: contract for node_id, contract in NODE_CONTRACTS.items() if node_id != "run_thesis"}
    violations = validate_contracts(contracts=custom)
    assert any("节点未登记契约：run_thesis" in violation for violation in violations)


def test_validate_contracts_catches_node_outside_node_order():
    alien = NodeContract(
        node_id="brand_new_node",
        preconditions=["-"],
        postconditions=["-"],
        detectors=[],
        recovery_ladder=(0,),
    )
    violations = validate_contracts(contracts={**NODE_CONTRACTS, "brand_new_node": alien})
    assert any("NODE_ORDER 之外的节点：brand_new_node" in violation for violation in violations)


def test_validate_contracts_catches_contract_key_mismatch():
    custom = dict(NODE_CONTRACTS)
    custom["run_thesis"] = NODE_CONTRACTS["run_thesis"].model_copy(update={"node_id": "typo_node"})
    violations = validate_contracts(contracts=custom)
    assert any("契约键与 node_id 不一致" in violation for violation in violations)


# ── CI 断言：恢复阶梯 ───────────────────────────────────────────────────────


def test_validate_contracts_catches_illegal_recovery_tier():
    custom = _with_contract(NODE_CONTRACTS["run_thesis"], recovery_ladder=(0, 7))
    violations = validate_contracts(contracts=custom)
    assert any("非法 Tier 7" in violation for violation in violations)


def test_validate_contracts_catches_duplicate_recovery_tier():
    custom = _with_contract(NODE_CONTRACTS["run_thesis"], recovery_ladder=(0, 1, 1))
    violations = validate_contracts(contracts=custom)
    assert any("重复 Tier" in violation for violation in violations)


def test_validate_contracts_catches_empty_recovery_ladder():
    custom = _with_contract(NODE_CONTRACTS["run_thesis"], recovery_ladder=())
    violations = validate_contracts(contracts=custom)
    assert any("recovery_ladder 为空：run_thesis" in violation for violation in violations)


def test_validate_contracts_catches_retry_without_tier_4():
    """§7.2 规则 4：声明回环预算就必须声明 Tier 4（受控回环）。"""
    custom = _with_contract(NODE_CONTRACTS["generate_report"], recovery_ladder=(0, 2))
    violations = validate_contracts(contracts=custom)
    assert any("阶梯未声明 Tier 4" in violation for violation in violations)


# ── CI 断言：回环预算 ↔ state 计数器（§14.2-A 断言 2） ───────────────────────


def test_only_declared_nodes_may_have_retries():
    retryable = {node_id for node_id, contract in NODE_CONTRACTS.items() if contract.max_retries > 0}
    assert retryable == set(_EXPECTED_RETRY_BUDGETS)
    assert retryable == {"collect_raw_facts", "generate_report"}
    assert NODE_CONTRACTS["collect_raw_facts"].max_retries == 1
    assert NODE_CONTRACTS["generate_report"].max_retries == 2


def test_max_retries_matches_state_counter_budget():
    """``max_retries`` 必须等于 ``OrchestratorState`` 的 ``max_*_rounds``（真值取自 state 模块）。"""
    assert set(NODE_BUDGETS) == set(_EXPECTED_RETRY_BUDGETS)
    for node_id, (counter, max_counter) in _EXPECTED_RETRY_BUDGETS.items():
        binding = NODE_BUDGETS[node_id]
        assert binding.counter == counter
        assert binding.max_counter == max_counter
        # 字段必须真实存在于 OrchestratorState
        assert counter in OrchestratorState.__annotations__
        assert max_counter in OrchestratorState.__annotations__
        # 契约上限 == state 硬上限 == 本测试独立声明的期望值
        expected_max = state_defaults()[max_counter]
        assert binding.max_value == expected_max
        assert NODE_CONTRACTS[node_id].max_retries == expected_max


def state_defaults() -> dict[str, int]:
    """从 ``collectors.py``（state 唯一初始化点）读回环预算硬上限真值。"""
    collectors_path = Path(nc.__file__).with_name("collectors.py")
    tree = ast.parse(collectors_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        pairs = {
            key.value: value.value
            for key, value in zip(node.value.keys, node.value.values, strict=False)
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, int)
        }
        if "max_supplement_rounds" in pairs:
            return {key: value for key, value in pairs.items() if key.startswith("max_")}
    raise AssertionError(f"未能从 {collectors_path} 解析出 state 计数器默认值")


def test_validate_contracts_catches_retry_without_state_counter():
    custom = _with_contract(NODE_CONTRACTS["generate_report"], max_retries=1)
    violations = validate_contracts(contracts=custom, budgets={})
    assert any("state 无对应计数器：generate_report" in violation for violation in violations)


def test_validate_contracts_catches_retry_over_state_limit():
    custom = _with_contract(NODE_CONTRACTS["generate_report"], max_retries=5)
    violations = validate_contracts(contracts=custom)
    assert any("超过 state 硬上限" in violation for violation in violations)
    assert any("max_report_review_rounds=2" in violation for violation in violations)


def test_validate_contracts_catches_state_budget_without_contract_declaration():
    """state 有预算但契约未声明 = 未登记的回环（§7.2 规则 4 禁止）。"""
    custom = _with_contract(NODE_CONTRACTS["generate_report"], max_retries=0)
    violations = validate_contracts(contracts=custom)
    assert any("契约未声明 max_retries：generate_report" in violation for violation in violations)


@pytest.mark.parametrize("mutated_budget", [1, 3, 5])
def test_every_retry_budget_mutation_is_detected(mutated_budget: int):
    """★ 变异测试（R2 会独立复跑）：把 state 侧预算改成任何不等于契约的值 → 必须报红。

    双向核对是刻意的：向上变异（3/5）也要红，否则"state 上限被调大而契约不同步"会静默通过，
    实际回环次数就会超过契约声明的授权范围（§7.2 规则 4）。
    """
    mutated = dict(NODE_BUDGETS)
    mutated["generate_report"] = NodeBudget(
        node_id="generate_report",
        counter="report_review_round",
        max_counter="max_report_review_rounds",
        max_value=mutated_budget,
    )
    violations = validate_contracts(budgets=mutated)
    assert violations, f"预算变异为 {mutated_budget} 时未报红"
    assert any("generate_report" in violation for violation in violations)


def test_unmutated_budget_binding_is_accepted():
    """对照：未变异的真实预算绑定必须是空违规（证明上面的变异确实来自值不符）。"""
    assert validate_contracts(budgets=dict(NODE_BUDGETS)) == []


def test_validate_contracts_accepts_consistent_injected_budget():
    """同一份违规表在预算与检测器注册表都合法注入时不应报错（证明违规来自不一致本身）。"""
    budget = {node: NodeBudget(**vars(binding)) for node, binding in NODE_BUDGETS.items()}
    declared = {name for contract in NODE_CONTRACTS.values() for name in contract.detectors}
    assert validate_contracts(budgets=budget, detectors={name: object() for name in declared}) == []


# ── CI 断言：WEIGHTED 边审计覆盖（§8.1 / §8.2 规则 2） ───────────────────────


def test_every_weighted_edge_has_audit_binding():
    weighted = {
        edge
        for contract in NODE_CONTRACTS.values()
        for edge, mode in contract.amplification_labels.items()
        if mode is AmplificationMode.WEIGHTED
    }
    assert weighted == set(_EXPECTED_WEIGHTED_EDGES)
    for edge in weighted:
        binding = AMPLIFICATION_AUDIT[edge]
        assert binding.edge == edge
        assert binding.auditor == _EXPECTED_AUDITORS[edge]
        assert binding.check and binding.weight and binding.requirement


def test_weighted_auditors_are_registered_review_nodes():
    """审计必须落在已登记契约、且**不是该 WEIGHTED 边消费方**的 review 节点上（§8.2 规则 2）。"""
    for edge, binding in AMPLIFICATION_AUDIT.items():
        assert binding.auditor in NODE_CONTRACTS, edge
        weighted_consumers = {
            node_id
            for node_id, contract in NODE_CONTRACTS.items()
            if contract.amplification_labels.get(edge) is AmplificationMode.WEIGHTED
        }
        assert weighted_consumers, f"{edge} 没有 WEIGHTED 消费方"
        assert binding.auditor not in weighted_consumers, f"{edge} 的审计节点不能是消费方自审"
        assert binding.auditor.startswith("review"), edge


def test_validate_contracts_catches_auditor_same_as_consumer():
    """消费方自审不算独立审计：违反应当被捕获（覆盖同源分支）。"""
    violations = validate_contracts(audit=_replace(AMPLIFICATION_AUDIT, "insight->thesis", auditor="run_thesis"))
    assert any("审计节点与消费方同源：run_thesis" in violation for violation in violations)


def test_validate_contracts_catches_weighted_edge_without_audit():
    audit = {edge: binding for edge, binding in AMPLIFICATION_AUDIT.items() if edge != "insight->thesis"}
    violations = validate_contracts(audit=audit)
    assert any("WEIGHTED 边无审计覆盖：insight->thesis" in violation for violation in violations)


def test_validate_contracts_catches_unknown_auditor_node():
    violations = validate_contracts(audit=_replace(AMPLIFICATION_AUDIT, "insight->thesis", auditor="ghost_reviewer"))
    assert any("审计节点未登记契约：ghost_reviewer" in violation for violation in violations)


def test_validate_contracts_catches_edge_declared_outside_document():
    """契约声明了 §8.1 之外的 WEIGHTED 边 → 必须补文档登记（§8.2 规则 3）。"""
    custom = _with_contract(
        NODE_CONTRACTS["run_thesis"],
        amplification_labels={
            "insight->thesis": AmplificationMode.WEIGHTED,
            "signal->thesis": AmplificationMode.WEIGHTED,
        },
    )
    violations = validate_contracts(contracts=custom)
    assert any("§8.1 未列出的 WEIGHTED 边" in violation for violation in violations)
    assert any("signal->thesis" in violation for violation in violations)


def test_validate_contracts_catches_audit_entry_outside_document():
    """审计表登记了 §8.1 未列出的边（即使契约里有人消费）同样报红。"""
    binding = AmplificationAuditBinding(
        edge="signal->thesis",
        auditor="review_thesis",
        check="weighted_direction_consistency",
        weight="示例系数 0.9",
        requirement="示例",
    )
    custom = _with_contract(
        NODE_CONTRACTS["run_thesis"],
        amplification_labels={
            "insight->thesis": AmplificationMode.WEIGHTED,
            "signal->thesis": AmplificationMode.WEIGHTED,
        },
    )
    violations = validate_contracts(contracts=custom, audit={**AMPLIFICATION_AUDIT, "signal->thesis": binding})
    assert any("审计表登记了 §8.1 未列出的边：signal->thesis" in violation for violation in violations)


def test_validate_contracts_catches_audit_binding_without_contract_declaration():
    """审计表有边但契约没声明（反向漂移，没有 WEIGHTED 消费方）。"""
    extra = AmplificationAuditBinding(
        edge="ghost->edge",
        auditor="review_report",
        check="core_view_direction_consistency",
        weight="n/a",
        requirement="n/a",
    )
    violations = validate_contracts(audit={**AMPLIFICATION_AUDIT, "ghost->edge": extra})
    assert any("没有 WEIGHTED 消费者" in violation for violation in violations)


def test_validate_contracts_catches_documented_edge_missing_from_contracts():
    """§8.1 列出的边必须在契约里有声明（防止只改测试不改契约）。"""
    custom = _with_contract(NODE_CONTRACTS["run_thesis"], amplification_labels={})
    violations = validate_contracts(contracts=custom)
    assert any("§8.1 登记的高风险 WEIGHTED 边未在契约中声明：insight->thesis" in violation for violation in violations)
