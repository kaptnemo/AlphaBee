"""P4（D3-C2）研究生命周期派生视图测试：``tracking/status.py`` + ``run_once`` 接线 + ``--track-alerts`` 展示
（``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.4-C）。

覆盖 §15.8 / §15.4-C 的三条关键断言：

1. **五态转移表 + 优先级互斥**：每态 ≥1 组输入；同时命中多条 ⇒ 取序小者；
2. **同源驱动（关键）**：用**真实** ``check_exit`` / ``monitor_triggers`` / ``detect_triggers`` 对假帧的
   输出驱动本函数（不是手写字符串），并逐条断言模块常量确为真实输出的前缀；
3. **无新阈值**：``status.py`` 不 import config、无模块级数值常量、无数值比较。

外加：``run_once`` 接线（含降级路径写 ``""`` 不猜）、``--track-alerts`` 展示状态字（只读）、
历史 JSONL 向后兼容。
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from alphabee.midterm.diff_consumers import (
    _EVIDENCE_RATE_TRIGGER,
    _TV_TRIGGER,
    check_exit,
    monitor_triggers,
)
from alphabee.midterm.models import (
    ArtifactRef,
    CompanyStateArtifact,
    CompanyStateDiff,
    EvidenceEvent,
    ExitCondition,
    FactorSnapshot,
    FundamentalFactor,
    PositionDiff,
    RiskFactor,
    StateShift,
    TrendFactor,
)
from alphabee.tracking import status as status_module
from alphabee.tracking.scheduler import TrackingReport, run_once
from alphabee.tracking.status import (
    EXIT_DOWNGRADE_PREFIX,
    EXIT_POSITION_DIVERGENCE_PREFIX,
    EXIT_TRIGGERED_PREFIX,
    MONITOR_TV_PREFIX,
    ResearchStatus,
    research_status,
)
from alphabee.tracking.triggers import Trigger, TriggerKind, detect_fact_triggers, detect_triggers

SYMBOL = "600519.SH"
D1 = "2026-09-01"
D2 = "2026-09-20"


# ── 构造器（全部离线：真实 midterm/triggers 函数 + 假帧） ─────────────────────


def _snapshot(
    as_of: str = D2,
    *,
    revenue_yoy: float = 12.0,
    price_change_pct: float = 1.0,
    news: str = "",
) -> FactorSnapshot:
    return FactorSnapshot(
        symbol=SYMBOL,
        as_of_date=as_of,
        fundamental=FundamentalFactor(revenue_yoy=revenue_yoy, net_profit_yoy=8.0),
        trend=TrendFactor(price_change_pct=price_change_pct),
        risk=RiskFactor(news_title=news),
    )


def _artifact(
    *,
    as_of: str = D2,
    stale_after: str | None = None,
    exit_met: bool = False,
    state: str = "S2",
) -> CompanyStateArtifact:
    artifact = CompanyStateArtifact(symbol=SYMBOL, thesis="核心假设", as_of_date=as_of, stale_after=stale_after)
    from alphabee.midterm.models import StateBelief

    artifact.state = StateBelief(distribution={state: 1.0}, argmax_state=state, entropy=0.0)
    if exit_met:
        artifact.exit_conditions = [ExitCondition(kind="thesis_broken", condition="现金流连续两季低于净利", met=True)]
    artifact.factor_snapshot = _snapshot(as_of)
    return artifact


def _frame_diff(
    *,
    tv: float = 0.0,
    downgrade: bool = False,
    divergence: bool = False,
    exit_conditions_met: tuple[str, ...] = (),
    elapsed_days: int = 19,
    new_evidence: int = 0,
) -> CompanyStateDiff:
    """造一个真实 ``CompanyStateDiff``（用于驱动 ``check_exit`` / ``monitor_triggers`` 的**委托**路径）。"""
    shift = StateShift(
        argmax_from="S2",
        argmax_to="S1" if downgrade else "S2",
        tv_distance=tv,
        kind="downgrade" if downgrade else "same",
    )
    return CompanyStateDiff(
        symbol=SYMBOL,
        prev=None,
        curr=ArtifactRef(id=f"{SYMBOL}:{D2}", date=D2, symbol=SYMBOL),
        elapsed_days=elapsed_days,
        state_shift=shift,
        position=PositionDiff(band_weight_divergence=divergence) if divergence else None,
        confidence=None,
        exit_conditions_met=list(exit_conditions_met),
        new_evidence=[
            EvidenceEvent(
                id=f"{SYMBOL}:ev:{index}",
                date=D2,
                kind="fundamental",
                description=f"证据 {index}",
                effect_on_thesis="refuting",
                confidence_delta=0.1,
            )
            for index in range(new_evidence)
        ],
    )


def _trigger(kind: TriggerKind, reason: str = "假触发") -> Trigger:
    return Trigger(kind=kind, symbol=SYMBOL, reason=reason, payload={"source": "test"})


def _providers(date: str, *, revenue_yoy: float = 12.0):
    return {
        "snapshot_provider": lambda symbol, _d=date, _r=revenue_yoy: _snapshot(_d, revenue_yoy=_r),
        "evidence_provider": lambda symbol, _d=date, **kwargs: [
            EvidenceEvent(
                id=f"{SYMBOL}:support:{_d}",
                date=_d,
                kind="expectation",
                description="分析师上修盈利预测",
                effect_on_thesis="confirming",
                confidence_delta=0.2,
            )
        ],
    }


# ── ① 五态与签名 ────────────────────────────────────────────────────────────


def test_enum_has_exactly_the_five_spec_states():
    assert [(member.name, member.value) for member in ResearchStatus] == [
        ("INVALIDATED", "invalidated"),
        ("RISK_ALERT", "risk_alert"),
        ("THESIS_CHANGED", "thesis_changed"),
        ("NEEDS_RESEARCH", "needs_research"),
        ("WAITING", "waiting"),
    ]
    assert "ACTIVE" not in ResearchStatus.__members__, "无 ACTIVE 态（§15.4-A）"


def test_research_status_signature_matches_spec():
    signature = inspect.signature(research_status)
    assert list(signature.parameters) == ["exit_reasons", "monitor_reasons", "triggers", "stale"]
    assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in signature.parameters.values())
    assert [parameter.default for parameter in signature.parameters.values()] == [(), (), (), False]


# ── ② 五态转移表 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"exit_reasons": ["退出条件触发：现金流连续两季低于净利"]}, ResearchStatus.INVALIDATED),
        ({"exit_reasons": ["状态降级：S2→S1"]}, ResearchStatus.RISK_ALERT),
        ({"exit_reasons": ["仓位带与实际仓位背离"]}, ResearchStatus.RISK_ALERT),
        ({"triggers": [_trigger(TriggerKind.PRICE_MOVE)]}, ResearchStatus.RISK_ALERT),
        ({"monitor_reasons": ["tv_distance=0.412>0.3"]}, ResearchStatus.THESIS_CHANGED),
        ({"stale": True}, ResearchStatus.NEEDS_RESEARCH),
        ({"triggers": [_trigger(TriggerKind.STALE_EXPIRED)]}, ResearchStatus.NEEDS_RESEARCH),
        ({"triggers": [_trigger(TriggerKind.FINANCIAL_REPORT)]}, ResearchStatus.NEEDS_RESEARCH),
        ({"triggers": [_trigger(TriggerKind.ANNOUNCEMENT)]}, ResearchStatus.NEEDS_RESEARCH),
        ({}, ResearchStatus.WAITING),
        ({"exit_reasons": [], "monitor_reasons": [], "triggers": [], "stale": False}, ResearchStatus.WAITING),
    ],
)
def test_transition_table(kwargs, expected):
    assert research_status(**kwargs) is expected


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # 同时命中多条 ⇒ 取判定序小者（互斥）
        (
            {
                "exit_reasons": ["退出条件触发：x", "状态降级：S2→S1"],
                "monitor_reasons": ["tv_distance=0.9>0.3"],
                "triggers": [_trigger(TriggerKind.PRICE_MOVE), _trigger(TriggerKind.STALE_EXPIRED)],
                "stale": True,
            },
            ResearchStatus.INVALIDATED,
        ),
        (
            {
                "exit_reasons": ["状态降级：S2→S1"],
                "monitor_reasons": ["tv_distance=0.9>0.3"],
                "triggers": [_trigger(TriggerKind.STALE_EXPIRED)],
                "stale": True,
            },
            ResearchStatus.RISK_ALERT,
        ),
        (
            {
                "monitor_reasons": ["tv_distance=0.9>0.3"],
                "triggers": [_trigger(TriggerKind.ANNOUNCEMENT)],
                "stale": True,
            },
            ResearchStatus.THESIS_CHANGED,
        ),
        ({"triggers": [_trigger(TriggerKind.FINANCIAL_REPORT)], "stale": True}, ResearchStatus.NEEDS_RESEARCH),
    ],
)
def test_priority_is_mutually_exclusive(kwargs, expected):
    assert research_status(**kwargs) is expected


def test_unknown_and_uncovered_kinds_stay_waiting():
    """规格外的输入不改状态字（保守投影，见模块 docstring 两条如实登记）。"""
    assert research_status(triggers=[_trigger(TriggerKind.MANUAL)]) is ResearchStatus.WAITING
    assert research_status(monitor_reasons=["evidence_arrival_rate=1.000>0.5"]) is ResearchStatus.WAITING
    assert research_status(triggers=[_trigger(TriggerKind.STALE_EXPIRED), _trigger(TriggerKind.MANUAL)]) is (
        ResearchStatus.NEEDS_RESEARCH
    )


def test_prefix_matching_is_prefix_not_substring():
    """口径 = **前缀**（上游格式化输出的开头），不是任意位置包含。"""
    assert research_status(exit_reasons=["（补充）退出条件触发：x"]) is ResearchStatus.WAITING
    assert research_status(monitor_reasons=["note: tv_distance=0.9>0.3"]) is ResearchStatus.WAITING


def test_deterministic_and_side_effect_free():
    kwargs = {"exit_reasons": ["状态降级：S2→S1"], "triggers": [_trigger(TriggerKind.PRICE_MOVE)]}
    first = research_status(**kwargs)
    second = research_status(**kwargs)
    assert first is second


# ── ③ 同源驱动（关键）：真实 check_exit / monitor_triggers / detect_triggers ──


def test_same_source_exit_conditions_drive_invalidated():
    """真实 ``check_exit``：退出条件新满足 ⇒ reasons 以常量开头 ⇒ INVALIDATED。"""
    signal = check_exit(_frame_diff(exit_conditions_met=("现金流连续两季低于净利",)))

    assert signal.reasons and signal.reasons[0].startswith(EXIT_TRIGGERED_PREFIX)
    assert signal.reasons[0] == f"{EXIT_TRIGGERED_PREFIX}：现金流连续两季低于净利"
    assert research_status(exit_reasons=signal.reasons) is ResearchStatus.INVALIDATED


def test_same_source_downgrade_drives_risk_alert():
    """真实 ``check_exit``：状态降级 ⇒ RISK_ALERT（前缀常量即为真实输出前缀）。"""
    signal = check_exit(_frame_diff(downgrade=True))

    assert signal.reasons == [f"{EXIT_DOWNGRADE_PREFIX}：S2→S1"]
    assert research_status(exit_reasons=signal.reasons) is ResearchStatus.RISK_ALERT


def test_same_source_position_divergence_drives_risk_alert():
    """真实 ``check_exit``：仓位带背离（固定文案）⇒ RISK_ALERT。"""
    signal = check_exit(_frame_diff(divergence=True))

    assert signal.reasons == [EXIT_POSITION_DIVERGENCE_PREFIX]
    assert research_status(exit_reasons=signal.reasons) is ResearchStatus.RISK_ALERT


def test_same_source_tv_distance_drives_thesis_changed():
    """真实 ``monitor_triggers``：``tv_distance`` 超**既有常量** ``_TV_TRIGGER`` ⇒ THESIS_CHANGED。"""
    over = check_exit(_frame_diff(tv=_TV_TRIGGER + 0.01))
    assert over.reasons == []
    monitor = monitor_triggers(_frame_diff(tv=_TV_TRIGGER + 0.01))

    assert monitor.triggers and monitor.triggers[0].startswith(MONITOR_TV_PREFIX)
    assert monitor.triggers[0] == f"{MONITOR_TV_PREFIX}{_TV_TRIGGER + 0.01:.3f}>{_TV_TRIGGER}"
    assert research_status(monitor_reasons=monitor.triggers) is ResearchStatus.THESIS_CHANGED


def test_same_source_tv_boundary_uses_the_existing_constant_not_a_new_one():
    """边界与既有常量同源：``tv == _TV_TRIGGER`` **不**触发（严格大于）；略高才触发。

    这条钉子同时证明本模块没有第二份阈值：判据完全由 ``monitor_triggers`` 的既有常量决定。
    """
    at_threshold = monitor_triggers(_frame_diff(tv=_TV_TRIGGER))
    just_over = monitor_triggers(_frame_diff(tv=_TV_TRIGGER + 0.001))

    assert at_threshold.triggers == []
    assert research_status(monitor_reasons=at_threshold.triggers) is ResearchStatus.WAITING
    assert just_over.triggers and research_status(monitor_reasons=just_over.triggers) is (ResearchStatus.THESIS_CHANGED)
    assert _EVIDENCE_RATE_TRIGGER == 0.5  # 另一个既有常量：只做"未复制"的旁证（本模块不出现它）


def test_same_source_evidence_rate_alone_is_a_documented_conservative_projection():
    """真实 ``monitor_triggers``：**只**由 ``evidence_arrival_rate`` 触发 ⇒ 不改状态字（规格式保守投影）。

    §15.4-A 的优先级表第 3 序只认 ``tv_distance=`` 前缀；此情形要改必须回规格改表（§15.0 C-5）。
    """
    monitor = monitor_triggers(_frame_diff(new_evidence=20, elapsed_days=10))

    assert any(reason.startswith("evidence_arrival_rate=") for reason in monitor.triggers)
    assert not any(reason.startswith(MONITOR_TV_PREFIX) for reason in monitor.triggers)
    assert research_status(monitor_reasons=monitor.triggers) is ResearchStatus.WAITING


def test_same_source_stale_expired_from_real_detect_triggers():
    """真实 ``detect_triggers``：``stale_after`` 已过 ⇒ STALE_EXPIRED ⇒ NEEDS_RESEARCH。"""
    found = detect_triggers(SYMBOL, as_of=D2, artifact=_artifact(stale_after=D1))

    assert TriggerKind.STALE_EXPIRED in {trigger.kind for trigger in found}
    assert research_status(triggers=found, stale=True) is ResearchStatus.NEEDS_RESEARCH


def test_same_source_price_move_from_real_detect_triggers():
    """真实 ``detect_triggers``：``tv_distance`` 超阈走的仍是 ``monitor_triggers``（委托路径）。"""
    found = detect_triggers(SYMBOL, as_of=D2, artifact=_artifact(), frame_diff=_frame_diff(tv=_TV_TRIGGER + 0.2))

    assert TriggerKind.PRICE_MOVE in {trigger.kind for trigger in found}
    assert research_status(triggers=found) is ResearchStatus.RISK_ALERT


def test_same_source_financial_report_from_real_detect_fact_triggers():
    """真实 ``detect_fact_triggers``：财报类字段刷新 ⇒ FINANCIAL_REPORT ⇒ NEEDS_RESEARCH。"""
    found = detect_fact_triggers(
        SYMBOL,
        snapshot=_snapshot(D2, revenue_yoy=40.0),
        prev_snapshot=_snapshot(D1, revenue_yoy=12.0),
    )

    assert TriggerKind.FINANCIAL_REPORT in {trigger.kind for trigger in found}
    assert research_status(triggers=found) is ResearchStatus.NEEDS_RESEARCH


# ── ④ 无新阈值 / 无 config / 无 IO ──────────────────────────────────────────


def test_module_imports_are_pure_and_never_read_config():
    """AST：运行期 import 恰为纯依赖 + ``tracking.triggers``；不 import config / IO / 数据层。"""
    tree = ast.parse(Path(status_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert imported == {
        "__future__",
        "enum",
        "collections.abc",
        "alphabee.tracking.triggers",
    }
    source = Path(status_module.__file__).read_text(encoding="utf-8")
    for needle in ("get_settings", "record_event", "Path(", "open("):
        assert needle not in source, f"status.py 不得出现 {needle}"


def test_module_introduces_no_new_numeric_threshold():
    """AST：没有任何**数值常量**（模块级赋值或比较操作数）⇒ 不可能引入新阈值。"""
    tree = ast.parse(Path(status_module.__file__).read_text(encoding="utf-8"))

    numeric_module_constants = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int | float)
        and not isinstance(node.value.value, bool)
    ]
    comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(operand, ast.Constant) and isinstance(operand.value, int | float) for operand in node.comparators
        )
    ]

    assert numeric_module_constants == []
    assert comparisons == []


# ── ⑤ 接线：run_once ───────────────────────────────────────────────────────


def test_tracking_report_field_defaults_to_empty():
    """追加字段带默认值：未计算 = ``""``（不是猜测值）。"""
    assert TrackingReport(symbol=SYMBOL, as_of=D2).research_status == ""


def test_run_once_writes_a_status_consistent_with_its_own_fields(tmp_path):
    """接线一致性：``report.research_status`` == 用 report 自身三个真源重算的值（同源，不抄字符串）。"""
    report = run_once(
        SYMBOL,
        as_of=D1,
        state_dir=tmp_path / "state",
        alert_dir=tmp_path / "alerts",
        **_providers(D1),
    )

    expected = research_status(
        exit_reasons=report.exit_reasons,
        monitor_reasons=report.monitor_reasons,
        triggers=report.triggers,
        stale=any(trigger.kind == TriggerKind.STALE_EXPIRED for trigger in report.triggers),
    ).value

    assert report.research_status == expected
    assert report.research_status in {member.value for member in ResearchStatus}


def test_run_once_end_to_end_financial_refresh_yields_needs_research(tmp_path):
    """端到端：第二帧财报类字段刷新（真实 ``detect_fact_triggers``）⇒ 落盘帧带 ``needs_research``。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    run_once(SYMBOL, as_of=D1, state_dir=state_dir, alert_dir=alert_dir, **_providers(D1, revenue_yoy=12.0))

    report = run_once(
        SYMBOL,
        as_of=D2,
        state_dir=state_dir,
        alert_dir=alert_dir,
        **_providers(D2, revenue_yoy=40.0),
    )

    assert TriggerKind.FINANCIAL_REPORT in {trigger.kind for trigger in report.triggers}
    assert report.research_status == ResearchStatus.NEEDS_RESEARCH.value


