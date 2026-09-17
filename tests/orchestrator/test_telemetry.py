"""F5 单测：偏离度量层 + 只读时间线视图（§14.7 F5 行 / §11.1 / §14.5-B / §15 验收 1）。

覆盖：
- ``DeviationMetrics`` 八字段与 §14.5-B **逐字一致**（名称 + 数值项 ``float | None`` + 曲线 ``list[float]``）；
- 八项指标：**每项一条正例 + 一条分母为 0/缺失 → ``None``**（含合成账本的整体口径值）；
- 检测率/静默劣化率的"节点级 vs 兜底发现上游遗留"判据（节点序差 ≤0 / >0）；
- 恢复半衰期 = 检测到 resolved 的**节点数中位数**；在轨曲线随**节点序**；
- ``store_metrics``：同库新表 ``deviation_metrics``（只 create_all 新表）、按 run_id 幂等 upsert、
  开关关闭 → **零写入**（双侧）、DB 不可用 → 只 warning 不抛（fail-open）；
- ``render_deviation_timeline``：从账本重建「节点序 × 偏离 × 检测时延 × 恢复动作」、两次渲染逐字节相同、
  无数据 → 确定性空视图而非异常（含"账本读取打桩后抛异常"这一层 fail-open 边界）；
- run 尾部节点接线：写入账本后落库指标、既有 5 个 key 语义不变、不变式
  ``written + failed + skipped == issue_count`` 保持、度量层异常 fail-open；
- CLI ``--deviations``：``parse_args`` 真行为（按文件路径加载，避开包 ``__init__`` 的重 import 链）
  + ``main()`` 分派的结构钉住（AST，含"必须早于 chat/query 分支"的顺序断言）；
- 模块级**不读配置**、零 LLM（§14.6 / §16 反模式）。

注意：本文件**不 import** ``alphabee.apps.cli`` 包（``__init__`` 会 import ``main`` → orchestrator →
``tushare.set_token`` 写 ``$HOME/tk.csv`` 并可能联网，实测不可用），改为按文件路径加载单个模块。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

import alphabee.config as config_mod
import alphabee.data_fetch.database as db_mod
import alphabee.data_fetch.deviation_store as store_mod
import alphabee.orchestrator.services.telemetry as telemetry
from alphabee.core.schemas import (
    Decision,
    DeviationClass,
    Issue,
    IssueScope,
    IssueSeverity,
    IssueStatus,
    Run,
    RunStatus,
    Step,
)
from alphabee.data_fetch.deviation_store import record_event
from alphabee.data_fetch.models import Base, DeviationEvent, DeviationMetric
from alphabee.orchestrator.nodes import record_deviations as sink_mod
from alphabee.orchestrator.services.deviation import NODE_INDEX

REPO_ROOT = Path(__file__).resolve().parents[2]

#: §14.5-B 的八字段（顺序即文档顺序）。
_METRIC_FIELDS = (
    "detection_rate",
    "mean_detection_latency",
    "recovery_rate",
    "recovery_half_life",
    "amplification_overturn_rate",
    "on_track_curve",
    "budget_consumption",
    "silent_degradation_rate",
)

#: ``deviation_metrics`` 表列（表结构契约）。
_METRIC_COLUMNS = (
    "metric_id",
    "run_id",
    *_METRIC_FIELDS,
    "created_at",
    "updated_at",
)


# ── fixtures / helpers ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_ledger_db(monkeypatch):
    """每个用例一个独立 SQLite 文件（复用失败库的 ``DATA_FETCH_DB_PATH`` 覆盖口）。

    autouse 是**安全措施**：保证任何用例都不可能写到开发机的真实 ``fetch_events.db``。
    """
    db_mod.reset_db()
    store_mod.reset_init_cache()

    descriptor, path = tempfile.mkstemp(suffix=".db")
    os.close(descriptor)
    monkeypatch.setenv("DATA_FETCH_DB_PATH", path)

    yield

    db_mod.reset_db()
    store_mod.reset_init_cache()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def no_ambient_deviation_config(monkeypatch):
    """把 ``get_settings`` 换成"无 deviation 段"的替身：指标口径不依赖开发机 ``config.yaml``。

    需要别的配置的用例在自己内部再 patch 一次（覆盖本 fixture 的 patch）。
    """

    class _Settings:
        deviation = None

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())


def _event(
    *,
    step_id: str | None = "collect_raw_facts",
    detected_at_step: str | None = "collect_raw_facts",
    deviation_class: str = DeviationClass.D1_DATA.value,
    category: str = "missing_data",
    severity: str = "high",
    resolved: bool = False,
    recovery_action: str | None = None,
    amplified_by: list[str] | None = None,
    fingerprint: str = "fp-0001",
) -> DeviationEvent:
    """内存里的账本行（不落库）：指标计算是纯函数，故多数用例无需 DB。"""
    return DeviationEvent(
        run_id="run-1",
        symbol="600519.SH",
        step_id=step_id,
        detected_at_step=detected_at_step,
        deviation_class=deviation_class,
        category=category,
        severity=severity,
        fingerprint=fingerprint,
        occurrence_count=1,
        resolved=resolved,
        recovery_action=recovery_action,
        amplified_by=list(amplified_by or []),
    )


def _state(
    *,
    nodes: tuple[str, ...] = ("collect_raw_facts", "run_analysis_engines", "record_deviations"),
    decisions: tuple[Decision, ...] = (),
    issues: tuple[Issue, ...] = (),
    context: dict | None = None,
) -> dict:
    return {
        "run": Run(
            id="run-1",
            goal="t",
            status=RunStatus.RUNNING,
            context={"symbol": "600519.SH", **dict(context or {})},
        ),
        "steps": [Step(id=node, kind=node) for node in nodes],
        "decisions": list(decisions),
        "issues": list(issues),
    }


def _audit_decision(index: int, *, consistent: bool, marker: bool = True, confidence: float | None = None) -> Decision:
    """§8.2 放大审计 Decision（F3 发射点：rationale 标记 + confidence 0.85/0.3 两条线索）。"""
    rationale = f"[放大审计|insight->thesis] 加权方向审计（{'一致' if consistent else '**不一致**'}）：示例"
    if not marker:
        rationale = f"[放大审计|insight->thesis] 加权方向审计（{'一致' if consistent else '不一致'}）：无粗体标记"
    return Decision(
        id=f"decision-{index}",
        maker=telemetry.AMPLIFICATION_AUDIT_MAKER,
        rationale=rationale,
        confidence=0.85 if consistent else 0.3 if confidence is None else confidence,
    )


_issue_counter = iter(range(1000))


def _issue(
    *,
    severity: IssueSeverity = IssueSeverity.HIGH,
    status: IssueStatus = IssueStatus.OPEN,
    recovery_action: str | None = None,
    amplified_by: tuple[str, ...] = (),
    category: str = "missing_data",
) -> Issue:
    return Issue(
        id=f"issue-{next(_issue_counter)}",
        severity=severity,
        category=category,
        message="示例偏离",
        status=status,
        scope=IssueScope.DATA,
        recovery_action=recovery_action,
        amplified_by=list(amplified_by),
    )


def _patch_settings(monkeypatch, *, ledger: bool | None = None, d_max=None, severity_weight=None):
    """把 ``get_settings()`` 换成受控替身（§14.6 的 ``deviation.ledger`` / ``budget``）。"""

    class _Ledger:
        enabled = ledger

    class _Budget:
        def __init__(self):
            if d_max is not None:
                self.d_max = d_max
            if severity_weight is not None:
                self.severity_weight = severity_weight

    class _Deviation:
        def __init__(self):
            if ledger is not None:
                self.ledger = _Ledger()
            self.budget = _Budget()

    class _Settings:
        deviation = _Deviation()

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())


def _metrics() -> telemetry.DeviationMetrics:
    return telemetry.DeviationMetrics(
        detection_rate=0.5,
        mean_detection_latency=2.0,
        recovery_rate=0.25,
        recovery_half_life=3.0,
        amplification_overturn_rate=1.0,
        on_track_curve=[1.0, 0.5],
        budget_consumption=0.125,
        silent_degradation_rate=0.0,
    )


def _metric_row_count(run_id: str) -> int:
    db_mod.init_db()
    session = db_mod.get_session()
    try:
        return int(session.query(DeviationMetric).filter(DeviationMetric.run_id == run_id).count())
    finally:
        session.close()


# ── 契约：八字段逐字一致 ───────────────────────────────────────────────────


def test_metric_fields_match_contract():
    """§14.5-B：八个字段名 + 顺序逐字一致。"""
    assert tuple(telemetry.DeviationMetrics.model_fields) == _METRIC_FIELDS


def test_numeric_fields_are_optional_float_and_curve_is_float_list():
    """数值项 ``float | None``、``on_track_curve`` 为 ``list[float]``。"""
    fields = telemetry.DeviationMetrics.model_fields
    for name in _METRIC_FIELDS:
        if name == "on_track_curve":
            continue
        annotation = str(fields[name].annotation)
        assert "float" in annotation and "None" in annotation, (name, annotation)
        assert fields[name].default is None

    metrics = telemetry.DeviationMetrics(on_track_curve=[0.5])
    assert metrics.on_track_curve == [0.5]
    assert all(isinstance(value, float) for value in metrics.on_track_curve)
    # 未给数值项 → 全部 None（缺失即 None，不是 0）
    for name in _METRIC_FIELDS:
        if name != "on_track_curve":
            assert getattr(metrics, name) is None


def test_metrics_table_columns_match_contract():
    """``deviation_metrics`` 表列 = 八项指标 + 身份/时间戳；与账本表同 Base、同库。"""
    assert DeviationMetric.__tablename__ == "deviation_metrics"
    assert DeviationMetric.metadata is Base.metadata
    assert set(DeviationMetric.__table__.columns.keys()) == set(_METRIC_COLUMNS)
    assert "deviation_events" in Base.metadata.tables
    assert "deviation_metrics" in Base.metadata.tables


def test_metrics_table_is_created_by_init_db_and_coexists_with_ledger():
    db_mod.init_db()
    from sqlalchemy import inspect

    tables = set(inspect(db_mod.get_engine()).get_table_names())
    assert {"deviation_events", "deviation_metrics"} <= tables


# ── ① 检测率 / 平均检测时延 / 静默劣化率 ───────────────────────────────────


def _detection_ledger() -> list[DeviationEvent]:
    """合成账本：1 条节点级（时延 0）+ 2 条兜底发现上游遗留（时延 1 / 13）+ 1 条节点序未知。"""
    return [
        _event(fingerprint="fp-node", category="missing_data"),
        _event(
            step_id="run_thesis",
            detected_at_step="review_thesis",
            deviation_class=DeviationClass.D3_ARGUMENT.value,
            category="amplification_direction_conflict",
            fingerprint="fp-gate-1",
        ),
        _event(
            step_id="collect_raw_facts",
            detected_at_step="review_report",
            deviation_class=DeviationClass.D2_STRUCTURE.value,
            category="insight_degraded",
            fingerprint="fp-gate-2",
        ),
        _event(step_id=None, detected_at_step="review_report", category="stale", fingerprint="fp-unknown"),
    ]


def test_detection_rate_counts_node_level_over_gate_fallback():
    """§11.1：检测率 = 节点级发现 ÷（节点级发现 + 兜底发现的上游遗留）= 1 / 3。"""
    metrics = telemetry.compute_deviation_metrics(_state(), _detection_ledger())
    assert metrics.detection_rate == 0.3333


def test_detection_rate_is_one_when_all_found_at_their_own_node():
    ledger = [_event(fingerprint="a"), _event(fingerprint="b", category="blocked")]
    assert telemetry.compute_deviation_metrics(_state(), ledger).detection_rate == 1.0


@pytest.mark.parametrize("ledger", [[], [_event(step_id=None, detected_at_step=None)]])
def test_detection_rate_none_when_denominator_missing(ledger):
    """无已知节点序差 / 空账本 → ``None``（**不静默回退 0**）。"""
    assert telemetry.compute_deviation_metrics(_state(), ledger).detection_rate is None


def test_mean_detection_latency_is_mean_of_known_latencies():
    """平均检测时延 = (0 + 1 + 13) / 3（未知节点序的事件不计入）。"""
    metrics = telemetry.compute_deviation_metrics(_state(), _detection_ledger())
    assert metrics.mean_detection_latency == 4.6667


@pytest.mark.parametrize("ledger", [[], [_event(step_id=None, detected_at_step=None)]])
def test_mean_detection_latency_none_without_known_latency(ledger):
    assert telemetry.compute_deviation_metrics(_state(), ledger).mean_detection_latency is None


def test_silent_degradation_rate_counts_gate_found_d1_d2_only():
    """兜底发现的 D1/D2 ÷ 全部 = 1 / 4（节点级 D1 与 D3 都不计）。"""
    metrics = telemetry.compute_deviation_metrics(_state(), _detection_ledger())
    assert metrics.silent_degradation_rate == 0.25


def test_silent_degradation_rate_ignores_node_level_d1_and_gate_found_d3():
    ledger = [
        _event(fingerprint="n", deviation_class=DeviationClass.D1_DATA.value),
        _event(
            fingerprint="g",
            step_id="run_thesis",
            detected_at_step="review_thesis",
            deviation_class=DeviationClass.D3_ARGUMENT.value,
        ),
    ]
    assert telemetry.compute_deviation_metrics(_state(), ledger).silent_degradation_rate == 0.0


def test_silent_degradation_rate_none_when_ledger_empty():
    assert telemetry.compute_deviation_metrics(_state(), []).silent_degradation_rate is None


# ── ② 恢复率 / 恢复半衰期 ─────────────────────────────────────────────────


def _recovery_ledger() -> list[DeviationEvent]:
    return [
        # ① resolved + 有 action → 计入分子
        _event(fingerprint="fp-1", resolved=True, recovery_action="rerun_round=1"),
        # ② resolved 但无 action → 不计入分子
        _event(
            fingerprint="fp-2",
            detected_at_step="review_thesis",
            step_id="run_thesis",
            deviation_class=DeviationClass.D3_ARGUMENT.value,
            resolved=True,
        ),
        # ③ 有 action 但未 resolved → 不计入分子
        _event(fingerprint="fp-3", category="blocked", recovery_action="degraded_tier=2"),
        # ④ 未 resolved、无 action
        _event(fingerprint="fp-4", category="stale"),
    ]


def test_recovery_rate_counts_resolved_with_action_over_all_rows():
    """§11.1：恢复率 = "有 recovery_action 且 resolved" ÷ 全部 = 1 / 4。"""
    assert telemetry.compute_deviation_metrics(_state(), _recovery_ledger()).recovery_rate == 0.25


@pytest.mark.parametrize("ledger", [[], [_event(resolved=True)]])
def test_recovery_rate_none_when_denominator_missing(ledger):
    """空账本 → ``None``；"resolved 但无 action" 也不足以构成分子（分母为 0 时同理）。"""
    metrics = telemetry.compute_deviation_metrics(_state(), ledger)
    assert metrics.recovery_rate == (None if not ledger else 0.0)


def test_recovery_half_life_is_median_node_distance_from_detection_to_resolution():
    """恢复半衰期 = 各 resolved 行的「末位节点序 − 检测端节点序」**中位数**。

    末位节点 = ``record_deviations``（序 14）；检测端 = ``collect_raw_facts``(0) / ``run_thesis``(8) /
    ``review_thesis``(9) ⇒ 跨度 {14, 6, 5} ⇒ 中位数 **6.0**；未 resolved 的行**不参与**。

    这里刻意取 3 条跨度（奇数、且**不对称**）而非 2 条：2 条时中位数与均值恒等（如 {14, 5} → 9.5 / 9.5），
    "median → mean" 变异会**存活**；本用例下均值 = 8.3333 ≠ 6.0 ⇒ 该变异必被杀死。
    """
    state = _state(nodes=("collect_raw_facts", "run_analysis_engines", "record_deviations"))
    ledger = [
        _event(fingerprint="fp-hl-1", resolved=True),  # 检测端 0 → 跨度 14
        _event(fingerprint="fp-hl-2", step_id="run_analysis_engines", detected_at_step="run_thesis", resolved=True),
        _event(fingerprint="fp-hl-3", step_id="run_thesis", detected_at_step="review_thesis", resolved=True),
        _event(fingerprint="fp-hl-4", detected_at_step="review_report"),  # 未 resolved → 不参与
    ]
    metrics = telemetry.compute_deviation_metrics(state, ledger)
    assert NODE_INDEX["record_deviations"] == 14  # 口径锚点（NODE_ORDER 由 F0 冻结）
    assert metrics.recovery_half_life == 6.0  # 中位数（均值会是 8.3333）


def test_recovery_half_life_ignores_unresolved_rows():
    state = _state()
    ledger = [
        _event(fingerprint="fp-r", resolved=True),
        _event(fingerprint="fp-nr", detected_at_step="review_report"),  # 未 resolved → 忽略
    ]
    metrics = telemetry.compute_deviation_metrics(state, ledger)
    assert metrics.recovery_half_life == 14.0  # 只算 resolved 那条：14 − 0


@pytest.mark.parametrize(
    ("ledger", "state"),
    [
        ([], _state()),  # 无 resolved 行
        ([_event(resolved=True)], {"run": None, "steps": []}),  # 无已执行节点
        ([_event(resolved=True, detected_at_step="not_a_node")], _state()),  # 检测端节点序未知
    ],
)
def test_recovery_half_life_none_when_undefined(ledger, state):
    assert telemetry.compute_deviation_metrics(state, ledger).recovery_half_life is None


# ── ③ 加权边推翻率 ─────────────────────────────────────────────────────────


def test_amplification_overturn_rate_is_share_of_inconsistent_audits():
    """§8.2：amplification_audit Decision 中"方向不一致"占比 = 1 / 2。"""
    state = _state(decisions=(_audit_decision(0, consistent=True), _audit_decision(1, consistent=False)))
    assert telemetry.compute_deviation_metrics(state, []).amplification_overturn_rate == 0.5


@pytest.mark.parametrize(
    ("marker", "confidence"),
    [(True, None), (False, None), (True, 0.3), (False, 0.3)],
)
def test_amplification_overturn_detected_by_marker_or_confidence(marker, confidence):
    """两条独立线索（rationale 粗体标记 / confidence ≤ 0.3）任一命中即判"方向不一致"。"""
    state = _state(
        decisions=(
            _audit_decision(0, consistent=False, marker=marker, confidence=confidence),
            _audit_decision(1, consistent=True),
        )
    )
    assert telemetry.compute_deviation_metrics(state, []).amplification_overturn_rate == 0.5


def test_amplification_overturn_rate_none_without_audit_decisions():
    """无 ``amplification_audit`` Decision（含只有别的 maker）→ ``None``。"""
    assert telemetry.compute_deviation_metrics(_state(), []).amplification_overturn_rate is None
    other = Decision(id="d-other", maker="thesis_reviewer", rationale="维度 verdict", confidence=0.9)
    assert telemetry.compute_deviation_metrics(_state(decisions=(other,)), []).amplification_overturn_rate is None


def test_amplification_overturn_rate_zero_when_all_consistent():
    state = _state(decisions=(_audit_decision(0, consistent=True), _audit_decision(1, consistent=True)))
    assert telemetry.compute_deviation_metrics(state, []).amplification_overturn_rate == 0.0


# ── ④ 在轨曲线 ────────────────────────────────────────────────────────────


def test_on_track_curve_follows_node_order():
    """P(节点产出无 D1–D5 偏离) 随节点序：collect_raw_facts 产出过偏离 ⇒ 4 节点曲线如下。"""
    state = _state(nodes=("collect_raw_facts", "resolve_industry_context", "run_analysis_engines", "record_deviations"))
    ledger = [_event(fingerprint="fp-a"), _event(fingerprint="fp-b", category="stale")]
    metrics = telemetry.compute_deviation_metrics(state, ledger)
    assert metrics.on_track_curve == [0.0, 0.5, 0.6667, 0.75]


def test_on_track_curve_ignores_ledger_order_and_unknown_producers():
    """曲线只由"产出地节点"决定：账本顺序无关；产出地未知的事件不冤枉任何节点。"""
    state = _state(nodes=("collect_raw_facts", "run_analysis_engines"))
    produced = _event(step_id="run_analysis_engines", fingerprint="fp-engines")
    unknown = _event(step_id="not_a_node", fingerprint="fp-unknown")
    ordered = telemetry.compute_deviation_metrics(state, [produced, unknown])
    reversed_ = telemetry.compute_deviation_metrics(
        state, [_event(step_id="not_a_node", fingerprint="fp-unknown"), _event(step_id="run_analysis_engines")]
    )
    assert ordered.on_track_curve == reversed_.on_track_curve == [1.0, 0.5]
    assert telemetry.compute_deviation_metrics(state, [unknown]).on_track_curve == [1.0, 1.0]


def test_on_track_curve_empty_without_executed_nodes():
    """无已执行节点 → ``[]``（不伪造曲线；也不是 ``None``）。"""
    assert telemetry.compute_deviation_metrics({"run": None, "steps": []}, []).on_track_curve == []


# ── ⑤ 预算消耗 ────────────────────────────────────────────────────────────


def test_budget_consumption_uses_recovery_cost_exposure():
    """§10.2：D_cum = Σ cost_exposure(未 resolved issue) = 10（high，无恢复动作），D_max = 40。"""
    state = _state(
        issues=(
            _issue(),  # high → 10 × 1 × 1 = 10
            _issue(recovery_action="rerun_round=1"),  # 已恢复 → 敞口 0
            _issue(status=IssueStatus.RESOLVED),  # resolved → 不计入
        ),
        context={telemetry.D_MAX_CONTEXT_KEY: 40},
    )
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption == 0.25


def test_budget_consumption_applies_amplification_factor():
    """放大因子（§10.1）：``amplified_by`` 非空 ⇒ 权重 ×(1+n)。"""
    state = _state(
        issues=(_issue(severity=IssueSeverity.MEDIUM, amplified_by=("insight->thesis", "insight->report")),),
        context={telemetry.D_MAX_CONTEXT_KEY: 9},
    )
    # medium=3 × (1+2) = 9 ⇒ 9 / 9 = 1.0
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption == 1.0


def test_budget_consumption_none_when_limit_missing_or_nonpositive(monkeypatch):
    """``D_max`` 缺失 / 非正 → ``None``（缺失分母不回退 0）。"""
    state = _state(issues=(_issue(),))
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption is None

    zero = _state(issues=(_issue(),), context={telemetry.D_MAX_CONTEXT_KEY: 0})
    assert telemetry.compute_deviation_metrics(zero, []).budget_consumption is None


def test_budget_limit_from_settings_mapping_by_task_kind(monkeypatch):
    """``settings.deviation.budget.d_max`` 为映射时按 ``run.context['task_kind']`` 取键（缺省 analysis）。"""
    _patch_settings(monkeypatch, d_max={"analysis": 100, "tracking": 20})
    tracking = _state(issues=(_issue(),), context={telemetry.TASK_KIND_CONTEXT_KEY: "tracking"})
    assert telemetry.compute_deviation_metrics(tracking, []).budget_consumption == 0.5  # 10 / 20

    default_kind = _state(issues=(_issue(),))
    assert telemetry.compute_deviation_metrics(default_kind, []).budget_consumption == 0.1  # 10 / 100


def test_budget_limit_from_settings_scalar(monkeypatch):
    """标量 ``d_max`` 同样可用（配置形态未定时两条路都不猜）。"""
    _patch_settings(monkeypatch, d_max=50)
    state = _state(issues=(_issue(),))
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption == 0.2


# ── ⑥ F5-D2（t78）：§14.6 的 d_max 落进配置模型 ⇒ 生产路径不再恒 None ───────


def test_budget_settings_default_d_max_matches_section_14_6():
    """§14.6 缺省 ``d_max = {"analysis": 60, "tracking": 20}``；既有两字段默认值一字不改（F2 语义）。"""
    from alphabee.config import DeviationBudgetSettings

    settings = DeviationBudgetSettings()
    assert settings.d_max == {"analysis": 60, "tracking": 20}
    assert settings.severity_weight == {"low": 1, "medium": 3, "high": 10, "critical": 30}
    assert settings.cost_exposure_threshold == 30


@pytest.mark.parametrize("value", [45, None])
def test_budget_settings_tolerates_scalar_and_none_d_max(value):
    """类型放宽（``dict | int | None``）：标量与 None 都被接受，避免配置畸形在 import 期抛错。"""
    from alphabee.config import DeviationBudgetSettings

    assert DeviationBudgetSettings(d_max=value).d_max == value


def test_deviation_settings_constructs_without_deviation_section():
    """import 期安全：整段 ``deviation`` 缺失时也能取到带默认值的 ``DeviationSettings``。"""
    from alphabee.config import DeviationSettings

    deviation = DeviationSettings()
    assert deviation.budget.d_max == {"analysis": 60, "tracking": 20}
    assert deviation.ledger.enabled is True  # 既有五段语义不变


def _default_config_settings():
    """**真实默认配置**（不替换 ``d_max`` 数值）：直接用 ``DeviationSettings()`` 的缺省值。"""
    from alphabee.config import DeviationSettings

    class _Settings:
        deviation = DeviationSettings()

    return _Settings()


def test_budget_consumption_non_none_under_default_config(monkeypatch):
    """★ 本任务核心目标：默认配置下生产路径给出非 None 值 = D_cum / 60 = 0.1667（此前恒 None）。"""
    monkeypatch.setattr(config_mod, "get_settings", _default_config_settings)
    state = _state(issues=(_issue(),))  # high → 10 × 1 × 1 = 10
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption == 0.1667


def test_budget_consumption_uses_tracking_budget_under_default_config(monkeypatch):
    """默认配置按 ``task_kind`` 取键：tracking → 10 / 20 = 0.5。"""
    monkeypatch.setattr(config_mod, "get_settings", _default_config_settings)
    state = _state(issues=(_issue(),), context={telemetry.TASK_KIND_CONTEXT_KEY: "tracking"})
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption == 0.5


@pytest.mark.parametrize("task_kind", [None, "", "   "])
def test_budget_limit_falls_back_to_analysis_key(monkeypatch, task_kind):
    """``task_kind`` 缺失 / 空串 / 全空白 ⇒ 回落 §14.6 的缺省键 ``analysis``（三种写法等价）。"""
    monkeypatch.setattr(config_mod, "get_settings", _default_config_settings)
    context = {} if task_kind is None else {telemetry.TASK_KIND_CONTEXT_KEY: task_kind}
    state = _state(issues=(_issue(),), context=context)
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption == 0.1667


def test_budget_consumption_none_for_unknown_task_kind(monkeypatch):
    """未知任务类型没有配置预算 ⇒ ``None``（不暗中复用 analysis —— 没有配置就没有分母）。"""
    monkeypatch.setattr(config_mod, "get_settings", _default_config_settings)
    state = _state(issues=(_issue(),), context={telemetry.TASK_KIND_CONTEXT_KEY: "unknown_kind"})
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption is None


@pytest.mark.parametrize("bad", [None, {}, object(), [], "not-a-number", {"analysis": "n/a"}])
def test_budget_consumption_none_when_d_max_malformed(monkeypatch, bad):
    """``d_max`` 缺失 / 空映射 / 类型异常 ⇒ ``None``（双侧用例的 None 侧，**绝不静默回退 0**）。"""

    class _Budget:
        d_max = bad

    class _Deviation:
        budget = _Budget()

    class _Settings:
        deviation = _Deviation()

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    state = _state(issues=(_issue(),))
    assert telemetry.compute_deviation_metrics(state, []).budget_consumption is None


def test_example_config_d_max_matches_model_defaults():
    """``config.yaml.example`` 的 ``deviation.budget.d_max`` 与 §14.6 / 模型缺省值**同源**（防漂移）。"""
    import yaml

    from alphabee.config import DeviationBudgetSettings, DeviationSettings

    example = yaml.safe_load((REPO_ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    configured = example["deviation"]["budget"]["d_max"]
    assert configured == {"analysis": 60, "tracking": 20}
    assert configured == DeviationBudgetSettings().d_max
    # 示例配置必须能被模型**接受**（键名打错会在此暴露，而不是在用户 import 期）
    deviation = DeviationSettings(**example["deviation"])
    assert deviation.budget.d_max == configured


# ── 合成账本整体口径 ───────────────────────────────────────────────────────


def test_all_eight_metrics_on_one_synthetic_ledger(monkeypatch):
    """一条合成账本 + state 一次算出八项正确值（§14.5-B 的整体口径回归）。"""
    _patch_settings(monkeypatch, d_max=40)
    state = _state(
        nodes=("collect_raw_facts", "resolve_industry_context", "run_analysis_engines", "record_deviations"),
        decisions=(_audit_decision(0, consistent=True), _audit_decision(1, consistent=False)),
        issues=(_issue(),),
    )
    ledger = [
        _event(fingerprint="fp-1", resolved=True, recovery_action="rerun_round=1"),
        _event(
            fingerprint="fp-2",
            step_id="run_thesis",
            detected_at_step="review_thesis",
            deviation_class=DeviationClass.D3_ARGUMENT.value,
            resolved=True,
        ),
        _event(
            fingerprint="fp-3",
            detected_at_step="review_report",
            deviation_class=DeviationClass.D2_STRUCTURE.value,
            category="insight_degraded",
        ),
        _event(step_id=None, detected_at_step="review_report", category="stale", fingerprint="fp-4"),
    ]
    assert telemetry.compute_deviation_metrics(state, ledger).model_dump() == {
        "detection_rate": 0.3333,
        "mean_detection_latency": 4.6667,
        "recovery_rate": 0.25,
        "recovery_half_life": 9.5,
        "amplification_overturn_rate": 0.5,
        "on_track_curve": [0.0, 0.5, 0.6667, 0.75],
        "budget_consumption": 0.25,
        "silent_degradation_rate": 0.25,
    }


def test_compute_deviation_metrics_tolerates_empty_state_and_ledger():
    """空 state / 空账本 → 全 ``None`` + 空曲线，绝不抛异常。"""
    assert telemetry.compute_deviation_metrics({}, []).model_dump() == {
        "detection_rate": None,
        "mean_detection_latency": None,
        "recovery_rate": None,
        "recovery_half_life": None,
        "amplification_overturn_rate": None,
        "on_track_curve": [],
        "budget_consumption": None,
        "silent_degradation_rate": None,
    }


# ── 落库 / 读取 ───────────────────────────────────────────────────────────


def test_store_metrics_roundtrips_all_eight_fields():
    metrics = _metrics()
    telemetry.store_metrics(metrics, run_id="run-store")

    loaded = telemetry.load_metrics("run-store")
    assert loaded is not None
    assert loaded.model_dump() == metrics.model_dump()
    assert loaded.on_track_curve == [1.0, 0.5]  # JSON 列往返仍是 list[float]


def test_store_metrics_is_idempotent_per_run():
    """同一 run 重放 → 仍是一行（按 run_id upsert），值取最新。"""
    telemetry.store_metrics(_metrics(), run_id="run-idem")
    telemetry.store_metrics(telemetry.DeviationMetrics(on_track_curve=[1.0]), run_id="run-idem")

    assert _metric_row_count("run-idem") == 1
    loaded = telemetry.load_metrics("run-idem")
    assert loaded is not None and loaded.on_track_curve == [1.0] and loaded.detection_rate is None


def test_store_metrics_shares_the_ledger_database():
    """指标与账本同库同 Base：写一条账本 + 一条指标，两张表都能在同一 DB 文件里查到。"""
    record_event(_issue(category="missing_data"), run_id="run-same-db", symbol="600519.SH")
    telemetry.store_metrics(_metrics(), run_id="run-same-db")

    from sqlalchemy import inspect

    db_mod.init_db()
    inspector = inspect(db_mod.get_engine())
    assert {"deviation_events", "deviation_metrics"} <= set(inspector.get_table_names())
    assert _metric_row_count("run-same-db") == 1
    assert len(store_mod.list_events(run_id="run-same-db")) == 1


def test_store_metrics_zero_write_when_ledger_disabled(monkeypatch):
    """开关关闭 ⇒ **零写入**（表存在也不落行）。"""
    db_mod.init_db()  # 先建表：确保"零写入"不是因为表不存在
    _patch_settings(monkeypatch, ledger=False)

    telemetry.store_metrics(_metrics(), run_id="run-off")

    assert _metric_row_count("run-off") == 0
    # 再把开关切回打开复核：读不到不是因为"表不存在"或"配置仍处于关闭态"
    _patch_settings(monkeypatch, ledger=True)
    assert telemetry.load_metrics("run-off") is None


def test_store_metrics_is_fail_open_when_db_unavailable(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(telemetry, "get_session", _boom)

    with caplog.at_level("WARNING"):
        telemetry.store_metrics(_metrics(), run_id="run-broken")  # 不抛

    assert any("fail-open" in record.message for record in caplog.records)


def test_load_metrics_is_fail_open_when_db_unavailable(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(telemetry, "get_session", _boom)

    with caplog.at_level("WARNING"):
        assert telemetry.load_metrics("run-any") is None
    assert any("fail-open" in record.message for record in caplog.records)


def test_load_metrics_returns_none_for_unknown_run():
    assert telemetry.load_metrics("never-stored") is None


def test_ledger_enabled_is_fail_open_when_config_raises(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr(config_mod, "get_settings", _boom)
    with caplog.at_level("WARNING"):
        assert telemetry.ledger_enabled() is True  # §14.6 默认开启
    assert any("fail-open" in record.message for record in caplog.records)


def test_ledger_enabled_matches_sink_switch(monkeypatch):
    """指标写入开关与账本 sink 的开关**必须同值**（否则会出现"写了指标没写账本"）。"""
    _patch_settings(monkeypatch, ledger=False)
    assert telemetry.ledger_enabled() is False
    assert sink_mod.ledger_switches()[0] is False

    _patch_settings(monkeypatch, ledger=True)
    assert telemetry.ledger_enabled() is True
    assert sink_mod.ledger_switches()[0] is True


# ── 只读时间线 ────────────────────────────────────────────────────────────


def test_render_deviation_timeline_rebuilds_from_ledger():
    """§15 验收 1：节点序 × 偏离 × 检测时延 × 恢复动作，全部可从账本重建。"""
    record_event(
        Issue(
            id="issue-tl-1",
            severity=IssueSeverity.HIGH,
            category="missing_data",
            message="缺 3 个字段，采集失败",
            related_step="collect_raw_facts",
            scope=IssueScope.DATA,
            detected_at_step="collect_raw_facts",
            recovery_action="rerun_round=1",
        ),
        run_id="run-tl",
        symbol="600519.SH",
    )
    record_event(
        Issue(
            id="issue-tl-2",
            severity=IssueSeverity.CRITICAL,
            category="amplification_direction_conflict",
            message="[放大方向不一致] 加权边 insight->thesis 方向相反",
            related_step="run_thesis",
            scope=IssueScope.REVIEW,
            deviation_class=DeviationClass.D3_ARGUMENT,
            detected_at_step="review_thesis",
            amplified_by=["insight->thesis"],
        ),
        run_id="run-tl",
        symbol="600519.SH",
    )

    text = telemetry.render_deviation_timeline("run-tl")

    assert text.startswith("偏离时间线 · run_id=run-tl · 记录 2 条")
    for column in ("节点序", "检测节点", "产生节点", "分类", "类目", "严重度", "时延", "恢复动作", "状态", "放大"):
        assert column in text
    # 节点序 + 检测端/产生端 + 检测时延
    assert f"[{NODE_INDEX['collect_raw_facts']:2d}]" in text
    assert f"[{NODE_INDEX['review_thesis']:2d}]" in text
    assert "collect_raw_facts" in text and "run_thesis" in text
    # 检测时延：review_thesis(9) − run_thesis(8) = 1
    assert " 1    " in text
    # 恢复动作与放大边
    assert "rerun_round=1" in text
    assert "<insight->thesis>" in text
    # 分类分布（D1–D5 全量键恒出现，便于 grep 统计）
    assert "d3_argument=1" in text and "d1_data=0" in text
    assert "汇总：已恢复 0 / 未恢复 2" in text


def test_render_deviation_timeline_is_byte_stable():
    """不渲染时间戳 ⇒ 同一账本两次渲染逐字节相同（可进 golden 比对）。"""
    record_event(_issue(), run_id="run-stable", symbol="600519.SH")
    first = telemetry.render_deviation_timeline("run-stable")
    second = telemetry.render_deviation_timeline("run-stable")
    assert first == second


def test_render_deviation_timeline_empty_view_is_deterministic():
    text = telemetry.render_deviation_timeline("run-none")
    assert text == (
        "偏离时间线 · run_id=run-none · 记录 0 条\n"
        "无记录：账本为空，或该 run 未产生偏离（可用 --deviations <run_id> 指定其他 run）。"
    )
    # run_id 为空也给出确定性视图（CLI 在账本全空时走这条）
    assert telemetry.render_deviation_timeline("").startswith("偏离时间线 · run_id=(未指定) · 记录 0 条")


def test_render_deviation_timeline_is_fail_open_when_store_raises(monkeypatch, caplog):
    """账本读取打桩后抛异常 → 仍是确定性空视图（视图层不把异常抛给 CLI）。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(store_mod, "list_events", _boom)
    with caplog.at_level("WARNING"):
        text = telemetry.render_deviation_timeline("run-boom")
    assert text.startswith("偏离时间线 · run_id=run-boom · 记录 0 条")
    assert any("fail-open" in record.message for record in caplog.records)


