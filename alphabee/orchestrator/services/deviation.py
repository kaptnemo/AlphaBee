"""偏离分类法与上报工具（deviation-control framework §4/§5，F0a；F0b 追加账本 sink 登记）。

本模块是「偏离分类 → 即时检测 → 阶梯恢复 → 放大审计 → 预算升级」协议的共享底座：

* :data:`NODE_ORDER` —— 主流水线节点序（与 ``orchestrator/agent.py`` 实装一致），
  用于检测时延（§11）与后续 ``NODE_CONTRACTS`` 登记校验（F1a）；
  F0b 起包含 run 尾部账本 sink ``record_deviations``；
* :data:`CLASS_BY_CATEGORY` / :func:`resolve_deviation_class` —— 把历史
  ``Issue.category`` **惰性**归一化到 D1–D5：旧 issue 的 ``deviation_class`` 为
  ``None``，消费方按需解析，不写回字段、不改历史 JSON；
* :func:`record_deviation` —— 检测器统一上报入口（§5.1），只构造 ``Issue``；
* :func:`step_index` / :func:`detection_latency` —— 偏离时间线工具（§11）。

工程约束（§14.0）：

1. **节点不做 IO**：``record_deviation`` 只产对象，持久化统一在 run 尾部
   （``nodes/record_deviations.py``，F0b）。节点保持确定性、可重放。
2. **只读**：所有函数都不修改传入的 ``Issue``（惰性映射只返回结果，不回写字段）。
3. **依赖方向**：本模块只依赖 ``alphabee.core``（schemas）与 ``alphabee.utils``，
   不 import 任何 orchestrator 节点/图/数据层模块，避免 import 环；
   也**不**在 import 期读取 ``get_settings()``（§14.6）。
"""

from __future__ import annotations

from alphabee.core.schemas import DeviationClass, Issue, IssueScope, IssueSeverity
from alphabee.utils.pipeline import make_id

__all__ = [
    "CLASS_BY_CATEGORY",
    "NODE_INDEX",
    "NODE_ORDER",
    "detection_latency",
    "record_deviation",
    "resolve_deviation_class",
    "step_index",
]

#: 主流水线节点序（与 ``orchestrator/agent.py`` 的 ``_graph.add_node`` 顺序一致）。
#: 检测时延按本序取下标；**新增节点必须同步登记**（frozen contract test 断言）。
NODE_ORDER: tuple[str, ...] = (
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
    "record_deviations",  # F0b：run 尾部偏离账本 sink（§14.1-D）
    "finalize_message",
)

#: ``node_id → 节点序``（0-based）；不在 :data:`NODE_ORDER` 中的节点 → 不猜测。
NODE_INDEX: dict[str, int] = {node_id: index for index, node_id in enumerate(NODE_ORDER)}

#: 历史 ``Issue.category`` → 偏离分类的归一化表（§4 v1：只做映射，不改 category 命名）。
#: 未登记的 category 走 :func:`resolve_deviation_class` 的保守回退 D2。
#:
#: 上半段来自文档 §4/§14.1-B 的设计清单；下半段（标 ``# 现网``）是按**现网真实生产点**补齐的
#: 扩展项（F0 review 前经 captain 批准）：``alphabee/`` 里所有 ``Issue(category=...)`` 字面量
#: 都必须显式登记，否则会集体回落 D2、系统性污染 §11/F5 的 per-class 画像。
#: 由 ``tests/orchestrator/test_deviation_service.py::test_every_produced_issue_category_is_registered``
#: 自动扫描源码做覆盖守卫（新增 category 未登记即失败）。
CLASS_BY_CATEGORY: dict[str, DeviationClass] = {
    # ── D1 数据偏离（采集/数据质量）
    "missing_data": DeviationClass.D1_DATA,
    "blocked": DeviationClass.D1_DATA,
    "stale": DeviationClass.D1_DATA,
    "numeric_inconsistency": DeviationClass.D1_DATA,
    "cross_source_conflict": DeviationClass.D1_DATA,
    # 现网：公司档案 / 同业 / 行业基准缺失或陈旧
    "company_track_missing": DeviationClass.D1_DATA,
    "company_track_stale": DeviationClass.D1_DATA,
    "peer_group_missing": DeviationClass.D1_DATA,
    "peer_group_benchmarks_missing": DeviationClass.D1_DATA,
    "industry_context_missing": DeviationClass.D1_DATA,
    "industry_benchmarks_missing": DeviationClass.D1_DATA,
    # ── D2 结构偏离（解析/schema/降级/上下文构建）
    "parse_error": DeviationClass.D2_STRUCTURE,
    "schema": DeviationClass.D2_STRUCTURE,
    "degraded": DeviationClass.D2_STRUCTURE,
    # 现网：四级降级产出与 LLM 输出结构问题
    "insight_degraded": DeviationClass.D2_STRUCTURE,
    "driver_profile_degraded": DeviationClass.D2_STRUCTURE,
    "context_build_failure": DeviationClass.D2_STRUCTURE,
    "empty_response": DeviationClass.D2_STRUCTURE,
    # ── D3 论证偏离（无证据结论/论点冲突/证据链断裂）
    "thesis_gap": DeviationClass.D3_ARGUMENT,
    "thesis_conflict": DeviationClass.D3_ARGUMENT,
    "evidence_chain_incomplete": DeviationClass.D3_ARGUMENT,
    "unverified_claim": DeviationClass.D3_ARGUMENT,
    # 现网：论点告警、已结算冲突、候选冲突、待验证项
    "thesis_warning": DeviationClass.D3_ARGUMENT,
    "verified_conflict": DeviationClass.D3_ARGUMENT,
    "conflict": DeviationClass.D3_ARGUMENT,
    "verification_needed": DeviationClass.D3_ARGUMENT,
    # ── D4 状态偏离（假设失效/上下文截断/环境状态迁移）
    "assumption_invalidated": DeviationClass.D4_STATE,
    "context_truncated": DeviationClass.D4_STATE,
    "state_drift": DeviationClass.D4_STATE,
    # 现网：市场总分跌破阈值 = 论点所依赖的环境状态发生迁移
    "market_regime": DeviationClass.D4_STATE,
    # ── D5 控制偏离（回环打满/预算耗尽/流水线控制层失败）
    "report_rewrite_needed": DeviationClass.D5_CONTROL,
    "budget_exhausted": DeviationClass.D5_CONTROL,
    # 现网：子代理调用失败、中期决策节点（及其 reporter）失败、通用失败类
    "subagent_failure": DeviationClass.D5_CONTROL,
    "midterm_decision_failed": DeviationClass.D5_CONTROL,
    "midterm_decision_report_failed": DeviationClass.D5_CONTROL,
    "failure": DeviationClass.D5_CONTROL,
}