def test_persisted_alert_frame_carries_the_status(tmp_path):
    """接线**位置**钉子：状态字在 ``_write_alerts`` 之前写入 ⇒ 落盘的告警帧自带该字段。

    （如果把它挪到落盘之后，落盘帧就会缺这个字段 —— §15.4-B 的"report 组装完成后、`_write_alerts`
    之前"是规格的一部分。）
    """
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    run_once(SYMBOL, as_of=D1, state_dir=state_dir, alert_dir=alert_dir, **_providers(D1))
    report = run_once(
        SYMBOL,
        as_of=D2,
        state_dir=state_dir,
        alert_dir=alert_dir,
        **_providers(D2, revenue_yoy=40.0),
    )

    path = alert_dir / f"{SYMBOL}.jsonl"
    assert path.is_file(), "前提：本帧应有告警落盘（有触发）"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "前提：告警文件非空"
    assert rows[-1]["research_status"] == report.research_status != ""


@pytest.mark.parametrize("as_of", [D1, D2])
def test_degraded_path_keeps_research_status_empty(tmp_path, monkeypatch, as_of):
    """降级/异常路径写 ``""``（未计算）而不是猜一个状态字（§15.4-B/C）。"""
    import alphabee.tracking.scheduler as scheduler_module

    def _boom(*args, **kwargs):
        raise RuntimeError("reconcile 炸了")

    monkeypatch.setattr(scheduler_module, "_reconcile_frame", _boom)

    report = run_once(
        SYMBOL,
        as_of=as_of,
        state_dir=tmp_path / "state",
        alert_dir=tmp_path / "alerts",
        **_providers(as_of),
    )

    assert report.degraded is True
    assert report.research_status == ""


