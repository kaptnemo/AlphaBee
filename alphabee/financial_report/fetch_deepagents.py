"""Fetch financial report content using a deepagents-based agent.

This is the deepagents sibling of `fetch.py`: instead of driving the
Claude Agent SDK, it builds a `create_deep_agent` over a virtual
`FilesystemBackend` rooted at the report folder, so the agent can only
`ls` / `read_file` / `glob` / `grep` the markdown split produced by
`report_parser.py`. The prompt and directory tree are shared with
`fetch.py`.

``ReportFilesystemBackend`` additionally turns a failed ``read_file`` into a
"可能的真实路径" hint based on the cached ``ReportIndex``: it sits at the
backend layer so the main agent and deepagents' general-purpose subagent are
guarded alike (design §7.1).
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv

# .env must be loaded before alphabee imports: `alphabee.config.settings`
# bakes llm.api_key at import time.
load_dotenv()

from pathlib import Path  # noqa: E402

import structlog  # noqa: E402
from deepagents import create_deep_agent  # noqa: E402
from deepagents.backends import FilesystemBackend  # noqa: E402
from deepagents.backends.protocol import ReadResult  # noqa: E402
from deepagents.middleware._tool_exclusion import _ToolExclusionMiddleware  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.runnables.config import RunnableConfig  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402

from alphabee.financial_report import report_cache  # noqa: E402
from alphabee.financial_report.fetch import build_fetch_prompt  # noqa: E402
from alphabee.financial_report.report_cache import (  # noqa: E402
    ReportIndex,
    get_report_index,
    resolve_single_file_dir,
    suggest_paths,
)
from alphabee.utils import create_chat_model  # noqa: E402

logger = structlog.get_logger(__name__)

#: read_file 未命中时最多给出的候选路径数（设计 §7.2）。
MAX_PATH_HINTS = 5

#: 未命中提示的固定前缀：prompt 中要求模型优先使用最匹配的一条，不匹配则改 grep。
PATH_HINTS_HEADER = "可能的真实路径（优先使用最匹配的一条；不匹配请改用 grep）："


class ReportFilesystemBackend(FilesystemBackend):
    """报告虚拟文件系统 + ``read_file`` 未命中的相似路径提示（设计 §7.2）。

    守卫放在 backend 层（而不是 middleware）是因为 deepagents 的 ``middleware=``
    只作用于主 agent，backend 被主 agent、general-purpose subagent 及任何嵌套
    agent 共享。

    行为约定：

    - 成功读取完全沿用父类语义（同样的 ``ReadResult``，不做任何改写）；
    - **仅提示、不拦截**：只有错误文本含 ``not found`` 时才追加候选路径，不启用
      strict allowlist，避免报告解析过程中新写入的文件被 index 陈旧状态误伤；
    - 候选来自已有的 :class:`ReportIndex`，不重新扫描目录；
    - 每次未命中记录 ``read_file_miss`` 日志（root/version/requested/hints_count），
      不记录文件内容。
    """

    def __init__(self, root_dir: str | Path, index: ReportIndex) -> None:
        super().__init__(root_dir=str(root_dir), virtual_mode=True)
        self._index = index
        # 保留 index 文件集合，供可选严格模式使用；本期只做提示，不做 allowlist。
        self._rel_set = frozenset(index.rel_files)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        result = super().read(file_path, offset=offset, limit=limit)
        error = result.error
        if not error or "not found" not in error:
            return result

        # 目录被误当文件读取、且目录内只有一个 markdown 时，目标无歧义，直接读取。
        resolved = resolve_single_file_dir(self._index, file_path)
        if resolved is not None:
            resolved_result = super().read(f"/{resolved}", offset=offset, limit=limit)
            if resolved_result.error is None:
                logger.info(
                    "read_file_autocorrect_directory",
                    root=str(self._index.root),
                    version=self._index.version,
                    requested=file_path,
                    resolved=resolved,
                )
                return resolved_result

        hints = suggest_paths(self._index, file_path, n=MAX_PATH_HINTS)
        logger.info(
            "read_file_miss",
            root=str(self._index.root),
            version=self._index.version,
            requested=file_path,
            hints_count=len(hints),
        )
        if not hints:
            return result
        hint_lines = "\n".join(f"- {hint}" for hint in hints)
        return ReadResult(error=f"{error}\n\n{PATH_HINTS_HEADER}\n{hint_lines}")


def create_report_fetch_agent(
    report_dir: str | Path,
    *,
    index: ReportIndex | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Factory: build a deep agent scoped to a report folder (virtual backend).

    The virtual filesystem exposes only the report's markdown tree, so the
    agent cannot read anything outside it.

    Args:
        report_dir: 报告目录（``report_parser`` 的输出）。
        index: 目录 index；缺省时内部取 :func:`get_report_index` 的缓存结果，
            system prompt 因而复用压缩目录树，同 root 连续构建只扫描目录一次。
            缓存构建异常时按 fail-open 回退到旧行为（现场渲染完整目录树，
            backend 也不再附加相似路径提示）。
    """
    report_path = Path(report_dir)
    if not report_path.is_dir():
        raise FileNotFoundError(f"Report directory not found: {report_path}")

    if index is None:
        try:
            index = get_report_index(report_path)
        except FileNotFoundError:
            # 报告目录不存在：保持原有异常语义，不被缓存吞掉。
            raise
        except Exception:
            logger.warning(
                "report_cache",
                kind="index",
                result="fallback",
                root=str(report_path),
                exc_info=True,
            )
            index = None

    system_prompt = build_fetch_prompt(report_path.name, "", report_dir=report_path, index=index)
    # index 仍用于 prompt；path_guard_enabled=False 时只关掉 backend 的相似路径提示。
    if index is not None and report_cache.REPORT_CACHE_CONFIG.path_guard_enabled:
        backend: FilesystemBackend = ReportFilesystemBackend(report_path, index)
    else:
        # 关闭守卫或 index fail-open：退回普通虚拟 backend（无路径提示）。
        backend = FilesystemBackend(root_dir=str(report_path.resolve()), virtual_mode=True)
    return create_deep_agent(
        model=create_chat_model("financial_report"),
        system_prompt=system_prompt,
        backend=backend,
        middleware=[
            # 硬性移除 ls：目录树已在 system prompt 中给出，禁止代理再做目录遍历。
            # 保留 grep/glob/read_file —— 它们只作用于本报告文件夹，是检索正文所必需。
            _ToolExclusionMiddleware(excluded=frozenset({"ls", "write_file", "edit_file", "execute"})),
        ],
    )


