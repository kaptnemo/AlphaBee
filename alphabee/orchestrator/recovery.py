"""恢复阶梯协议化：把各节点零散的"修补 / 降级 / 跳过 / 回环 / 升级"统一成一份裁决（F2 / §7、§14.3-A）。

**核心动作**：节点出口检测器报出偏离后，由 :func:`choose_recovery` 按
「契约声明的 ``recovery_ladder`` + ``state`` 计数器 + 偏离严重度」**唯一地**决定恢复档位，
而不是各节点自己隐式判断（现状：insight 四级降级、report rewrite、data gap 各写各的）。

阶梯取值见 §7：T0 完整通过 / T1 局部修复 / T2 降级产出 / T3 骨架跳过 / T4 受控回环 / T5 升级。
契约侧声明见 :data:`alphabee.orchestrator.node_contracts.NODE_CONTRACTS`。

工程约束（§14.0）：

* **只读**：本模块只读 ``state`` 的计数器，**不写回**；轮次自增仍由既有路由负责
  （``gates.route_after_report_review``）；
* **无 IO、无 import 期配置读取**：§14.6 的严重度权重/阈值经 :func:`_recovery_settings` 惰性读取，
  配置缺失 → 内置默认（fail-open）；
* **确定性**：同输入同输出，便于单测穷举 7 条分支。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from alphabee.core.schemas import Issue

logger = logging.getLogger(__name__)

__all__ = [
    "BUDGET_EXHAUSTED_ISSUE_CATEGORY",
    "RecoveryDecision",
    "RecoveryTier",
    "choose_recovery",
    "cost_exposure",
    "recovery_switches",
]

_SEVERITY_WEIGHT_DEFAULT: dict[str, int] = {"low": 1, "medium": 3, "high": 10, "critical": 30}
_COST_EXPOSURE_THRESHOLD_DEFAULT = 30

#: §14.6 ``deviation.recovery.enabled`` 的默认值（fail-open：配置缺失也启用）。
_DEFAULT_RECOVERY_ENABLED = True

#: 预算耗尽（D5 控制偏离）的**稳定 category**——供落库方构造 ``Issue(category=...)``。
#: 与 :data:`alphabee.orchestrator.services.deviation.CLASS_BY_CATEGORY` 的
#: ``"budget_exhausted": D5_CONTROL`` 登记同源；两处任一改名即由分类守卫测试报红。
BUDGET_EXHAUSTED_ISSUE_CATEGORY = "budget_exhausted"

#: ★★ **哨兵值，绝不登记** —— 见 :data:`RERUN_BUDGET_CHECK_CATEGORY` 的说明（t53）。
#:
#: 它**不是**一条偏离类目，只是"本次回环是否还有预算"这一裁决请求的触发值：
#:
#: * **不得**登记进 :data:`alphabee.orchestrator.services.deviation.CLASS_BY_CATEGORY`
#:   （登记即污染 F0 分类守卫与 §11/F5 的 per-class 画像）；
#: * **不得**写进任何 ``Issue`` 的 ``category``（不落库，见 gates.py 的探针用法）；
#: * 该性质由 ``tests/orchestrator/test_recovery.py::test_rerun_budget_check_sentinel_is_not_a_registered_category``
#:   与 ``tests/orchestrator/test_report_review_gate.py::test_gate_never_emits_the_rerun_budget_probe_sentinel``
#:   **守护**（后者为 gate 侧「产出 Issue 里不得出现该值」）。
#:
#: 背景：回环裁决问的是"还能不能再跑一轮"（纯预算问题），而不是"哪个 category 该降级"。
#: 但 :func:`choose_recovery` 的入参是 ``issues``，且"无 issue → T0（keep）"是它的第 1 条分支，
#: 因此调用方必须带一条**不会命中 T1/T2/T3 类目**的偏离，才能让裁决落到"可重试"分支。
#: 它刻意不匹配 recovery 内的三个类目集合（否则会被误判成"可修补/可降级/骨架"）。
#:
#: **可发现性（t53）**：本常量**刻意保留公开名**（不做下划线私有化）——它必须能被
#: ``tests/orchestrator/test_recovery.py`` 直接 import 才能钉住"未登记"这一性质；同时
#: 已从 :data:`__all__` 移除（不再属于模块公开 API 面），避免后续期"顺手登记"。
#: 若将来要彻底私有化，必须同步改 ``gates.py`` 的 import 与两个测试文件 —— 那需要**另开任务**。
RERUN_BUDGET_CHECK_CATEGORY = "rerun_budget_check"


#: D2 结构类偏离中"可通过 coerce / lenient parse 局部修补"的稳定 category（§6.2「可 coerce」）。
_PATCHABLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "artifact_schema_invalid",
        "insight_missing",
        "parse_error",
        "schema",
    }
)

#: 数据缺口 / 产物空洞（→ T2 降级产出）的稳定 category。
_DEGRADE_CATEGORIES: frozenset[str] = frozenset(
    {
        "derived_facts_empty",
        "report_input_missing",
        "missing_data",
        "blocked",
        "stale",
        "insight_degraded",
        "degraded",
    }
)

#: 核心输入整体缺失（→ T3 骨架/跳过）的稳定 category。
_SKELETON_CATEGORIES: frozenset[str] = frozenset(
    {
        "industry_context_missing",
        "peer_group_missing",
        "company_track_missing",
    }
)


class RecoveryTier(IntEnum):
    """§7 的六档恢复阶梯（值即 ``Issue.recovery_cost`` 的基础）。"""

    TIER_0_COMPLETE = 0  # 完整通过
    TIER_1_PATCH = 1  # 局部修复（确定性结构修补，无 LLM）
    TIER_2_DEGRADE = 2  # 降级产出（degraded=True + 确定性兜底）
    TIER_3_SKELETON = 3  # 骨架/跳过（显式声明缺失）
    TIER_4_RERUN = 4  # 受控回环（有限次重跑上游，唯一允许的图回环）
    TIER_5_ESCALATE = 5  # 升级（终止 run 或最终产物显式暴露失败）


@dataclass(frozen=True)
class RecoveryDecision:
    """一次恢复裁决的结果；``cost`` 可直接写入 ``Issue.recovery_cost``。"""

    tier: RecoveryTier
    action: str  # "keep" / "patch" / "degraded_tier=2" / "skeleton" / "rerun_round=1" / "escalated"
    cost: int  # 0..5
    reason: str
    #: **机读**的偏离 category（仅当本档位对应一条稳定 category 时非 None）。
    #: ``None`` ⇒ 调用方按自身节点语义决定落库口径。当前唯一填充值：
    #: :data:`BUDGET_EXHAUSTED_ISSUE_CATEGORY`（回环预算耗尽 → D5）。
    #: 刻意不把这类信息只塞进自然语言 ``reason``：下游无法机器判定（F2-2）。
    issue_category: str | None = None


def recovery_switches() -> bool:
    """读取恢复阶梯开关（§14.6 ``deviation.recovery.enabled``），容忍配置缺失。

    与 :func:`alphabee.orchestrator.services.detection.detection_switches` 同口径：
    **模块级不读配置**，运行时逐层 ``getattr``；配置段/字段缺失或读取异常 → ``True``
    （fail-open，与 §14.6 默认值一致）。关闭即回退到既有隐式判断（§14.8 PR5 的回滚方式）。
    """
    try:
        from alphabee.config import get_settings

        recovery = getattr(getattr(get_settings(), "deviation", None), "recovery", None)
        if recovery is None:
            return _DEFAULT_RECOVERY_ENABLED
        return bool(getattr(recovery, "enabled", _DEFAULT_RECOVERY_ENABLED))
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断裁决
        logger.warning("deviation recovery settings unavailable (fail-open, default on): %s", exc)
        return _DEFAULT_RECOVERY_ENABLED


def _recovery_settings() -> tuple[dict[str, int], int]:
    """惰性读取 §14.6 的严重度权重与代价阈值；缺失/异常 → 内置默认（fail-open、不在 import 期读配置）。"""
    try:
        from alphabee.config import get_settings

        budget = getattr(getattr(get_settings(), "deviation", None), "budget", None)
        if budget is None:
            return dict(_SEVERITY_WEIGHT_DEFAULT), _COST_EXPOSURE_THRESHOLD_DEFAULT
        weights = getattr(budget, "severity_weight", None) or {}
        resolved = {key: int(weights.get(key, default)) for key, default in _SEVERITY_WEIGHT_DEFAULT.items()}
        threshold = int(getattr(budget, "cost_exposure_threshold", _COST_EXPOSURE_THRESHOLD_DEFAULT))
        return resolved, threshold
    except Exception:  # noqa: BLE001 - 配置不可用绝不打断裁决
        return dict(_SEVERITY_WEIGHT_DEFAULT), _COST_EXPOSURE_THRESHOLD_DEFAULT


def _severity_name(severity: Any) -> str:
    return getattr(severity, "value", None) or str(severity or "").lower()


def _amplification_factor(issue: Issue) -> int:
    """§10.1 的放大因子：被下游放大（``amplified_by`` 非空）时按条数加权（1 + n，保守）。"""
    return 1 + len(getattr(issue, "amplified_by", None) or [])


def cost_exposure(issue: Issue) -> int:
    """§10.1 代价敞口：``severity_weight × amplification_factor × (1 − recoverability)``。

    已恢复（``recovery_action`` 非空且非 "keep"）⇒ 敞口 ≈ 0。
    """
    weights, _ = _recovery_settings()
    weight = weights.get(_severity_name(issue.severity), _SEVERITY_WEIGHT_DEFAULT["low"])
    recoverability = 0 if (getattr(issue, "recovery_action", None) or "") in {"", "keep"} else 1
    return int(weight * _amplification_factor(issue) * (1 - recoverability))


def _categories(issues: list[Issue]) -> set[str]:
    return {(getattr(issue, "category", "") or "").strip().lower() for issue in issues}


def _has_critical_over_threshold(issues: list[Issue]) -> tuple[bool, str]:
    """第 7 条分支：存在 critical 偏离且代价敞口超阈值 → 越过中间档直接升级。"""
    _, threshold = _recovery_settings()
    for issue in issues:
        if _severity_name(issue.severity) != "critical":
            continue
        exposure = cost_exposure(issue)
        if exposure > threshold:
            return True, f"critical 偏离代价敞口 {exposure} > 阈值 {threshold}"
    return False, ""


def _in_ladder(contract: Any, tier: RecoveryTier) -> bool:
    ladder = tuple(getattr(contract, "recovery_ladder", ()) or ())
    return int(tier) in ladder


def choose_recovery(
    node_id: str,
    issues: list[Issue],
    *,
    contract: Any,
    state: dict[str, Any] | None = None,
) -> RecoveryDecision:
    """按契约 ``ladder`` + ``state`` 计数器决定恢复档位（§14.3-A）。

    **评估顺序（F2-4：分支编号 ≠ 评估顺序）**：第 0 步先做一次**短路裁决**——命中 critical
    且 :func:`cost_exposure` 超阈值 → 直接返回 ``TIER_5``（越过所有中间档，不再评估 1–5）；
    否则按 1→5 顺序评估，都不中再落 6。下面的编号即 §14.3-A 的编号，**不代表执行先后**：

    0. （**短路，优先于 1–5**）critical 且代价敞口超阈值 → **T5**（越过中间档）；
    1. 无 issue → **T0**；
    2. 命中"局部可修"（schema 可 coerce / parse 可修）且 ``1 ∈ ladder`` → **T1**；
    3. 命中"降级条件"（数据缺口 / 产物空洞）且 ``2 ∈ ladder`` → **T2**；
    4. 命中"骨架条件"（核心输入整体缺失）且 ``3 ∈ ladder`` → **T3**；
    5. 命中"可重试"（``max_retries > 0`` 且 ``state`` 轮次 < 上限）且 ``4 ∈ ladder`` → **T4**；
    6. 其余（含 5 的预算耗尽）→ **T5**。

    第 6 条的两个失败面**刻意区分**：``4 ∈ ladder`` 的"回环预算耗尽"是**可机读**的
    （``issue_category="budget_exhausted"``，因为它是一条稳定 D5 category，必须能落库），
    而"阶梯不含可修档"没有对应的稳定 category（各节点语义不同），故 ``issue_category=None``。

    ``state`` 为只读入参：仅读 ``supplement_round``/``max_supplement_rounds`` 与
    ``report_review_round``/``max_report_review_rounds``，**不写回**（轮次自增由既有路由负责）。
    """
    state = state or {}
    if not issues:
        return RecoveryDecision(RecoveryTier.TIER_0_COMPLETE, "keep", 0, f"{node_id}: 无偏离，完整通过")

    escalated, why = _has_critical_over_threshold(issues)
    if escalated:
        return RecoveryDecision(RecoveryTier.TIER_5_ESCALATE, "escalated", 5, f"{node_id}: {why}")

    categories = _categories(issues)

    if categories & _PATCHABLE_CATEGORIES and _in_ladder(contract, RecoveryTier.TIER_1_PATCH):
        return RecoveryDecision(
            RecoveryTier.TIER_1_PATCH,
            "patch",
            1,
            f"{node_id}: 命中可局部修补偏离 {sorted(categories & _PATCHABLE_CATEGORIES)}",
        )

    if categories & _DEGRADE_CATEGORIES and _in_ladder(contract, RecoveryTier.TIER_2_DEGRADE):
        return RecoveryDecision(
            RecoveryTier.TIER_2_DEGRADE,
            "degraded_tier=2",
            2,
            f"{node_id}: 命中数据缺口/产物空洞 {sorted(categories & _DEGRADE_CATEGORIES)}",
        )

    if categories & _SKELETON_CATEGORIES and _in_ladder(contract, RecoveryTier.TIER_3_SKELETON):
        return RecoveryDecision(
            RecoveryTier.TIER_3_SKELETON,
            "skeleton",
            3,
            f"{node_id}: 核心输入整体缺失 {sorted(categories & _SKELETON_CATEGORIES)}",
        )

    max_retries = int(getattr(contract, "max_retries", 0) or 0)
    if max_retries > 0 and _in_ladder(contract, RecoveryTier.TIER_4_RERUN):
        counter_key, max_key = _rerun_counters(getattr(contract, "node_id", None) or node_id)
        used = int(state.get(counter_key, 0) or 0)
        limit = state.get(max_key, max_retries)
        limit = max_retries if limit is None else int(limit)
        if used < limit:
            return RecoveryDecision(
                RecoveryTier.TIER_4_RERUN,
                f"rerun_round={used + 1}",
                4,
                f"{node_id}: 可重试且预算未耗尽（{counter_key}={used} < {limit}）",
            )
        return RecoveryDecision(
            RecoveryTier.TIER_5_ESCALATE,
            "escalated",
            5,
            f"{node_id}: 回环预算耗尽（{counter_key}={used} >= {limit}）",
            BUDGET_EXHAUSTED_ISSUE_CATEGORY,
        )

    return RecoveryDecision(
        RecoveryTier.TIER_5_ESCALATE, "escalated", 5, f"{node_id}: 无可用恢复档位（阶梯不含可修档）"
    )


#: 节点 → (已用轮次计数器, 硬上限计数器)；与 ``node_contracts.NODE_BUDGETS`` 同一事实来源。
def _rerun_counters(node_id: str) -> tuple[str, str]:
    from alphabee.orchestrator.node_contracts import NODE_BUDGETS

    binding = NODE_BUDGETS.get(node_id)
    if binding is not None:
        return binding.counter, binding.max_counter
    return "supplement_round", "max_supplement_rounds"
