"""Fetch financial report content using a deepagents-based agent.

This is the deepagents sibling of `fetch.py`: instead of driving the
Claude Agent SDK, it builds a `create_deep_agent` over a virtual
`FilesystemBackend` rooted at the report folder, so the agent can only
`ls` / `read_file` / `glob` / `grep` the markdown split produced by
`report_parser.py`. The prompt and directory tree are shared with
`fetch.py`.
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv

# .env must be loaded before alphabee imports: `alphabee.config.settings`
# bakes llm.api_key at import time.
load_dotenv()

from pathlib import Path  # noqa: E402

from deepagents import create_deep_agent  # noqa: E402
from deepagents.backends import FilesystemBackend  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.runnables.config import RunnableConfig  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402

from alphabee.financial_report.fetch import build_fetch_prompt  # noqa: E402
from alphabee.utils import create_chat_model  # noqa: E402


def create_report_fetch_agent(report_dir: str | Path) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Factory: build a deep agent scoped to a report folder (virtual backend).

    The virtual filesystem exposes only the report's markdown tree, so the
    agent cannot read anything outside it.
    """
    report_path = Path(report_dir)
    if not report_path.is_dir():
        raise FileNotFoundError(f"Report directory not found: {report_path}")

    system_prompt = build_fetch_prompt(report_path.name, "", report_dir=report_path)
    backend = FilesystemBackend(root_dir=str(report_path.resolve()), virtual_mode=True)
    return create_deep_agent(
        model=create_chat_model("financial_report"),
        system_prompt=system_prompt,
        backend=backend,
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