def _render_tool_call(msg: AIMessage) -> list[str]:
    lines: list[str] = []
    for call in msg.tool_calls or []:
        name = call.get("name", "")
        args = call.get("args", {})
        if name == "read_file":
            lines.append(f"[read_file] {args.get('file_path', '')}")
        elif name == "grep":
            lines.append(f"[grep] {args.get('pattern', '')}")
        elif name == "glob":
            lines.append(f"[glob] {args.get('pattern', '')}")
        elif name == "ls":
            lines.append(f"[ls] {args.get('path', '/')}")
        else:
            lines.append(f"[{name}] {args}")
    return lines


async def fetch_report_content_deepagents(
    report_dir: str | Path,
    query: str,
    *,
    max_steps: int = 40,
    verbose: bool = True,
) -> str:
    """Run a deepagents agent over a report folder and return the extraction.

    Args:
        report_dir: 报告文件夹路径（report_parser 输出的目录）。
        query: 需要从报告中提取内容的问题。
        max_steps: 允许的最大 agent 执行步数（langgraph recursion_limit）。
        verbose: 为 True 时实时打印 agent 的工具调用过程（ls/read_file/grep/glob）。

    Returns:
        agent 输出的纯文本内容（含工具调用日志）。
    """
    agent = create_report_fetch_agent(report_dir)

    output_lines: list[str] = []
    config: RunnableConfig = {"recursion_limit": max_steps}
    async for event in agent.astream(
        {"messages": [HumanMessage(content=query)]},
        config=config,
    ):
        for update in event.values():
            if not isinstance(update, dict):
                continue
            for msg in update.get("messages", []):
                if isinstance(msg, AIMessage):
                    tool_lines = _render_tool_call(msg)
                    for line in tool_lines:
                        output_lines.append(line)
                        if verbose:
                            print(line, flush=True)
                    if msg.content and not msg.tool_calls:
                        text = msg.content if isinstance(msg.content, str) else ""
                        if text.strip():
                            output_lines.append(text.strip())
                            if verbose:
                                print(text.strip(), flush=True)
                elif isinstance(msg, ToolMessage):
                    if verbose:
                        content = msg.content if isinstance(msg.content, str) else ""
                        preview = content[:120] + "…" if len(content) > 120 else content
                        print(f"    ↳ {preview}", flush=True)

    return "\n".join(output_lines)


if __name__ == "__main__":
    import asyncio
    import sys

    if len(sys.argv) < 3:
        print("Usage: python fetch_deepagents.py <report_dir> <query>")
        sys.exit(1)

    report_dir = sys.argv[1]
    query = sys.argv[2]

    content = asyncio.run(fetch_report_content_deepagents(report_dir, query, verbose=True))
    print(content)
