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

import asyncio
import difflib
import os
import posixpath
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，运行时保持模块顶层轻量
    from langgraph.graph.state import CompiledStateGraph

logger = structlog.get_logger(__name__)

REPORT_MANIFEST_NAME = ".report_manifest.json"

#: ``difflib.get_close_matches`` 的相似度门槛（设计 §7.2 建议约 0.5）。
SUGGEST_PATH_CUTOFF = 0.5

#: 文件名里常见的章节编号/前缀，用于“语义主干”比较：
#: ``39、_长期借款`` / ``（1）_长期借款分类`` / ``第八节_财务报告`` → 去掉编号后再匹配。
_NUMBER_PREFIX_RE = re.compile(
    r"^(?:"
    r"[（(]\s*(?:\d+|[一二三四五六七八九十百]+)\s*[）)]"
    r"|\d+\s*[、.．]"
    r"|[一二三四五六七八九十百]+\s*[、.．]"
    r"|第(?:\d+|[一二三四五六七八九十百]+)[节章]"
    r")[\s_\-－—、.．]*"
)

#: 续篇后缀：``-续`` / ``_续`` / ``（续）``。
_TRAILING_CONTINUATION_RE = re.compile(r"(?:[-_－—]\s*续|（续）|\(续\))$")

#: agent 缓存 key 的 prompt 结构版本：``build_fetch_prompt`` 结构变更时 +1。
FETCH_PROMPT_VERSION = 1

#: agent 缓存 key 的模型组件名，与 ``create_chat_model("financial_report")`` 对齐。
AGENT_MODEL_COMPONENT = "financial_report"

# index cache key: (root.resolve() 的字符串, version)
_IndexKey = tuple[str, str]

# agent cache key: (root.resolve() 的字符串, version, prompt_version, model component)
_AgentKey = tuple[str, str, int, str]


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
    """缓存配置（§3.4）。未接 ``config.yaml``，测试可 monkeypatch 模块级实例。

    四个开关互相独立（§5.4）：``enabled`` 是总开关；``index_enabled`` / ``agent_enabled``
    分别控制两级缓存；``path_guard_enabled`` 控制 backend 是否挂相似路径提示。
    """

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
#: agent 缓存：key -> (expire_at, compiled graph)；同样用 OrderedDict 维护 LRU。
_agent_entries: OrderedDict[_AgentKey, tuple[float, Any]] = OrderedDict()
#: agent 同 key single-flight：等待中的协程共享同一个 ``asyncio.Task``。
_agent_inflight: dict[_AgentKey, asyncio.Task[Any]] = {}
#: 单把 threading 锁保护两级缓存的 dict/计数读写：临界区内没有任何 ``await``
#: （只是一小段内存操作），因此 async 路径持有它不会阻塞事件循环；也规避了
#: module 级 ``asyncio.Lock`` 跨事件循环复用时绑定旧 loop 的问题。
_lock = threading.Lock()
_stats = _CacheStats()
_agent_stats = _CacheStats()
#: clear 世代：clear 之后旧 in-flight 构建不再回填缓存（避免跨测试污染）。
_generation = 0


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


