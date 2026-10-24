"""L1 缓存：财报目录 index（文件清单 + 压缩目录树 + 版本指纹）。

设计见 ``docs/implementation/financial_report/REPORT_INDEX_AGENT_CACHE_DESIGN.md``：

- §3.1 ``ReportIndex`` 数据契约；
- §3.2 ``report_version`` 版本指纹（manifest ``stat`` 优先）；
- §3.3 index key 为 ``(str(root.resolve()), version)``；
- §3.4 ``ReportCacheConfig`` 默认值（本期不接 ``config.yaml``，测试可 monkeypatch
  模块级实例 :data:`REPORT_CACHE_CONFIG`）；
- §6 TTL + LRU + 不缓存负结果；
- §7.2 ``suggest_paths``：``read_file`` 未命中时基于 index 给出相似真实路径；
- §8.1 压缩目录树（章节目录 + 每个目录的直接 ``.md`` 数量）。

本模块刻意保持轻量：顶层只依赖标准库与 ``structlog``，**不** import
``alphabee.tools`` / ``deepagents`` / ``alphabee.utils.llm`` / ``fetch_deepagents``
（``report_parser`` 会 import 本模块，不能因此拖入 LLM/agent 重依赖），也不读取
``config.yaml``。
"""

from __future__ import annotations

import difflib
import os
import posixpath
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

REPORT_MANIFEST_NAME = ".report_manifest.json"

#: ``difflib.get_close_matches`` 的相似度门槛（设计 §7.2 建议约 0.5）。
SUGGEST_PATH_CUTOFF = 0.5

# index cache key: (root.resolve() 的字符串, version)
_IndexKey = tuple[str, str]


@dataclass(frozen=True)
class ReportIndex:
    """一份报告目录的不可变索引，可被多个并发 invocation 共享。

    Attributes:
        root: ``resolve()`` 后的绝对路径。
        version: 版本指纹，见 :func:`report_version`。
        rel_files: 全部 ``.md`` 相对路径（posix 分隔符、无前导 ``/``、稳定排序）。
        rel_dirs: 全部目录相对路径（posix 分隔符、无前导 ``/``、稳定排序）。
        tree_compact: 给 prompt 用的压缩目录树（章节目录 + 直接 ``.md`` 数量）。
        total_files: ``len(rel_files)``。
        built_at: ``time.monotonic()``，仅供诊断。
    """

    root: Path
    version: str
    rel_files: tuple[str, ...]
    rel_dirs: tuple[str, ...]
    tree_compact: str
    total_files: int
    built_at: float


@dataclass(frozen=True)
class ReportCacheConfig:
    """缓存配置（§3.4）。本期只用 index 相关字段，agent 字段留给 P2。"""

    enabled: bool = True
    index_enabled: bool = True
    agent_enabled: bool = True
    path_guard_enabled: bool = True
    index_ttl_s: float = 1800.0
    agent_ttl_s: float = 1800.0
    max_index_entries: int = 64
    max_agent_entries: int = 16
    small_report_full_paths: int = 80


#: 模块级配置实例；不接 ``config.yaml``，测试可直接 monkeypatch 本名字。
REPORT_CACHE_CONFIG = ReportCacheConfig()


class _CacheStats:
    """缓存计数器（读取时全部在锁内，写时同样在锁内）。"""

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.builds = 0
        self.evictions = 0

    def snapshot(self, entries: int) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "builds": self.builds,
            "evictions": self.evictions,
            "entries": entries,
        }


#: index 缓存：key -> (expire_at, ReportIndex)；OrderedDict 顺序即 LRU 顺序。
_entries: OrderedDict[_IndexKey, tuple[float, ReportIndex]] = OrderedDict()
#: 同 key single-flight：等待中的线程挂在同一个 threading.Event 上。
_inflight: dict[_IndexKey, threading.Event] = {}
_lock = threading.Lock()
_stats = _CacheStats()


