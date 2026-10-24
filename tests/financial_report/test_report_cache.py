"""财报目录 index 与 fetch agent 缓存（``report_cache.py``）单测。

P0（§11）：index 构建不变量、version 稳定性与 manifest 变化、TTL 过期、LRU 上限、
key 隔离、并发只构建 1 次、构建异常不缓存、stats 计数。

P2（§5/§6/§11）：agent 缓存 single-flight、热态复用、TTL/LRU、fail-open 回退、
FileNotFoundError 透传、开关降级、失效/清空、同一 compiled graph 并发无串话。

测试只使用 ``tmp_path`` 上的真实目录/文件与 fake model，不依赖真实 LLM 或网络。
"""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from structlog.testing import capture_logs

from alphabee import PROJECT_ROOT
from alphabee.financial_report import fetch_deepagents, report_cache
from alphabee.financial_report.report_cache import (
    FETCH_PROMPT_VERSION,
    REPORT_CACHE_CONFIG,
    ReportCacheConfig,
    ReportIndex,
    aget_report_agent,
    build_report_index,
    clear_report_caches,
    get_report_index,
    invalidate_report_caches,
    report_version,
    stats,
)


def _write_report(root: Path, rel_files: list[str]) -> Path:
    """在 ``root`` 下造一份「报告」：若干 .md + 一个 manifest。"""
    root.mkdir(parents=True, exist_ok=True)
    for rel_file in rel_files:
        path = root / rel_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel_file}\n内容。\n", encoding="utf-8")
    (root / ".report_manifest.json").write_text(
        f'{{"file_count": {len(rel_files)}}}\n',
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch):
    """每个用例使用干净缓存与默认配置（避免全局状态互相污染）。"""
    clear_report_caches()
    monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig())
    yield
    clear_report_caches()


class TestBuildReportIndex:
    def test_defaults_match_design(self) -> None:
        assert REPORT_CACHE_CONFIG == ReportCacheConfig(
            enabled=True,
            index_enabled=True,
            agent_enabled=True,
            path_guard_enabled=True,
            index_ttl_s=1800.0,
            agent_ttl_s=1800.0,
            max_index_entries=64,
            max_agent_entries=16,
            small_report_full_paths=80,
        )

    def test_index_invariants(self, tmp_path: Path) -> None:
        _write_report(tmp_path, ["b/2.md", "a/1.md", "a/sub/3.md", "root.md"])
        (tmp_path / "notes.txt").write_text("非 md 文件不计入索引", encoding="utf-8")

        index = build_report_index(tmp_path)

        assert isinstance(index, ReportIndex)
        assert dataclasses.is_dataclass(index)
        assert index.root == tmp_path.resolve()
        assert index.root.is_absolute()
        # 排序稳定、posix 分隔符、无前导 /
        assert index.rel_files == ("a/1.md", "a/sub/3.md", "b/2.md", "root.md")
        assert index.rel_dirs == ("a", "a/sub", "b")
        for rel in (*index.rel_files, *index.rel_dirs):
            assert not rel.startswith("/")
            assert "\\" not in rel
        # total_files 与磁盘实际 .md 数一致
        assert index.total_files == len(index.rel_files)
        assert index.total_files == len(list(tmp_path.rglob("*.md")))
        assert index.built_at > 0
        # 压缩目录树：章节目录 + 直接 .md 数
        assert "/a (1 files)" in index.tree_compact
        assert "/a/sub (1 files)" in index.tree_compact
        assert "/b (1 files)" in index.tree_compact
        assert "/ (1 files)" in index.tree_compact

    def test_index_is_frozen(self, tmp_path: Path) -> None:
        _write_report(tmp_path, ["a.md"])
        index = build_report_index(tmp_path)
        with pytest.raises(dataclasses.FrozenInstanceError):
            index.total_files = 2  # type: ignore[misc]

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            build_report_index(tmp_path / "nope")
        with pytest.raises(FileNotFoundError):
            get_report_index(tmp_path / "nope")
        assert stats()["entries"] == 0