# ── ⑥ --track-alerts 展示（只读） ───────────────────────────────────────────


def _alert_row(*, research_status_value: str, state: str = "S2") -> dict:
    return TrackingReport(
        symbol=SYMBOL,
        as_of=D2,
        state=state,
        research_status=research_status_value,
        exit_reasons=["状态降级：S2→S1"] if research_status_value == "risk_alert" else [],
    ).model_dump(mode="json")


def test_track_alerts_view_shows_derived_status_word(tmp_path, monkeypatch):
    """§15.4-B/C：视图展示派生状态字；同时存在时并列认知状态（只读渲染）。"""
    cli_main = __import__("alphabee.apps.cli.main", fromlist=["x"])
    alert_dir = tmp_path / "alerts"
    alert_dir.mkdir(parents=True)
    path = alert_dir / f"{SYMBOL}.jsonl"
    path.write_text(json.dumps(_alert_row(research_status_value="risk_alert"), ensure_ascii=False) + "\n", "utf-8")
    monkeypatch.setattr("alphabee.tracking.scheduler.default_alert_dir", lambda: alert_dir)

    text = cli_main.render_track_alerts_view(SYMBOL)

    assert f"状态={ResearchStatus.RISK_ALERT.value}" in text
    assert "认知状态=S2" in text


