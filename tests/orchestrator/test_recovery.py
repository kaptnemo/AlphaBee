"""F2 单测：恢复阶梯裁决 ``choose_recovery``（§14.3-A 的 7 条分支 + 预算 + 代价敞口）。

覆盖目标（t9 acceptance）：

* 阶梯 **7 条分支**逐条：无 issue→T0；critical 超敞口→T5；可修补→T1；数据缺口→T2；
  核心输入缺失→T3；可重试且预算未耗尽→T4；其余/预算耗尽→T5；
* ``RecoveryTier`` 值域 0–5（IntEnum，可直接作 ``Issue.recovery_cost``）；
* 预算耗尽 → **T5 + 语义为 "budget exhausted"**；
* ``recovery_cost`` 赋值（``decision.cost`` == tier 值，可直接落到 Issue）；
* **只读**：``choose_recovery`` 不改入参 ``state`` / ``issues``；
* 契约 ``recovery_ladder`` 不含对应档位时**不得**越权选择该档（回落到 T5）。

T25（F2 维修的证据落盘，t40 的回归保护）：

* ``apply_degradation`` 为 Tier 2/3 的**唯一写入点**（正例 + ``tier<2`` 反例 + 非 mapping 入参）；
* ``RecoveryDecision.issue_category`` 的**机读**语义（预算耗尽 → ``budget_exhausted``；其它 → ``None``）；
* ``RERUN_BUDGET_CHECK_CATEGORY`` 是**不落库**的路由触发哨兵（未登记分类表、不出现在产出 Issue 里）；
* 关 ``deviation.recovery.enabled`` → 回落旧行为。
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from alphabee.core.schemas import Artifact, Issue, IssueSeverity, Run, RunStatus
from alphabee.orchestrator.gates import review_report, route_after_report_review
from alphabee.orchestrator.node_contracts import NODE_CONTRACTS, NodeContract
from alphabee.orchestrator.recovery import (
    BUDGET_EXHAUSTED_ISSUE_CATEGORY,
    RERUN_BUDGET_CHECK_CATEGORY,
    RecoveryDecision,
    RecoveryTier,
    choose_recovery,
    cost_exposure,
)
from alphabee.orchestrator.services.degradation import (
    DEGRADATION_FLAG,
    DEGRADATION_REASON_FIELD,
    apply_degradation,
)


def _issue(
    category: str,
    *,
    severity: IssueSeverity = IssueSeverity.MEDIUM,
    recovery_action: str | None = None,
    amplified_by: list[str] | None = None,
) -> Issue:
    return Issue(
        id=f"issue-{category}",
        severity=severity,
        category=category,
        message=f"偏离 {category}",
        recovery_action=recovery_action,
        amplified_by=list(amplified_by or []),
    )


def _contract(node_id: str = "run_analysis_engines") -> NodeContract:
    """取真实契约（阶梯/预算来自 §7.1 登记表，而非测试自造）。"""
    return NODE_CONTRACTS[node_id]


def _legacy_report_route(state: dict) -> str:
    """F2 之前 ``route_after_report_review`` 的**内联判据**（逐字保留，作为等价性基准）。"""
    if state.get("report_rewrite_needed") and state.get("report_review_round", 0) < state.get(
        "max_report_review_rounds", 2
    ):
        return "generate_report"
    return "finalize_message"


# ── 值域与结构 ──────────────────────────────────────────────────────────────


def test_recovery_tiers_cover_zero_to_five():
    assert [int(t) for t in RecoveryTier] == [0, 1, 2, 3, 4, 5]
    assert RecoveryTier.TIER_0_COMPLETE == 0
    assert RecoveryTier.TIER_5_ESCALATE == 5


def test_decision_cost_equals_tier_value():
    """``decision.cost`` 必须等于档位值，才能直接写入 ``Issue.recovery_cost``。"""
    decision = choose_recovery("run_analysis_engines", [], contract=_contract(), state={})
    assert isinstance(decision, RecoveryDecision)
    assert decision.cost == int(decision.tier) == 0


# ── 7 条分支 ────────────────────────────────────────────────────────────────


def test_branch_1_no_issue_is_tier_0():
    decision = choose_recovery("run_analysis_engines", [], contract=_contract(), state={})
    assert decision.tier is RecoveryTier.TIER_0_COMPLETE
    assert decision.cost == 0
    assert decision.action == "keep"


def test_branch_2_critical_over_exposure_escalates_directly():
    """critical + 代价敞口**超过**阈值 → 越过中间档直接 T5。

    口径：§10.1 判据是"超阈值"（严格大于）。未放大的 critical 敞口 == 30 **等于**阈值，故不触发；
    被下游放大时 30 × (1+1) = 60 > 30 → 触发（见下一条用例钉住边界）。
    """
    issue = _issue("blocked", severity=IssueSeverity.CRITICAL, amplified_by=["insight->thesis"])
    assert cost_exposure(issue) == 60
    decision = choose_recovery("run_analysis_engines", [issue], contract=_contract(), state={})
    assert decision.tier is RecoveryTier.TIER_5_ESCALATE
    assert decision.cost == 5
    assert "代价敞口" in decision.reason


def test_critical_exactly_at_threshold_is_not_direct_escalation():
    """边界：敞口 == 阈值（未放大的 critical = 30）**不**触发直升级分支，走正常阶梯。"""
    issue = _issue("industry_context_missing", severity=IssueSeverity.CRITICAL)
    assert cost_exposure(issue) == 30
    decision = choose_recovery("run_analysis_engines", [issue], contract=_contract("run_analysis_engines"), state={})
    assert decision.tier is RecoveryTier.TIER_3_SKELETON


def test_branch_3_patchable_hits_tier_1_when_in_ladder():
    contract = _contract("run_thesis")  # ladder (0,1,2)
    decision = choose_recovery("run_thesis", [_issue("artifact_schema_invalid")], contract=contract, state={})
    assert decision.tier is RecoveryTier.TIER_1_PATCH
    assert decision.cost == 1
    assert decision.action == "patch"


def test_branch_4_data_gap_hits_tier_2_when_in_ladder():
    contract = _contract("run_analysis_engines")  # ladder (0,2,3)
    decision = choose_recovery("run_analysis_engines", [_issue("derived_facts_empty")], contract=contract, state={})
    assert decision.tier is RecoveryTier.TIER_2_DEGRADE
    assert decision.cost == 2
    assert decision.action == "degraded_tier=2"


def test_branch_5_core_input_missing_hits_tier_3_when_in_ladder():
    contract = _contract("run_analysis_engines")  # ladder 含 3
    decision = choose_recovery(
        "run_analysis_engines", [_issue("industry_context_missing")], contract=contract, state={}
    )
    assert decision.tier is RecoveryTier.TIER_3_SKELETON
    assert decision.cost == 3


def test_branch_6_retryable_hits_tier_4_when_budget_remains():
    """``generate_report`` 有 max_retries=2 且 ladder 含 4；未知类偏离 → T4。"""
    contract = _contract("generate_report")
    state = {"report_review_round": 0, "max_report_review_rounds": 2}
    decision = choose_recovery("generate_report", [_issue("thesis_gap")], contract=contract, state=state)
    assert decision.tier is RecoveryTier.TIER_4_RERUN
    assert decision.cost == 4
    assert decision.action == "rerun_round=1"


def test_branch_6b_collect_raw_facts_uses_its_own_counter():
    contract = _contract("collect_raw_facts")  # ladder (0,4,2)，max_retries=1
    decision = choose_recovery(
        "collect_raw_facts",
        [_issue("unmapped_situation")],
        contract=contract,
        state={"supplement_round": 0, "max_supplement_rounds": 1},
    )
    assert decision.tier is RecoveryTier.TIER_4_RERUN
    assert "supplement_round" in decision.reason


def test_branch_7_budget_exhausted_escalates_with_reason():
    """预算耗尽 → T5，且原因须点明 budget exhausted。"""
    contract = _contract("generate_report")
    state = {"report_review_round": 2, "max_report_review_rounds": 2}
    decision = choose_recovery("generate_report", [_issue("thesis_gap")], contract=contract, state=state)
    assert decision.tier is RecoveryTier.TIER_5_ESCALATE
    assert decision.cost == 5
    assert "预算耗尽" in decision.reason


def test_branch_7b_no_retry_budget_is_tier_5():
    contract = _contract("run_analysis_engines")  # max_retries=0
    decision = choose_recovery("run_analysis_engines", [_issue("mystery")], contract=contract, state={})
    assert decision.tier is RecoveryTier.TIER_5_ESCALATE


# ── 阶梯越权保护 ────────────────────────────────────────────────────────────


def test_ladder_does_not_authorize_tier_1_means_fall_back_to_tier_5():
    """契约阶梯不含 1（如 run_analysis_engines=(0,2,3)）→ 不得选 T1，应回落 T5。"""
    contract = _contract("run_analysis_engines")
    assert 1 not in contract.recovery_ladder
    decision = choose_recovery("run_analysis_engines", [_issue("artifact_schema_invalid")], contract=contract, state={})
    assert decision.tier is RecoveryTier.TIER_5_ESCALATE


def test_action_node_only_allows_tier_0_or_5():
    """§9.4「行动类输出永不自动执行」：resolve_midterm_decision 阶梯 (0,5)。"""
    contract = _contract("resolve_midterm_decision")
    assert set(contract.recovery_ladder) <= {0, 5}
    decision = choose_recovery("resolve_midterm_decision", [_issue("derived_facts_empty")], contract=contract, state={})
    assert decision.tier is RecoveryTier.TIER_5_ESCALATE


# ── 只读性与确定性 ──────────────────────────────────────────────────────────


def test_choose_recovery_is_read_only():
    state = {"report_review_round": 0, "max_report_review_rounds": 2}
    issues = [_issue("thesis_gap")]
    state_snapshot = copy.deepcopy(state)
    issues_snapshot = [issue.model_dump(mode="json") for issue in issues]

    choose_recovery("generate_report", issues, contract=_contract("generate_report"), state=state)

    assert state == state_snapshot, "choose_recovery 修改了入参 state"
    assert [issue.model_dump(mode="json") for issue in issues] == issues_snapshot


def test_choose_recovery_is_deterministic():
    contract = _contract("generate_report")
    state = {"report_review_round": 1, "max_report_review_rounds": 2}
    issues = [_issue("thesis_gap")]
    first = choose_recovery("generate_report", issues, contract=contract, state=state)
    second = choose_recovery("generate_report", issues, contract=contract, state=state)
    assert first == second


@pytest.mark.parametrize("tier_value", [0, 1, 2, 3, 4, 5])
def test_every_tier_is_reachable_via_some_branch(tier_value: int):
    """6 个档位都必须至少有可达分支（防某档成为死代码）。"""
    reachable = _reachable_tiers()
    assert tier_value in reachable


def _reachable_tiers() -> set[int]:
    reached: set[int] = set()
    reached.add(
        int(choose_recovery("run_analysis_engines", [], contract=_contract("run_analysis_engines"), state={}).tier)
    )
    reached.add(
        int(
            choose_recovery(
                "run_thesis", [_issue("artifact_schema_invalid")], contract=_contract("run_thesis"), state={}
            ).tier
        )
    )
    reached.add(
        int(
            choose_recovery(
                "run_analysis_engines",
                [_issue("derived_facts_empty")],
                contract=_contract("run_analysis_engines"),
                state={},
            ).tier
        )
    )
    reached.add(
        int(
            choose_recovery(
                "run_analysis_engines",
                [_issue("industry_context_missing")],
                contract=_contract("run_analysis_engines"),
                state={},
            ).tier
        )
    )
    reached.add(
        int(
            choose_recovery(
                "generate_report",
                [_issue("thesis_gap")],
                contract=_contract("generate_report"),
                state={"report_review_round": 0},
            ).tier
        )
    )
    reached.add(
        int(
            choose_recovery(
                "run_analysis_engines", [_issue("mystery")], contract=_contract("run_analysis_engines"), state={}
            ).tier
        )
    )
    return reached


# ── cost_exposure（§10.1） ─────────────────────────────────────────────────


def test_cost_exposure_weights_and_amplification():
    base = cost_exposure(_issue("x", severity=IssueSeverity.LOW))
    assert base == 1
    amplified = cost_exposure(_issue("x", severity=IssueSeverity.LOW, amplified_by=["insight->thesis"]))
    assert amplified == 2  # 1 × (1+1)


def test_cost_exposure_is_zero_when_already_recovered():
    issue = _issue("x", severity=IssueSeverity.CRITICAL, recovery_action="degraded_tier=2")
    assert cost_exposure(issue) == 0


def test_recovered_critical_does_not_trigger_direct_escalation():
    """已恢复的 critical 敞口≈0 → 不走第 2 条直升级分支（落到阶梯裁决）。"""
    issue = _issue("industry_context_missing", severity=IssueSeverity.CRITICAL, recovery_action="skeleton")
    decision = choose_recovery("run_analysis_engines", [issue], contract=_contract("run_analysis_engines"), state={})
    assert decision.tier is RecoveryTier.TIER_3_SKELETON


def test_apply_degradation_writes_both_fields_and_is_tier_gated():
    """F2-1：``apply_degradation`` 是 Tier2/3 的**唯一写入点**，两个字段名同时写。

    正例 + 反例一起钉住（§14.3-A：``degraded=True`` + ``degradation_reason`` 由它统一写，
    避免各节点手写字段名漂移）：
    * ``tier>=2``（Tier2 降级产出 / Tier3 骨架）→ ``degraded=True``；
    * ``tier<2``（Tier0 完整通过 / Tier1 局部修复）→ ``degraded=False``：
      T1 是**确定性修补**，产物仍是完整语义，不得被下游阻尼（``degraded_inputs``）误伤；
    * 任何档位下**两个字段名都必须同时出现**（单独写一个就是漂移）。
    """
    for tier in (2, 3):
        marked = apply_degradation({"core_view": "x"}, tier, f"降级-{tier}")
        assert marked[DEGRADATION_FLAG] is True, f"tier={tier} 必须标记降级"
        assert marked[DEGRADATION_REASON_FIELD] == f"降级-{tier}"

    for tier in (0, 1):
        unmarked = apply_degradation({"core_view": "x"}, tier, "局部修补")
        assert unmarked[DEGRADATION_FLAG] is False, f"tier={tier} 不得标记降级（T1 是修补而非降级）"
        assert unmarked[DEGRADATION_REASON_FIELD] == "局部修补", "字段名必须同时存在（防漂移）"

    assert DEGRADATION_FLAG == "degraded" and DEGRADATION_REASON_FIELD == "degradation_reason"


def test_apply_degradation_returns_new_mapping_and_never_mutates_input():
    """F2-1：调用方无需防副作用 —— 返回新 dict、入参不被变异、非 mapping 也不抛异常。"""
    original = {"core_view": "x", DEGRADATION_FLAG: False, DEGRADATION_REASON_FIELD: ""}
    snapshot = dict(original)

    out = apply_degradation(original, 2, "确定性兜底")

    assert out is not original, "必须返回新 dict（不得返回入参本身）"
    assert original == snapshot, "入参被原地修改（禁止变异）"
    assert out[DEGRADATION_FLAG] is True and out[DEGRADATION_REASON_FIELD] == "确定性兜底"

    # 非 mapping（标量 / 字符串 / pydantic 模型等）没有可写 metadata 面：不抛异常、原值不丢
    raw = apply_degradation("raw-text", 2, "解析失败")
    assert raw[DEGRADATION_FLAG] is True
    assert raw[DEGRADATION_REASON_FIELD] == "解析失败"
    assert raw["value"] == "raw-text"


def test_budget_exhausted_branch_carries_machine_readable_issue_category():
    """F2-2：回环预算耗尽必须**机器可判**（不能只把"耗尽"写在自然语言 reason 里）。"""
    contract = _contract("generate_report")
    state = {"report_review_round": 2, "max_report_review_rounds": 2}

    decision = choose_recovery("generate_report", [_issue("thesis_gap")], contract=contract, state=state)

    assert decision.tier is RecoveryTier.TIER_5_ESCALATE
    assert decision.issue_category == BUDGET_EXHAUSTED_ISSUE_CATEGORY == "budget_exhausted"
    assert decision.cost == 5  # 可直接写入 Issue.recovery_cost


def test_non_budget_escalations_carry_no_issue_category():
    """F2-2 反例：只有"回环预算耗尽"带机读 category；其余档位一律 ``None``。

    ``None`` 的语义 = "按调用方自身节点语义决定落库口径"，因此**任何**非预算分支都不得带
    该标记（否则会被上游/账本误当成 D5 控制偏离）。
    """
    contract_gen = _contract("generate_report")
    contract_eng = _contract("run_analysis_engines")
    branches = {
        "T0 无偏离": choose_recovery("generate_report", [], contract=contract_gen, state={}),
        "T2 数据缺口": choose_recovery(
            "run_analysis_engines", [_issue("derived_facts_empty")], contract=contract_eng, state={}
        ),
        "T3 核心输入缺失": choose_recovery(
            "run_analysis_engines", [_issue("industry_context_missing")], contract=contract_eng, state={}
        ),
        "T4 预算未耗尽": choose_recovery(
            "generate_report",
            [_issue("thesis_gap")],
            contract=contract_gen,
            state={"report_review_round": 0, "max_report_review_rounds": 2},
        ),
        "T5 critical 短路": choose_recovery(
            "run_analysis_engines",
            [_issue("blocked", severity=IssueSeverity.CRITICAL)],
            contract=contract_eng,
            state={},
        ),
        "T5 阶梯无可修档": choose_recovery(
            "run_analysis_engines", [_issue("mystery")], contract=contract_eng, state={}
        ),
    }

    tagged = {name: decision.tier for name, decision in branches.items() if decision.issue_category is not None}
    assert tagged == {}, f"非预算分支带了预算标记：{tagged}"
    assert branches["T4 预算未耗尽"].tier is RecoveryTier.TIER_4_RERUN
    assert branches["T4 预算未耗尽"].action == "rerun_round=1"


@pytest.mark.parametrize("round_no", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("max_rounds", [0, 1, 2, 3, 5])
@pytest.mark.parametrize("rewrite_needed", [True, False])
def test_report_route_equivalent_to_legacy_implicit_condition(round_no, max_rounds, rewrite_needed):
    """F2-3 等价性（50 格参数化）：新裁决 ≡ 旧内联判据的**单个格**。

    旧判据（F2 之前 ``route_after_report_review`` 的内联写法）：
    ``state["report_rewrite_needed"] and state["report_review_round"] < state["max_report_review_rounds"]``
    —— 本用例把它的**文本**重写成 ``_legacy_report_route`` 并与真实路由函数逐格比对，
    因此任何"顺手改条件"的改动都会在某一格爆红（而不是静默改变回环次数）。

    **前提（防止后人误读为端到端已验证）**：report gate 回环在 ``orchestrator/agent.py`` 里
    目前仍是**注释态**，故本用例验证的是"统一裁决与旧隐式判断**行为等价**"，
    **不是**"整图回环已在线跑过"。
    """
    partial = {
        "report_rewrite_needed": rewrite_needed,
        "report_review_round": round_no,
        "max_report_review_rounds": max_rounds,
    }
    assert route_after_report_review(partial) == _legacy_report_route(partial)  # type: ignore[arg-type]
    # 空 state（旧判据的缺省路径）同样等价
    assert route_after_report_review({}) == _legacy_report_route({})  # type: ignore[arg-type]


def test_report_route_falls_back_to_baseline_predicate_when_recovery_switch_off(monkeypatch):
    """F2-5：关 ``deviation.recovery.enabled``（§14.8 PR5 回滚）⇒ **回落 pre-F2 基线谓词**。

    修正说明（t51）：本用例此前断言"关闭 ⇒ 一律 `finalize_message`"，那**不等于** pre-F2：
    基线谓词是 ``rewrite_needed and used < limit``，在预算尚存时（即 route 看到的
    ``written < limit``）**本来就允许回环**。若关闭开关即禁止回环，报告重写会被整体禁用
    ⇒ 回滚开关变成行为变更，与 §14.8 的回滚语义冲突。

    **口径**：route 读的是 gate **写回后**的 ``report_review_round``（= ``written``）；本用例
    因此走"gate → 归并 state → route"的真实链路（与 :func:`test_gate_and_route_bilateral_consistency`
    同口径），并用 pre-F2 判据 ``written < limit`` 逐格对照。**关闭开关 = 纯回滚、零新增行为**：
    回环能力保留、``RunStatus`` 与 pre-F2 一致、**不落** D5 偏离记录（pre-F2 也没有该记录）。
    """
    for round_no, max_rounds, expect_route, expect_status in (
        (0, 2, "generate_report", RunStatus.RUNNING),  # written=1 < 2 ⇒ 回环侧
        (1, 2, "finalize_message", RunStatus.PARTIAL),  # written=2 = 2 ⇒ 边界收口
        (2, 2, "finalize_message", RunStatus.PARTIAL),  # written=3 > 2 ⇒ 越界收口
    ):
        _disable_recovery(monkeypatch)
        state = _report_gate_state(round_no=round_no, max_rounds=max_rounds)

        result = asyncio.run(review_report(state, {}))
        merged = {**state, **result}

        assert route_after_report_review(merged) == expect_route, f"k={round_no}/{max_rounds}"
        assert result["run"].status is expect_status, f"k={round_no}/{max_rounds}"
        assert not [i for i in result["issues"] if i.category == BUDGET_EXHAUSTED_ISSUE_CATEGORY], (
            "关闭开关时不得新增 D5 记录（纯回滚）"
        )
        # 与 pre-F2 判据逐格对照：route 回环 ⟺ written < limit
        assert (route_after_report_review(merged) == "generate_report") == (result["report_review_round"] < max_rounds)

    # 纯路由分支：不需要重写 ⇒ 即使关闭开关也不回环
    _disable_recovery(monkeypatch)
    assert (
        route_after_report_review(
            {"report_rewrite_needed": False, "report_review_round": 0, "max_report_review_rounds": 2}  # type: ignore[arg-type]
        )
        == "finalize_message"
    )


def test_rerun_budget_check_sentinel_is_not_a_registered_category():
    """★ 哨兵不落库：``RERUN_BUDGET_CHECK_CATEGORY`` 只是路由触发值。

    背景：``choose_recovery`` 的第 1 条分支是"无 issue → T0（keep）"，而"还能不能再跑一轮"
    是纯预算查询 ⇒ 调用方必须带一条不命中 T1/T2/T3 类目的触发偏离。该哨兵**不得**被登记进
    ``CLASS_BY_CATEGORY``：一旦登记，它就会参与 F0 的分类守卫与 §11/F5 的 per-class 画像，
    把一个内部触发值污染成"真实偏离类目"。
    """
    from alphabee.orchestrator.services.deviation import CLASS_BY_CATEGORY

    assert RERUN_BUDGET_CHECK_CATEGORY == "rerun_budget_check"
    assert RERUN_BUDGET_CHECK_CATEGORY not in CLASS_BY_CATEGORY, "哨兵被误登记进分类表"
    assert BUDGET_EXHAUSTED_ISSUE_CATEGORY in CLASS_BY_CATEGORY, "预算耗尽应登记为 D5（对照组）"
    assert CLASS_BY_CATEGORY[BUDGET_EXHAUSTED_ISSUE_CATEGORY].value == "d5_control"


# ── T51：F2-4 双侧一致性（gate ↔ route 同快照）+ F2-5 回滚语义 ──────────────


def _report_gate_state(*, round_no: int, max_rounds: int) -> dict:
    """report gate 的**必然过不了**的最小 state（缺章节 ⇒ 产 blocking_issues ⇒ 需要重写）。

    ``run`` 用 :class:`Run` 实例（与 ``test_report_review_gate.py`` 同形态），
    便于直接断言 ``run.status``。
    """
    return {
        "run": Run(id="run-1", goal="g", status=RunStatus.RUNNING, context={}),
        "steps": [],
        "artifacts": [
            Artifact(
                id="artifact-report",
                type="report",
                producer_step="generate_report",
                value={
                    "title": "t",
                    "sections": {"executive_summary": "总结", "risks": ""},
                    "summary": "总体判断",
                    "risk_count": {"high": 1},
                    "overall_confidence": "high",
                    "disclosed_issue_ids": [],
                },
            )
        ],
        "observations": [],
        "issues": [],
        "decisions": [],
        "final_artifact_id": "artifact-report",
        "report_review_round": round_no,
        "max_report_review_rounds": max_rounds,
        "llm_review": False,
    }


def _disable_recovery(monkeypatch) -> None:
    """把 ``deviation.recovery.enabled`` 置 False（§14.8 PR5 回滚模拟）。"""
    import alphabee.config as config_mod

    class _Off:
        deviation = type("D", (), {"recovery": type("R", (), {"enabled": False})()})()

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Off())


def test_report_rerun_decision_aligns_gate_and_route_on_k1_boundary():
    """★ F2-4（k=1 边界）：gate 与 route 必须**同快照同判**，且预算耗尽时 run 要置 PARTIAL。

    边界为何关键：``max_report_review_rounds=2`` 时，第 2 次进 gate（``report_review_round=1``）
    正是"本轮写回后总轮次 = 2 = 上限"的那一格。修复前 gate 传**自增前**值 ⇒ 判"预算未耗尽"
    （``rerun_possible=True`` ⇒ 不置 PARTIAL），而 route 读**写回后**值 ⇒ 判 escalate（不回环）
    ⇒ 出现"仍需重写、预算已耗尽、实际没回环，但 run 不标 PARTIAL"的**基线回归**。
    现在 gate 传自增后的总轮次（== route 读到的值）⇒ 两侧必然同判。
    """
    state = _report_gate_state(round_no=1, max_rounds=2)

    result = asyncio.run(review_report(state, {}))
    route_decision = route_after_report_review({**state, **result})

    assert result["report_rewrite_needed"] is True
    assert route_decision == "finalize_message", "route 判定预算耗尽 ⇒ 不回环"
    assert result["run"].status is RunStatus.PARTIAL, "预算耗尽且未回环 ⇒ 必须置 PARTIAL（pre-F2 亦然）"
    assert [issue for issue in result["issues"] if issue.category == BUDGET_EXHAUSTED_ISSUE_CATEGORY], (
        "拒绝回环必须同时落一条 D5（不得无记录收口）"
    )
    assert route_decision == ("generate_report" if result["run"].status is RunStatus.RUNNING else "finalize_message")


@pytest.mark.parametrize("round_no", [0, 1, 2, 3])
@pytest.mark.parametrize("max_rounds", [1, 2, 3])
def test_gate_and_route_bilateral_consistency(round_no, max_rounds):
    """★ F2-4 双侧一致性矩阵（12 格）：gate↔route 同判 + 三不变量 + 回环优先。

    t51 新增（t50 的 50 格矩阵只覆盖"旧谓词 vs 新谓词"，**没有**覆盖 gate↔route 之间的一致性，
    这正是 F2-4 漏网的原因）。每格断言三条等价命题：
    ① route 回环 ⟺ 写回后的总轮次 < 上限（= pre-F2 判据）；
    ② D5 预算偏离出现 ⟺ route 拒绝回环；
    ③ ``RunStatus.PARTIAL`` ⟺ route 拒绝回环（即 gate 的 PARTIAL 与 route 的路由同源）。
    另断言预算尚存时必须**真的回环**（不许"有预算也不回环"的静默保守化）。
    """
    state = _report_gate_state(round_no=round_no, max_rounds=max_rounds)

    result = asyncio.run(review_report(state, {}))
    merged = {**state, **result}  # 模拟 LangGraph 把 partial update 归并回 state
    loops = route_after_report_review(merged) == "generate_report"
    budget_issue = [issue for issue in result["issues"] if issue.category == BUDGET_EXHAUSTED_ISSUE_CATEGORY]

    assert result["report_rewrite_needed"] is True
    assert loops == (result["report_review_round"] < max_rounds), "① route 与 pre-F2 判据不等价"
    assert bool(budget_issue) == (not loops), "② D5 与拒绝回环必须同真同假"
    assert (result["run"].status is RunStatus.PARTIAL) == (not loops), "③ PARTIAL 与拒绝回环必须同真同假"
    if loops:
        assert result["report_review_round"] < max_rounds, "有预算却不回环：静默保守化"


def test_gate_baseline_path_adds_no_deviation_record_when_recovery_switch_off(monkeypatch):
    """开关关闭（``deviation.recovery.enabled=False``）⇒ **纯回滚到 pre-F2**（t55 定稿的 (B) 方案）。

    口径三件套（与 ``gates._baseline_rerun_decision`` 与 ``DeviationRecoverySettings`` 的
    docstring 同源，见 §14.8 PR5）：
    ① **回环能力保留** —— route 看到的 ``written < limit`` 时照样回环（pre-F2 行为）；
    ② ``RunStatus`` 与 pre-F2 一致 —— "要重写却没回环" ⇒ ``PARTIAL``；
    ③ **不新增** D5 ``budget_exhausted`` —— D5 生产者本身是 F2 新增物，关掉 F2 开关却新增记录
       就不构成回滚（「残留 issue 无害」指既有记录无需清理，不是"回滚必须新增记录"）。

    两格都验（max=2）：``k=0`` ⇒ ``written=1 < 2`` 回环 + ``RUNNING``；``k=1``（= max−1）⇒
    ``written=2 = 2`` 收口 + ``PARTIAL``。
    """
    for round_no, max_rounds, expect_route, expect_status in (
        # 口径：比较对象是 **route 看到的写回后轮次**（written = round_no+1）
        # 与上限：written < max ⇒ 回环。max=2 时 ⇒ k=0 回环、k>=1 收口。
        (0, 2, "generate_report", RunStatus.RUNNING),
        (1, 2, "finalize_message", RunStatus.PARTIAL),
        (2, 2, "finalize_message", RunStatus.PARTIAL),
    ):
        _disable_recovery(monkeypatch)
        state = _report_gate_state(round_no=round_no, max_rounds=max_rounds)

        result = asyncio.run(review_report(state, {}))

        assert route_after_report_review({**state, **result}) == expect_route, f"k={round_no}/{max_rounds}"
        assert result["run"].status is expect_status, f"k={round_no}/{max_rounds}"
        budget_issue = [issue for issue in result["issues"] if issue.category == BUDGET_EXHAUSTED_ISSUE_CATEGORY]
        assert budget_issue == [], f"关闭开关时不得新增 D5 记录（k={round_no}/{max_rounds}）"
        assert any(issue.category == "report_rewrite_needed" for issue in result["issues"]), (
            "pre-F2 的 report_rewrite_needed 记录仍应存在"
        )


def test_gate_still_loops_with_budget_when_recovery_switch_off(monkeypatch):
    """F2-5：关闭开关**不等于**禁止回环 —— k=0/max=2 时基线谓词本就允许回环（pre-F2 行为）。"""
    _disable_recovery(monkeypatch)
    state = _report_gate_state(round_no=0, max_rounds=2)

    result = asyncio.run(review_report(state, {}))

    assert route_after_report_review({**state, **result}) == "generate_report"
    assert result["run"].status is RunStatus.RUNNING
    assert [issue for issue in result["issues"] if issue.category == BUDGET_EXHAUSTED_ISSUE_CATEGORY] == []