class TestReportVersion:
    def test_manifest_version_is_stable(self, tmp_path: Path) -> None:
        _write_report(tmp_path, ["a.md"])
        manifest_stat = (tmp_path / ".report_manifest.json").stat()
        expected = f"manifest:{manifest_stat.st_mtime_ns}:{manifest_stat.st_size}"

        assert report_version(tmp_path) == expected
        assert report_version(tmp_path) == expected

    def test_dir_fallback_without_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        expected = f"dir:{tmp_path.stat().st_mtime_ns}"
        assert report_version(tmp_path) == expected
        assert report_version(tmp_path) == expected


class TestGetReportIndex:
    def test_second_call_hits_cache(self, tmp_path: Path) -> None:
        _write_report(tmp_path, ["a.md"])

        first = get_report_index(tmp_path)
        second = get_report_index(tmp_path)

        assert first is second
        snapshot = stats()
        assert snapshot["builds"] == 1
        assert snapshot["hits"] == 1
        assert snapshot["entries"] == 1

    def test_manifest_rewrite_changes_key_and_rebuilds(self, tmp_path: Path) -> None:
        _write_report(tmp_path, ["a.md"])
        old_index = get_report_index(tmp_path)

        # 重写 manifest（size 变化）+ 新增文件 → version 变化 → 新 key
        (tmp_path / ".report_manifest.json").write_text(
            '{"file_count": 2, "rewritten": true}\n',
            encoding="utf-8",
        )
        (tmp_path / "b.md").write_text("x", encoding="utf-8")

        new_index = get_report_index(tmp_path)

        assert new_index.version != old_index.version
        assert new_index.version.startswith("manifest:")
        assert new_index.total_files == 2
        assert stats()["builds"] == 2

        # 新 key 继续命中，不再重建；旧 index 不会被当成新版本复用
        assert get_report_index(tmp_path) is new_index
        assert stats()["builds"] == 2

    def test_ttl_expiry_rebuilds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig(index_ttl_s=10.0))
        _write_report(tmp_path, ["a.md"])
        clock = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])

        first = get_report_index(tmp_path)
        clock[0] += 9.0
        assert get_report_index(tmp_path) is first
        assert stats()["builds"] == 1

        clock[0] += 2.0  # 越过 TTL
        rebuilt = get_report_index(tmp_path)
        assert rebuilt is not first
        assert stats()["builds"] == 2

    def test_lru_eviction_caps_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig(max_index_entries=2))
        roots = [_write_report(tmp_path / f"r{i}", [f"{i}.md"]) for i in range(3)]

        idx0 = get_report_index(roots[0])
        idx1 = get_report_index(roots[1])
        idx2 = get_report_index(roots[2])

        snapshot = stats()
        assert snapshot["entries"] == 2
        assert snapshot["evictions"] == 1
        # 最旧的 r0 被淘汰，r1/r2 仍命中
        assert get_report_index(roots[2]) is idx2
        assert get_report_index(roots[1]) is idx1
        assert stats()["evictions"] == 1
        # r0 需要重建，并再次触发一次淘汰
        rebuilt0 = get_report_index(roots[0])
        assert rebuilt0 is not idx0
        assert stats()["evictions"] == 2
        assert stats()["entries"] == 2

    def test_keys_isolated_per_root_and_invalidate(self, tmp_path: Path) -> None:
        root_a = _write_report(tmp_path / "a", ["1.md"])
        root_b = _write_report(tmp_path / "b", ["1.md", "2.md"])

        idx_a = get_report_index(root_a)
        idx_b = get_report_index(root_b)
        assert idx_a.root != idx_b.root
        assert idx_a.total_files == 1
        assert idx_b.total_files == 2

        invalidate_report_caches(root_a)

        assert get_report_index(root_b) is idx_b  # 其他 root 不受影响
        rebuilt_a = get_report_index(root_a)
        assert rebuilt_a is not idx_a
        assert rebuilt_a.total_files == 1
        assert stats()["builds"] == 3

        invalidate_report_caches()
        assert stats()["entries"] == 0

    def test_concurrent_callers_build_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_report(tmp_path, ["a.md", "sub/b.md"])
        real_build = report_cache.build_report_index
        calls: list[object] = []
        calls_lock = threading.Lock()

        def counting_build(root: str | Path) -> ReportIndex:
            with calls_lock:
                calls.append(root)
            time.sleep(0.05)  # 放大并发窗口，验证其余调用在等待同一个构建
            return real_build(root)

        monkeypatch.setattr(report_cache, "build_report_index", counting_build)

        results: list[ReportIndex] = []

        def worker() -> None:
            results.append(get_report_index(tmp_path))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

        assert len(calls) == 1
        assert len(results) == 10
        assert all(result is results[0] for result in results)
        assert stats()["builds"] == 1

    def test_build_failure_is_not_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_report(tmp_path, ["a.md"])
        real_build = report_cache.build_report_index
        failed_calls: list[object] = []

        def failing_build(root: str | Path) -> ReportIndex:
            failed_calls.append(root)
            raise RuntimeError("boom")

        monkeypatch.setattr(report_cache, "build_report_index", failing_build)

        with pytest.raises(RuntimeError):
            get_report_index(tmp_path)
        assert failed_calls == [tmp_path]
        assert stats()["entries"] == 0

        # 失败没有留下阻塞中的 in-flight，也没有缓存负结果：下一次仍会重建
        monkeypatch.setattr(report_cache, "build_report_index", real_build)
        recovered: list[ReportIndex] = []

        def recover() -> None:
            recovered.append(get_report_index(tmp_path))

        thread = threading.Thread(target=recover)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(recovered) == 1
        assert recovered[0].total_files == 1
        assert stats()["entries"] == 1

    def test_disabled_index_switch_skips_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig(index_enabled=False))
        _write_report(tmp_path, ["a.md"])

        first = get_report_index(tmp_path)
        second = get_report_index(tmp_path)

        assert first is not second
        assert first.total_files == second.total_files == 1
        snapshot = stats()
        assert snapshot["entries"] == 0
        assert snapshot["builds"] == 0


