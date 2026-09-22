"""CLI 入口编排（从根 main.py 拆出）。

``main()`` 只负责「解析参数 → 按模式分派」，不包含渲染/流式/解析的具体实现——
那些已分别下沉到 renderer / streaming / chat / tasks / args 模块。

研究连续体 P2（D2-B2，``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.2-C）在本模块加了两个
入口级接缝：

* :func:`enforce_preflight_gate` —— **阻断侧**：该标的存在未对账的陈旧状态时按
  ``deviation.tracking.block_stale_runs`` 决定是否拒绝本次交互请求（``--allow-stale`` 显式放行）；
  记账与阻断解耦：放行**不**取消记账（记账在 ``collect_raw_facts`` 侧恒发生）。
* :func:`print_track_alerts_view` —— ``--track-alerts`` 的**只读视图**，同时修掉
  ``data/tracking/alerts/*.jsonl`` 的 dead-end（§14.3 D2 依据）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from alphabee.apps.cli.args import normalize_query, parse_args
from alphabee.apps.cli.chat import run_chat_session
from alphabee.apps.cli.colors import Color, color, set_color_enabled
from alphabee.apps.cli.renderer import print_footer, print_header
from alphabee.apps.cli.streaming import run_query
from alphabee.apps.cli.tasks import handle_task_cli
from alphabee.orchestrator.services.preflight import PreflightVerdict, check_preflight
from alphabee.utils import configure_logging
from alphabee.workflow import render_monitor_report, run_framework_monitor

logger = logging.getLogger(__name__)

#: ``deviation.tracking.max_alerts_shown`` 的 fail-open 缺省（与 ``DeviationTrackingSettings`` 同值）。
DEFAULT_MAX_ALERTS_SHOWN = 20


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


# ── 研究连续体 P2（D2-B2）：入口前置校验（阻断侧）+ 告警只读视图 ─────────────


def symbol_from_query(query: str) -> str | None:
    """从查询串提取**首个**股票代码（与 ``collectors._first_symbol`` 同源：``extract_symbols_from_query``）。

    fail-open：股票清单缺失/读取失败/查询为空 → ``None``（空标的在入口校验里**不阻断**），
    入口 gate 绝不因符号解析问题崩溃或误拦。
    """
    if not query:
        return None
    try:
        from alphabee.tools.common import extract_symbols_from_query

        symbols = extract_symbols_from_query(query)
    except Exception as exc:  # noqa: BLE001 - 符号解析失败只降级为"不阻断"
        logger.warning("symbol extraction failed (preflight skipped): %s", exc)
        return None
    return next(iter(symbols.values()), None) if symbols else None


def block_stale_runs_from_settings() -> bool:
    """``deviation.tracking.block_stale_runs``（缺段/异常 → ``True`` = 默认阻断，fail-open）。

    ``True`` 是 ``DeviationTrackingSettings`` 的默认值（§15.7），故"配置不可读"与"未配置"
    都落在同一默认上；异常绝不因此放行或崩溃。
    """
    try:
        from alphabee.config import get_settings

        section = getattr(getattr(get_settings(), "deviation", None), "tracking", None)
        value = getattr(section, "block_stale_runs", None)
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断 CLI（fail-open 到默认阻断）
        logger.warning("deviation.tracking.block_stale_runs unavailable (fail-open=True): %s", exc)
        return True
    return True if value is None else bool(value)


def max_alerts_shown_from_settings() -> int:
    """``deviation.tracking.max_alerts_shown``（缺段/异常/非正 → ``DEFAULT_MAX_ALERTS_SHOWN``）。"""
    try:
        from alphabee.config import get_settings

        section = getattr(getattr(get_settings(), "deviation", None), "tracking", None)
        value = getattr(section, "max_alerts_shown", None)
    except Exception as exc:  # noqa: BLE001 - 只读视图的显示条数，绝不打断 CLI
        logger.warning(
            "deviation.tracking.max_alerts_shown unavailable (fail-open=%s): %s",
            DEFAULT_MAX_ALERTS_SHOWN,
            exc,
        )
        return DEFAULT_MAX_ALERTS_SHOWN
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_ALERTS_SHOWN


def enforce_preflight_gate(args: argparse.Namespace, *, start_ts: float) -> PreflightVerdict:
    """入口前置校验的**阻断侧**（§15.2-C）：命中且未显式放行 ⇒ 打印提示并 ``SystemExit(3)``。

    * ``block_enabled`` 取配置 ``deviation.tracking.block_stale_runs``（``false`` ⇒ 本函数恒不阻断，
      只剩 ``collect_raw_facts`` 侧的记账）；
    * ``--allow-stale`` 是**显式放行**：放行**不**意味着不记账 —— 记账在 ``collect_raw_facts``
      侧恒发生（§15.2 设计决策 3；§11 验收 2 修订口径："不可能**静默地**继续"）；
    * 退出码 ``3`` 与 tracking ``--loop`` 的 ``2`` 同风格：明确的"**未执行**"退出码（非错误码 1），
      便于外部脚本区分"被前置校验拦下"与"运行失败"；
    * 判定本身永不抛（fail-open）；``--track-alerts`` / 监控框架 / task records 等模式不走本函数。
    """
    verdict = check_preflight(
        symbol_from_query(args.query or ""),
        block_enabled=block_stale_runs_from_settings(),
    )
    if verdict.blocking and not args.allow_stale:
        print(color("  ⚠ 该标的存在未对账的陈旧状态，已阻断；如需继续请加 --allow-stale", Color.YELLOW))
        print_footer(
            0,
            time.monotonic() - start_ts,
            enhance=args.enhance,
            llm_review=args.llm_review,
            midterm=args.midterm,
        )
        raise SystemExit(3)
    return verdict


def _alerts_root() -> Path:
    """告警目录（``tracking.scheduler.default_alert_dir()``；import 推迟到函数内，见调用方）。"""
    from alphabee.tracking.scheduler import default_alert_dir

    return Path(default_alert_dir())


def _read_alert_rows(path: Path, limit: int) -> list[dict[str, Any]]:
    """读 JSONL 的**末尾 limit 条可用行**（文件缺失 → ``[]``；坏行/非对象行跳过、不抛）。

    与 ``services/preflight.py::_read_last_alert_row`` 是同一份存储格式的两个消费面（前者取末行判
    "有无未消费触发"，这里取末 N 行做展示）—— 两处都**只读**、都对坏行宽容。
    """
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows[-limit:] if limit > 0 else rows


def _kind_label(value: Any) -> str:
    """触发 kind 的可读标签（枚举 → ``value``，其余 → ``str``）。"""
    return str(getattr(value, "value", value))


def _format_alert_frame(report: Any, index: int) -> list[str]:
    """一帧 ``TrackingReport`` → 渲染行（``as_of`` / 状态 / 触发 / 退出信号 / 行动类输出，§15.2-C）。"""
    # P4（D3-C2）会新增 ``TrackingReport.research_status`` 派生状态字；此处 getattr 前向兼容：
    # 字段存在即显示（P4 落地后自动生效），否则回落既有 ``state``（认知状态 argmax），**不伪造**派生视图。
    status = str(getattr(report, "research_status", "") or report.state or "—")
    kinds = [_kind_label(trigger.kind) for trigger in report.triggers]
    suffix = f"[{', '.join(kinds)}]" if kinds else ""
    degraded = "  降级=是" if getattr(report, "degraded", False) else ""
    lines = [
        f"[{index:02d}] {report.symbol or '—'}  as_of={report.as_of or '—'}  状态={status}"
        f"  触发={len(kinds)}{suffix}  退出信号={len(report.exit_reasons)}"
        f"  行动类={len(report.blocked_actions)}{degraded}"
    ]
    for trigger in report.triggers:
        lines.append(f"      · 触发 {_kind_label(trigger.kind)}：{trigger.reason}")
    for reason in report.exit_reasons:
        lines.append(f"      · 退出信号：{reason}")
    for action in report.blocked_actions:
        lines.append(f"      · 行动类输出（Tier {report.escalation_tier}，须人工确认）：{action}")
    return lines


def render_track_alerts_view(symbol: str | None = None, *, limit: int | None = None) -> str:
    """把 ``data/tracking/alerts/*.jsonl`` 渲染成文本（**纯读**；异常 → 单行提示，不抛）。

    * ``symbol`` 为空 → 列出目录下**全部**标的（按文件名排序，确定性）；
    * 每个标的取末尾 ``limit`` 帧（缺省 ``deviation.tracking.max_alerts_shown``，fail-open 20）；
    * 用 ``TrackingReport.model_validate`` 还原（F4 落盘的就是它的 JSON），单行不可解析只跳过；
    * **不写任何文件、不触发 run、不改编排** —— §15.2-E 的回滚方式就是"不调用即可"。
    """
    try:
        from alphabee.tracking.scheduler import TrackingReport

        alert_dir = _alerts_root()
    except Exception as exc:  # noqa: BLE001 - 只读视图绝不打断 CLI
        return f"跟踪告警视图不可用（fail-open）：{exc}"

    shown = (
        limit
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        else max_alerts_shown_from_settings()
    )
    name = str(symbol or "").strip()
    targets = [name] if name else [path.stem for path in sorted(alert_dir.glob("*.jsonl"))]

    lines = [f"===== 跟踪告警（只读视图，§15.2-C） 目录={alert_dir} 每标的末 {shown} 帧 ====="]
    if not targets:
        lines.append("（无告警记录）")
        return "\n".join(lines)

    for target in targets:
        path = alert_dir / f"{target}.jsonl"
        rows = _read_alert_rows(path, shown)
        lines.append(f"── {target}  文件={path}  命中 {len(rows)} 帧")
        if not rows:
            lines.append("      （无告警记录：该标的尚未产出触发帧，或文件不存在）")
            continue
        index = 0
        for row in rows:
            try:
                report = TrackingReport.model_validate(row)
            except Exception as exc:  # noqa: BLE001 - 单行坏数据只跳过，不打断视图
                lines.append(f"[??] 告警行不可解析（跳过）：{exc}")
                continue
            index += 1
            lines.extend(_format_alert_frame(report, index))
    return "\n".join(lines)


def print_track_alerts_view(symbol: str | None = None, *, limit: int | None = None) -> None:
    """打印跟踪告警（只读视图，§15.2-C；修掉 ``data/tracking/alerts/*.jsonl`` 的 dead-end）。

    import 推迟到 :func:`render_track_alerts_view` 内（与 :func:`print_deviations_view` 同理由）：
    只有真的要看告警视图时才拉起 ``alphabee.tracking`` 链，普通查询/对话路径不付这份 import 成本。
    """
    print()
    print(render_track_alerts_view(symbol, limit=limit))
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

    # ── 跟踪告警（只读视图，研究连续体 P2 / D2-B2）：不触发 run、不写库、不改编排 ──
    if args.track_alerts is not None:
        print_track_alerts_view(args.track_alerts or None)
        return

    # ── 入口前置校验（阻断侧，研究连续体 P2 / D2-B2）：进入 run_query/chat 之前判定 ──
    # 「记录」与「阻断」解耦：这里只决定**是否执行**；命中事实的记账在 collect_raw_facts 侧恒发生，
    # 所以 `--allow-stale` 放行后仍然可审计（§11 验收 2 修订口径）。
    start_ts = time.monotonic()
    enforce_preflight_gate(args, start_ts=start_ts)

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
