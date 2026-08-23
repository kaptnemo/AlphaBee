"""多轮对话（从根 main.py 拆出）。"""

from __future__ import annotations

from typing import Any

from alphabee.apps.cli.args import normalize_query
from alphabee.apps.cli.colors import Color, color
from alphabee.apps.cli.parsing import append_turn_history
from alphabee.apps.cli.renderer import print_chat_help
from alphabee.apps.cli.streaming import run_query


async def run_chat_session(
    initial_query: str | None = None,
    *,
    enhance: bool = False,
    llm_review: bool = False,
) -> None:
    history: list[Any] = []
    turn = 1

    print_chat_help()

    if initial_query:
        initial_query = normalize_query(initial_query)
        answer = await run_query(initial_query, history, enhance=enhance, llm_review=llm_review)
        append_turn_history(history, initial_query, answer)
        turn += 1

    while True:
        try:
            raw = input(color(f"[{turn:02d}] 你> ", Color.BOLD, Color.CYAN))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        query = raw.strip()
        if not query:
            continue

        command = query.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            print()
            print(color("  👋 会话结束", Color.DIM))
            print()
            break
        if command == "/clear":
            history.clear()
            turn = 1
            print()
            print(color("  ♻ 上下文已清空", Color.DIM))
            print()
            continue

        query = normalize_query(query)
        answer = await run_query(query, history, enhance=enhance, llm_review=llm_review)
        append_turn_history(history, query, answer)
        turn += 1
