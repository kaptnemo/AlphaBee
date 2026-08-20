"""通用异步任务框架（JobStore + 三件套工具注册）测试。"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from alphabee.mcp.jobs import (
    JobStatus,
    JobStore,
    register_job_tools,
)


def _call_tool(tool, *args):
    """调用 FastMCP Tool（fn 可能是同步包装或协程函数，统一处理）。"""
    result = tool.fn(*args)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def _status(job_store: JobStore, kind: str, task_id: str) -> dict:
    st = job_store.status(kind, task_id)
    assert st is not None
    return st


def test_jobstore_lifecycle(tmp_path):
    store = JobStore(root=tmp_path)
    task_id = store.create("demo", payload={"x": 1})
    job_path = tmp_path / "demo" / task_id / "job.json"
    assert job_path.is_file()

    st = _status(store, "demo", task_id)
    assert st["status"] == JobStatus.QUEUED
    assert st["kind"] == "demo"
    assert st["progress"] == 0.0

    assert store.start("demo", task_id) is True
    assert _status(store, "demo", task_id)["status"] == JobStatus.RUNNING

    store.update("demo", task_id, progress=50.0, message="处理中")
    assert _status(store, "demo", task_id)["progress"] == 50.0

    store.succeed("demo", task_id, result={"path": "/tmp/x.md"})
    st = _status(store, "demo", task_id)
    assert st["status"] == JobStatus.SUCCEEDED
    assert st["result"] == {"path": "/tmp/x.md"}
    assert st["completed_at"] is not None

    # 终态后 cancel 不再改动成功结果（status 保持 succeeded？不——cancel 置 cancelled）
    # 说明：cancel 对已终态任务仅置 cancelled 并返回 True（状态机由调用方约束使用时机）
    store.cancel("demo", task_id)
    assert _status(store, "demo", task_id)["status"] == JobStatus.CANCELLED


def test_jobstore_fail_and_load(tmp_path):
    store = JobStore(root=tmp_path)
    task_id = store.create("demo")
    store.start("demo", task_id)
    store.fail("demo", task_id, error="boom")
    st = _status(store, "demo", task_id)
    assert st["status"] == JobStatus.FAILED
    assert st["error"] == "boom"

    assert store.load("demo", "missing") is None
    assert store.status("demo", "missing") is None
    assert store.cancel("demo", "missing") is False


def test_jobstore_workspace_resolver_mode(tmp_path):
    """workspace_resolver 模式：job.json 落在 resolver(task_id) 返回的工作区。"""

    def resolver(task_id: str) -> Path:
        return tmp_path / "tasks" / task_id

    store = JobStore(workspace_resolver=resolver, list_dir=tmp_path / "tasks")
    task_id = store.create("pdf_ocr", payload={"pdf_path": "/tmp/a.pdf"})
    assert (tmp_path / "tasks" / task_id / "job.json").is_file()

    store.start("pdf_ocr", task_id)
    store.succeed("pdf_ocr", task_id, result={"markdown_path": "/tmp/a.cleaned.md"})
    st = _status(store, "pdf_ocr", task_id)
    assert st["status"] == JobStatus.SUCCEEDED

    # 再建一个任务，list 应返回 2 个
    task2 = store.create("pdf_ocr", payload={})
    listed = store.list(kind="pdf_ocr")
    assert {t["task_id"] for t in listed} == {task_id, task2}
    # 按状态过滤
    assert {t["task_id"] for t in store.list(kind="pdf_ocr", status=JobStatus.SUCCEEDED)} == {task_id}


def test_jobstore_payload_persisted(tmp_path):
    store = JobStore(root=tmp_path)
    payload = {"pdf_path": "/tmp/r.pdf", "keep_pages": True, "nested": {"a": 1}}
    task_id = store.create("pdf_ocr", payload=payload)
    job = store.load("pdf_ocr", task_id)
    assert job["payload"] == payload


def test_register_job_tools_registers_trio():
    """register_job_tools 注册 status/wait/list/cancel/result 五件套。"""
    mcp = FastMCP("test-jobs")
    store = JobStore(root=Path(__import__("tempfile").mkdtemp()))
    register_job_tools(mcp, kind="pdf_ocr", store=store, result_renderer=lambda s, t: {"markdown_path": "/x.md"})

    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert names == {
        "get_pdf_ocr_status",
        "wait_pdf_ocr_task",
        "list_pdf_ocr_tasks",
        "cancel_pdf_ocr_task",
        "get_pdf_ocr_result",
    }


def test_generated_tools_behavior(tmp_path):
    """生成工具的实际行为：status/list/cancel/result。"""
    mcp = FastMCP("test-jobs")
    store = JobStore(root=tmp_path)
    register_job_tools(mcp, kind="pdf_ocr", store=store, result_renderer=lambda s, t: {"markdown_path": "/x.md"})

    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    task_id = store.create("pdf_ocr", payload={})

    # status
    st = _call_tool(tools["get_pdf_ocr_status"], task_id)
    assert st["status"] == JobStatus.QUEUED

    # list
    lst = _call_tool(tools["list_pdf_ocr_tasks"], None, 50)
    assert lst["count"] == 1
    assert lst["tasks"][0]["task_id"] == task_id

    # result：未成功时报错
    with pytest.raises(ValueError, match="尚未成功"):
        _call_tool(tools["get_pdf_ocr_result"], task_id)

    # 成功后可取结果
    store.succeed("pdf_ocr", task_id, result={"markdown_path": "/x.md"})
    res = _call_tool(tools["get_pdf_ocr_result"], task_id)
    assert res["markdown_path"] == "/x.md"

    # cancel
    task2 = store.create("pdf_ocr", payload={})
    c = _call_tool(tools["cancel_pdf_ocr_task"], task2)
    assert c["cancelled"] is True
    assert c["status"] == JobStatus.CANCELLED
    assert store.is_cancelled("pdf_ocr", task2)

    # 不存在的任务
    with pytest.raises(FileNotFoundError):
        _call_tool(tools["get_pdf_ocr_status"], "missing")
    with pytest.raises(FileNotFoundError):
        _call_tool(tools["get_pdf_ocr_result"], "missing")
    with pytest.raises(FileNotFoundError):
        _call_tool(tools["wait_pdf_ocr_task"], "missing")


def test_wait_task_blocks_until_terminal(tmp_path):
    """wait_<kind>_task 阻塞到终态；超时返回当前状态。"""
    import threading

    mcp = FastMCP("test-jobs")
    store = JobStore(root=tmp_path)
    register_job_tools(mcp, kind="pdf_ocr", store=store, result_renderer=lambda s, t: {"markdown_path": "/x.md"})
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    task_id = store.create("pdf_ocr", payload={})
    store.start("pdf_ocr", task_id)

    # 后台线程 0.3s 后标记成功
    def _finish():
        import time

        time.sleep(0.3)
        store.succeed("pdf_ocr", task_id, result={"markdown_path": "/x.md"})

    t = threading.Thread(target=_finish)
    t.start()

    # 阻塞等待（timeout 足够），应返回 succeeded
    st = _call_tool(tools["wait_pdf_ocr_task"], task_id, 10.0, 0.05)
    assert st["status"] == JobStatus.SUCCEEDED
    t.join()

    # 超时路径：新任务保持 running，timeout 极小 → 返回当前 running 状态
    task2 = store.create("pdf_ocr", payload={})
    store.start("pdf_ocr", task2)
    st2 = _call_tool(tools["wait_pdf_ocr_task"], task2, 0.1, 0.02)
    assert st2["status"] == JobStatus.RUNNING


def test_jobstore_json_structure(tmp_path):
    store = JobStore(root=tmp_path)
    task_id = store.create("demo", payload={"q": 1})
    job = json.loads((tmp_path / "demo" / task_id / "job.json").read_text(encoding="utf-8"))
    assert set(job) >= {
        "task_id", "kind", "status", "progress", "message", "created_at",
        "started_at", "completed_at", "payload", "result", "error", "pid", "fingerprint",
    }