#: 未知 category 的保守回退：归为结构偏离（§14.1-B）。宁可少分类，不猜测语义。
_UNKNOWN_CATEGORY_CLASS: DeviationClass = DeviationClass.D2_STRUCTURE


def resolve_deviation_class(issue: Issue) -> DeviationClass:
    """解析 issue 的偏离分类：显式字段优先，否则按 ``category`` 惰性映射。

    * ``issue.deviation_class`` 非 None → 直接返回（检测器显式声明优先）；
    * 否则查 :data:`CLASS_BY_CATEGORY`（category 先做 ``strip().lower()`` 归一化，
      容忍大小写/空白漂移）；
    * 未登记 category → ``D2_STRUCTURE``（保守，见 :data:`_UNKNOWN_CATEGORY_CLASS`）。

    只读函数：不会写回 ``issue.deviation_class``。
    """
    if issue.deviation_class is not None:
        return issue.deviation_class
    category = (issue.category or "").strip().lower()
    return CLASS_BY_CATEGORY.get(category, _UNKNOWN_CATEGORY_CLASS)


def step_index(node_id: str | None) -> int | None:
    """节点 id → 节点序（0-based）。``None`` 或未登记节点 → ``None``（不猜测）。"""
    if node_id is None:
        return None
    return NODE_INDEX.get(node_id)


def record_deviation(
    class_: DeviationClass,
    severity: IssueSeverity,
    message: str,
    *,
    detected_at_step: str,
    related_step: str | None = None,
    related_artifact: str | None = None,
    category: str | None = None,
    scope: IssueScope = IssueScope.REPORT,
    recovery_action: str | None = None,
    recovery_cost: int | None = None,
    amplified_by: list[str] | None = None,
) -> Issue:
    """偏离统一上报入口（§5.1）：构造一条带分类/来源/恢复信息的 :class:`Issue`。

    :param class_: 偏离分类（D1–D5）。
    :param severity: 业务严重度。
    :param message: 人类可读描述。
    :param detected_at_step: 后置检测器所在节点 id（**必填**，偏离时间线的检测端）。
    :param related_step: 偏离产生地节点 id；缺省取 ``detected_at_step``（节点自检）。
    :param related_artifact: 关联产物 id。
    :param category: 稳定类目名；缺省取 ``class_.value``。账本指纹依赖它，必须稳定。
    :param scope: 产出阶段（planning/data/report/review/evaluation）。
    :param recovery_action: 恢复动作标识（如 ``rerun_round=1`` / ``escalated``）。
    :param recovery_cost: 恢复代价（重跑轮数/降级层数；0=未恢复；None=未尝试）。
    :param amplified_by: 放大边 key 列表；``None`` → 空列表（不复制调用方 list）。

    注意：本函数**不写库、不修改 state**（§14.1-B 设计决策），调用方把返回的 issue
    放进节点返回的 partial state（``OrchestratorState.issues`` 的 ``_merge_by_id``
    reducer 按 id 幂等合并），持久化在 run 尾部完成。
    """
    return Issue(
        id=make_id("issue"),
        severity=severity,
        category=category or class_.value,
        message=message,
        related_step=related_step or detected_at_step,
        related_artifact=related_artifact,
        scope=scope,
        deviation_class=class_,
        detected_at_step=detected_at_step,
        recovery_action=recovery_action,
        recovery_cost=recovery_cost,
        amplified_by=list(amplified_by or []),
    )


def detection_latency(issue: Issue) -> int | None:
    """检测时延（节点数）= ``step_index(detected_at_step) − step_index(related_step)``。

    任一端节点未知（``None`` 或未登记）→ ``None``（不猜测）。

    返回值**不做非负夹取**：负数表示检测器接线位置早于偏离产生地（mis-wired），
    需要暴露而不是被静默抹平。
    """
    detected = step_index(issue.detected_at_step)
    origin = step_index(issue.related_step)
    if detected is None or origin is None:
        return None
    return detected - origin