async def aget_report_agent(root: str | Path) -> CompiledStateGraph[Any, Any, Any, Any]:
    """异步 agent 缓存入口（设计 §3.3 / §3.5 / §4 / §5 / §6）。

    agent key 为
    ``(str(root.resolve()), version, FETCH_PROMPT_VERSION, "financial_report")``：
    报告重写 → version 变化 → 新 key；prompt 结构或模型组件变化 → 新 key。

    行为：

    - 命中缓存直接返回同一个 compiled graph；
    - 未命中时同 key 只有一个协程真正构建（``asyncio.Task`` 单飞），其余等待
      同一结果；构建在 ``asyncio.to_thread`` 中执行，不在事件循环里跑同步重活；
    - TTL（``agent_ttl_s``）+ LRU（``max_agent_entries``）与 index 缓存一致；
    - 失败 fail-open（§4.4）：``FileNotFoundError`` 原样抛出（报告目录不存在）；
      其他异常记录 warning 并回退同步工厂 ``create_report_fetch_agent(root)``，
      回退产物不进缓存、不缓存负结果；
    - ``enabled=False`` 或 ``agent_enabled=False`` 时不读写 agent 缓存，每次新建
      （此时 index 是否复用由 index 开关决定）。

    Args:
        root: 报告目录（``report_parser.write_markdown_report_folder`` 的输出）。

    Returns:
        ``CompiledStateGraph``（可被多个并发 ``ainvoke`` 复用，每次调用使用各自的
        messages，不共享对话历史）。

    Raises:
        FileNotFoundError: 报告目录不存在。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Report directory not found: {root_path}")

    config = REPORT_CACHE_CONFIG
    if not (config.enabled and config.agent_enabled):
        # 降级开关：不读写 agent 缓存，每次新建（index 仍按自己的开关复用）。
        return await _build_agent_direct(root_path)

    resolved_root = str(root_path.resolve())
    version = report_version(root_path)
    key: _AgentKey = (resolved_root, version, FETCH_PROMPT_VERSION, AGENT_MODEL_COMPONENT)
    loop = asyncio.get_running_loop()

    hit_agent: CompiledStateGraph[Any, Any, Any, Any] | None = None
    task: asyncio.Task[CompiledStateGraph[Any, Any, Any, Any]] | None = None

    with _lock:
        now = time.monotonic()
        _purge_expired_agent_locked(now)
        cached = _agent_entries.get(key)
        owner = False
        if cached is not None and cached[0] > now:
            _agent_entries.move_to_end(key)
            _agent_stats.hits += 1
            entries = len(_agent_entries)
            hit_agent = cached[1]
        else:
            task = _agent_inflight.get(key)
            if task is not None and task.get_loop() is not loop:
                # 不跨事件循环等待（多次 asyncio.run / 每个测试一个 loop）：
                # 旧 loop 的 in-flight 任务只让它自己清理，不在这里 await。
                _agent_inflight.pop(key, None)
                task = None
            if task is None:
                task = loop.create_task(_build_agent_for_cache(root_path, key, _generation))
                _agent_inflight[key] = task
                task.add_done_callback(_make_agent_inflight_cleanup(key))
                _agent_stats.misses += 1
                _agent_stats.builds += 1
                owner = True
            entries = len(_agent_entries)

    if hit_agent is not None:
        _log_agent_event(
            result="hit",
            root=resolved_root,
            version=version,
            duration_ms=0.0,
            entries=entries,
        )
        return hit_agent

    if owner:
        _log_agent_event(
            result="miss",
            root=resolved_root,
            version=version,
            duration_ms=0.0,
            entries=entries,
        )

    # 命中分支已 return；走到这里一定是 single-flight 的构建任务（新建或复用）。
    # 显式断言既表达不变式，也让静态检查把 task 从 Optional 窄化。
    assert task is not None

    try:
        return await asyncio.shield(task)
    except FileNotFoundError:
        # 报告目录不存在：保持原有异常语义，不吞、不回退。
        raise
    except Exception:
        # 构建体已记录 result="error"（含耗时与栈）；这里只做 fail-open 回退，
        # 回退产物不进缓存，保证查询仍可用（§4.4）。
        return await _fallback_agent(root_path, resolved_root, version)


def invalidate_report_caches(root: str | Path | None = None) -> None:
    """失效 index + agent 缓存（§6.1）。

    除了删除已完成的缓存条目，若该 root 存在在途的 agent 构建，这里会递增一轮
    ``_generation``，使该构建完成后的写回被丢弃（与 :func:`clear_report_caches`
    一致的不回填语义）；在途构建本身仍会正常返回给调用方，只是不再进缓存。

    Args:
        root: 指定报告目录时只清该 root 的全部版本；``None`` 时清空全部。
    """
    global _generation
    resolved_root = str(Path(root).resolve()) if root is not None else None
    with _lock:
        stale_keys = [key for key in _entries if resolved_root is None or key[0] == resolved_root]
        for key in stale_keys:
            _entries.pop(key, None)
        _wake_inflight_locked(resolved_root)
        entries = len(_entries)

        stale_agent_keys = [
            agent_key for agent_key in _agent_entries if resolved_root is None or agent_key[0] == resolved_root
        ]
        for agent_key in stale_agent_keys:
            _agent_entries.pop(agent_key, None)
        agent_entries = len(_agent_entries)
        stale_agent_inflight = [
            agent_key for agent_key in _agent_inflight if resolved_root is None or agent_key[0] == resolved_root
        ]
        if stale_agent_keys or stale_agent_inflight:
            # 有东西需要失效时才推进世代：避免不相关的在途构建被无谓丢弃。
            _generation += 1

    logger.info(
        "report_cache",
        kind="index",
        result="invalidate",
        root=resolved_root,
        entries=entries,
    )
    if stale_agent_keys or stale_agent_inflight:
        logger.info(
            "report_cache",
            kind="agent",
            result="invalidate",
            root=resolved_root,
            entries=agent_entries,
        )


def clear_report_caches() -> None:
    """清空 index + agent 缓存与计数器（测试专用）。

    同时递增 ``_generation``：clear 之前启动、clear 之后才完成的 in-flight 构建
    不会把结果回填进缓存；尚未完成的 agent 任务会被取消，避免跨事件循环残留。
    """
    global _generation, _stats, _agent_stats
    with _lock:
        _entries.clear()
        _wake_inflight_locked(None)
        pending_agent_tasks = [task for task in _agent_inflight.values() if not task.done()]
        _agent_inflight.clear()
        _agent_entries.clear()
        _stats = _CacheStats()
        _agent_stats = _CacheStats()
        _generation += 1
    try:
        current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    for task in pending_agent_tasks:
        # 只在本 loop 里直接 cancel；其他 loop 的任务交给它自己的 loop 清理，
        # 避免在已关闭/未运行 loop 上操作任务。
        task_loop = task.get_loop()
        if task_loop is current_loop:
            task.cancel()
            continue
        if task_loop.is_closed():
            continue
        try:
            task_loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # pragma: no cover - loop 在检查后被关闭的竞态
            continue


def stats() -> dict[str, Any]:
    """返回 index 与 agent 两级缓存的计数器快照（先剔除过期项）。

    顶层键（``hits/misses/builds/evictions/entries``）保持为 index 缓存以兼容既有
    调用；``index`` / ``agent`` 两个子字典分别给出两级缓存的明细。
    """
    with _lock:
        now = time.monotonic()
        _purge_expired_locked(now)
        _purge_expired_agent_locked(now)
        index_snapshot = _stats.snapshot(len(_entries))
        agent_snapshot = _agent_stats.snapshot(len(_agent_entries))
    return {**index_snapshot, "index": index_snapshot, "agent": agent_snapshot}


def _semantic_stem(name: str) -> str:
    """去掉章节编号/前缀/续篇后缀，得到用于匹配的“语义主干”。"""
    stem = posixpath.splitext(name)[0]
    stem = _NUMBER_PREFIX_RE.sub("", stem)
    stem = _TRAILING_CONTINUATION_RE.sub("", stem)
    return stem.strip(" _-－—、.．")


def _files_under(index: ReportIndex, directory: str) -> list[str]:
    """返回某个目录下的全部 markdown 相对路径（稳定排序由 index 保证）。"""
    prefix = f"{directory}/"
    return [rel_file for rel_file in index.rel_files if rel_file.startswith(prefix)]


def suggest_paths(index: ReportIndex, requested: str, *, n: int = 5) -> list[str]:
    """基于 index 给出 ``requested`` 最可能对应的真实文件路径（设计 §7.2）。

    匹配顺序（分数从高到低）：

    0. **请求本身是目录**（模型把章节目录误写成 ``xxx.md``）→ 直接列出该目录下
       的文件。这是最常见的误读：``39、_长期借款.md`` 实际是目录
       ``39、_长期借款/``，真实内容在 ``（1）_长期借款分类.md``。
    1. 同名目录别名（父目录写错、章节目录名对）→ 列出该目录下的文件；
    2. 精确 basename（目录可错、文件名对）；
    3. 语义主干精确（去掉 ``39、_`` / ``（1）_`` / ``-续`` 后相等）；
    4. 语义主干包含（如 ``长期借款`` → ``长期借款分类``）；
    5. 原始 basename 包含（兜底）。
    同目录候选加固定加成，保证“目录对、文件名错”优先于“文件名对、目录错”。

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
    if not requested_rel:
        return []

    requested_dir = posixpath.dirname(requested_rel)
    requested_base = posixpath.basename(requested_rel)
    requested_no_ext = posixpath.splitext(requested_rel)[0]
    requested_core = _semantic_stem(requested_base)

    # 0) 请求路径本身是目录（允许模型误写成 ``xxx.md``）：目录下的文件才是真候选。
    exact_dir: str | None = None
    if requested_rel in index.rel_dirs:
        exact_dir = requested_rel
    elif requested_no_ext != requested_rel and requested_no_ext in index.rel_dirs:
        exact_dir = requested_no_ext
    if exact_dir is not None:
        return [f"/{rel_file}" for rel_file in _files_under(index, exact_dir)[:n]]

    scores: dict[str, float] = {}

    def add(rel_file: str, score: float) -> None:
        if score > scores.get(rel_file, 0.0):
            scores[rel_file] = score

    # 1) 同名目录别名：父目录写错、但章节目录名对。
    requested_dir_name = posixpath.basename(requested_no_ext)
    for directory in index.rel_dirs:
        dir_name = posixpath.basename(directory)
        dir_core = _semantic_stem(dir_name)
        if dir_name != requested_dir_name and not (requested_core and dir_core == requested_core):
            continue
        bonus = 30.0 if posixpath.dirname(directory) == requested_dir else 0.0
        for rel_file in _files_under(index, directory):
            add(rel_file, 160.0 + bonus)

    # 2) 文件级匹配。
    for rel_file in index.rel_files:
        base = posixpath.basename(rel_file)
        core = _semantic_stem(base)
        same_dir = posixpath.dirname(rel_file) == requested_dir

        if rel_file == requested_rel:
            score = 200.0
        elif base == requested_base:
            score = 150.0
        elif requested_core and core == requested_core:
            score = 140.0
        elif requested_core and core and (requested_core in core or core in requested_core):
            ratio = difflib.SequenceMatcher(None, requested_core, core).ratio()
            score = 110.0 + 30.0 * ratio
        elif requested_base and (requested_base in base or base in requested_base):
            score = 90.0
        else:
            continue
        if same_dir:
            score += 30.0
        add(rel_file, score)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [f"/{rel_file}" for rel_file, _ in ordered[:n]]


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