def report_version(root: str | Path) -> str:
    """返回报告目录的版本指纹（§3.2）。

    优先使用报告写入时生成的 ``.report_manifest.json``：报告整体重写会替换
    manifest，``mtime_ns + size`` 足以区分版本，且只需要一次 ``stat``。
    无 manifest 时退化为目录自身的 ``mtime_ns``（手工编辑 markdown 的陈旧风险
    由 TTL 兜底）。
    """
    root_path = Path(root)
    manifest = root_path / REPORT_MANIFEST_NAME
    try:
        manifest_stat = manifest.stat()
    except OSError:
        pass
    else:
        return f"manifest:{manifest_stat.st_mtime_ns}:{manifest_stat.st_size}"
    root_stat = root_path.stat()
    return f"dir:{root_stat.st_mtime_ns}"


def build_report_index(root: str | Path) -> ReportIndex:
    """构建一份报告的索引：一次 ``os.walk`` 同时收集 ``.md`` 文件与目录。

    Args:
        root: 报告目录（``report_parser.write_markdown_report_folder`` 的输出）。

    Returns:
        不可变的 :class:`ReportIndex`。

    Raises:
        FileNotFoundError: 报告目录不存在（保持原有异常语义，不做负缓存）。
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"Report directory not found: {root_path}")

    rel_files: list[str] = []
    rel_dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames.sort()
        current = Path(dirpath)
        if current != root_path:
            rel_dirs.append(current.relative_to(root_path).as_posix())
        for name in sorted(filenames):
            if name.endswith(".md"):
                rel_files.append((current / name).relative_to(root_path).as_posix())

    rel_files.sort()
    rel_dirs.sort()
    return ReportIndex(
        root=root_path,
        version=report_version(root_path),
        rel_files=tuple(rel_files),
        rel_dirs=tuple(rel_dirs),
        tree_compact=_render_compact_tree(rel_files, rel_dirs),
        total_files=len(rel_files),
        built_at=time.monotonic(),
    )


def get_report_index(root: str | Path) -> ReportIndex:
    """同步入口：命中 index 缓存则直接返回，否则 single-flight 构建（§3.5）。

    缓存 key 为 ``(str(root.resolve()), version)``：manifest 重写 → version 变化
    → 新 key 并重建，旧 key 不会被复用。构建异常不写缓存（fail-open 由调用方
    回退），且一定会释放同 key 的等待者。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Report directory not found: {root_path}")

    config = REPORT_CACHE_CONFIG
    if not (config.enabled and config.index_enabled):
        # 关闭开关时等价旧行为：每次直接构建，不读写缓存。
        return build_report_index(root_path)

    resolved_root = str(root_path.resolve())
    while True:
        # 每轮重新取 version：被 wake 的等待者要看到最新 manifest。
        version = report_version(root_path)
        key: _IndexKey = (resolved_root, version)
        with _lock:
            now = time.monotonic()
            _purge_expired_locked(now)
            cached = _entries.get(key)
            if cached is not None and cached[0] > now:
                _entries.move_to_end(key)
                _stats.hits += 1
                entries = len(_entries)
                hit_index: ReportIndex | None = cached[1]
                wait_event: threading.Event | None = None
            else:
                hit_index = None
                wait_event = _inflight.get(key)
                owner = wait_event is None
                if owner:
                    wait_event = threading.Event()
                    _inflight[key] = wait_event
                    _stats.misses += 1
                    _stats.builds += 1
                entries = len(_entries)

        if hit_index is not None:
            _log_cache_event(
                result="hit",
                root=resolved_root,
                version=version,
                duration_ms=0.0,
                entries=entries,
            )
            return hit_index
        if wait_event is not None and not owner:
            wait_event.wait()
            continue

        started = time.perf_counter()
        try:
            built = build_report_index(root_path)
        except BaseException:
            duration_ms = (time.perf_counter() - started) * 1000.0
            with _lock:
                current = _inflight.pop(key, None)
                if current is not None:
                    current.set()
            logger.warning(
                "report_cache",
                kind="index",
                result="error",
                root=resolved_root,
                version=version,
                duration_ms=round(duration_ms, 3),
                exc_info=True,
            )
            # 不缓存负结果：下次调用仍会重建。
            raise

        duration_ms = (time.perf_counter() - started) * 1000.0
        with _lock:
            expire_at = time.monotonic() + config.index_ttl_s
            store_key: _IndexKey = (str(built.root), built.version)
            _entries[store_key] = (expire_at, built)
            _entries.move_to_end(store_key)
            evicted = _evict_locked(config.max_index_entries)
            current = _inflight.pop(key, None)
            if current is not None:
                current.set()
            entries = len(_entries)

        _log_cache_event(
            result="build",
            root=resolved_root,
            version=built.version,
            duration_ms=duration_ms,
            entries=entries,
        )
        for evicted_root, evicted_version in evicted:
            logger.info(
                "report_cache",
                kind="index",
                result="evict",
                root=evicted_root,
                version=evicted_version,
                entries=entries,
            )
        return built