def test_track_alerts_view_falls_back_for_frames_without_status(tmp_path, monkeypatch):
    """历史/降级帧（该字段为 ``""``）⇒ 回落认知状态，不伪造派生状态字。"""
    cli_main = __import__("alphabee.apps.cli.main", fromlist=["x"])
    alert_dir = tmp_path / "alerts"
    alert_dir.mkdir(parents=True)
    path = alert_dir / f"{SYMBOL}.jsonl"
    path.write_text(json.dumps(_alert_row(research_status_value=""), ensure_ascii=False) + "\n", "utf-8")
    monkeypatch.setattr("alphabee.tracking.scheduler.default_alert_dir", lambda: alert_dir)

    text = cli_main.render_track_alerts_view(SYMBOL)

    assert "状态=S2" in text
    assert "认知状态=" not in text


def test_track_alerts_view_is_read_only(tmp_path, monkeypatch):
    """展示状态字不得写任何文件（纯读；§15.4 回滚 = 删展示行即可）。"""
    cli_main = __import__("alphabee.apps.cli.main", fromlist=["x"])
    alert_dir = tmp_path / "alerts"
    alert_dir.mkdir(parents=True)
    path = alert_dir / f"{SYMBOL}.jsonl"
    path.write_text(json.dumps(_alert_row(research_status_value="invalidated"), ensure_ascii=False) + "\n", "utf-8")
    monkeypatch.setattr("alphabee.tracking.scheduler.default_alert_dir", lambda: alert_dir)
    before = {item.name: item.read_bytes() for item in alert_dir.iterdir()}

    cli_main.render_track_alerts_view(SYMBOL)

    assert {item.name: item.read_bytes() for item in alert_dir.iterdir()} == before


# ── ⑦ 向后兼容（§15.0 C-1） ────────────────────────────────────────────────


def test_historical_alert_lines_still_deserialize():
    legacy = {
        "symbol": SYMBOL,
        "as_of": D2,
        "triggers": [],
        "state": "S0",
        "exit_reasons": [],
        "monitor_reasons": [],
        "blocked_actions": [],
        "deviations_recorded": 0,
    }
    report = TrackingReport.model_validate(legacy)

    assert report.research_status == ""


def test_real_alert_file_lines_still_deserialize():
    """真实产物钉：``data/tracking/alerts/*.jsonl`` 全行在新增字段后仍可反序列化。"""
    alert_root = Path("data/tracking/alerts")
    files = sorted(alert_root.glob("*.jsonl")) if alert_root.is_dir() else []
    if not files:
        pytest.skip(f"本地无告警产物：{alert_root}")

    checked = 0
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            report = TrackingReport.model_validate(json.loads(line))
            assert report.research_status in {"", *(member.value for member in ResearchStatus)}
            checked += 1
    assert checked > 0
