"""P0：财报目录 index 缓存（``alphabee/financial_report/report_cache.py``）单测。

覆盖设计 §11 中 P0 相关用例：index 构建不变量、version 稳定性与 manifest 变化、
TTL 过期、LRU 上限、key 隔离、并发只构建 1 次、构建异常不缓存、stats 计数。

测试只使用 ``tmp_path`` 上的真实目录/文件，不依赖 LLM 或网络。
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from alphabee import PROJECT_ROOT
from alphabee.financial_report import report_cache
from alphabee.financial_report.report_cache import (
    REPORT_CACHE_CONFIG,
    ReportCacheConfig,
    ReportIndex,
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
