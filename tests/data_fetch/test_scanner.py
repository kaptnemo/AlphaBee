"""Integration tests for data_fetch.scanner."""

import os
import tempfile

import pytest

import alphabee.data_fetch.database as db_mod
import alphabee.data_fetch.recorder as rec_mod
from alphabee.data_fetch.models import (
    DataFetchIssue,
    DataFixTask,
    IssueStatus,
    TaskStatus,
)
from alphabee.data_fetch.recorder import record_failure
from alphabee.data_fetch.scanner import (
    get_open_tasks,
    mark_issue_fixed,
    mark_task,
    scan_and_create_tasks,
)


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
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


class TestScanAndCreateTasks:
    def test_no_issues_returns_empty(self):
        tasks = scan_and_create_tasks(max_tasks=10)
        assert len(tasks) == 0

    def test_single_issue_creates_task(self):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
            error_message="Connection timeout",
            symbol="600519.SH",
        )

        tasks = scan_and_create_tasks(max_tasks=10)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status == TaskStatus.PENDING
        assert task.prompt_context is not None
        assert "tushare" in task.prompt_context
        assert "timeout" in task.prompt_context.lower()
        assert "Connection timeout" in task.prompt_context
        assert task.patch_target is not None

    def test_issue_status_becomes_active_after_scan(self):
        _, issue = record_failure(
            provider="tushare",
            api_name="daily",
            error_type="permission",
        )
        assert issue.status == IssueStatus.NEW

        scan_and_create_tasks(max_tasks=10)

        # Re-read from DB — scan updates in a different session
        session = db_mod.get_session()
        updated = (
            session.query(DataFetchIssue)
            .filter(DataFetchIssue.issue_id == issue.issue_id)
            .first()
        )
        assert updated is not None
        assert updated.status == IssueStatus.ACTIVE

    def test_fixed_issue_not_scanned(self):
        _, issue = record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )
        issue.status = IssueStatus.FIXED
        session = db_mod.get_session()
        session.merge(issue)
        session.commit()

        tasks = scan_and_create_tasks(max_tasks=10)
        assert len(tasks) == 0

    def test_duplicate_scan_does_not_create_duplicate_tasks(self):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )

        tasks1 = scan_and_create_tasks(max_tasks=10)
        assert len(tasks1) == 1

        tasks2 = scan_and_create_tasks(max_tasks=10)
        assert len(tasks2) == 0  # already has pending task

    def test_max_tasks_limit(self):
        for i in range(3):
            record_failure(
                provider="tushare",
                api_name=f"api_{i}",
                error_type="timeout",
            )

        tasks = scan_and_create_tasks(max_tasks=2)
        assert len(tasks) == 2

    def test_scan_skips_pending_tasks(self):
        _, issue = record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )

        # First scan creates task
        scan_and_create_tasks(max_tasks=10)

        # Manually reset issue back to new (simulating fix reopening)
        issue.status = IssueStatus.NEW
        session = db_mod.get_session()
        session.merge(issue)
        session.commit()

        # Second scan should NOT create new task because one is still pending
        tasks = scan_and_create_tasks(max_tasks=10)
        assert len(tasks) == 0

    def test_prompt_context_contains_key_info(self):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="permission",
            error_message="Token expired or insufficient permissions",
            symbol="600519.SH",
            missing_fields=["roe", "gross_margin"],
            request_payload={"ts_code": "600519.SH", "start_date": "20230101"},
        )

        tasks = scan_and_create_tasks(max_tasks=10)
        assert len(tasks) == 1

        ctx = tasks[0].prompt_context
        assert "Token expired" in ctx
        assert "600519.SH" in ctx
        assert "roe" in ctx
        assert "Missing fields" in ctx
        assert "Relevant Code" in ctx
        assert "Recommended Fix Strategy" in ctx
        assert "Recommended Actions" in ctx
        assert "Agent Instructions" in ctx


class TestMarkTask:
    def test_mark_task_done(self):
        _, issue = record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        task_id = tasks[0].task_id

        mark_task(task_id, "done", "Fixed by adding retry logic")

        session = db_mod.get_session()
        updated = session.query(DataFixTask).filter(DataFixTask.task_id == task_id).first()
        assert updated.status == TaskStatus.DONE
        assert "retry logic" in updated.result_summary

    def test_mark_task_invalid_status_falls_back_to_failed(self):
        _, issue = record_failure(
            provider="tushare", api_name="income", error_type="timeout",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        mark_task(tasks[0].task_id, "garbage_status")

        session = db_mod.get_session()
        updated = session.query(DataFixTask).filter(DataFixTask.task_id == tasks[0].task_id).first()
        assert updated.status == TaskStatus.FAILED


class TestMarkIssueFixed:
    def test_mark_issue_fixed(self):
        _, issue = record_failure(
            provider="tushare", api_name="income", error_type="timeout",
        )
        mark_issue_fixed(issue.issue_id, "Switched to AkShare fallback")

        session = db_mod.get_session()
        updated = (
            session.query(DataFetchIssue)
            .filter(DataFetchIssue.issue_id == issue.issue_id)
            .first()
        )
        assert updated is not None
        assert updated.status == IssueStatus.FIXED
        assert "AkShare" in (updated.resolution_note or "")

    def test_mark_issue_fixed_closes_tasks(self):
        _, issue = record_failure(
            provider="tushare", api_name="income", error_type="timeout",
        )
        scan_and_create_tasks(max_tasks=10)
        mark_issue_fixed(issue.issue_id)

        session = db_mod.get_session()
        tasks = (
            session.query(DataFixTask)
            .filter(DataFixTask.issue_id == issue.issue_id)
            .all()
        )
        for t in tasks:
            assert t.status == TaskStatus.DONE


class TestGetOpenTasks:
    def test_returns_only_pending_and_running(self):
        _, issue = record_failure(
            provider="tushare", api_name="income", error_type="timeout",
        )
        result = scan_and_create_tasks(max_tasks=10)
        task = result[0]

        tasks = get_open_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_id == task.task_id

        mark_task(task.task_id, "done", "Fixed")
        tasks = get_open_tasks()
        assert len(tasks) == 0