# ── agent 缓存内部实现 ─────────────────────────────────────────────────────


def _purge_expired_agent_locked(now: float) -> None:
    """剔除过期的 agent 缓存条目（调用方必须已持锁）。"""
    expired = [key for key, (expire_at, _) in _agent_entries.items() if expire_at <= now]
    for key in expired:
        _agent_entries.pop(key, None)


def _evict_agent_locked(max_entries: int) -> list[_AgentKey]:
    """agent 缓存的 LRU 淘汰（调用方必须已持锁）。"""
    limit = max(1, max_entries)
    evicted: list[_AgentKey] = []
    while len(_agent_entries) > limit:
        key, _ = _agent_entries.popitem(last=False)
        _agent_stats.evictions += 1
        evicted.append(key)
    return evicted


def _agent_entries_count() -> int:
    with _lock:
        _purge_expired_agent_locked(time.monotonic())
        return len(_agent_entries)


def _make_agent_inflight_cleanup(key: _AgentKey) -> Any:
    """构造同 key in-flight 任务的完成回调（清理登记、吞掉未取回的异常告警）。"""

    def _cleanup(task: asyncio.Task[Any]) -> None:
        with _lock:
            if _agent_inflight.get(key) is task:
                _agent_inflight.pop(key, None)
        if not task.cancelled():
            # 标记异常已取回：clear/取消场景下无人 await 时也不会刷警告。
            task.exception()

    return _cleanup