class TestModuleImportWeight:
    def test_report_cache_import_stays_lightweight(self) -> None:
        """report_parser 会 import 本模块：不能拖入 tools/LLM/agent 重依赖。"""
        code = (
            "import sys\n"
            "import alphabee.financial_report.report_cache  # noqa: F401\n"
            "heavy = [\n"
            "    name\n"
            "    for name in (\n"
            "        'deepagents',\n"
            "        'alphabee.tools',\n"
            "        'alphabee.tools.financial_report',\n"
            "        'alphabee.utils.llm',\n"
            "        'alphabee.financial_report.fetch_deepagents',\n"
            "    )\n"
            "    if name in sys.modules\n"
            "]\n"
            "raise SystemExit(f'heavy imports leaked: {heavy}' if heavy else 0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr


# ── P2：fetch agent 缓存 ──────────────────────────────────────────────────


class EchoChatModel(BaseChatModel):
    """假模型：回显最后一条 HumanMessage，不发真实网络请求。

    用于验证「同一 compiled graph 并发 20 个不同 query 无串话」，同时让
    ``report_cache`` 只构建 1 次 agent。
    """

    @property
    def _llm_type(self) -> str:
        return "echo-chat-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> EchoChatModel:
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_human = next((msg for msg in reversed(messages) if isinstance(msg, HumanMessage)), None)
        text = last_human.content if last_human is not None else ""
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"echo: {text}"))])

    async def _agenerate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(0.005)  # 放大并发窗口，验证各 invocation 互不串话
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.fixture
def counting_builder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """把 ``create_deep_agent`` 换成计数哨兵：不建真实模型，也能统计构建次数。"""
    calls: list[dict[str, Any]] = []
    calls_lock = threading.Lock()

    def fake_create_deep_agent(**kwargs: Any) -> object:
        with calls_lock:
            calls.append(kwargs)
        return object()

    monkeypatch.setattr(fetch_deepagents, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())
    return calls


