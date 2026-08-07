"""Fetch financial report content using the Claude Agent SDK.

The markdown annual report has been split into a folder structure by
`report_parser.py` (see `reports/江西沃格光电集团股份有限公司_2025_年年度报告/`).
This module drives a Claude agent over that folder — in the same way Claude
Code navigates a codebase — using `ls` / `grep` / `glob` / `read` to locate
and extract the sections relevant to a query.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TypeGuard

from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock, UserMessage

MessageWithContent = UserMessage | AssistantMessage


def build_file_tree(report_dir: Path, max_entries: int = 500) -> str:
    """Render the folder structure as an indented tree (dirs and .md files only)."""
    entries: list[str] = []
    count = 0
    truncated = False

    def walk(current: Path, prefix: str) -> None:
        nonlocal count, truncated
        children = sorted(
            (p for p in current.iterdir() if p.is_dir() or p.suffix == ".md"),
            key=lambda p: (not p.is_dir(), p.name),
        )
        for i, child in enumerate(children):
            if count >= max_entries:
                truncated = True
                return
            is_last = i == len(children) - 1
            branch = "└── " if is_last else "├── "
            entries.append(f"{prefix}{branch}{child.name}")
            count += 1
            if child.is_dir():
                walk(child, prefix + ("    " if is_last else "│   "))

    walk(report_dir, "")
    if truncated:
        entries.append(f"...（目录过大，已截断，仅显示前 {max_entries} 项）")
    return "\n".join(entries)


def build_fetch_prompt(report_name: str, query: str, report_dir: Path | None = None) -> str:
    """Build the agent prompt for extracting report content."""
    tree = build_file_tree(report_dir) if report_dir is not None else "(无)"
    return f"""\
# 任务：从年度报告文件夹中提取相关内容

你现在位于年度报告 `{report_name}` 的文件夹中。报告正文被拆分成了按章节组织的
markdown 文件（每个文件对应一个小节，部分章节还嵌套了子文件夹）。

请根据下面的问题，从报告中找出相关内容：

<query>
{query}
</query>

## 报告目录结构

以下已给出完整目录结构，**不要**再执行 ls/find/pwd 等目录遍历操作：

<file_tree>
{tree}
</file_tree>

## 操作步骤（只需 3 步，请一次完成，不要重复遍历或重复读取）

1. 用 Grep 工具在文件夹内搜索问题关键词，一次定位所有相关文件。
2. 用 Read 读取命中的文件，精确摘取与问题直接相关的段落和表格数据；
   只读与问题相关的最小文件集，不要顺带读取无关章节。
3. 只有 Grep 没有命中、或需要对文件名做模式匹配时，才用 Glob 补查。

## 输出要求

- 只摘录报告中真实存在的内容，不要编造或推测。
- 保留原文中的数字、表格和表述，不做改写。
- 如果报告中没有相关内容，直接回复"报告中未找到相关内容"。
- 最后给出结论时，注明引用自哪个文件（文件名即可）。
"""


def _is_message(msg: object) -> TypeGuard[MessageWithContent]:
    """The SDK stream yields events of many types; only assistant/user messages
    carry `content` blocks."""
    return isinstance(msg, MessageWithContent) and isinstance(msg.content, list)


def _iter_text_blocks(msg: MessageWithContent) -> Iterator[str]:
    for block in msg.content:
        if isinstance(block, TextBlock) and block.text:
            yield block.text


def _render_tool_call(msg: object) -> list[str]:
    """Render a tool-use message to a human-readable line."""
    lines: list[str] = []
    if not _is_message(msg):
        return lines
    for block in msg.content:
        if isinstance(block, ToolUseBlock):
            if block.name == "Bash":
                cmd = block.input.get("command", "") if isinstance(block.input, dict) else str(block.input)
                lines.append(f"[Bash] {cmd[:160]}")
            elif block.name in ("Read", "Grep", "Glob"):
                fp = block.input.get("file_path", "")
                pat = block.input.get("pattern", "")
                lines.append(f"[{block.name}] {fp or pat}")
    return lines


async def fetch_report_content(
    report_dir: str | Path,
    query: str,
    *,
    max_turns: int = 40,
    verbose: bool = True,
) -> str:
    """Run a Claude agent over a report folder and return the extracted content.

    Args:
        report_dir: 报告文件夹路径（report_parser 输出的目录）。
        query: 需要从报告中提取内容的问题。
        max_turns: 允许的最大 agent 执行轮数。
        verbose: 为 True 时实时打印 agent 的工具调用过程（ls/grep/read 等）。

    Returns:
        agent 输出的纯文本内容（含工具调用日志）。
    """
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk import query as sdk_query

    report_path = Path(report_dir)
    if not report_path.is_dir():
        raise FileNotFoundError(f"Report directory not found: {report_path}")

    prompt = build_fetch_prompt(report_path.name, query, report_dir=report_path)

    output_lines: list[str] = []
    async for msg in sdk_query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Grep", "Glob"],
            permission_mode="acceptEdits",
            cwd=str(report_path),
            max_turns=max_turns,
            model="deepseek-v4-pro",
        ),
    ):
        tool_lines = _render_tool_call(msg)
        for line in tool_lines:
            output_lines.append(line)
            if verbose:
                print(line, flush=True)

        if not _is_message(msg):
            continue
        output_lines.extend(_iter_text_blocks(msg))

    return "\n".join(output_lines)


if __name__ == "__main__":
    import asyncio
    import sys

    if len(sys.argv) < 3:
        print("Usage: python fetch.py <report_dir> <query>")
        sys.exit(1)

    report_dir = sys.argv[1]
    query = sys.argv[2]

    content = asyncio.run(fetch_report_content(report_dir, query, verbose=True))
    print(content)