def invalidate_report_caches(root: str | Path | None = None) -> None:
    """失效 index 缓存（本期只清 index；agent 缓存 P2 再扩展）。

    Args:
        root: 指定报告目录时只清该 root 的全部版本；``None`` 时清空全部。
    """
    resolved_root = str(Path(root).resolve()) if root is not None else None
    with _lock:
        stale_keys = [key for key in _entries if resolved_root is None or key[0] == resolved_root]
        for key in stale_keys:
            _entries.pop(key, None)
        _wake_inflight_locked(resolved_root)
        entries = len(_entries)
    logger.info(
        "report_cache",
        kind="index",
        result="invalidate",
        root=resolved_root,
        entries=entries,
    )


def clear_report_caches() -> None:
    """清空全部缓存与计数器（测试专用）。"""
    global _stats
    with _lock:
        _entries.clear()
        _wake_inflight_locked(None)
        _stats = _CacheStats()


def stats() -> dict[str, int]:
    """返回 ``hits/misses/builds/evictions/entries`` 快照（先剔除过期项）。"""
    with _lock:
        _purge_expired_locked(time.monotonic())
        return _stats.snapshot(len(_entries))


def suggest_paths(index: ReportIndex, requested: str, *, n: int = 5) -> list[str]:
    """基于 index 给出 ``requested`` 最可能对应的真实文件路径（设计 §7.2）。

    算法顺序：

    1. basename 精确匹配（忽略目录）：模型目录拼错、文件名正确的场景直接命中；
       再做 basename 包含匹配（按 basename 相似度降序）：处理漏 ``(1)_`` 前缀、
       多/少 ``-续`` 等规范化差异；
    2. ``difflib.get_close_matches`` 在完整相对路径上做相似匹配
       （``cutoff=SUGGEST_PATH_CUTOFF``）：处理拼错字符、整体路径相近；
    3. 目录前缀过滤：请求本身是目录时先给该目录下的文件，并让同目录候选优先
       （稳定排序）：目录对、文件名错时先给同目录的真实文件。

    候选全部来自已有的 :class:`ReportIndex`，**不**重新扫描目录。返回路径统一
    带前导 ``/``（与虚拟文件系统风格一致，可直接交给 ``read_file``），最多 ``n`` 条、
    去重。

    Args:
        index: 报告目录索引。
        requested: 模型请求的路径（容忍反斜杠、前导 ``/``、重复斜杠等写法）。
        n: 最多返回的候选数量。

    Returns:
        带前导 ``/`` 的候选相对路径列表；无候选时返回空列表。
    """
    if n <= 0 or not index.rel_files:
        return []

    requested_rel = _normalize_rel_path(requested)
    requested_dir = posixpath.dirname(requested_rel)
    requested_base = posixpath.basename(requested_rel)

    ordered: list[str] = []
    seen: set[str] = set()

    def add(rel_file: str) -> None:
        if rel_file and rel_file not in seen:
            seen.add(rel_file)
            ordered.append(rel_file)

    if requested_base:
        # 1a) basename 精确匹配：目录可错，文件名对即可命中
        for rel_file in index.rel_files:
            if posixpath.basename(rel_file) == requested_base:
                add(rel_file)

        # 1b) basename 包含匹配（如漏 (1)_ 前缀），按 basename 相似度降序
        basename_scored: list[tuple[float, str]] = []
        for rel_file in index.rel_files:
            real_base = posixpath.basename(rel_file)
            if real_base == requested_base:
                continue
            if requested_base in real_base or real_base in requested_base:
                ratio = difflib.SequenceMatcher(None, requested_base, real_base).ratio()
                basename_scored.append((ratio, rel_file))
        basename_scored.sort(key=lambda item: item[0], reverse=True)
        for _, rel_file in basename_scored:
            add(rel_file)

    # 2) 完整相对路径的相似匹配（拼错字符、漏 (1)_ 等前缀差异）
    close_matches = difflib.get_close_matches(
        requested_rel,
        index.rel_files,
        n=max(n * 4, n),
        cutoff=SUGGEST_PATH_CUTOFF,
    )
    for rel_file in close_matches:
        add(rel_file)

    # 3a) 请求本身就是目录（read_file 误传目录）：先给该目录下的文件
    if requested_rel and requested_rel in index.rel_dirs:
        prefix = f"{requested_rel}/"
        for rel_file in index.rel_files:
            if rel_file.startswith(prefix):
                add(rel_file)

    # 3b) 同目录候选优先（稳定排序保留上述优先级）
    ordered.sort(key=lambda rel_file: posixpath.dirname(rel_file) != requested_dir)

    return [f"/{rel_file}" for rel_file in ordered[:n]]


