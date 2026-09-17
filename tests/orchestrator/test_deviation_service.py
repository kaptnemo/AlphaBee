"""F0a 服务测试：节点序 + 分类法惰性映射 + 上报入口（§14.7）。

覆盖：惰性映射全 category（对照 §4 表格逐项断言）；**现网 category 全覆盖守卫**（扫描源码里
所有 ``Issue(category=...)`` 生产点，未登记者失败）；未登记 category → D2；
``detection_latency`` 计算与 None 分支；``record_deviation`` 的字段填充与 id 唯一；
``NODE_ORDER`` 与 ``orchestrator/agent.py`` 实装一致（不 import agent，避免
import 期 tushare ``set_token`` 的 ``$HOME/tk.csv`` 副作用）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from alphabee.core.schemas import DeviationClass, Issue, IssueScope, IssueSeverity
from alphabee.orchestrator.services import deviation as deviation_service
from alphabee.orchestrator.services.deviation import (
    CLASS_BY_CATEGORY,
    NODE_INDEX,
    NODE_ORDER,
    detection_latency,
    record_deviation,
    resolve_deviation_class,
    step_index,
)

_REPO_ROOT = Path(deviation_service.__file__).resolve().parents[2].parent
_AGENT_PATH = _REPO_ROOT / "alphabee" / "orchestrator" / "agent.py"

# §4 表格 + §14.1-B 的期望映射（含 F0 review 前经 captain 批准的现网覆盖扩展），
# 独立于实现书写（防止"实现即期望"的循环断言）。
_EXPECTED_CLASS_BY_CATEGORY: dict[str, DeviationClass] = {
    # D1
    "missing_data": DeviationClass.D1_DATA,
    "blocked": DeviationClass.D1_DATA,
    "stale": DeviationClass.D1_DATA,
    "numeric_inconsistency": DeviationClass.D1_DATA,
    "cross_source_conflict": DeviationClass.D1_DATA,
    "company_track_missing": DeviationClass.D1_DATA,
    "company_track_stale": DeviationClass.D1_DATA,
    "peer_group_missing": DeviationClass.D1_DATA,
    "peer_group_benchmarks_missing": DeviationClass.D1_DATA,
    "industry_context_missing": DeviationClass.D1_DATA,
    "industry_benchmarks_missing": DeviationClass.D1_DATA,
    # D2
    "parse_error": DeviationClass.D2_STRUCTURE,
    "schema": DeviationClass.D2_STRUCTURE,
    "degraded": DeviationClass.D2_STRUCTURE,
    "insight_degraded": DeviationClass.D2_STRUCTURE,
    "driver_profile_degraded": DeviationClass.D2_STRUCTURE,
    "context_build_failure": DeviationClass.D2_STRUCTURE,
    "empty_response": DeviationClass.D2_STRUCTURE,
    # D3
    "thesis_gap": DeviationClass.D3_ARGUMENT,
    "thesis_conflict": DeviationClass.D3_ARGUMENT,
    "evidence_chain_incomplete": DeviationClass.D3_ARGUMENT,
    "unverified_claim": DeviationClass.D3_ARGUMENT,
    "thesis_warning": DeviationClass.D3_ARGUMENT,
    "verified_conflict": DeviationClass.D3_ARGUMENT,
    "conflict": DeviationClass.D3_ARGUMENT,
    "verification_needed": DeviationClass.D3_ARGUMENT,
    # D4
    "assumption_invalidated": DeviationClass.D4_STATE,
    "context_truncated": DeviationClass.D4_STATE,
    "state_drift": DeviationClass.D4_STATE,
    "market_regime": DeviationClass.D4_STATE,
    # D5
    "report_rewrite_needed": DeviationClass.D5_CONTROL,
    "budget_exhausted": DeviationClass.D5_CONTROL,
    "subagent_failure": DeviationClass.D5_CONTROL,
    "midterm_decision_failed": DeviationClass.D5_CONTROL,
    "midterm_decision_report_failed": DeviationClass.D5_CONTROL,
    "failure": DeviationClass.D5_CONTROL,
    # ── F1b：后置检测器与报告 gate 的稳定 category（§6.2 / §6.3）
    "derived_facts_empty": DeviationClass.D1_DATA,
    "report_input_missing": DeviationClass.D1_DATA,
    "artifact_schema_invalid": DeviationClass.D2_STRUCTURE,
    "insight_missing": DeviationClass.D2_STRUCTURE,
    "verdict_without_evidence": DeviationClass.D3_ARGUMENT,
    "assumption_based_claim": DeviationClass.D3_ARGUMENT,
    # ── F3：§8.1 加权边审计的方向不一致上报（review_thesis 节点）
    "amplification_direction_conflict": DeviationClass.D3_ARGUMENT,
}

# 确实未登记的 category（只允许走 D2 保守兜底）：历史/外部来源或尚未命名的类目。
_TRULY_UNKNOWN_CATEGORIES = ("", "   ", "totally_unknown", "legacy_category_x", "mix_of_things")


# §14.1-B 的节点序（与 agent.py 实装逐项一致）；F0b 追加 run 尾部账本 sink。
_EXPECTED_NODE_ORDER: tuple[str, ...] = (
    "collect_raw_facts",
    "resolve_industry_context",
    "resolve_company_track",
    "resolve_driver_profile",
    "run_analysis_engines",
    "explore_conflicts",
    "verify_hypotheses",
    "synthesize_insights",
    "run_thesis",
    "review_thesis",
    "resolve_midterm_decision",
    "midterm_decision_reporter",
    "generate_report",
    "review_report",
    "record_deviations",
    "finalize_message",
)


def _legacy_issue(category: str, **overrides: object) -> Issue:
    """构造一条"旧 issue"：``deviation_class`` 缺省为 None，只能靠 category 惰性映射。"""
    payload: dict[str, object] = {
        "id": "issue-legacy",
        "severity": IssueSeverity.MEDIUM,
        "category": category,
        "message": "legacy issue",
        "related_step": "run_analysis_engines",
        "scope": IssueScope.DATA,
    }
    payload.update(overrides)
    return Issue(**payload)


def _graph_registered_nodes_in_source_order() -> list[str]:
    """从 agent.py 源码按书写顺序取出 ``_graph.add_node("name", ...)`` 的节点名。"""
    tree = ast.parse(_AGENT_PATH.read_text(encoding="utf-8"))
    names: list[str] = []
    for statement in tree.body:  # 只看模块级语句，保持源码书写顺序
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


def _graph_edges_in_source_order() -> list[tuple[str, str]]:
    """从 agent.py 源码取出**生效的** ``_graph.add_edge("a", "b")``（跳过注释掉的边）。"""
    tree = ast.parse(_AGENT_PATH.read_text(encoding="utf-8"))
    edges: list[tuple[str, str]] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_edge":
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "_graph"):
            continue
        if len(call.args) >= 2 and all(
            isinstance(argument, ast.Constant) and isinstance(argument.value, str) for argument in call.args[:2]
        ):
            edges.append((call.args[0].value, call.args[1].value))
    return edges


# ── NODE_ORDER ──────────────────────────────────────────────────────────────


def test_node_order_matches_contract():
    assert NODE_ORDER == _EXPECTED_NODE_ORDER
    assert len(NODE_ORDER) == 16
    assert len(set(NODE_ORDER)) == len(NODE_ORDER)
    # 图哨兵不应登记为节点
    assert not [node for node in NODE_ORDER if node.startswith("__")]


def test_node_order_matches_agent_registration_order():
    registered = _graph_registered_nodes_in_source_order()
    assert registered, f"未能从 {_AGENT_PATH} 解析出节点注册（注册方式变更需同步本测试）"
    assert registered == list(NODE_ORDER)


def test_node_index_is_complete_and_zero_based():
    assert NODE_INDEX == {node: index for index, node in enumerate(NODE_ORDER)}
    assert NODE_INDEX["collect_raw_facts"] == 0
    assert NODE_INDEX["finalize_message"] == len(NODE_ORDER) - 1


# ── 账本 sink 的图接线（F0b / §14.1-D） ──────────────────────────────────────


def test_graph_ledger_sink_wiring():
    edges = _graph_edges_in_source_order()

    # §14.1-D 的两条规定边
    assert ("review_report", "record_deviations") in edges
    assert ("record_deviations", "finalize_message") in edges
    # 可达性：当前 review_report 未被启用，默认路径必须同样经过 sink
    assert ("generate_report", "record_deviations") in edges
    # finalize_message 只由 sink 收口，避免绕过账本的旁路
    assert [source for source, target in edges if target == "finalize_message"] == ["record_deviations"]
    assert ("generate_report", "finalize_message") not in edges


def test_node_order_keeps_ledger_sink_after_its_sources():
    """NODE_ORDER 必须保持拓扑序：sink 晚于两条入边的源节点，早于 finalize_message。"""
    assert NODE_INDEX["review_report"] < NODE_INDEX["record_deviations"]
    assert NODE_INDEX["generate_report"] < NODE_INDEX["record_deviations"]
    assert NODE_INDEX["record_deviations"] < NODE_INDEX["finalize_message"]


def test_step_index_known_unknown_and_none():
    for index, node_id in enumerate(NODE_ORDER):
        assert step_index(node_id) == index
    assert step_index(None) is None
    assert step_index("__start__") is None
    assert step_index("node_that_does_not_exist") is None


# ── 分类法惰性映射 ───────────────────────────────────────────────────────────


def test_class_by_category_matches_documented_table():
    assert CLASS_BY_CATEGORY == _EXPECTED_CLASS_BY_CATEGORY
    for category, expected in _EXPECTED_CLASS_BY_CATEGORY.items():
        assert CLASS_BY_CATEGORY[category] is expected


def _issue_producer_categories() -> dict[str, list[str]]:
    """扫描 ``alphabee/`` 里**三种** category 声明形态 → ``{category: [位置]}``。

        采集面（F0 review 后加固；此前只认形态①，装饰器声明的 category 会被漏掉）：

        1. ``Issue(category="...")`` —— 直接构造 issue 的节点/gate；
        2. ``@detector(name, category)`` —— **F1b 检测器的真实声明机制**，``category`` 是
           **第二个位置参数**，不经 ``Issue(...)``，故必须单独采集；
        3. ``record_deviation(..., category="...")`` —— 统一上报入口。

        只认函数/类名为 ``Issue`` / ``record_deviation`` 的调用（排除 ``IssueRecord`` /
        ``ReportIssuePayload`` / ``DeviationEvent`` / web_search_guard 的 ``_ScanRule`` 等其它同名关键字）。

    **F0 自包含约束（重要）**：本文件属 F0 认证集（锚 ``fb3bced``），**禁止 import 任何 F1 模块
        （如 ``orchestrator.detectors``）** —— 否则在 ``fb3bced`` 上重跑 F0 认证会因模块不存在而失败、
        破坏双锚点记账。因此这里只做**纯源码 AST 扫描**（形态①/②/③），
        「注册表 category 必须已登记」的**运行时**断言落在 ``tests/orchestrator/test_detectors.py``。
    """
    found: dict[str, list[str]] = {}

    def _record(category: object, path: Path, lineno: int) -> None:
        if isinstance(category, str):
            found.setdefault(category, []).append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")

    def _decorator_category(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """形态②：``@detector("name", "category")`` 的第二个位置参数。"""
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "detector" or len(decorator.args) < 2:
                continue
            second = decorator.args[1]
            if isinstance(second, ast.Constant):
                _record(second.value, path, decorator.lineno)

    for path in sorted((_REPO_ROOT / "alphabee").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _decorator_category(node)
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in {"Issue", "record_deviation"}:
                continue
            for keyword in node.keywords:
                if keyword.arg == "category" and isinstance(keyword.value, ast.Constant):
                    _record(keyword.value.value, path, node.lineno)
    return found


def test_every_produced_issue_category_is_registered():
    """覆盖守卫（§4 扩展，F0 review 前经 captain 批准）：现网每个 ``Issue.category`` 生产点
    都必须显式登记，否则会集体回落 D2、系统性污染 per-class 画像。

    新增 category 时的两种正确做法：(1) 补 ``CLASS_BY_CATEGORY``（并在 ``_EXPECTED_CLASS_BY_CATEGORY``
    与本测试文件同步）；(2) 让生产方显式传 ``deviation_class``（检测器路径）。
    """
    produced = _issue_producer_categories()
    assert produced, "未能扫描到任何 Issue(category=...) 生产点，扫描逻辑需与代码同步"

    unmapped = {category: locations for category, locations in produced.items() if category not in CLASS_BY_CATEGORY}
    assert not unmapped, (
        "以下现网 category 未在 CLASS_BY_CATEGORY 显式登记（会全部回落 D2）："
        f"{unmapped}；请补映射或让生产方显式传 deviation_class"
    )
    # 扫描结果必须与冻结期望表一致（防实现漂移）
    assert set(produced) <= set(_EXPECTED_CLASS_BY_CATEGORY)


@pytest.mark.parametrize(("category", "expected"), sorted(_EXPECTED_CLASS_BY_CATEGORY.items()))
def test_lazy_mapping_covers_every_category(category: str, expected: DeviationClass):
    issue = _legacy_issue(category)
    assert issue.deviation_class is None  # 惰性映射的前提：字段未显式填写
    assert resolve_deviation_class(issue) is expected


def test_explicit_deviation_class_wins_over_category():
    explicit = _legacy_issue("missing_data", deviation_class=DeviationClass.D4_STATE)
    assert resolve_deviation_class(explicit) is DeviationClass.D4_STATE

    explicit_unknown_category = _legacy_issue("unknown_category_x", deviation_class=DeviationClass.D5_CONTROL)
    assert resolve_deviation_class(explicit_unknown_category) is DeviationClass.D5_CONTROL


@pytest.mark.parametrize("category", _TRULY_UNKNOWN_CATEGORIES)
def test_unknown_category_falls_back_to_d2(category: str):
    """兜底语义不变：只有真正未登记的 category 才回落 D2。"""
    assert category.strip().lower() not in CLASS_BY_CATEGORY
    assert resolve_deviation_class(_legacy_issue(category)) is DeviationClass.D2_STRUCTURE


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("conflict", DeviationClass.D3_ARGUMENT),
        ("verified_conflict", DeviationClass.D3_ARGUMENT),
        ("thesis_warning", DeviationClass.D3_ARGUMENT),
        ("insight_degraded", DeviationClass.D2_STRUCTURE),
        ("driver_profile_degraded", DeviationClass.D2_STRUCTURE),
        ("company_track_stale", DeviationClass.D1_DATA),
        ("peer_group_missing", DeviationClass.D1_DATA),
        ("subagent_failure", DeviationClass.D5_CONTROL),
        ("midterm_decision_failed", DeviationClass.D5_CONTROL),
        ("market_regime", DeviationClass.D4_STATE),
    ],
)
def test_real_world_category_extension(category: str, expected: DeviationClass):
    """F0 覆盖扩展：现网真实 category 按语义登记（不再回落 D2）。"""
    assert CLASS_BY_CATEGORY[category] is expected
    assert resolve_deviation_class(_legacy_issue(category)) is expected


def test_category_lookup_tolerates_case_and_whitespace():
    assert resolve_deviation_class(_legacy_issue("  Missing_Data ")) is DeviationClass.D1_DATA
    assert resolve_deviation_class(_legacy_issue("THESIS_GAP")) is DeviationClass.D3_ARGUMENT


def test_resolve_deviation_class_does_not_mutate_issue():
    issue = _legacy_issue("parse_error")
    assert resolve_deviation_class(issue) is DeviationClass.D2_STRUCTURE
    assert issue.deviation_class is None  # 只读：惰性映射不写回字段


# ── record_deviation ────────────────────────────────────────────────────────


def test_record_deviation_fills_all_contract_fields():
    issue = record_deviation(
        DeviationClass.D1_DATA,
        IssueSeverity.HIGH,
        "derived facts 为空但 fact_values 非空（引擎故障）",
        detected_at_step="run_analysis_engines",
        related_step="run_analysis_engines",
        related_artifact="artifact-derived-facts",
        category="derived_facts_empty",
        scope=IssueScope.DATA,
        recovery_action="degraded_tier=2",
        recovery_cost=2,
        amplified_by=["insight_to_thesis"],
    )

    assert issue.id.startswith("issue-")
    assert issue.severity is IssueSeverity.HIGH
    assert issue.category == "derived_facts_empty"
    assert issue.message == "derived facts 为空但 fact_values 非空（引擎故障）"
    assert issue.related_step == "run_analysis_engines"
    assert issue.related_artifact == "artifact-derived-facts"
    assert issue.scope is IssueScope.DATA
    assert issue.deviation_class is DeviationClass.D1_DATA
    assert issue.detected_at_step == "run_analysis_engines"
    assert issue.recovery_action == "degraded_tier=2"
    assert issue.recovery_cost == 2
    assert issue.amplified_by == ["insight_to_thesis"]
    assert issue.status.value == "open"


def test_record_deviation_defaults():
    issue = record_deviation(
        DeviationClass.D3_ARGUMENT,
        IssueSeverity.MEDIUM,
        "证据链不完整",
        detected_at_step="review_thesis",
    )

    # category 缺省取分类值（账本指纹依赖它稳定）
    assert issue.category == "d3_argument"
    # related_step 缺省取检测节点（节点自检语义）
    assert issue.related_step == "review_thesis"
    assert issue.scope is IssueScope.REPORT
    assert issue.related_artifact is None
    assert issue.recovery_action is None
    assert issue.recovery_cost is None
    assert issue.amplified_by == []


@pytest.mark.parametrize("class_", list(DeviationClass))
def test_record_deviation_keeps_class_for_every_member(class_: DeviationClass):
    issue = record_deviation(class_, IssueSeverity.LOW, "m", detected_at_step="run_thesis")
    assert issue.deviation_class is class_
    assert resolve_deviation_class(issue) is class_
    assert issue.category == class_.value


def test_record_deviation_amplified_by_is_a_copy_and_optional():
    shared = ["insight_to_thesis"]
    issue = record_deviation(
        DeviationClass.D2_STRUCTURE,
        IssueSeverity.LOW,
        "m",
        detected_at_step="synthesize_insights",
        amplified_by=shared,
    )
    shared.append("thesis_to_report")
    assert issue.amplified_by == ["insight_to_thesis"]

    no_amplification = record_deviation(
        DeviationClass.D2_STRUCTURE,
        IssueSeverity.LOW,
        "m",
        detected_at_step="synthesize_insights",
        amplified_by=None,
    )
    assert no_amplification.amplified_by == []


def test_record_deviation_ids_are_unique():
    issues = [
        record_deviation(DeviationClass.D2_STRUCTURE, IssueSeverity.LOW, "m", detected_at_step="run_thesis")
        for _ in range(200)
    ]
    ids = [issue.id for issue in issues]
    assert len(set(ids)) == len(ids)


def test_record_deviation_is_pure_object_factory():
    """节点不做 IO（§14.1-B）：上报只产 Issue，不引入数据层/配置依赖。"""
    issue = record_deviation(DeviationClass.D4_STATE, IssueSeverity.LOW, "m", detected_at_step="generate_report")
    assert isinstance(issue, Issue)
    # 再次调用不受前一次影响（无模块级可变状态）
    other = record_deviation(DeviationClass.D4_STATE, IssueSeverity.LOW, "m", detected_at_step="generate_report")
    assert other.id != issue.id


# ── detection_latency ───────────────────────────────────────────────────────


def test_detection_latency_downstream_detector():
    issue = _legacy_issue("missing_data", related_step="run_analysis_engines", detected_at_step="review_report")
    # 引擎出口产生、报告复核才发现 → 时延 9 个节点（O(流水线) 现状的证据）
    assert NODE_INDEX["review_report"] - NODE_INDEX["run_analysis_engines"] == 9
    assert detection_latency(issue) == 9


def test_detection_latency_zero_when_node_self_checks():
    issue = _legacy_issue("missing_data", related_step="explore_conflicts", detected_at_step="explore_conflicts")
    assert detection_latency(issue) == 0


def test_detection_latency_none_branches():
    # 无检测节点（旧 issue / 非检测器产出）
    assert detection_latency(_legacy_issue("missing_data", related_step="run_thesis")) is None
    # 无产生地（detected_at_step 被缺省填充时不会发生，但需容错）
    assert detection_latency(_legacy_issue("missing_data", related_step=None, detected_at_step="run_thesis")) is None
    # 两端都不是登记节点
    assert detection_latency(_legacy_issue("missing_data", related_step="nope", detected_at_step="other")) is None
    # 单端未登记
    assert detection_latency(_legacy_issue("missing_data", related_step="nope", detected_at_step="run_thesis")) is None
    assert detection_latency(_legacy_issue("missing_data", related_step="run_thesis", detected_at_step="nope")) is None


def test_detection_latency_keeps_negative_for_miswired_detector():
    """检测器接线早于偏离产生地 → 负数暴露问题，不静默夹取为 0。"""
    issue = _legacy_issue("missing_data", related_step="finalize_message", detected_at_step="collect_raw_facts")
    latency = detection_latency(issue)
    assert latency == -NODE_INDEX["finalize_message"]
    assert latency is not None and latency < 0


def test_detection_latency_for_record_deviation_output():
    issue = record_deviation(
        DeviationClass.D1_DATA,
        IssueSeverity.LOW,
        "m",
        detected_at_step="run_analysis_engines",
        related_step="collect_raw_facts",
    )
    assert detection_latency(issue) == NODE_INDEX["run_analysis_engines"] == 4


# ── 依赖方向（§14.0） ────────────────────────────────────────────────────────


def test_service_module_does_not_import_forbidden_layers():
    tree = ast.parse(Path(deviation_service.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_prefixes = (
        "alphabee.data_fetch",  # 数据层不得被服务层反向依赖
        "alphabee.orchestrator.nodes",  # services ← nodes 方向禁止
        "alphabee.orchestrator.agent",
        "alphabee.config",  # 不在 import 期读配置（§14.6）
    )
    for module in imported:
        assert not module.startswith(forbidden_prefixes), f"deviation.py 不得 import {module}"
    assert imported <= {"__future__", "alphabee.core.schemas", "alphabee.utils.pipeline"}
