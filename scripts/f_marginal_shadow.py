#!/usr/bin/env python3
"""F 边际改善影子期观测 CLI（设计 ``MIDTERM_F_MARGINAL_DESIGN`` §5.2 / §7.1 G-8）。

薄壳：只做**参数解析 + I/O + 打印**，全部统计口径复用
:mod:`alphabee.midterm.marginal_shadow`（``load_frames`` / ``dedupe_frames`` /
``shadow_report``），不在此复制任何公式。

数据源（默认只读 tracking 帧，保持改造前行为）::

    python scripts/f_marginal_shadow.py                          # 默认 data/midterm/state
    python scripts/f_marginal_shadow.py --state-dir data/midterm/state
    python scripts/f_marginal_shadow.py --paths a.jsonl b.json    # 指定文件（替代默认目录）
    python scripts/f_marginal_shadow.py --source task-records     # 只读 task_records
    python scripts/f_marginal_shadow.py --source both             # state + task_records 合并
    python scripts/f_marginal_shadow.py --json                    # 机器可读输出

``--source both`` 时 state 帧排在前面，再由 ``dedupe_frames`` 按 ``(symbol, as_of_date)``
去重（默认）；即同标的同日优先保留字段更全的 tracking 帧。``--no-dedupe`` 关闭去重
（把同日多次查询当作多帧，仅用于排查，不用于 G-8 判读）。

G-8 门槛（全部满足才允许把 ``f_marginal_mode`` 从 ``shadow`` 改为 ``on``）：
真实帧 ≥ 50、``median|ΔF|`` ≤ 0.10、状态翻转率 ≤ 10%、危险翻转率 ≤ 3%、覆盖率 ≥ 85%。
判读为 ``FAIL`` 时本脚本照常退出 0（观测工具不承担门禁强制），门禁结论写入报告。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from alphabee.midterm.marginal_shadow import (
    G8_MAX_ABS_DELTA_F_MEDIAN,
    G8_MAX_DANGEROUS_FLIP_RATE,
    G8_MAX_FLIP_RATE,
    G8_MIN_COVERAGE,
    G8_MIN_FRAMES,
    dedupe_frames,
    load_frames,
    shadow_report,
)

_DEFAULT_STATE_DIR = "data/midterm/state"

#: 数据源（默认 ``state``，保持改造前的输出与语义不变）。
_SOURCE_STATE = "state"
_SOURCE_TASK_RECORDS = "task-records"
_SOURCE_BOTH = "both"
_SOURCES = (_SOURCE_STATE, _SOURCE_TASK_RECORDS, _SOURCE_BOTH)


def _fmt(value: Any, digits: int = 4) -> str:
    """确定性格式化：``None`` → ``—``，float 保留 ``digits`` 位，bool → 通过/不通过。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _render_text(
    report: dict[str, Any],
    *,
    n_frames: int,
    skipped: int,
    sources: list[str],
    duplicates: int = 0,
) -> str:
    dedupe_suffix = f"  去重丢弃={duplicates}" if duplicates else ""
    lines = [
        "F 边际影子观测（设计 §5.2 / §7.1 G-8）",
        f"  语料: {', '.join(sources) if sources else '—'}",
        f"  帧数: n_frames={n_frames}  skipped={skipped}{dedupe_suffix}"
        f"  fundamental={report['n_fundamental_frames']}  含边际键={report['n_with_marginal']}",
        # G-8 判读用 ex-Q1 口径（P0-3）；旧口径保留作对照，避免打印值与门禁自相矛盾。
        "S1 ΔF 分布:"
        f"  |ΔF| 中位数(ex-Q1)={_fmt(report['median_abs_delta_ex_q1'])}"
        f"  P90={_fmt(report['abs_delta_f_p90'])}"
        f"  （G-8 口径；全帧中位数={_fmt(report['abs_delta_f_median'])}）"
        f"  （分母 n_delta_frames_ex_q1={report['n_delta_frames_ex_q1']}"
        f" / n_delta_frames={report['n_delta_frames']}）",
        "S2 状态翻转率:"
        f"  {_fmt(report['flip_rate'])}"
        f"  （n_flips={report['n_flips']} / n_flip_frames={report['n_flip_frames']}）",
        "S3 危险翻转率:"
        f"  {_fmt(report['dangerous_flip_rate'])}"
        f"  （n_dangerous_flips={report['n_dangerous_flips']}，S5→{{S1,S2,S3}}）",
        "S4 加速度覆盖率:"
        f"  (ex-Q1)={_fmt(report['coverage_ex_q1'])}"
        f"  （G-8 口径；全帧覆盖率={_fmt(report['coverage'])}）"
        f"  （{report['n_with_marginal_ex_q1']} / {report['n_fundamental_frames_ex_q1']}）",
        "G-8 门槛判读:",
        f"  n_frames >= {G8_MIN_FRAMES}: {_fmt(report['n_frames_ok'])}（{n_frames}）",
        f"  median|ΔF|(ex-Q1) <= {G8_MAX_ABS_DELTA_F_MEDIAN}: {_fmt(report['median_ok'])}"
        f"（{_fmt(report['median_abs_delta_ex_q1'])}；全帧={_fmt(report['abs_delta_f_median'])}）",
        f"  flip_rate <= {G8_MAX_FLIP_RATE}: {_fmt(report['flip_ok'])}（{_fmt(report['flip_rate'])}）",
        f"  dangerous_flip_rate <= {G8_MAX_DANGEROUS_FLIP_RATE}: {_fmt(report['dangerous_ok'])}"
        f"（{_fmt(report['dangerous_flip_rate'])}）",
        f"  coverage(ex-Q1) >= {G8_MIN_COVERAGE}: {_fmt(report['coverage_ok'])}"
        f"（{_fmt(report['coverage_ex_q1'])}；全帧={_fmt(report['coverage'])}）",
        f"  ⇒ G-8 {'通过（可评审 PR-4 开权重）' if report['g8_pass'] else '不通过（PR-4 开权重不得执行）'}",
    ]
    return "\n".join(lines)


