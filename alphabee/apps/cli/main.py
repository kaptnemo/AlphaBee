"""CLI 入口编排（从根 main.py 拆出）。

``main()`` 只负责「解析参数 → 按模式分派」，不包含渲染/流式/解析的具体实现——
那些已分别下沉到 renderer / streaming / chat / tasks / args 模块。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from alphabee.apps.cli.args import normalize_query, parse_args
from alphabee.apps.cli.chat import run_chat_session
from alphabee.apps.cli.colors import Color, color, set_color_enabled
from alphabee.apps.cli.renderer import print_footer, print_header
from alphabee.apps.cli.streaming import run_query
from alphabee.apps.cli.tasks import handle_task_cli
from alphabee.utils import configure_logging
from alphabee.workflow import render_monitor_report, run_framework_monitor


def print_deviations_view(run_id: str | None) -> None:
    """打印偏离账本时间线（F5，§14.5-B / §15 验收 1 的只读视图）。

    * ``run_id`` 为 ``None``/空 → 取账本中最近一次 run（``latest_run_id``）；仍无 → 渲染确定性空视图；
    * **只读**：不改编排、不触发 run、不写库；§14.8 PR8 的回滚方式就是"不调用即可"；
    * import 推迟到本函数内：只有真的要看偏离视图时才拉起 ``data_fetch``/SQLAlchemy 链，
      避免给普通查询/对话路径增加 import 成本。
    """
    from alphabee.orchestrator.services.telemetry import latest_run_id, render_deviation_timeline

    print()
    print(render_deviation_timeline(run_id or latest_run_id() or ""))
    print()


def main() -> None:
    args = parse_args()
    if args.monitor_framework and not args.symbol:
        raise SystemExit("--monitor-framework 模式下必须同时提供 --symbol")

    if args.query:
        args.query = normalize_query(args.query)

    if args.no_color or not sys.stdout.isatty():
        set_color_enabled(False)

    configure_logging(log_dir=Path(args.log_dir))

    # Keep file logging but suppress the console handler so it doesn't
    # mix with our pretty-printed output.
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setLevel(logging.WARNING)

    if args.monitor_framework:
        start_ts = time.monotonic()
        print_header(
            f"监控框架：{args.monitor_framework} | 标的：{args.symbol}",
            enhance=args.enhance,
            llm_review=args.llm_review,
            midterm=False,
        )
        result = asyncio.run(
            run_framework_monitor(
                framework_path=args.monitor_framework,
                symbol=args.symbol,
                periods=args.monitor_periods,
            )
        )
        print(color("  💡 最终回答", Color.BOLD, Color.GREEN))
        print(render_monitor_report(result))
        print_footer(1, time.monotonic() - start_ts, enhance=False, llm_review=False, midterm=False)
        return

    # ── 偏离账本时间线（只读视图，F5）──
    if args.deviations is not None:
        print_deviations_view(args.deviations)
        return

    # ── Task records CLI ──
    if args.task_stats or args.distill or args.task_history or args.task_record:
        handle_task_cli(args)
        return
    if args.chat or not args.query:
        asyncio.run(
            run_chat_session(
                args.query,
                enhance=args.enhance,
                llm_review=args.llm_review,
                midterm=args.midterm,
            )
        )
        return

    asyncio.run(
        run_query(
            args.query,
            enhance=args.enhance,
            llm_review=args.llm_review,
            midterm=args.midterm,
        )
    )
