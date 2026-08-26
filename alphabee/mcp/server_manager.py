"""自启动/自停止的 PDF OCR MCP 服务管理器。

Agent 工厂在**创建时**调用 :meth:`PdfOcrMCPServerManager.start` 拉起
``alphabee.mcp.pdf_ocr_server``，并把连接配置（``servers_config``）交给
``MultiServerMCPClient``；进程退出时通过 ``atexit`` / :meth:`stop` 自动回收。

支持的 transport（:meth:`start` 前在构造时指定 ``transport=``）：

- ``"streamable-http"``（默认）：以常驻子进程方式启动 HTTP 服务，等端口就绪后返回
  ``http://<host>:<port>/mcp``。一次拉起、多次工具调用复用同一服务进程；
- ``"stdio"``：不保留常驻进程。``MultiServerMCPClient`` 会在**每次工具调用**时
  以 stdio 方式拉起一个新的服务子进程（进程间直接管道通信，无 HTTP 序列化开销）。
  上传的 PDF 与 OCR 任务产物都持久化在磁盘（``outputs/pdf_ocr/``），跨调用不丢状态；
  ``start()`` 只做一次"子进程能正常启动"的冒烟探测（拉起 → 存活确认 → 立即回收）。

设计说明
--------
- 端口自动挑选（``port=0``，仅 streamable-http 需要），多 agent 并发创建时互不冲突。
- streamable-http 就绪探测用 TCP 连接轮询；stdio 用启动冒烟探测。
- 同一个进程内可同时管理多个服务（registry），``stop_all_pdf_ocr_servers`` 一键回收。
- **stdio 协议安全**：服务进程的 stdout 是 MCP 协议通道，loader 的所有诊断输出
  （进度条、重试日志等）都写入 stderr，不会污染协议。
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
from typing import Any

# 进程内已启动的服务管理器（用于统一回收）
_ACTIVE_MANAGERS: list[PdfOcrMCPServerManager] = []
_ACTIVE_LOCK = threading.Lock()

VALID_TRANSPORTS = ("streamable-http", "stdio")


def _register(manager: PdfOcrMCPServerManager) -> None:
    with _ACTIVE_LOCK:
        if manager not in _ACTIVE_MANAGERS:
            _ACTIVE_MANAGERS.append(manager)


def _unregister(manager: PdfOcrMCPServerManager) -> None:
    with _ACTIVE_LOCK:
        if manager in _ACTIVE_MANAGERS:
            _ACTIVE_MANAGERS.remove(manager)


def get_active_pdf_ocr_servers() -> list[PdfOcrMCPServerManager]:
    """返回本进程内所有已启动的 PDF OCR MCP 服务管理器（用于提前 stop / 查询配置）。"""
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
    """管理一个 PDF OCR MCP 服务子进程的生命周期（支持 streamable-http / stdio）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        transport: str = "streamable-http",
        timeout: float = 90.0,
        log_file: str | Path | None = None,
        python_executable: str | None = None,
    ) -> None:
        if transport not in VALID_TRANSPORTS:
            raise ValueError(f"transport must be one of {VALID_TRANSPORTS}, got: {transport!r}")
        self.transport = transport
        self.host = host
        # 0 = 启动时自动挑选（仅 streamable-http 需要端口）
        self.port = port
        self.timeout = timeout
        self.python_executable = python_executable or sys.executable
        self.log_file = Path(log_file) if log_file else None
        self.process: subprocess.Popen[bytes] | None = None
        self.url: str | None = None
        self._started = False

    # ── 配置构造 ──────────────────────────────────────────────────────────

    @property
    def command_args(self) -> list[str]:
        """拉起服务子进程的完整命令行（``[python, -m, alphabee.mcp.pdf_ocr_server, ...]``）。"""
        args = [
            self.python_executable,
            "-m",
            "alphabee.mcp.pdf_ocr_server",
            "--transport",
            self.transport,
        ]
        if self.transport == "streamable-http":
            args += ["--host", self.host, "--port", str(self.port)]
        return args

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # 子进程从零导入 alphabee 包，需要能看到项目根目录
        project_root = str(Path(__file__).resolve().parents[2])
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = project_root + os.pathsep + existing_pythonpath if existing_pythonpath else project_root
        return env

    @property
    def servers_config(self) -> dict[str, Any]:
        """``MultiServerMCPClient`` 可直接使用的服务器连接配置。"""
        if self.transport == "streamable-http":
            if self.url is None:
                raise RuntimeError("PdfOcrMCPServerManager.start() must be called before servers_config")
            return {
                "pdf_ocr": {
                    "transport": "streamable-http",
                    "url": self.url,
                }
            }
        return {
            "pdf_ocr": {
                "transport": "stdio",
                "command": self.python_executable,
                "args": self.command_args[1:],
                "env": self._build_env(),
            }
        }

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self, transport: str | None = None) -> PdfOcrMCPServerManager:
        """启动 MCP 服务；返回 self 便于链式调用。可重复调用（幂等）。

        Args:
            transport: 覆盖构造时指定的传输模式（``"streamable-http"`` / ``"stdio"``）。
                仅在首次启动前生效；已启动后忽略。streamable-http 且端口为 0 时
                在启动时自动挑选空闲端口。
        """
        if transport is not None:
            if transport not in VALID_TRANSPORTS:
                raise ValueError(f"transport must be one of {VALID_TRANSPORTS}, got: {transport!r}")
            # 已启动后忽略 transport 变更，保持当前连接配置稳定
            if not self._started:
                self.transport = transport

        if self._started:
            return self
        if self.process is not None and self.process.poll() is None:
            self._started = True
            return self

        if self.transport == "streamable-http":
            if not self.port:
                self.port = _pick_free_port(self.host)
            self._start_streamable_http()
        else:
            self._probe_stdio_server()

        self._started = True
        _register(self)
        if self.transport == "streamable-http":
            atexit.register(self.stop)
        return self

    def _start_streamable_http(self) -> None:
        args = self.command_args
        env = self._build_env()

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
                f"PDF OCR MCP server did not become ready within {self.timeout}s ({self.host}:{self.port})"
            )
        self.url = f"http://{self.host}:{self.port}/mcp"

    def _probe_stdio_server(self) -> None:
        """stdio 冒烟探测：拉起服务子进程，确认能正常启动（导入不崩溃）后立即回收。

        真正的工具调用由 ``MultiServerMCPClient`` 在每次调用时另行拉起子进程；
        这里只负责在 agent 创建时验证服务可用。
        """
        args = self.command_args
        env = self._build_env()

        log_handle = None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(self.log_file, "a", encoding="utf-8")

        probe: subprocess.Popen[bytes] | None = None
        try:
            probe = subprocess.Popen(
                args,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log_handle or subprocess.DEVNULL,
                stderr=log_handle or subprocess.DEVNULL,
            )
        except Exception:
            if log_handle is not None:
                log_handle.close()
            raise

        # 启动冒烟窗口：进程若在窗口内退出（如 import 失败）即为启动失败；
        # 存活到窗口结束说明服务正常等待 stdin，可以回收。
        probe_deadline = min(self.timeout, 15.0)
        deadline = time.monotonic() + probe_deadline
        while time.monotonic() < deadline:
            if probe.poll() is not None:
                self._terminate_probe(probe)
                raise RuntimeError(
                    f"PDF OCR MCP server (stdio) exited during startup probe: returncode={probe.returncode}"
                )
            time.sleep(0.2)
        self._terminate_probe(probe)

    @staticmethod
    def _terminate_probe(probe: subprocess.Popen[bytes]) -> None:
        if probe.poll() is None:
            try:
                probe.terminate()
                probe.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                probe.kill()
                probe.wait(timeout=5.0)
            except OSError:
                pass

    def stop(self) -> None:
        """停止服务（streamable-http 终止常驻进程；stdio 无常驻进程，为 no-op）。可重复调用。"""
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
