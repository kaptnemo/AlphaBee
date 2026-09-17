"""F0b 单测：偏离账本表 + ``deviation_store`` + run 尾部 sink 节点（§14.7）。

覆盖：
- ``DeviationEvent`` 表结构与 ``create_all``（与既有失败库共享同一 Base / DB 文件）；
- ``record_event`` 同指纹 upsert（计数累加、last_seen 刷新、不新增行）；
- 数字不同但归一化后同指纹；``normalize=False`` 时不再归并；
- ``mark_resolved`` / ``list_events`` / ``summarize_symbol`` / ``purge_before``；
- fail-open：DB 不可用 / 写入异常 / 读取异常都不抛，只返回中性值；
- sink 节点：写入本 run issues、不回写 ``state["issues"]``、开关关闭时零写入、
  store 抛错时不打断且返回 step 计数。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

import alphabee.data_fetch.database as db_mod
import alphabee.data_fetch.deviation_store as store_mod
from alphabee.core.schemas import DeviationClass, Issue, IssueScope, IssueSeverity, IssueStatus
from alphabee.data_fetch.deviation_store import (
    TOP_RECURRING,
    compute_deviation_fingerprint,
    list_events,
    mark_resolved,
    normalize_message,
    purge_before,
    record_event,
    summarize_symbol,
)
from alphabee.data_fetch.models import Base, DeviationEvent
from alphabee.orchestrator.nodes import record_deviations as sink_mod

_EVENT_COLUMNS = (
    "event_id",
    "run_id",
    "symbol",
    "step_id",
    "detected_at_step",
    "deviation_class",
    "category",
    "severity",
    "fingerprint",
    "occurrence_count",
    "message",
    "recovery_action",
    "recovery_cost",
    "amplified_by",
    "resolved",
    "first_seen_at",
    "last_seen_at",
)


@pytest.fixture(autouse=True)
def isolated_ledger_db(monkeypatch):
    """每个用例一个独立 SQLite 文件（复用失败库的 DATA_FETCH_DB_PATH 覆盖口）。"""
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


def _issue(
    message: str = "derived facts 为空",
    *,
    category: str = "missing_data",
    deviation_class: DeviationClass | None = DeviationClass.D1_DATA,
    severity: IssueSeverity = IssueSeverity.HIGH,
    detected_at_step: str | None = "run_analysis_engines",
    related_step: str | None = "collect_raw_facts",
    status: IssueStatus = IssueStatus.OPEN,
    recovery_action: str | None = None,
    recovery_cost: int | None = None,
    amplified_by: list[str] | None = None,
) -> Issue:
    return Issue(
        id="issue-abcdef123456",
        severity=severity,
        category=category,
        message=message,
        related_step=related_step,
        status=status,
        scope=IssueScope.DATA,
        deviation_class=deviation_class,
        detected_at_step=detected_at_step,
        recovery_action=recovery_action,
        recovery_cost=recovery_cost,
        amplified_by=list(amplified_by or []),
    )


# ── 表结构 ──────────────────────────────────────────────────────────────────


def test_deviation_event_table_registered_on_shared_base():
    assert DeviationEvent.__tablename__ == "deviation_events"
    assert DeviationEvent.metadata is Base.metadata
    # 与失败库共用同一 metadata → 同一个 init_db() 建表，无需新库文件
    assert "data_fetch_events" in Base.metadata.tables
    assert "deviation_events" in Base.metadata.tables


def test_deviation_event_columns_match_contract():
    assert set(DeviationEvent.__table__.columns.keys()) == set(_EVENT_COLUMNS)


def test_create_all_creates_table(tmp_path):
    db_mod.init_db()
    from sqlalchemy import inspect

    tables = inspect(db_mod.get_engine()).get_table_names()
    assert "deviation_events" in tables


# ── normalize / fingerprint ────────────────────────────────────────────────


def test_normalize_message_replaces_numbers_and_squeezes_whitespace():
    assert normalize_message("PE TTM 达 176.73 倍，营收 -11.80%") == "PE TTM 达 # 倍，营收 -#%"
    assert normalize_message("多行\n文本\t含  空白 2024-01-02") == "多行 文本 含 空白 #-#-#"
    assert normalize_message(None) == ""
    assert normalize_message("") == ""


def test_fingerprint_stable_and_component_sensitive():
    base = {
        "deviation_class": "d1_data",
        "category": "missing_data",
        "message": "缺字段 3 个",
        "symbol": "600519.SH",
        "detected_at_step": "run_analysis_engines",
    }
    assert compute_deviation_fingerprint(**base) == compute_deviation_fingerprint(**base)
    assert len(compute_deviation_fingerprint(**base)) == 16

    for field, other in (
        ("deviation_class", "d2_structure"),
        ("category", "blocked"),
        ("message", "缺字段 4 个"),
        ("symbol", "000001.SZ"),
        ("detected_at_step", "review_thesis"),
    ):
        assert compute_deviation_fingerprint(**{**base, field: other}) != compute_deviation_fingerprint(**base)


# ── record_event upsert ────────────────────────────────────────────────────


def test_record_event_inserts_new_row():
    event = record_event(_issue(), run_id="run-1", symbol="600519.SH", step_id="collect_raw_facts")

    assert event is not None
    assert event.event_id is not None
    assert event.run_id == "run-1"
    assert event.symbol == "600519.SH"
    assert event.step_id == "collect_raw_facts"
    assert event.detected_at_step == "run_analysis_engines"
    assert event.deviation_class == "d1_data"
    assert event.category == "missing_data"
    assert event.severity == "high"
    assert event.occurrence_count == 1
    assert event.resolved is False
    assert event.amplified_by == []
    assert event.first_seen_at == event.last_seen_at


def test_record_event_same_fingerprint_upserts_count():
    first = record_event(_issue(), run_id="run-1", symbol="600519.SH")
    second = record_event(_issue(), run_id="run-2", symbol="600519.SH")
    third = record_event(_issue(), run_id="run-3", symbol="600519.SH")

    assert first is not None and second is not None and third is not None
    assert first.event_id == second.event_id == third.event_id  # 不新增行
    assert third.occurrence_count == 3
    assert third.first_seen_at == first.first_seen_at  # 首见时间保持
    assert third.last_seen_at >= first.last_seen_at  # 末次刷新
    assert third.run_id == "run-3"  # run_id 记录最近一次上报
    assert len(list_events()) == 1


def test_record_event_different_numbers_share_fingerprint():
    """§14.1-C：数字不同但归一化相同 → 同一指纹（否则复发率统计失效）。"""
    first = record_event(_issue(message="PE TTM 达 176.73 倍，营收同比 -11.80%"), run_id="run-1", symbol="600519.SH")
    second = record_event(_issue(message="PE TTM 达 45.20 倍，营收同比 -3.05%"), run_id="run-2", symbol="600519.SH")

    assert first is not None and second is not None
    assert first.event_id == second.event_id
    assert second.occurrence_count == 2
    assert second.message == "PE TTM 达 45.20 倍，营收同比 -3.05%"  # 行内 message 取最近一次观测


def test_record_event_normalize_false_keeps_numbers_distinct():
    first = record_event(_issue(message="缺 3 个字段"), run_id="run-1", normalize=False)
    second = record_event(_issue(message="缺 4 个字段"), run_id="run-2", normalize=False)

    assert first is not None and second is not None
    assert first.fingerprint != second.fingerprint
    assert len(list_events()) == 2


def test_record_event_fingerprint_differs_by_symbol_and_detector():
    same = record_event(_issue(), run_id="run-1", symbol="600519.SH")
    other_symbol = record_event(_issue(), run_id="run-1", symbol="000001.SZ")
    other_detector = record_event(_issue(detected_at_step="review_thesis"), run_id="run-1", symbol="600519.SH")

    assert same is not None and other_symbol is not None and other_detector is not None
    assert len({same.event_id, other_symbol.event_id, other_detector.event_id}) == 3


def test_record_event_updates_recovery_and_resolved_on_recurrence():
    resolved_issue = _issue(status=IssueStatus.RESOLVED, recovery_action="rerun_round=1", recovery_cost=1)
    first = record_event(resolved_issue, run_id="run-1")
    assert first is not None
    assert first.resolved is True
    assert first.recovery_action == "rerun_round=1"
    assert first.recovery_cost == 1

    # 复发（本次仍未恢复）→ resolved 回到 False（对齐失败库 FIXED→NEW 的复发语义）
    second = record_event(_issue(amplified_by=["insight_to_thesis"]), run_id="run-2")
    assert second is not None
    assert second.event_id == first.event_id
    assert second.occurrence_count == 2
    assert second.resolved is False
    assert second.amplified_by == ["insight_to_thesis"]


def test_record_event_falls_back_to_d2_when_class_missing():
    """数据层保守兜底：未解析的旧 issue（deviation_class=None）按 D2 记（与 resolve 回退同值）。"""
    event = record_event(_issue(deviation_class=None), run_id="run-1")

    assert event is not None
    assert event.deviation_class == DeviationClass.D2_STRUCTURE.value


def test_record_event_accepts_unknown_run_id():
    event = record_event(_issue(), run_id="")

    assert event is not None
    assert event.run_id == ""


# ── fail-open ─────────────────────────────────────────────────────────────


def test_record_event_is_fail_open_when_db_unavailable(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(store_mod, "get_session", _boom)

    with caplog.at_level("WARNING"):
        assert record_event(_issue(), run_id="run-1", symbol="600519.SH") is None
    assert any("fail-open" in record.message for record in caplog.records)


def test_record_event_is_fail_open_when_commit_fails(monkeypatch):
    class _FailingSession:
        def query(self, *args, **kwargs):
            class _Query:
                def filter(self, *a, **k):
                    return self

                def first(self):
                    return None

            return _Query()

        def add(self, _obj):
            return None

        def commit(self):
            raise RuntimeError("disk full")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    monkeypatch.setattr(store_mod, "get_session", _FailingSession)

    assert record_event(_issue(), run_id="run-1") is None


def test_reads_are_fail_open_when_db_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(store_mod, "get_session", _boom)

    assert list_events() == []
    assert mark_resolved("deadbeefdeadbeef", recovery_action="escalated", recovery_cost=0) is False
    assert purge_before(datetime.now()) == 0
    profile = summarize_symbol("600519.SH")
    assert profile["total"] == 0
    assert profile["avg_detection_latency"] is None


def test_init_db_failure_is_fail_open(monkeypatch):
    def _boom():
        raise RuntimeError("cannot create table")

    monkeypatch.setattr(store_mod, "init_db", _boom)

    assert record_event(_issue(), run_id="run-1") is None
    assert list_events() == []


# ── mark_resolved / list_events / purge_before ──────────────────────────────


def test_mark_resolved_by_fingerprint():
    event = record_event(_issue(), run_id="run-1", symbol="600519.SH")
    assert event is not None

    assert mark_resolved(event.fingerprint, recovery_action="degraded_tier=2", recovery_cost=2) is True
    stored = list_events()[0]
    assert stored.resolved is True
    assert stored.recovery_action == "degraded_tier=2"
    assert stored.recovery_cost == 2
    assert stored.last_seen_at == event.last_seen_at  # 恢复不是观测，不刷新 last_seen_at


def test_mark_resolved_unknown_fingerprint_returns_false():
    assert mark_resolved("0123456789abcdef", recovery_action="escalated", recovery_cost=0) is False


def test_mark_resolved_rejects_issue_id():
    """表内不存 Issue.id（§14.1-C）→ 传 issue-xxx 匹配不到，返回 False（调用方应传 fingerprint）。"""
    record_event(_issue(), run_id="run-1")
    assert mark_resolved("issue-abcdef123456", recovery_action="escalated", recovery_cost=1) is False


def test_list_events_filters_and_orders():
    first = record_event(_issue(category="missing_data"), run_id="run-1", symbol="600519.SH")
    second = record_event(
        _issue(category="parse_error", deviation_class=DeviationClass.D2_STRUCTURE),
        run_id="run-2",
        symbol="000001.SZ",
    )
    assert first is not None and second is not None

    assert {event.event_id for event in list_events(symbol="600519.SH")} == {first.event_id}
    assert {event.event_id for event in list_events(run_id="run-2")} == {second.event_id}
    assert {event.event_id for event in list_events(deviation_class=DeviationClass.D2_STRUCTURE)} == {second.event_id}
    assert {event.event_id for event in list_events(deviation_class="d1_data")} == {first.event_id}

    now = datetime.now()
    assert len(list_events()) == 2
    assert list_events(since=now + timedelta(seconds=1)) == []
    assert len(list_events(since=now - timedelta(seconds=1))) == 2


def test_purge_before_removes_old_rows():
    record_event(_issue(), run_id="run-1", symbol="600519.SH")

    assert purge_before(datetime.now() - timedelta(days=1)) == 0
    assert len(list_events()) == 1
    assert purge_before(datetime.now() + timedelta(seconds=1)) == 1
    assert list_events() == []


# ── summarize_symbol（per-symbol 画像） ────────────────────────────────────


def test_summarize_symbol_aggregates():
    record_event(_issue(category="missing_data"), run_id="run-1", symbol="600519.SH")
    record_event(_issue(category="missing_data"), run_id="run-2", symbol="600519.SH")  # 复发
    record_event(
        _issue(category="parse_error", deviation_class=DeviationClass.D2_STRUCTURE, message="LLM JSON 漂移"),
        run_id="run-2",
        symbol="600519.SH",
    )
    record_event(_issue(category="missing_data"), run_id="run-3", symbol="000001.SZ")  # 别的标的

    profile = summarize_symbol("600519.SH")

    assert profile["symbol"] == "600519.SH"
    assert profile["total"] == 2  # 两个指纹
    assert profile["total_occurrences"] == 3
    assert profile["unresolved"] == 2
    assert profile["class_counts"] == {
        "d1_data": 1,
        "d2_structure": 1,
        "d3_argument": 0,
        "d4_state": 0,
        "d5_control": 0,
    }
    assert profile["first_seen_at"] is not None and profile["last_seen_at"] is not None

    assert len(profile["top_recurring"]) == 1
    recurring = profile["top_recurring"][0]
    assert recurring["category"] == "missing_data"
    assert recurring["occurrence_count"] == 2


def test_summarize_symbol_top_recurring_is_capped_and_sorted():
    for index, category in enumerate(("missing_data", "parse_error", "thesis_gap", "state_drift", "blocked", "stale")):
        for _ in range(index + 2):  # occurrence_count 2..7
            record_event(_issue(message=f"{category} 偏离样例", category=category), run_id="run-1", symbol="600519.SH")

    profile = summarize_symbol("600519.SH")

    assert len(profile["top_recurring"]) == TOP_RECURRING
    counts = [row["occurrence_count"] for row in profile["top_recurring"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 7


def test_summarize_symbol_empty_profile_for_unknown_symbol():
    profile = summarize_symbol("000001.SZ")

    assert profile["total"] == 0
    assert profile["total_occurrences"] == 0
    assert profile["unresolved"] == 0
    assert profile["top_recurring"] == []
    assert profile["avg_detection_latency"] is None
    assert profile["first_seen_at"] is None
    assert profile["last_seen_at"] is None
    assert set(profile["class_counts"]) == {member.value for member in DeviationClass}


def test_summarize_symbol_avg_detection_latency_uses_injected_step_index():
    record_event(
        _issue(message="检测在报告复核", detected_at_step="review_report", related_step="run_analysis_engines"),
        run_id="run-1",
        symbol="600519.SH",
    )
    record_event(
        _issue(message="检测在论文评审", detected_at_step="review_thesis", related_step="explore_conflicts"),
        run_id="run-1",
        symbol="600519.SH",
    )

    node_index = {"run_analysis_engines": 4, "review_report": 13, "explore_conflicts": 5, "review_thesis": 9}

    def step_index(node_id: str | None) -> int | None:
        return node_index.get(node_id) if node_id else None

    profile = summarize_symbol("600519.SH", step_index=step_index)
    assert profile["avg_detection_latency"] == ((13 - 4) + (9 - 5)) / 2

    # 未注入 step_index（数据层不反向 import orchestrator）→ None，不猜
    assert summarize_symbol("600519.SH")["avg_detection_latency"] is None


def test_summarize_symbol_ignores_unknown_nodes_for_latency():
    record_event(
        _issue(message="来源未知", detected_at_step="not_a_node", related_step="run_analysis_engines"),
        run_id="run-1",
        symbol="600519.SH",
    )

    profile = summarize_symbol("600519.SH", step_index=lambda node_id: None)
    assert profile["avg_detection_latency"] is None


# ── sink 节点（orchestrator/nodes/record_deviations.py） ────────────────────


def _state_with_issues(*issues: Issue) -> dict:
    from alphabee.core import Run, RunStatus

    return {
        "run": Run(id="run-sink", goal="test", status=RunStatus.RUNNING, context={"symbol": "600519.SH"}),
        "issues": list(issues),
    }


def _assert_sink_counts_consistent(step) -> None:
    """不变式：written + failed + skipped == issue_count（fail-open 下也要可观测）。"""
    inputs = step.inputs
    assert inputs["written"] + inputs["failed"] + inputs["skipped"] == inputs["issue_count"]


async def test_sink_node_writes_issues_and_returns_step():
    issue = _issue(deviation_class=None)  # 未解析的旧 issue → 节点负责惰性解析
    state = _state_with_issues(issue)

    update = await sink_mod.record_deviations(state, {})

    assert len(update["steps"]) == 1
    step = update["steps"][0]
    assert step.id == "record_deviations"
    assert step.inputs["written"] == 1
    assert step.inputs["failed"] == 0
    assert step.inputs["skipped"] == 0
    assert step.inputs["symbol"] == "600519.SH"
    _assert_sink_counts_consistent(step)

    stored = list_events(symbol="600519.SH")
    assert len(stored) == 1
    assert stored[0].deviation_class == "d1_data"  # 由 category=missing_data 惰性映射得到
    assert stored[0].run_id == "run-sink"
    assert stored[0].step_id == "collect_raw_facts"  # issue.related_step


async def test_sink_node_does_not_mutate_state_issues():
    issue = _issue(deviation_class=None)
    state = _state_with_issues(issue)

    await sink_mod.record_deviations(state, {})

    assert state["issues"][0].deviation_class is None  # 原 issue 不被写回
    assert issue.deviation_class is None


async def test_sink_node_skips_when_ledger_disabled(monkeypatch):
    monkeypatch.setattr(sink_mod, "ledger_switches", lambda: (False, True))
    state = _state_with_issues(_issue())

    update = await sink_mod.record_deviations(state, {})

    assert update["steps"][0].inputs["skipped"] == 1
    assert update["steps"][0].inputs["written"] == 0
    _assert_sink_counts_consistent(update["steps"][0])
    assert list_events() == []


async def test_sink_node_is_fail_open_and_reports_counts(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(sink_mod, "record_event", _boom)
    state = _state_with_issues(_issue(), _issue(category="parse_error"))

    update = await sink_mod.record_deviations(state, {})  # 不抛

    step = update["steps"][0]
    assert step.inputs["written"] == 0
    assert step.inputs["failed"] == 2  # 整体异常被吞，未写成的部分计入 failed（可观测）
    assert step.status.value == "succeeded"
    _assert_sink_counts_consistent(step)


async def test_sink_node_counts_store_failures(monkeypatch):
    monkeypatch.setattr(sink_mod, "record_event", lambda *args, **kwargs: None)
    state = _state_with_issues(_issue())

    update = await sink_mod.record_deviations(state, {})

    assert update["steps"][0].inputs["failed"] == 1
    _assert_sink_counts_consistent(update["steps"][0])


async def test_sink_node_tolerates_missing_run_and_issues():
    update = await sink_mod.record_deviations({}, {})

    assert update["steps"][0].inputs["issue_count"] == 0
    assert update["steps"][0].inputs["symbol"] is None
    _assert_sink_counts_consistent(update["steps"][0])


# ── §14.6 开关（deviation.ledger.*） ─────────────────────────────────────────


def test_ledger_switches_default_on_when_config_section_missing():
    """配置模型尚未落地（§14.6 由后续阶段加入）→ 默认开启，且不抛。"""
    assert sink_mod.ledger_switches() == (True, True)


def test_ledger_switches_reads_config_section_when_present(monkeypatch):
    """§14.6 落地后开关即刻生效（无需再改本节点）：deviation.ledger.enabled / fingerprint_normalize。"""
    import alphabee.config as config_mod

    class _Ledger:
        enabled = False
        fingerprint_normalize = False

    class _Deviation:
        ledger = _Ledger()

    class _Settings:
        deviation = _Deviation()

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    assert sink_mod.ledger_switches() == (False, False)


def test_ledger_switches_are_fail_open_when_config_raises(monkeypatch, caplog):
    import alphabee.config as config_mod

    def _boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr(config_mod, "get_settings", _boom)

    with caplog.at_level("WARNING"):
        assert sink_mod.ledger_switches() == (True, True)
    assert any("fail-open" in record.message for record in caplog.records)


async def test_sink_node_honours_fingerprint_normalize_switch(monkeypatch):
    """normalize=False → 数字不同即不同指纹（两行而不是一行）。"""
    monkeypatch.setattr(sink_mod, "ledger_switches", lambda: (True, False))
    state = _state_with_issues(
        _issue(message="缺 3 个字段"),
        _issue(message="缺 4 个字段", category="blocked"),
    )

    update = await sink_mod.record_deviations(state, {})

    assert update["steps"][0].inputs["written"] == 2
    assert len(list_events(symbol="600519.SH")) == 2