def _load_state_frames(paths: list[str] | None, state_dir: str) -> tuple[list[dict[str, Any]], int, list[str]]:
    """读取 state 源帧（``--paths`` 优先于 ``--state-dir``）。"""
    state_sources = list(paths) if paths else [state_dir]
    frames, skipped = load_frames(state_sources)
    return frames, skipped, [str(source) for source in state_sources]


def _load_task_record_frames(task_records_dir: str | None) -> tuple[list[dict[str, Any]], int, str]:
    """读取 task_records 源帧（``TaskStore`` 默认目录或 ``--task-records-dir``）。"""
    from alphabee.task_records.shadow import load_task_record_frames as _adapt
    from alphabee.task_records.store import TaskStore

    store = TaskStore(base_dir=task_records_dir)
    total = store.count()
    # 显式传 total：``load_all`` 默认 limit=100，而 G-8 语料会超过 100 帧。
    records = store.load_all(limit=total) if total else []
    frames, skipped = _adapt(records)
    return frames, skipped, f"task_records:{store.base_dir}"


def _collect_frames(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], int, int, list[str]]:
    """按 ``--source`` 汇总帧，返回 ``(frames, skipped, duplicates, sources)``。"""
    frames: list[dict[str, Any]] = []
    sources: list[str] = []
    skipped = 0

    if args.source in (_SOURCE_STATE, _SOURCE_BOTH):
        state_frames, state_skipped, state_sources = _load_state_frames(args.paths, args.state_dir)
        frames.extend(state_frames)
        skipped += state_skipped
        sources.extend(state_sources)

    if args.source in (_SOURCE_TASK_RECORDS, _SOURCE_BOTH):
        record_frames, record_skipped, record_source = _load_task_record_frames(args.task_records_dir)
        frames.extend(record_frames)
        skipped += record_skipped
        sources.append(record_source)

    duplicates = 0
    if not args.no_dedupe:
        frames, duplicates = dedupe_frames(frames)
    return frames, skipped, duplicates, sources


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="F 边际改善影子期观测（S1–S8 + G-8 判读）")
    parser.add_argument(
        "--source",
        choices=_SOURCES,
        default=_SOURCE_STATE,
        help="数据源：state（默认，tracking 帧）/ task-records（查询帧）/ both（合并）",
    )
    parser.add_argument(
        "--state-dir",
        default=_DEFAULT_STATE_DIR,
        help=f"历史帧目录（默认 {_DEFAULT_STATE_DIR}；目录内按文件名展开 *.jsonl / *.json）",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="显式指定 state 帧文件/目录（给出时**替代** --state-dir）",
    )
    parser.add_argument(
        "--task-records-dir",
        default=None,
        help="task_records 根目录（缺省 TaskStore 默认目录；仅 --source task-records/both 使用）",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="关闭 (symbol, as_of_date) 去重（同日多次查询算多帧，仅用于排查）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（含逐项指标与 G-8 判读）")
    args = parser.parse_args(argv)

    if args.source == _SOURCE_TASK_RECORDS and args.paths:
        parser.error("--paths 属于 state 源；--source task-records 时请改用 --task-records-dir")

    frames, skipped, duplicates, sources = _collect_frames(args)
    report = shadow_report(frames)

    if args.json:
        payload = {
            "sources": sources,
            "skipped": skipped,
            "duplicates": duplicates,
            **report,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(
        _render_text(
            report,
            n_frames=len(frames),
            skipped=skipped,
            sources=sources,
            duplicates=duplicates,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
