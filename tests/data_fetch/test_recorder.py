"""Integration tests for data_fetch.recorder and database."""

import os
import tempfile

import pytest

import alphabee.data_fetch.database as db_mod
import alphabee.data_fetch.recorder as rec_mod
from alphabee.data_fetch.models import DataFetchEvent, DataFetchIssue
from alphabee.data_fetch.recorder import record_failure


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    """Each test gets a fresh temp-file SQLite database."""
    db_mod.reset_db()
    rec_mod._init_done = False

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATA_FETCH_DB_PATH", path)

    yield

    db_mod.reset_db()
    rec_mod._init_done = False
    try:
        os.unlink(path)
    except OSError:
        pass


class TestRecordFailure:
    def test_single_failure_creates_event_and_issue(self):
        event, issue = record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
            error_message="Connection timed out",
            symbol="600519.SH",
            severity="high",
        )

        assert event.event_id is not None
        assert event.provider == "tushare"
        assert event.api_name == "income"
        assert event.error_type.value == "timeout"
        assert event.symbol == "600519.SH"
        assert event.severity.value == "high"

        assert issue.issue_id is not None
        assert issue.occurrence_count == 1
        assert issue.status.value == "new"
        assert issue.sample_event_id == event.event_id

    def test_dedup_increments_count(self):
        _, issue1 = record_failure(
            provider="tushare",
            api_name="daily",
            error_type="permission",
            error_message="Token expired",
        )
        _, issue2 = record_failure(
            provider="tushare",
            api_name="daily",
            error_type="permission",
            error_message="Token expired",
        )

        assert issue1.issue_id == issue2.issue_id
        assert issue2.occurrence_count == 2

    def test_different_fingerprints_create_separate_issues(self):
        _, issue1 = record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )
        _, issue2 = record_failure(
            provider="akshare",
            api_name="stock_news",
            error_type="network",
        )

        assert issue1.issue_id != issue2.issue_id

    def test_missing_fields_included_in_fingerprint(self):
        _, issue1 = record_failure(
            provider="tushare",
            api_name="fina_indicator",
            error_type="missing_field",
            missing_fields=["roe", "gross_margin"],
        )
        _, issue2 = record_failure(
            provider="tushare",
            api_name="fina_indicator",
            error_type="missing_field",
            missing_fields=["roe", "gross_margin"],
        )
        _, issue3 = record_failure(
            provider="tushare",
            api_name="fina_indicator",
            error_type="missing_field",
            missing_fields=["roe"],
        )

        assert issue1.issue_id == issue2.issue_id
        assert issue1.issue_id != issue3.issue_id

    def test_fixed_issue_reopens_on_new_occurrence(self):
        _, issue = record_failure(
            provider="tushare",
            api_name="balance_sheet",
            error_type="empty_response",
        )
        issue.status = "fixed"

        session = db_mod.get_session()
        session.merge(issue)
        session.commit()

        _, reopened = record_failure(
            provider="tushare",
            api_name="balance_sheet",
            error_type="empty_response",
        )

        assert reopened.status.value == "new"
        assert reopened.occurrence_count == 2

    def test_invalid_enum_values_fallback(self):
        event, issue = record_failure(
            provider="tushare",
            api_name="income",
            error_type="not_a_valid_type",
            severity="ultra_critical",
        )

        assert event.error_type.value == "unknown"
        assert event.severity.value == "medium"

    def test_title_generation(self):
        _, issue1 = record_failure(
            provider="tushare",
            api_name="income",
            error_type="permission",
        )
        assert "权限不足" in issue1.title

        _, issue2 = record_failure(
            provider="tushare",
            api_name="income",
            error_type="missing_field",
            missing_fields=["revenue", "profit"],
        )
        assert "缺少字段" in issue2.title
        assert "revenue" in issue2.title

        _, issue3 = record_failure(
            provider="akshare",
            api_name="stock_news",
            error_type="network",
        )
        assert "网络错误" in issue3.title

    def test_events_persist_across_calls(self):
        record_failure(
            provider="tushare",
            api_name="daily",
            error_type="timeout",
        )

        session = db_mod.get_session()
        count = session.query(DataFetchEvent).count()
        assert count == 1

        record_failure(
            provider="tushare",
            api_name="daily",
            error_type="timeout",
        )

        count = session.query(DataFetchEvent).count()
        assert count == 2


class TestCaptureFailure:
    def test_fire_and_forget_never_raises(self):
        from alphabee.data_fetch.integrations import capture_failure

        capture_failure(
            provider="test_fnf",
            api_name="test_api",
            error_type="unknown",
            error_message="some error",
        )

    def test_tracked_decorator_sync(self):
        from alphabee.data_fetch.integrations import tracked

        @tracked(provider="test_provider", api_name="test_func")
        def will_fail():
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError):
            will_fail()

        session = db_mod.get_session()
        events = (
            session.query(DataFetchEvent)
            .filter(DataFetchEvent.provider == "test_provider")
            .all()
        )
        assert len(events) == 1
        assert events[0].error_type.value == "timeout"

    def test_tracked_severity_map(self):
        from alphabee.data_fetch.integrations import tracked

        @tracked(
            provider="sev_test",
            severity_map={TimeoutError: "high", ValueError: "low"},
        )
        def will_timeout():
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError):
            will_timeout()

        session = db_mod.get_session()
        event = (
            session.query(DataFetchEvent)
            .filter(DataFetchEvent.provider == "sev_test")
            .first()
        )
        assert event is not None
        assert event.severity.value == "high"
