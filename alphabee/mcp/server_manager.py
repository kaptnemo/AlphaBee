"""自启动/自停止的 PDF OCR MCP 服务管理器。

Agent 工厂在**创建时**调用 :meth:`PdfOcrMCPServerManager.start`，以子进程方式拉起
``alphabee.mcp.pdf_ocr_server``（streamable-http 传输），等端口就绪后把 URL 交给
``MultiServerMCPClient`` 连接；进程退出时通过 ``atexit`` / :meth:`stop` 自动回收，
避免遗留孤儿 MCP 服务进程。

设计说明
--------
- 使用 streamable-http 而非 stdio：``langchain_mcp_adapters`` 的 MCP 工具是
  「每次调用新建 session」的，stdio 会为每次工具调用重新拉起一个子进程
  （paddleocr 导入开销大、上传状态丢失）；streamable-http 则复用一个常驻服务进程。
- 端口自动挑选（``port=0``），多 agent 并发创建时互不冲突。
- 就绪探测用 TCP 连接轮询：子进程完成模块导入并绑定端口后即视为就绪。
- 同一个进程内可同时管理多个服务（registry），``stop_all_pdf_ocr_servers`` 一键回收。
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# 进程内已启动的服务管理器（用于统一回收）
_ACTIVE_MANAGERS: list[PdfOcrMCPServerManager] = []
_ACTIVE_LOCK = threading.Lock()


def _register(manager: PdfOcrMCPServerManager) -> None:
    with _ACTIVE_LOCK:
        if manager not in _ACTIVE_MANAGERS:
            _ACTIVE_MANAGERS.append(manager)


def _unregister(manager: PdfOcrMCPServerManager) -> None:
    with _ACTIVE_LOCK:
        if manager in _ACTIVE_MANAGERS:
            _ACTIVE_MANAGERS.remove(manager)


def get_active_pdf_ocr_servers() -> list[PdfOcrMCPServerManager]:
    """返回本进程内所有已启动的 PDF OCR MCP 服务管理器（用于提前 stop / 查询 URL）。"""
    with _ACTIVE_LOCK:
        return list(_ACTIVE_MANAGERS)


def stop_all_pdf_ocr_servers() -> None:
    """停止所有由本进程启动的 PDF OCR MCP 服务。"""
    with _ACTIVE_LOCK:
        managers = list(_ACTIVE_MANAGERS)
    for manager in managers:
        try:
            manager.stop()
        except Exception:  # noqa: BLE001 - 清理路径不允许抛错
            pass


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class PdfOcrMCPServerManager:
    """管理一个 PDF OCR MCP 服务子进程的生命周期。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        timeout: float = 90.0,
        log_file: str | Path | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.host = host
        self.port = port if port else _pick_free_port(host)
        self.timeout = timeout
        self.python_executable = python_executable or sys.executable
        self.log_file = Path(log_file) if log_file else None
        self.process: subprocess.Popen | None = None
        self.url: str | None = None
        self._started = False

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self) -> PdfOcrMCPServerManager:
        """启动服务子进程并等待端口就绪；返回 self 便于链式调用。"""
        if self._started:
            return self
        if self.process is not None and self.process.poll() is None:
            self._started = True
            return self

        args = [
            self.python_executable,
            "-m",
            "alphabee.mcp.pdf_ocr_server",
            "--transport",
            "streamable-http",
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        env = dict(os.environ)
        # 子进程从零导入 alphabee 包，需要能看到项目根目录
        project_root = str(Path(__file__).resolve().parents[2])
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            project_root + os.pathsep + existing_pythonpath if existing_pythonpath else project_root
        )

        log_handle = None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(self.log_file, "a", encoding="utf-8")

        try:
            self.process = subprocess.Popen(
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle or subprocess.DEVNULL,
                stderr=log_handle or subprocess.DEVNULL,
            )
        except Exception:
            if log_handle is not None:
                log_handle.close()
            raise

        if not _wait_for_port(self.host, self.port, self.timeout):
            self.stop()
            raise TimeoutError(
                f"PDF OCR MCP server did not become ready within {self.timeout}s "
                f"({self.host}:{self.port})"
            )

        self.url = f"http://{self.host}:{self.port}/mcp"
        self._started = True
        _register(self)
        atexit.register(self.stop)
        return self

    def stop(self) -> None:
        """停止服务子进程并回收资源（可重复调用）。"""
        self._started = False
        proc = self.process
        self.process = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
            except OSError:
                pass
        _unregister(self)

    def __enter__(self) -> PdfOcrMCPServerManager:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - 解释器退出时的兜底
            pass
