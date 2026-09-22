"""P3（D1-A2）tracking 偏离入账本测试：``tracking/ledger.py`` + ``scheduler.run_once`` 接线 +
``--deviations`` 来源标注（``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.3-D）。

覆盖 §15.8 / §15.3-D 的六条：

1. **投影表逐条（5 行）**：条件命中 → 对应 `category` / `deviation_class` / severity / `recovery_action`；
2. **与既有指纹去重协同**：同标的连续两帧同类偏离 → 账本**一行**、`occurrence_count` 递增、`run_id` 刷新；
3. **`persist=False`**：不写帧、不写账本（`deviations_recorded == 0`）；
4. **DB 不可用**（monkeypatch `record_event` 抛异常）→ 返回 0、不抛；
5. **覆盖守卫**：5 个新 category 均已登记 `CLASS_BY_CATEGORY`；
6. **`--deviations`**：跟踪帧带 `[track]` 标注；`latest_run_id()` 不返回跟踪帧。

纪律：账本用例一律跑在**每个用例独立的 SQLite 文件**上（复用既有 ``DATA_FETCH_DB_PATH`` 覆盖口，
与 ``tests/orchestrator/test_telemetry.py`` 同款 autouse fixture）—— 任何用例都不可能写到开发机的
真实 ``fetch_events.db``。帧/快照/证据全部离线注入，不触网、不依赖真实数据。
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from pathlib import Path

import pytest

import alphabee.data_fetch.database as db_mod
import alphabee.data_fetch.deviation_store as store_mod
import alphabee.orchestrator.services.telemetry as telemetry
from alphabee.core import DeviationClass, Issue, IssueSeverity
from alphabee.midterm.models import (
    CompanyStateArtifact,
    EvidenceEvent,
    FactorSnapshot,
    FundamentalFactor,
    RiskFactor,
    TrendFactor,
)
from alphabee.midterm.persistence import append_artifact
from alphabee.orchestrator.services.deviation import (
    CLASS_BY_CATEGORY,
    NODE_INDEX,
    detection_latency,
    resolve_deviation_class,
)
from alphabee.tracking import ledger as ledger_mod
from alphabee.tracking.ledger import (
    TRACKING_RUN_PREFIX,
    TRACKING_STEP,
    is_tracking_run_id,
    record_tracking_deviations,
    tracking_issues,
    tracking_run_id,
)
from alphabee.tracking.scheduler import (
    ContradictionAccounting,
    TrackingReport,
    reconcile,
    run_once,
)

SYMBOL = "600519.SH"
AS_OF = "2026-09-01"
LATER = "2026-09-20"


# ── fixtures / 构造器 ───────────────────────────────────────────────────────


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


def _report(**overrides) -> TrackingReport:
    base: dict = {"symbol": SYMBOL, "as_of": AS_OF}
    base.update(overrides)
    return TrackingReport(**base)


def _snapshot(date: str) -> FactorSnapshot:
    return FactorSnapshot(
        symbol=SYMBOL,
        as_of_date=date,
        fundamental=FundamentalFactor(revenue_yoy=12.0, net_profit_yoy=8.0),
        trend=TrendFactor(price_change_pct=1.0),
        risk=RiskFactor(news_title=""),
    )


def _evidence(date: str) -> list[EvidenceEvent]:
    """两向证据（id 带日期 ⇒ 跨帧是"新证据"）。"""
    return [
        EvidenceEvent(
            id=f"{SYMBOL}:support:{date}",
            date=date,
            kind="expectation",
            description="分析师上修盈利预测",
            effect_on_thesis="confirming",
            confidence_delta=0.2,
        ),
        EvidenceEvent(
            id=f"{SYMBOL}:oppose:{date}",
            date=date,
            kind="fundamental",
            description="经营现金流弱于净利润",
            effect_on_thesis="refuting",
            confidence_delta=0.25,
        ),
    ]


def _providers(date: str) -> dict:
    return {
        "snapshot_provider": lambda symbol, _d=date: _snapshot(_d),
        "evidence_provider": lambda symbol, _d=date, **kwargs: _evidence(_d),
    }


def _plain_issue(category: str = "missing_data") -> Issue:
    """一条与 tracking 无关的普通偏离（用于"分析 run 不被打标"的对照）。"""
    return Issue(
        id=f"issue-{category}",
        severity=IssueSeverity.MEDIUM,
        category=category,
        message=f"分析 run 的偏离：{category}",
        related_step="collect_raw_facts",
        detected_at_step="collect_raw_facts",
    )


# ── ① 签名 / 前缀约定（§15.3-A） ─────────────────────────────────────────────


def test_tracking_run_id_uses_the_prefix_convention():
    assert TRACKING_RUN_PREFIX == "track"
    assert tracking_run_id(SYMBOL, AS_OF) == f"track:{SYMBOL}:{AS_OF}"
    assert tracking_run_id(SYMBOL, AS_OF).startswith("track:")


def test_tracking_run_id_fits_the_frozen_run_id_column():
    """账本 ``run_id`` 列宽 64（``deviation_events`` 17 列已冻结）：前缀约定必须放得下真实形态。"""
    assert len(tracking_run_id("600519.SH", "2026-09-22")) <= 64
    assert len(tracking_run_id("000001.SZ", "2026-12-31")) <= 64


def test_is_tracking_run_id_only_matches_the_prefix():
    assert is_tracking_run_id(f"track:{SYMBOL}:{AS_OF}") is True
    assert is_tracking_run_id("  track:600519.SH:2026-09-01  ") is True
    assert is_tracking_run_id("orch-run-abc") is False
    assert is_tracking_run_id("tracking-run") is False
    assert is_tracking_run_id("") is False
    assert is_tracking_run_id(None) is False


def test_tracking_run_prefix_matches_telemetry_prefix():
    """两处前缀**同口径**（telemetry 不 import tracking：依赖方向不得反向 §15.0 C-6 ⇒ 用用例钉住）。"""
    assert telemetry._TRACKING_RUN_PREFIX == TRACKING_RUN_PREFIX
    assert telemetry.is_tracking_run_id(tracking_run_id(SYMBOL, AS_OF)) is True
    assert telemetry.is_tracking_run_id("orch-run-1") is False


def test_tracking_package_exports_the_ledger_api():
    import alphabee.tracking as tracking_pkg

    for name in ("TRACKING_RUN_PREFIX", "record_tracking_deviations", "tracking_issues", "tracking_run_id"):
        assert name in tracking_pkg.__all__, f"{name} 未登记进 __all__"
        assert hasattr(tracking_pkg, name)


# ── ② 投影表逐条（§15.3-A 的 5 行） ─────────────────────────────────────────


_PROJECTION_TABLE = (
    (
        {"exit_reasons": ["SENTINEL_EXIT"]},
        "tracking_exit_signal",
        DeviationClass.D3_ARGUMENT,
        IssueSeverity.HIGH,
        "escalate",
        "SENTINEL_EXIT",
    ),
    (
        {"monitor_reasons": ["tv_distance=0.412>0.3"]},
        "tracking_drift_trigger",
        DeviationClass.D4_STATE,
        IssueSeverity.MEDIUM,
        "deep_research_due",
        "tv_distance=0.412>0.3",
    ),
    (
        {"contradiction": ContradictionAccounting(forced_sides=["opposing"])},
        "tracking_contradiction_forced",
        DeviationClass.D3_ARGUMENT,
        IssueSeverity.MEDIUM,
        "forced_accounting",
        "opposing",
    ),
    (
        {"degraded": True, "degraded_reason": "上游快照缺失（revenue_yoy 取不到）"},
        "tracking_frame_degraded",
        DeviationClass.D2_STRUCTURE,
        IssueSeverity.MEDIUM,
        "degraded_frame",
        "上游快照缺失",
    ),
    (
        {"skipped_reason": f"as_of={AS_OF} 未推进（上一帧 {LATER}）→ 不产差分"},
        "tracking_frame_skipped",
        DeviationClass.D5_CONTROL,
        IssueSeverity.LOW,
        "diff_skipped",
        "未推进",
    ),
)


@pytest.mark.parametrize(
    ("overrides", "category", "deviation_class", "severity", "recovery_action", "reason_text"),
    _PROJECTION_TABLE,
)
def test_projection_table_row(overrides, category, deviation_class, severity, recovery_action, reason_text):
    """投影表逐字落地：category / class / severity / recovery_action / related_step / message 文本。"""
    issues = tracking_issues(_report(**overrides))
    matching = [issue for issue in issues if issue.category == category]

    assert len(matching) == 1, f"{category} 应恰好产 1 条，实际 {[i.category for i in issues]}"
    issue = matching[0]
    assert issue.deviation_class is deviation_class  # **显式给定**，不是 None（不依赖惰性回退）
    assert issue.severity is severity
    assert issue.recovery_action == recovery_action
    assert issue.related_step == TRACKING_STEP and issue.detected_at_step == TRACKING_STEP
    assert AS_OF in issue.message, "message 必须带 report.as_of"
    assert reason_text in issue.message, "message 必须带具体 reason 文本（不是只写类别名）"
    # 显式字段与映射表一致（两处都必须对）
    assert CLASS_BY_CATEGORY[category] is deviation_class
    assert resolve_deviation_class(issue) is deviation_class


def test_clean_frame_projects_nothing():
    assert tracking_issues(_report()) == []


def test_all_five_conditions_project_five_issues():
    report = _report(
        exit_reasons=["SENTINEL_EXIT"],
        monitor_reasons=["tv_distance=0.9>0.3"],
        contradiction=ContradictionAccounting(forced_sides=["opposing"]),
        degraded=True,
        degraded_reason="降级原因",
        skipped_reason="未推进",
    )
    assert [issue.category for issue in tracking_issues(report)] == [
        "tracking_exit_signal",
        "tracking_drift_trigger",
        "tracking_contradiction_forced",
        "tracking_frame_degraded",
        "tracking_frame_skipped",
    ]


def test_malformed_fields_are_treated_as_empty_not_crashing():
    """字段类型畸形（列表字段被写成标量/字符串）⇒ 保守当作空，不逐字符展开、不抛。

    ``TrackingReport`` 是 Pydantic 模型（会拒绝 `str`→`list[str]`），故这里用 duck-typed 替身
    直击投影函数的容错面。
    """

    class _Odd:
        symbol = SYMBOL
        as_of = AS_OF
        exit_reasons = "SENTINEL_EXIT"  # 标量而非 list：不得被逐字符展开成 13 条
        monitor_reasons = None
        contradiction = None
        degraded = "yes"  # 真值字符串 ⇒ 判为降级帧
        degraded_reason = ""
        skipped_reason = ""

    issues = tracking_issues(_Odd())

    assert [issue.category for issue in issues] == ["tracking_frame_degraded"]


def test_detection_latency_is_none_for_tracking_frames():
    """跟踪帧不在节点序内 ⇒ ``detection_latency`` 返回 ``None``：**预期行为**，不得"修"。"""
    assert "tracking" not in NODE_INDEX
    issue = tracking_issues(_report(exit_reasons=["SENTINEL_EXIT"]))[0]
    assert detection_latency(issue) is None


# ── ③ 覆盖守卫（5 个新 category 已登记） ────────────────────────────────────


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("tracking_exit_signal", DeviationClass.D3_ARGUMENT),
        ("tracking_drift_trigger", DeviationClass.D4_STATE),
        ("tracking_contradiction_forced", DeviationClass.D3_ARGUMENT),
        ("tracking_frame_degraded", DeviationClass.D2_STRUCTURE),
        ("tracking_frame_skipped", DeviationClass.D5_CONTROL),
    ],
)
def test_tracking_categories_are_registered(category, expected):
    assert CLASS_BY_CATEGORY[category] is expected


# ── ④ 写账本 / 指纹去重 ─────────────────────────────────────────────────────


def test_record_tracking_deviations_writes_each_issue():
    report = _report(exit_reasons=["SENTINEL_EXIT"], monitor_reasons=["tv_distance=0.4>0.3"])

    assert record_tracking_deviations(report) == 2

    events = store_mod.list_events(run_id=tracking_run_id(SYMBOL, AS_OF))
    assert {event.category for event in events} == {"tracking_exit_signal", "tracking_drift_trigger"}
    assert {event.deviation_class for event in events} == {"d3_argument", "d4_state"}
    assert all(event.symbol == SYMBOL for event in events)
    assert all(event.step_id == TRACKING_STEP for event in events)
    assert all(event.run_id == f"track:{SYMBOL}:{AS_OF}" for event in events)


def test_clean_frame_writes_nothing():
    assert record_tracking_deviations(_report()) == 0
    assert store_mod.list_events() == []


def test_same_deviation_across_frames_upserts_and_refreshes_run_id():
    """§15.3-D：同标的连续两帧**同类**偏离 → 账本一行、``occurrence_count`` 递增、``run_id`` 刷新。

    归并依据是既有指纹（归一化 message：数字折成 ``#`` ⇒ 两帧里只有 ``as_of`` 不同也不产生新行）。
    """
    first = _report(as_of=AS_OF, exit_reasons=["现金流连续两季低于净利"])
    second = _report(as_of=LATER, exit_reasons=["现金流连续两季低于净利"])

    assert record_tracking_deviations(first) == 1
    assert record_tracking_deviations(second) == 1

    events = store_mod.list_events()
    assert len(events) == 1, "同一偏离必须收敛为一行（指纹去重）"
    event = events[0]
    assert event.occurrence_count == 2
    assert event.run_id == tracking_run_id(SYMBOL, LATER), "run_id 刷新为最近观测者"
    assert event.first_seen_at <= event.last_seen_at


def test_different_reason_text_creates_a_second_row():
    """指纹里含**归一化 message** ⇒ 同一 category 但不同 reason 文本是两条不同偏离（判别性对照）。"""
    record_tracking_deviations(_report(exit_reasons=["现金流连续两季低于净利"]))
    record_tracking_deviations(_report(exit_reasons=["审计意见非标"]))

    events = store_mod.list_events()
    assert len(events) == 2
    assert all(event.category == "tracking_exit_signal" for event in events)
    assert {event.occurrence_count for event in events} == {1}


# ── ⑤ fail-open ─────────────────────────────────────────────────────────────


def test_record_tracking_deviations_returns_zero_when_db_write_raises(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store_mod, "record_event", _boom)

    assert record_tracking_deviations(_report(exit_reasons=["SENTINEL_EXIT"])) == 0


def test_record_tracking_deviations_returns_zero_when_report_is_broken():
    class _Broken:
        def __getattr__(self, name: str):
            raise RuntimeError(f"broken report: {name}")

    assert record_tracking_deviations(_Broken()) == 0  # 不抛


def test_written_count_ignores_none_return(monkeypatch):
    """``record_event`` 返回 ``None``（写失败/未落库）不计入条数。"""
    monkeypatch.setattr(store_mod, "record_event", lambda *args, **kwargs: None)

    assert record_tracking_deviations(_report(exit_reasons=["SENTINEL_EXIT"])) == 0


def test_ledger_module_does_not_import_orchestrator_nodes():
    """依赖方向：ledger 的**运行期** import 只依赖 core/utils/data_fetch（不碰 orchestrator 节点/图）。

    ``alphabee.tracking.scheduler`` 只允许出现在 ``if TYPE_CHECKING:`` 块里（纯类型引用，运行期不 import
    —— 否则 ledger ↔ scheduler 成环）。
    """
    tree = ast.parse(Path(ledger_mod.__file__).read_text(encoding="utf-8"))

    runtime: set[str] = set()
    type_only: set[str] = set()
    for node in tree.body:
        target = type_only if _is_type_checking_block(node) else runtime
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Import):
                target.update(alias.name for alias in stmt.names)
            elif isinstance(stmt, ast.ImportFrom) and stmt.module:
                target.add(stmt.module)

    assert runtime == {
        "__future__",
        "logging",
        "typing",
        "alphabee.core",
        "alphabee.utils.pipeline",
        "alphabee.data_fetch.deviation_store",
    }
    assert type_only == {"alphabee.tracking.scheduler"}


def _is_type_checking_block(node: ast.stmt) -> bool:
    """``if TYPE_CHECKING:`` 块（只需认 ``TYPE_CHECKING`` 这一名字，`ast` 层面足够）。"""
    if not isinstance(node, ast.If):
        return False
    return any(isinstance(sub, ast.Name) and sub.id == "TYPE_CHECKING" for sub in ast.walk(node.test))


# ── ⑥ 接线：run_once（persist 开关）/ reconcile 不写 ────────────────────────


def _seed_frame_ahead(state_dir: Path, *, as_of: str = LATER) -> None:
    """在状态目录里预置一帧**更晚**的帧 ⇒ 随后以更早的 ``as_of`` 跑必然"未推进"（``skipped_reason`` 非空）。

    这一步是**判别性前提**：否则"首帧无任何触发"会让 `persist=False` 的用例即使没有 persist 门也通过
    （零偏离 → ``deviations_recorded == 0`` 恒成立），用例就失去鉴别力。
    """
    append_artifact(
        CompanyStateArtifact(symbol=SYMBOL, as_of_date=as_of, thesis="核心假设"),
        state_dir,
    )


def test_persist_false_writes_neither_frame_nor_ledger(tmp_path):
    """只读预演：``persist=False`` ⇒ 不落帧、**不写账本**（§15.3-B）。

    输入刻意选"未推进"帧（本来会产生 1 条 ``tracking_frame_skipped``）⇒ 该用例对"persist 门被拆掉"
    敏感（变异 M1 坐实）。
    """
    state_dir = tmp_path / "state"
    _seed_frame_ahead(state_dir)

    report = run_once(
        SYMBOL,
        as_of=AS_OF,
        state_dir=state_dir,
        alert_dir=tmp_path / "alerts",
        persist=False,
        **_providers(AS_OF),
    )

    assert report.skipped_reason, "前提：未推进帧必须带 skipped_reason（否则本用例的判定对象无效）"
    assert report.persisted is False
    assert report.deviations_recorded == 0
    assert store_mod.list_events() == []
    # 零写入：状态目录仍然只有预置的那一帧
    assert len((state_dir / f"{SYMBOL}.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1


def test_persist_true_writes_frame_and_ledger_for_the_same_input(tmp_path):
    """同一"未推进"输入下 ``persist=True`` ⇒ 帧 + 账本都写（与上一条构成判别性对照）。"""
    state_dir = tmp_path / "state"
    _seed_frame_ahead(state_dir)

    report = run_once(
        SYMBOL,
        as_of=AS_OF,
        state_dir=state_dir,
        alert_dir=tmp_path / "alerts",
        persist=True,
        **_providers(AS_OF),
    )

    assert report.skipped_reason
    assert report.persisted is True
    assert report.deviations_recorded == 1
    events = store_mod.list_events(run_id=tracking_run_id(SYMBOL, AS_OF))
    assert [event.category for event in events] == ["tracking_frame_skipped"]


def test_run_once_records_ledger_when_persist_true(tmp_path):
    """``persist=True`` ⇒ 帧落盘 + 偏离入账；账本 ``run_id`` 用 ``track:<symbol>:<as_of>``。"""
    first = run_once(
        SYMBOL,
        as_of=AS_OF,
        state_dir=tmp_path / "state",
        alert_dir=tmp_path / "alerts",
        persist=True,
        **_providers(AS_OF),
    )
    # 同一 as_of 再跑一次 ⇒ as_of 未推进 ⇒ skipped_reason 非空 ⇒ 必产 tracking_frame_skipped
    second = run_once(
        SYMBOL,
        as_of=AS_OF,
        state_dir=tmp_path / "state",
        alert_dir=tmp_path / "alerts",
        persist=True,
        **_providers(AS_OF),
    )

    assert second.skipped_reason, "前提：未推进帧必须带 skipped_reason（否则本用例的判定对象无效）"
    assert second.deviations_recorded >= 1
    events = store_mod.list_events(run_id=tracking_run_id(SYMBOL, AS_OF))
    assert {event.category for event in events} >= {"tracking_frame_skipped"}
    assert all(event.run_id.startswith(f"{TRACKING_RUN_PREFIX}:") for event in events)
    # 首帧即使无偏离也必须把计数写成 0（默认值 + 实测）
    assert isinstance(first.deviations_recorded, int)


def test_reconcile_does_not_touch_the_ledger(tmp_path):
    """§15.3-B：账本写入留在 ``run_once`` 上层；``reconcile`` 是"纯推进内核"，不写账本。"""
    reconcile(
        SYMBOL,
        as_of=AS_OF,
        state_dir=tmp_path / "state",
        persist=True,
        **_providers(AS_OF),
    )

    assert store_mod.list_events() == []


# ── ⑦ --deviations 展示口径（§15.3-C） ─────────────────────────────────────


def test_latest_run_id_skips_tracking_frames():
    """跟踪帧**不抢占**"最近一次分析 run"：即使它是最新观测，``latest_run_id()`` 也不返回它。"""
    store_mod.record_event(_plain_issue(), run_id="orch-run-analysis", symbol=SYMBOL)
    record_tracking_deviations(_report(exit_reasons=["SENTINEL_EXIT"]))

    assert telemetry.latest_run_id() == "orch-run-analysis"


def test_latest_run_id_returns_none_when_only_tracking_frames_exist():
    record_tracking_deviations(_report(exit_reasons=["SENTINEL_EXIT"]))

    assert telemetry.latest_run_id() is None


def test_render_timeline_marks_tracking_source_on_every_line():
    record_tracking_deviations(_report(exit_reasons=["SENTINEL_EXIT"], monitor_reasons=["tv_distance=0.4>0.3"]))

    text = telemetry.render_deviation_timeline(tracking_run_id(SYMBOL, AS_OF))
    lines = text.splitlines()

    assert lines and all(line.startswith("[track] ") for line in lines)
    assert f"[track] 偏离时间线 · run_id=track:{SYMBOL}:{AS_OF}" in lines[0]
    assert any("tracking_exit_signal" in line for line in lines)


def test_render_timeline_leaves_analysis_runs_unmarked():
    """零回归：非跟踪帧的渲染**逐字**不含 `[track]` 标注。"""
    store_mod.record_event(_plain_issue(), run_id="orch-run-analysis", symbol=SYMBOL)

    text = telemetry.render_deviation_timeline("orch-run-analysis")

    assert "[track]" not in text
    assert text.startswith("偏离时间线 · run_id=orch-run-analysis")


def test_render_timeline_is_byte_stable_for_tracking_frames():
    record_tracking_deviations(_report(exit_reasons=["SENTINEL_EXIT"]))

    run_id = tracking_run_id(SYMBOL, AS_OF)
    assert telemetry.render_deviation_timeline(run_id) == telemetry.render_deviation_timeline(run_id)


def test_tracking_frames_are_reachable_by_explicit_run_id():
    """跟踪帧虽不抢 ``latest_run_id()``，但**显式传 run_id** 即可见（§15.3-C 的决策）。"""
    record_tracking_deviations(_report(exit_reasons=["SENTINEL_EXIT"]))

    text = telemetry.render_deviation_timeline(tracking_run_id(SYMBOL, AS_OF))

    assert "记录 1 条" in text
    assert "tracking_exit_signal" in text


# ── ⑧ 向后兼容（§15.0 C-1：只 append 字段且带默认值） ───────────────────────


def test_deviations_recorded_defaults_to_zero():
    assert TrackingReport(symbol=SYMBOL, as_of=AS_OF).deviations_recorded == 0


def test_historical_alert_lines_still_deserialize():
    """历史告警 JSONL（无 ``deviations_recorded`` 字段）仍可反序列化（默认值兜住）。"""
    legacy = {
        "symbol": SYMBOL,
        "as_of": AS_OF,
        "triggers": [],
        "state": "S0",
        "exit_reasons": [],
        "monitor_reasons": [],
        "blocked_actions": [],
        "alerts_path": "",
        "persisted": False,
        "degraded": False,
        "degraded_reason": "",
        "skipped_reason": "",
    }
    report = TrackingReport.model_validate(legacy)

    assert report.deviations_recorded == 0
    assert tracking_issues(report) == []


def test_real_alert_file_lines_still_deserialize():
    """真实产物钉：``data/tracking/alerts/*.jsonl`` 的**全部**行在新增字段后仍可反序列化。"""
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
            assert report.deviations_recorded == 0  # 历史行没有该字段 ⇒ 默认值
            checked += 1
    assert checked > 0
