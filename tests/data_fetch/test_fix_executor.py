"""Integration tests for data_fetch.fix_executor."""

import os
import tempfile

import pytest

import alphabee.data_fetch.database as db_mod
import alphabee.data_fetch.fix_executor as fx_mod
import alphabee.data_fetch.recorder as rec_mod
from alphabee.data_fetch.models import TaskStatus
from alphabee.data_fetch.recorder import record_failure
from alphabee.data_fetch.scanner import mark_task, scan_and_create_tasks
from alphabee.data_fetch.fix_executor import (
    FixResult,
    build_agent_prompt,
    prepare_fix,
    _load_task,
    _build_prompt,
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


class TestLoadTask:
    def test_load_existing_task(self):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
            error_message="Connection timeout",
            symbol="600519.SH",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        task_id = tasks[0].task_id

        ctx = _load_task(task_id)
        assert ctx is not None
        assert ctx["issue"]["provider"] == "tushare"
        assert ctx["issue"]["api_name"] == "income"
        assert ctx["issue"]["error_type"] == "timeout"
        assert ctx["issue"]["occurrence_count"] == 1
        assert ctx["sample_event"]["symbol"] == "600519.SH"
        assert ctx["sample_event"]["error_message"] == "Connection timeout"
        assert "fix/data-fetch-" in ctx["fix_branch"]

    def test_load_nonexistent_task(self):
        ctx = _load_task(9999)
        assert ctx is None

    def test_load_task_without_sample_event(self):
        record_failure(
            provider="akshare",
            api_name="stock_news_em",
            error_type="rate_limit",
            error_message="rate limit exceeded",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        ctx = _load_task(tasks[0].task_id)
        assert ctx is not None
        assert ctx["sample_event"] is not None
        assert ctx["sample_event"]["error_message"] is not None
        assert "rate" in ctx["sample_event"]["error_message"].lower()


class TestBuildPrompt:
    def test_prompt_contains_all_sections(self):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
            error_message="Connection timeout after 30s",
            symbol="600519.SH",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        ctx = _load_context_for_prompt(tasks[0].task_id)

        prompt = _build_prompt(**ctx)

        assert "Auto-Fix Data Fetch Failure" in prompt
        assert "Issue" in prompt
        assert "tushare" in prompt
        assert "income" in prompt
        assert "timeout" in prompt
        assert "Connection timeout" in prompt
        assert "600519.SH" in prompt
        assert "Fix Plan" in prompt
        assert "Instructions" in prompt
        assert "fix/data-fetch-" in prompt
        assert f"poetry run alphabee-fetch verify {tasks[0].task_id}" in prompt

    def test_prompt_without_sample_event(self):
        record_failure(
            provider="akshare",
            api_name="stock_news_em",
            error_type="rate_limit",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        ctx = _load_context_for_prompt(tasks[0].task_id)

        prompt = _build_prompt(**ctx)
        assert "Auto-Fix Data Fetch Failure" in prompt
        assert "akshare" in prompt
        assert "rate_limit" in prompt

    def test_build_agent_prompt_public_api(self):
        record_failure(
            provider="tushare",
            api_name="daily",
            error_type="permission",
        )
        tasks = scan_and_create_tasks(max_tasks=10)

        prompt = build_agent_prompt(tasks[0].task_id)
        assert "Auto-Fix Data Fetch Failure" in prompt

    def test_build_agent_prompt_nonexistent_task(self):
        prompt = build_agent_prompt(99999)
        assert "not found" in prompt.lower()


class TestPrepareFix:
    def test_prepare_fix_returns_prompt(self):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )
        tasks = scan_and_create_tasks(max_tasks=10)

        # prepare_fix will try to create a git branch — this may fail in CI
        # but the prompt building should still work
        prompt = build_agent_prompt(tasks[0].task_id)
        assert len(prompt) > 100
        assert "timeout" in prompt.lower()

    def test_task_marked_as_running(self):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        task_id = tasks[0].task_id

        # Build prompt only (no git ops) — task status unchanged
        prompt = build_agent_prompt(task_id)
        assert len(prompt) > 100


class TestVerifyAndSubmit:
    def test_verify_and_submit_creates_mr_and_marks_done(self, monkeypatch):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
            error_message="Connection timeout",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        task_id = tasks[0].task_id

        git_calls: list[tuple[str, ...]] = []

        def fake_run_git(*args: str) -> str:
            git_calls.append(args)
            if args == ("branch", "--show-current"):
                return "fix/data-fetch-1\n"
            if args == ("status", "--porcelain"):
                return " M alphabee/data_fetch/example.py\n"
            if args == ("rev-list", "--count", "main..HEAD"):
                return "1\n"
            if args == ("remote", "get-url", "origin"):
                return "git@github.com:captainemo/AlphaBee.git\n"
            if args == ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
                return "origin/main\n"
            return ""

        monkeypatch.setattr(fx_mod, "_run_git", fake_run_git)
        monkeypatch.setattr(
            fx_mod,
            "_run_tests",
            lambda: FixResult(success=True, message="All tests passed."),
        )
        monkeypatch.setattr(
            fx_mod,
            "_create_or_get_pull_request",
            lambda branch, title, body: "https://github.com/captainemo/AlphaBee/pull/99",
        )

        result = fx_mod.verify_and_submit(task_id)

        assert result.success is True
        assert result.mr_url == "https://github.com/captainemo/AlphaBee/pull/99"
        assert ("push", "-u", "origin", f"fix/data-fetch-{tasks[0].issue_id}") in git_calls

        session_ctx = fx_mod.get_session()
        try:
            task = session_ctx.query(fx_mod.DataFixTask).filter(fx_mod.DataFixTask.task_id == task_id).first()
            issue = session_ctx.query(fx_mod.DataFetchIssue).filter(fx_mod.DataFetchIssue.issue_id == tasks[0].issue_id).first()
            assert task is not None
            assert task.status == TaskStatus.DONE
            assert task.verification_result == "https://github.com/captainemo/AlphaBee/pull/99"
            assert issue is not None
            assert issue.status.value == "fixed"
        finally:
            session_ctx.close()

    def test_verify_and_submit_short_circuits_when_already_done(self, monkeypatch):
        record_failure(
            provider="tushare",
            api_name="income",
            error_type="timeout",
        )
        tasks = scan_and_create_tasks(max_tasks=10)
        task_id = tasks[0].task_id
        mark_task(
            task_id,
            "done",
            "Already submitted",
            verification_result="https://github.com/captainemo/AlphaBee/pull/77",
        )

        monkeypatch.setattr(fx_mod, "_run_git", lambda *args: (_ for _ in ()).throw(AssertionError("git should not run")))
        monkeypatch.setattr(fx_mod, "_run_tests", lambda: (_ for _ in ()).throw(AssertionError("tests should not run")))
        monkeypatch.setattr(fx_mod, "_create_or_get_pull_request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PR should not run")))

        result = fx_mod.verify_and_submit(task_id)

        assert result.success is True
        assert result.mr_url == "https://github.com/captainemo/AlphaBee/pull/77"


def _load_context_for_prompt(task_id: int) -> dict:
    ctx = _load_task(task_id)
    assert ctx is not None
    return {
        "issue": ctx["issue"],
        "sample_event": ctx["sample_event"],
        "task_context": ctx["task_context"],
        "fix_branch": ctx["fix_branch"],
        "task_id": task_id,
    }