class TestAgentCache:
    async def test_hit_reuses_same_graph(self, tmp_path: Path, counting_builder: list[dict[str, Any]]) -> None:
        _write_report(tmp_path, ["a.md"])

        first = await aget_report_agent(tmp_path)
        second = await aget_report_agent(tmp_path)

        assert first is second
        assert len(counting_builder) == 1
        snapshot = stats()
        assert snapshot["agent"] == {"hits": 1, "misses": 1, "builds": 1, "evictions": 0, "entries": 1}
        assert snapshot["builds"] == 1  # index 只构建一次
        [key] = list(report_cache._agent_entries)
        assert key[0] == str(tmp_path.resolve())
        assert key[2] == FETCH_PROMPT_VERSION
        assert key[3] == "financial_report"

    async def test_miss_build_hit_events_are_logged(
        self, tmp_path: Path, counting_builder: list[dict[str, Any]]
    ) -> None:
        _write_report(tmp_path, ["a.md"])

        with capture_logs() as logs:
            await aget_report_agent(tmp_path)
            await aget_report_agent(tmp_path)

        agent_events = [entry for entry in logs if entry.get("kind") == "agent"]
        results = [entry["result"] for entry in agent_events]
        assert "miss" in results
        assert "build" in results
        assert "hit" in results
        for entry in agent_events:
            assert entry["root"] == str(tmp_path.resolve())
            assert entry["version"].startswith("manifest:")
            assert "duration_ms" in entry
            assert "entries" in entry

    async def test_build_runs_off_event_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_report(tmp_path, ["a.md"])
        worker_threads: list[str] = []

        def fake_create_deep_agent(**kwargs: Any) -> object:
            # 构建发生在 asyncio.to_thread 的工作线程：那里没有 running loop
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            worker_threads.append(threading.current_thread().name)
            return object()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", fake_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())

        await aget_report_agent(tmp_path)

        assert worker_threads
        assert worker_threads[0] != threading.current_thread().name

    async def test_concurrent_calls_build_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_report(tmp_path, ["a.md"])
        calls: list[dict[str, Any]] = []

        def slow_create_deep_agent(**kwargs: Any) -> object:
            calls.append(kwargs)
            time.sleep(0.05)  # 放大并发窗口，验证其余调用在等同一个构建
            return object()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", slow_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())

        agents = await asyncio.gather(*(aget_report_agent(tmp_path) for _ in range(10)))

        assert len(calls) == 1
        assert all(agent is agents[0] for agent in agents)
        snapshot = stats()["agent"]
        # 等待者共享同一个 in-flight 构建，不额外计 miss/build
        assert snapshot["builds"] == 1
        assert snapshot["misses"] == 1
        assert snapshot["entries"] == 1

    async def test_key_isolation_per_root(self, tmp_path: Path, counting_builder: list[dict[str, Any]]) -> None:
        root_a = _write_report(tmp_path / "a", ["1.md"])
        root_b = _write_report(tmp_path / "b", ["1.md", "2.md"])

        agent_a, agent_b = await asyncio.gather(aget_report_agent(root_a), aget_report_agent(root_b))

        assert agent_a is not agent_b
        assert len(counting_builder) == 2
        assert stats()["agent"]["entries"] == 2

    async def test_manifest_change_rebuilds_agent(self, tmp_path: Path, counting_builder: list[dict[str, Any]]) -> None:
        _write_report(tmp_path, ["a.md"])
        old_agent = await aget_report_agent(tmp_path)

        (tmp_path / ".report_manifest.json").write_text('{"file_count": 2, "rewritten": true}\n', encoding="utf-8")
        (tmp_path / "b.md").write_text("x", encoding="utf-8")

        new_agent = await aget_report_agent(tmp_path)

        assert new_agent is not old_agent
        assert len(counting_builder) == 2
        assert stats()["agent"]["builds"] == 2

    async def test_ttl_expiry_rebuilds_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting_builder: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig(agent_ttl_s=10.0))
        _write_report(tmp_path, ["a.md"])
        clock = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])

        first = await aget_report_agent(tmp_path)
        clock[0] += 9.0
        assert await aget_report_agent(tmp_path) is first
        clock[0] += 2.0  # 越过 agent TTL
        rebuilt = await aget_report_agent(tmp_path)

        assert rebuilt is not first
        assert stats()["agent"]["builds"] == 2

    async def test_lru_eviction_caps_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting_builder: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig(max_agent_entries=2))
        roots = [_write_report(tmp_path / f"r{index}", [f"{index}.md"]) for index in range(3)]

        first, second, third = [await aget_report_agent(root) for root in roots]

        snapshot = stats()["agent"]
        assert snapshot["entries"] == 2
        assert snapshot["evictions"] == 1
        assert await aget_report_agent(roots[2]) is third
        assert await aget_report_agent(roots[1]) is second
        rebuilt_first = await aget_report_agent(roots[0])
        assert rebuilt_first is not first
        assert stats()["agent"]["evictions"] == 2

    async def test_build_failure_falls_back_and_is_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_report(tmp_path, ["a.md"])
        calls: list[int] = []

        def flaky_create_deep_agent(**kwargs: Any) -> object:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("compile boom")
            return object()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", flaky_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())

        with capture_logs() as logs:
            fallback_agent = await aget_report_agent(tmp_path)

        assert fallback_agent is not None
        agent_events = [entry for entry in logs if entry.get("kind") == "agent"]
        results = {entry["result"] for entry in agent_events}
        assert "error" in results
        assert "fallback" in results
        assert len(calls) == 2  # 1 次缓存构建失败 + 1 次回退构建成功
        assert stats()["agent"]["entries"] == 0  # 回退产物不进缓存

        # 不缓存负结果：下一次仍会重新尝试缓存构建并成功
        cached_agent = await aget_report_agent(tmp_path)
        assert cached_agent is not fallback_agent
        assert len(calls) == 3
        assert stats()["agent"]["entries"] == 1

    def test_does_not_await_inflight_from_another_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """另一个事件循环遗留的 in-flight 任务不能被当前 loop await（会挂起/报错）。"""
        _write_report(tmp_path, ["a.md"])
        release = threading.Event()
        build_calls: list[int] = []
        build_lock = threading.Lock()

        def blocking_create_deep_agent(**kwargs: Any) -> object:
            with build_lock:
                build_calls.append(1)
                call_index = len(build_calls)
            if call_index == 1:
                release.wait(timeout=10)  # 卡住 loop A 的构建，制造跨 loop 的 in-flight
            return object()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", blocking_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())

        loop_a_result: list[Any] = []
        errors: list[BaseException] = []

        def run_loop_a() -> None:
            try:
                loop_a_result.append(asyncio.run(aget_report_agent(tmp_path)))
            except BaseException as exc:  # pragma: no cover - 失败时便于诊断
                errors.append(exc)
            finally:
                release.set()

        thread = threading.Thread(target=run_loop_a)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while len(build_calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(build_calls) == 1  # loop A 的构建已在途

            loop_b_agent = asyncio.run(aget_report_agent(tmp_path))  # 不应等待 loop A 的任务
        finally:
            release.set()
            thread.join(timeout=10)

        assert not thread.is_alive()
        assert errors == []
        assert loop_a_result and loop_a_result[0] is not None
        assert loop_b_agent is not None
        assert len(build_calls) == 2  # 两个 loop 各自构建，互不 await

    async def test_missing_root_raises_file_not_found(
        self, tmp_path: Path, counting_builder: list[dict[str, Any]]
    ) -> None:
        with pytest.raises(FileNotFoundError):
            await aget_report_agent(tmp_path / "nope")

        assert counting_builder == []
        assert stats()["agent"]["entries"] == 0

    async def test_builder_file_not_found_is_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting_builder: list[dict[str, Any]]
    ) -> None:
        _write_report(tmp_path, ["a.md"])

        def boom(root: Any) -> ReportIndex:
            raise FileNotFoundError("index gone")

        monkeypatch.setattr(report_cache, "get_report_index", boom)

        with pytest.raises(FileNotFoundError):
            await aget_report_agent(tmp_path)

        assert counting_builder == []  # 不进入 fail-open 回退
        assert stats()["agent"]["entries"] == 0

    async def test_agent_enabled_false_rebuilds_each_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting_builder: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig(agent_enabled=False))
        _write_report(tmp_path, ["a.md"])

        first = await aget_report_agent(tmp_path)
        second = await aget_report_agent(tmp_path)

        assert first is not second
        assert len(counting_builder) == 2
        snapshot = stats()
        assert snapshot["agent"]["entries"] == 0
        assert snapshot["agent"]["builds"] == 0
        assert snapshot["builds"] == 1  # index 仍复用

    async def test_enabled_false_disables_both_caches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting_builder: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(report_cache, "REPORT_CACHE_CONFIG", ReportCacheConfig(enabled=False))
        _write_report(tmp_path, ["a.md"])
        real_build = report_cache.build_report_index
        index_builds: list[object] = []

        def counting_build(root: Any) -> ReportIndex:
            index_builds.append(root)
            return real_build(root)

        monkeypatch.setattr(report_cache, "build_report_index", counting_build)

        first = await aget_report_agent(tmp_path)
        second = await aget_report_agent(tmp_path)

        assert first is not second
        assert len(counting_builder) == 2
        assert len(index_builds) == 2  # index 每次重建（等价旧行为，不读写缓存）
        snapshot = stats()
        assert snapshot["entries"] == 0
        assert snapshot["agent"]["entries"] == 0
        assert snapshot["builds"] == 0

    async def test_prompt_version_changes_agent_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting_builder: list[dict[str, Any]]
    ) -> None:
        _write_report(tmp_path, ["a.md"])
        await aget_report_agent(tmp_path)

        monkeypatch.setattr(report_cache, "FETCH_PROMPT_VERSION", FETCH_PROMPT_VERSION + 1)
        rebuilt = await aget_report_agent(tmp_path)

        assert rebuilt is not None
        assert len(counting_builder) == 2
        versions = {key[2] for key in report_cache._agent_entries}
        assert versions == {FETCH_PROMPT_VERSION, FETCH_PROMPT_VERSION + 1}

    async def test_invalidate_and_clear(self, tmp_path: Path, counting_builder: list[dict[str, Any]]) -> None:
        root_a = _write_report(tmp_path / "a", ["1.md"])
        root_b = _write_report(tmp_path / "b", ["1.md"])
        agent_a = await aget_report_agent(root_a)
        agent_b = await aget_report_agent(root_b)
        assert stats()["entries"] == 2
        assert stats()["agent"]["entries"] == 2

        invalidate_report_caches(root_a)

        assert stats()["entries"] == 1
        assert stats()["agent"]["entries"] == 1
        assert await aget_report_agent(root_b) is agent_b  # 其他 root 不受影响
        rebuilt = await aget_report_agent(root_a)
        assert rebuilt is not agent_a
        assert len(counting_builder) == 3

        clear_report_caches()
        assert stats()["entries"] == 0
        assert stats()["agent"]["entries"] == 0

    async def test_concurrent_queries_are_isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_report(tmp_path, ["a.md"])
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: EchoChatModel())
        queries = [f"并发问题-{index}" for index in range(20)]

        async def run(query: str) -> str:
            agent = await aget_report_agent(tmp_path)
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=query)]},
                config={"recursion_limit": 10},
            )
            answers = [msg.content for msg in result["messages"] if isinstance(msg, AIMessage) and msg.content]
            return answers[-1]

        answers = await asyncio.gather(*(run(query) for query in queries))

        assert answers == [f"echo: {query}" for query in queries]
        assert stats()["agent"]["builds"] == 1  # 20 个并发只构建 1 个 graph