async def _build_agent_for_cache(
    root_path: Path, key: _AgentKey, generation: int
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """single-flight 的构建体：线程池中建 index + compiled graph，再写回缓存。"""
    # 延迟 import：report_cache 顶层必须保持轻量（fetch_deepagents 依赖本模块）。
    from alphabee.financial_report.fetch_deepagents import create_report_fetch_agent

    started = time.perf_counter()
    try:
        index = await asyncio.to_thread(get_report_index, root_path)
        agent = await asyncio.to_thread(create_report_fetch_agent, root_path, index=index)
    except BaseException:
        duration_ms = (time.perf_counter() - started) * 1000.0
        logger.warning(
            "report_cache",
            kind="agent",
            result="error",
            root=key[0],
            version=key[1],
            duration_ms=round(duration_ms, 3),
            exc_info=True,
        )
        raise

    duration_ms = (time.perf_counter() - started) * 1000.0
    config = REPORT_CACHE_CONFIG
    with _lock:
        if generation == _generation:
            _agent_entries[key] = (time.monotonic() + config.agent_ttl_s, agent)
            _agent_entries.move_to_end(key)
            evicted = _evict_agent_locked(config.max_agent_entries)
        else:
            # clear_report_caches 已发生：不回填，避免污染新一轮缓存。
            evicted = []
        entries = len(_agent_entries)

    _log_agent_event(
        result="build",
        root=key[0],
        version=key[1],
        duration_ms=duration_ms,
        entries=entries,
    )
    for evicted_root, evicted_version, _, _ in evicted:
        logger.info(
            "report_cache",
            kind="agent",
            result="evict",
            root=evicted_root,
            version=evicted_version,
            entries=entries,
        )
    return agent


async def _build_agent_direct(root_path: Path) -> CompiledStateGraph[Any, Any, Any, Any]:
    """开关关闭时直接构建：不经 agent 缓存（index 是否复用由 index 开关决定）。"""
    from alphabee.financial_report.fetch_deepagents import create_report_fetch_agent

    started = time.perf_counter()
    agent = await asyncio.to_thread(create_report_fetch_agent, root_path)
    duration_ms = (time.perf_counter() - started) * 1000.0
    _log_agent_event(
        result="build",
        root=str(root_path.resolve()),
        version=report_version(root_path),
        duration_ms=duration_ms,
        entries=_agent_entries_count(),
    )
    return agent


async def _fallback_agent(root_path: Path, resolved_root: str, version: str) -> CompiledStateGraph[Any, Any, Any, Any]:
    """缓存构建失败时的 fail-open 回退：同步工厂现建（不缓存，§4.4）。"""
    from alphabee.financial_report.fetch_deepagents import create_report_fetch_agent

    started = time.perf_counter()
    agent = await asyncio.to_thread(create_report_fetch_agent, root_path)
    duration_ms = (time.perf_counter() - started) * 1000.0
    logger.warning(
        "report_cache",
        kind="agent",
        result="fallback",
        root=resolved_root,
        version=version,
        duration_ms=round(duration_ms, 3),
        entries=_agent_entries_count(),
    )
    return agent


def _log_agent_event(
    *,
    result: str,
    root: str,
    version: str,
    duration_ms: float,
    entries: int,
) -> None:
    """agent 缓存观测字段（§6.4 / §13.3）：不记录报告内容或密钥。"""
    logger.info(
        "report_cache",
        kind="agent",
        result=result,
        root=root,
        version=version,
        duration_ms=round(duration_ms, 3),
        entries=entries,
    )


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