def test_latest_run_id_reads_most_recent_non_empty_run():
    assert telemetry.latest_run_id() is None
    record_event(_issue(), run_id="run-old", symbol="600519.SH")
    record_event(_issue(category="blocked"), run_id="run-new", symbol="600519.SH")
    assert telemetry.latest_run_id() == "run-new"


def test_latest_run_id_is_fail_open_when_store_raises(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(store_mod, "list_events", _boom)
    assert telemetry.latest_run_id() is None


# ── run 尾部节点接线（F5 sink） ───────────────────────────────────────────


def _sink_state(*issues: Issue) -> dict:
    return _state(nodes=("collect_raw_facts", "record_deviations"), issues=issues)


async def test_sink_node_stores_metrics_and_keeps_count_invariant():
    """写入账本 → 计算并落库指标；既有 key 语义与计数不变式保持不变。"""
    update = await sink_mod.record_deviations(
        _sink_state(
            Issue(
                id="issue-sink-1",
                severity=IssueSeverity.HIGH,
                category="missing_data",
                message="缺 3 个字段",
                related_step="collect_raw_facts",
                scope=IssueScope.DATA,
            ),
            Issue(
                id="issue-sink-2",
                severity=IssueSeverity.MEDIUM,
                category="insight_degraded",
                message="insight 降级产出",
                related_step="run_analysis_engines",
                scope=IssueScope.DATA,
                detected_at_step="run_analysis_engines",
            ),
        ),
        {},
    )

    step = update["steps"][0]
    assert {key: value for key, value in step.inputs.items() if key != "metrics_stored"} == {
        "symbol": "600519.SH",
        "issue_count": 2,
        "written": 2,
        "failed": 0,
        "skipped": 0,
    }
    assert step.inputs["metrics_stored"] == 1
    assert step.inputs["written"] + step.inputs["failed"] + step.inputs["skipped"] == step.inputs["issue_count"]

    metrics = telemetry.load_metrics("run-1")
    assert metrics is not None
    assert metrics.recovery_rate == 0.0
    assert metrics.silent_degradation_rate == 0.0
    assert metrics.detection_rate == 1.0  # 两条都写在产生地节点
    assert len(metrics.on_track_curve) == 2  # collect_raw_facts + record_deviations


async def test_sink_node_skips_metrics_when_ledger_disabled(monkeypatch):
    """``deviation.ledger.enabled=false`` ⇒ 既不写账本也不写指标（PR8 纯回滚）。"""
    monkeypatch.setattr(sink_mod, "ledger_switches", lambda: (False, True))
    calls: list[str] = []
    monkeypatch.setattr(sink_mod, "store_metrics", lambda *a, **k: calls.append("store"))

    update = await sink_mod.record_deviations(_sink_state(_issue()), {})

    step = update["steps"][0]
    assert step.inputs == {
        "symbol": "600519.SH",
        "issue_count": 1,
        "written": 0,
        "failed": 0,
        "skipped": 1,
        "metrics_stored": 0,
    }
    assert calls == []
    assert telemetry.load_metrics("run-1") is None


async def test_sink_node_metrics_failure_is_fail_open(monkeypatch, caplog):
    """度量层抛异常 → 只 warning，step 仍 SUCCEEDED，账本计数不变（§14.0 约定 3）。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("metrics exploded")

    monkeypatch.setattr(sink_mod, "compute_deviation_metrics", _boom)
    monkeypatch.setattr(sink_mod, "store_metrics", lambda *args, **kwargs: None)

    with caplog.at_level("WARNING"):
        update = await sink_mod.record_deviations(_sink_state(_issue(), _issue(category="blocked")), {})

    step = update["steps"][0]
    assert step.status.value == "succeeded"
    assert step.inputs["metrics_stored"] == 0
    assert step.inputs["written"] == 2 and step.inputs["failed"] == 0 and step.inputs["skipped"] == 0
    assert any("fail-open" in record.message for record in caplog.records)


async def test_sink_node_survives_metrics_store_raising_from_load(monkeypatch, caplog):
    """落库"静默失败"（store 不抛、回读不到）→ ``metrics_stored`` 为 0（不谎报成功）。"""
    monkeypatch.setattr(sink_mod, "store_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(sink_mod, "load_metrics", lambda run_id: None)
    with caplog.at_level("WARNING"):
        update = await sink_mod.record_deviations(_sink_state(_issue()), {})
    assert update["steps"][0].inputs["metrics_stored"] == 0


# ── CLI 接入 ──────────────────────────────────────────────────────────────


def _load_cli_args_module():
    """按**文件路径**加载 ``apps/cli/args.py``。

    不能 ``import alphabee.apps.cli.args``：包 ``__init__`` 会 import ``main`` → ``orchestrator.agent``
    → ``tushare.set_token``（写 ``$HOME/tk.csv``、可能联网），在测试环境不可用（实测 OSError / 超时）。
    只执行这一个文件即可真实测到 argparse 行为，且不引入任何重依赖。
    """
    path = REPO_ROOT / "alphabee" / "apps" / "cli" / "args.py"
    spec = importlib.util.spec_from_file_location("_f5_cli_args", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["main.py", "--deviations"], ""),  # 省略 RUN_ID → 空串（由 CLI 解析为"最近一次 run"）
        (["main.py", "--deviations", "run-42"], "run-42"),
        (["main.py", "分析宁德时代"], None),  # 未给该开关 → None（不影响普通查询）
    ],
)
def test_cli_deviations_flag_parsing(monkeypatch, argv, expected):
    module = _load_cli_args_module()
    monkeypatch.setattr(sys, "argv", list(argv))
    assert module.parse_args().deviations == expected


def test_cli_main_dispatches_deviations_before_chat_and_query():
    """``main()`` 必须按 ``--deviations`` 分派到只读视图，且**早于** chat/query 分支。

    AST 结构钉住（不 import ``main``：见 ``_load_cli_args_module`` 的理由）。三件事：
    ① 定义了 ``print_deviations_view``；② 它调用 ``render_deviation_timeline`` + ``latest_run_id``；
    ③ ``main()`` 里有 ``if args.deviations is not None`` 分支 → 调用视图并 ``return``，
    且该分支行号早于 ``if args.chat or not args.query``（否则"只带 --deviations"会掉进对话模式）。
    """
    tree = ast.parse((REPO_ROOT / "alphabee" / "apps" / "cli" / "main.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    view = functions["print_deviations_view"]
    called = {call.func.id for call in ast.walk(view) if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
    assert {"render_deviation_timeline", "latest_run_id"} <= called

    main_fn = functions["main"]
    deviations_branch = [
        node for node in ast.walk(main_fn) if isinstance(node, ast.If) and "deviations" in ast.dump(node.test)
    ]
    assert len(deviations_branch) == 1
    branch = deviations_branch[0]
    assert any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "print_deviations_view"
        for node in branch.body
    ), "分派分支必须调用 print_deviations_view"
    assert any(isinstance(node, ast.Return) for node in branch.body), "分派后必须 return（不得继续跑流水线）"

    chat_branch = [
        node for node in ast.walk(main_fn) if isinstance(node, ast.If) and "attr='chat'" in ast.dump(node.test)
    ]
    assert chat_branch and branch.lineno < min(node.lineno for node in chat_branch)


# ── 模块卫生：不读配置 / 零 LLM ───────────────────────────────────────────


def _module_level_calls(tree: ast.Module, name: str) -> list[int]:
    """模块级语句（非函数/类体内）里对 ``name`` 的调用行号。"""
    lines: list[int] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Name) and func.id == name:
                lines.append(child.lineno)
            elif isinstance(func, ast.Attribute) and func.attr == name:
                lines.append(child.lineno)
    return lines


def test_telemetry_does_not_read_config_at_import_time():
    """§14.6：模块级不得调用 ``get_settings()``（``config/__init__.py`` 自身在 import 期读配置）。"""
    source = Path(telemetry.__file__).read_text(encoding="utf-8")
    assert _module_level_calls(ast.parse(source), "get_settings") == []


def test_telemetry_has_no_llm_dependency():
    """§16 反模式：度量层零 LLM（不得出现任何 LLM 工厂/模型类）。"""
    source = Path(telemetry.__file__).read_text(encoding="utf-8")
    for forbidden in ("create_chat_model", "create_structured_model", "ChatOpenAI", "langchain"):
        assert forbidden not in source, forbidden


def test_telemetry_public_api_surface_is_stable():
    """公开面就是 §14.5-B 的三个函数 + 落库/读取与视图辅助（新增即需同步本断言）。"""
    assert set(telemetry.__all__) == {
        "AMPLIFICATION_AUDIT_MAKER",
        "AMPLIFICATION_OVERTURN_MARKER",
        "D_MAX_CONTEXT_KEY",
        "OVERTURN_CONFIDENCE_CEILING",
        "TASK_KIND_CONTEXT_KEY",
        "DeviationMetrics",
        "compute_deviation_metrics",
        "latest_run_id",
        "ledger_enabled",
        "load_metrics",
        "render_deviation_timeline",
        "store_metrics",
    }
    assert callable(telemetry.compute_deviation_metrics)
    assert callable(telemetry.store_metrics)
    assert callable(telemetry.render_deviation_timeline)
