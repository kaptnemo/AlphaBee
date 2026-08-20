"""通用异步任务框架：JobStore（任务注册表）+ 三件套 MCP 工具注册。

背景：PDF→Markdown 这类「步骤多、耗时长、结果体积大」的任务如果封装成同步 MCP 工具，
会有超时、上下文膨胀、无进度、不可恢复等问题。本模块提供标准解法：

- :class:`JobStore`：把每个长任务收敛成一个任务目录（``job.json`` 记录
  queued/running/succeeded/failed/cancelled 状态机 + 进度 + 产物路径），
  提交后立即返回 ``task_id``，任何时刻可查询/续作/取消；
- :func:`register_job_tools`：为某个任务域自动生成三件套工具
  ``get_<kind>_status`` / ``list_<kind>_tasks`` / ``cancel_<kind>_task``
  （结果工具 ``get_<kind>_result`` 由调用方提供 ``result_renderer`` 生成）。

任务域接入方式（以 PDF OCR 为例）：:

    store = JobStore(workspace_resolver=get_task_workspace, list_dir=TASKS_DIR)
    register_job_tools(mcp, kind="pdf_ocr", store=store, result_renderer=_render_result)

    @mcp.tool()
    def submit_pdf_ocr(...) -> SubmitResult:   # 域专属：解析输入 → store.create → 拉起 worker
        task_id = store.create("pdf_ocr", payload=...)
        _spawn_worker(task_id)
        return SubmitResult(task_id=task_id, status="queued", ...)

执行模型：submit 只负责登记任务并拉起**独立 worker 子进程**（``--run-job <task_id>``），
worker 在子进程里跑重活并回写 job.json。这样：
- streamable-http 常驻服务与 stdio 按调用拉起子进程两种传输都适用（任务不随调用进程退出而丢失）；
- 任务进程崩溃不影响 MCP 服务进程；
- cancel 可终止 worker 进程（job.json 里记录 pid）。
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from alphabee import PROJECT_ROOT

# ── 任务状态常量 ────────────────────────────────────────────────────────────


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = (SUCCEEDED, FAILED, CANCELLED)


class JobCancelledError(RuntimeError):
    """任务被取消（由 cancel 工具置位后，worker 在下一个检查点抛出）。"""


# ── 任务状态模型（MCP 返回结构） ───────────────────────────────────────────


class TaskStatus:
    """任务状态的轻量结构（可直接 ``model_dump`` 成 dict 供 MCP 返回）。"""

    __slots__ = ("task_id", "kind", "status", "progress", "message", "created_at", "started_at", "completed_at",
                 "error", "result", "pid")

    def __init__(self, job: dict[str, Any]) -> None:
        self.task_id = job.get("task_id", "")
        self.kind = job.get("kind", "")
        self.status = job.get("status", JobStatus.QUEUED)
        self.progress = float(job.get("progress") or 0.0)
        self.message = job.get("message")
        self.created_at = job.get("created_at")
        self.started_at = job.get("started_at")
        self.completed_at = job.get("completed_at")
        self.error = job.get("error")
        self.result = job.get("result")
        self.pid = job.get("pid")

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result": self.result,
            "pid": self.pid,
        }


# ── JobStore：任务注册表 ───────────────────────────────────────────────────


class JobStore:
    """持久化任务注册表。

    每个任务一个目录 + ``job.json``（状态机 / 进度 / 产物 / pid）。

    Args:
        root: 默认任务根目录（仅 ``workspace_resolver`` 为 None 时使用：
            ``<root>/<kind>/<task_id>/``）。
        workspace_resolver: 由 ``task_id`` 解析任务工作区目录的函数
            （如 PDF OCR 的 ``get_task_workspace``），``job.json`` 写在
            该工作区内，与任务产物同目录。
        list_dir: 列出任务时扫描的目录（默认 ``root/<kind>``）。
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        workspace_resolver: Callable[[str], Path] | None = None,
        list_dir: str | Path | None = None,
    ) -> None:
        self.root = Path(root) if root else PROJECT_ROOT / "outputs" / "jobs"
        self.workspace_resolver = workspace_resolver
        self.list_dir = Path(list_dir) if list_dir else None

    # ── 路径 ──────────────────────────────────────────────────────────────

    def job_path(self, kind: str, task_id: str) -> Path:
        base = self.workspace_resolver(task_id) if self.workspace_resolver else self.root / kind / task_id
        return base / "job.json"

    def _scan_root(self, kind: str) -> Path:
        if self.list_dir is not None:
            return self.list_dir
        return self.root / kind

    # ── 读写 ──────────────────────────────────────────────────────────────

    def create(
        self,
        kind: str,
        *,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> str:
        """登记一个新任务（status=queued），返回 task_id。"""
        task_id = task_id or f"{kind}-{uuid4().hex[:12]}"
        job: dict[str, Any] = {
            "task_id": task_id,
            "kind": kind,
            "status": JobStatus.QUEUED,
            "progress": 0.0,
            "message": "queued",
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "completed_at": None,
            "payload": payload or {},
            "result": None,
            "error": None,
            "pid": None,
            "fingerprint": fingerprint,
        }
        self._write(kind, task_id, job)
        return task_id

    def load(self, kind: str, task_id: str) -> dict[str, Any] | None:
        path = self.job_path(kind, task_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, kind: str, task_id: str, job: dict[str, Any]) -> None:
        path = self.job_path(kind, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    def update(self, kind: str, task_id: str, **updates: Any) -> dict[str, Any] | None:
        """就地更新并持久化 job.json（读-改-写合并）。"""
        job = self.load(kind, task_id)
        if job is None:
            return None
        job.update(updates)
        self._write(kind, task_id, job)
        return job

    # ── 状态机 ────────────────────────────────────────────────────────────

    def start(self, kind: str, task_id: str) -> bool:
        job = self.load(kind, task_id)
        if job is None:
            return False
        if job.get("status") == JobStatus.CANCELLED:
            # 已被取消：保持取消状态，不允许 worker 覆盖为 running
            return True
        self.update(
            kind,
            task_id,
            status=JobStatus.RUNNING,
            message="running",
            started_at=datetime.now(UTC).isoformat(),
        )
        return True

    def succeed(self, kind: str, task_id: str, result: dict[str, Any]) -> bool:
        job = self.update(
            kind,
            task_id,
            status=JobStatus.SUCCEEDED,
            progress=100.0,
            message="succeeded",
            result=result,
            error=None,
            completed_at=datetime.now(UTC).isoformat(),
        )
        return job is not None

    def fail(self, kind: str, task_id: str, error: str) -> bool:
        job = self.update(
            kind,
            task_id,
            status=JobStatus.FAILED,
            message="failed",
            error=error,
            completed_at=datetime.now(UTC).isoformat(),
        )
        return job is not None

    def cancel(self, kind: str, task_id: str) -> bool:
        """取消任务：置状态为 cancelled；若 worker 还在运行则终止其进程。"""
        job = self.load(kind, task_id)
        if job is None:
            return False
        already_terminal = job.get("status") in JobStatus.TERMINAL
        self.update(
            kind,
            task_id,
            status=JobStatus.CANCELLED,
            message="cancelled",
            completed_at=datetime.now(UTC).isoformat(),
        )
        pid = job.get("pid")
        if pid and not already_terminal:
            _terminate_pid(int(pid))
        return True

    def is_cancelled(self, kind: str, task_id: str) -> bool:
        job = self.load(kind, task_id)
        return bool(job) and job.get("status") == JobStatus.CANCELLED

    def status(self, kind: str, task_id: str) -> dict[str, Any] | None:
        job = self.load(kind, task_id)
        return TaskStatus(job).model_dump() if job else None

    def list(
        self,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出任务（按创建时间倒序）。kind 为 None 时列出 store 下全部任务。"""
        scan_dir = self._scan_root(kind or "")
        jobs: list[dict[str, Any]] = []
        if not scan_dir.is_dir():
            return jobs
        if kind and self.workspace_resolver is None:
            # 默认布局 root/<kind>/<task_id>/job.json
            for entry in scan_dir.iterdir():
                job = self.load(kind, entry.name)
                if job:
                    jobs.append(job)
        else:
            # 平铺布局（list_dir 或 workspace_resolver 模式）：目录即任务
            for entry in scan_dir.iterdir():
                if not entry.is_dir():
                    continue
                job_file = entry / "job.json"
                if not job_file.is_file():
                    continue
                try:
                    job = json.loads(job_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if kind and job.get("kind") != kind:
                    continue
                jobs.append(job)
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs[:limit]


def _terminate_pid(pid: int) -> None:
    """终止 worker 进程（SIGTERM，超时后 SIGKILL）。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ── 四件套工具注册 ─────────────────────────────────────────────────────────


def register_job_tools(
    mcp: Any,
    *,
    kind: str,
    store: JobStore,
    human_name: str | None = None,
    result_renderer: Callable[[JobStore, str], dict[str, Any]] | None = None,
) -> None:
    """为任务域 ``kind`` 注册通用工具（FastMCP 实例）。

    注册的工具：
    - ``get_<kind>_status(task_id)``：查询状态 + 进度（queued/running/succeeded/failed/cancelled）
    - ``wait_<kind>_task(task_id, timeout_seconds=120, poll_interval=1.0)``：
      阻塞等待任务进入终态（asyncio.sleep 非阻塞轮询），超时返回当前状态
    - ``list_<kind>_tasks(status=None, limit=50)``：任务列表（按创建时间倒序）
    - ``cancel_<kind>_task(task_id)``：取消任务（终止 worker 进程）
    - ``get_<kind>_result(task_id)``：取成功结果（需提供 ``result_renderer``）

    Args:
        mcp: FastMCP 实例。
        kind: 任务域标识（如 ``"pdf_ocr"``），用于工具命名与 job.json 的 kind 字段。
        store: 该任务域的 JobStore。
        human_name: 人类可读名称（用于工具描述，缺省取 kind）。
        result_renderer: ``(store, task_id) -> dict``；提供时注册 ``get_<kind>_result``。
    """
    label = human_name or kind

    async def get_status(task_id: str) -> dict[str, Any]:
        """查询 {label} 任务的执行状态与进度。

        返回: task_id / status（queued|running|succeeded|failed|cancelled）/
        progress(0-100) / message / error / result / pid。
        status 为 succeeded 后可调用 get_{kind}_result 取结果。
        """
        result = store.status(kind, task_id)
        if result is None:
            raise FileNotFoundError(f"{label} task does not exist: {task_id}")
        return result

    get_status.__name__ = f"get_{kind}_status"
    get_status.__doc__ = get_status.__doc__.format(label=label, kind=kind)

    async def wait_task(
        task_id: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        """**阻塞等待** {label} 任务进入终态（succeeded/failed/cancelled）。

        服务端以 asyncio.sleep 非阻塞轮询（不占用事件循环），最多等
        ``timeout_seconds`` 秒；超时则返回当前（通常为 running）状态，
        调用方可再次调用本工具继续等待。返回结构同 get_{kind}_status。
        """
        import asyncio

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            result = store.status(kind, task_id)
            if result is None:
                raise FileNotFoundError(f"{label} task does not exist: {task_id}")
            if result["status"] in JobStatus.TERMINAL:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result  # 超时：返回当前状态（调用方决定是否继续等）
            await asyncio.sleep(min(max(0.05, poll_interval), remaining))

    wait_task.__name__ = f"wait_{kind}_task"
    wait_task.__doc__ = wait_task.__doc__.format(label=label, kind=kind)

    async def list_tasks(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        """列出 {label} 任务（按创建时间倒序），可选按 status 过滤。

        返回: count + tasks（每项含 task_id / status / progress / created_at 等）。
        """
        tasks = store.list(kind=kind, status=status, limit=limit)
        return {"count": len(tasks), "tasks": tasks}

    list_tasks.__name__ = f"list_{kind}_tasks"
    list_tasks.__doc__ = list_tasks.__doc__.format(label=label, kind=kind)

    async def cancel_task(task_id: str) -> dict[str, Any]:
        """取消 {label} 任务：置为 cancelled 并终止其 worker 进程。

        返回: task_id / cancelled / status。
        """
        cancelled = store.cancel(kind, task_id)
        job = store.load(kind, task_id)
        return {
            "task_id": task_id,
            "cancelled": cancelled,
            "status": (job or {}).get("status"),
        }

    cancel_task.__name__ = f"cancel_{kind}_task"
    cancel_task.__doc__ = cancel_task.__doc__.format(label=label, kind=kind)

    mcp.tool()(get_status)
    mcp.tool()(wait_task)
    mcp.tool()(list_tasks)
    mcp.tool()(cancel_task)

    if result_renderer is not None:

        async def get_result(task_id: str) -> dict[str, Any]:
            """取 {label} 任务的执行结果（任务须已 succeeded）。

            返回: 域专属结果（产物路径 + 统计信息），不内联大内容。
            """
            job = store.load(kind, task_id)
            if job is None:
                raise FileNotFoundError(f"{label} task does not exist: {task_id}")
            if job.get("status") != JobStatus.SUCCEEDED:
                raise ValueError(
                    f"task {task_id} 尚未成功（status={job.get('status')}）。"
                    f"先用 get_{kind}_status 轮询到 succeeded 再取结果。"
                )
            return result_renderer(store, task_id)

        get_result.__name__ = f"get_{kind}_result"
        get_result.__doc__ = get_result.__doc__.format(label=label, kind=kind)
        mcp.tool()(get_result)