def _normalize_rel_path(path: str | Path) -> str:
    """归一化为 index 的 posix 相对路径形式（无前导 ``/``，无重复斜杠）。"""
    text = str(path).replace("\\", "/").strip()
    if text.startswith("/"):
        text = text.lstrip("/")
    if not text:
        return ""
    normalized = posixpath.normpath(text)
    if normalized in {"", ".", "/"}:
        return ""
    return normalized.lstrip("/")


def _purge_expired_locked(now: float) -> None:
    """剔除已过期条目（调用方必须已持锁）。"""
    expired = [key for key, (expire_at, _) in _entries.items() if expire_at <= now]
    for key in expired:
        _entries.pop(key, None)


def _evict_locked(max_entries: int) -> list[_IndexKey]:
    """LRU 淘汰：超出上限时移除最旧条目（调用方必须已持锁）。"""
    limit = max(1, max_entries)
    evicted: list[_IndexKey] = []
    while len(_entries) > limit:
        key, _ = _entries.popitem(last=False)
        _stats.evictions += 1
        evicted.append(key)
    return evicted


def _wake_inflight_locked(root: str | None) -> None:
    """释放等待者（调用方必须已持锁）：不 set 会让同 key 的线程永远阻塞。"""
    keys = list(_inflight) if root is None else [key for key in _inflight if key[0] == root]
    for key in keys:
        event = _inflight.pop(key, None)
        if event is not None:
            event.set()


def _render_compact_tree(rel_files: list[str], rel_dirs: list[str]) -> str:
    """渲染压缩目录树：每个章节目录一行 ``/路径 (N files)``（§8.1）。"""
    counts: dict[str, int] = {}
    for rel_file in rel_files:
        parent = posixpath.dirname(rel_file)
        counts[parent] = counts.get(parent, 0) + 1

    lines = [f"/{rel_dir} ({counts[rel_dir]} files)" for rel_dir in rel_dirs if counts.get(rel_dir, 0) > 0]
    if counts.get("", 0) > 0:
        lines.insert(0, f"/ ({counts['']} files)")
    if not lines:
        return "（报告中未找到 markdown 文件）"
    return "\n".join(lines)


def _log_cache_event(
    *,
    result: str,
    root: str,
    version: str,
    duration_ms: float,
    entries: int,
) -> None:
    """统一观测字段（§6.4 / §13.3）：不记录文件内容。"""
    logger.info(
        "report_cache",
        kind="index",
        result=result,
        root=root,
        version=version,
        duration_ms=round(duration_ms, 3),
        entries=entries,
    )
