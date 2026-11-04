#!/usr/bin/env python3
"""F 口径校准报告脚本（设计 ``MIDTERM_F_DATA_GOVERNANCE_DESIGN`` §15 / P2-3）。

**薄壳**：只做参数解析 + I/O + 调用既有纯函数（``marginal_shadow`` 的观测与
``score_engine.fundamental_breakdown`` 的评分），不复制任何公式、**不修改模块常量**：

- 网格 ``_FUNDAMENTAL_YOY_SCALE`` × ``F_ACCEL_SCALE`` × ``_OCF_NEGATIVE_PENALTY`` ×
  ``F_MARGIN_DELTA_SCALE`` 通过 ``unittest.mock.patch.object`` 临时注入执行评分，
  退出后恢复原值（只读常量文件）；
- 样本门槛：真实帧 ≥ 50、覆盖 ≥ 20 只标的、≥ 4 个报告季度；不满足 ⇒ 输出
  「样本不足，不校准」、以 0 退出、不产网格结论；
- 产物：Markdown 报告 + 结构化 JSON（``schema_version`` / ``generated_at`` /
  ``sample`` / ``metrics`` / ``grid`` / ``verdict``）；前向收益模块不可用/未接入时
  显式跳过（``forward_returns`` 记录可用性与说明）；
- 不联网；仅供人工评审，不自动改常量。

用法::

    python scripts/f_calibration.py                       # 默认 data/midterm/state
    python scripts/f_calibration.py --paths a.jsonl b.json
    python scripts/f_calibration.py --source both --out tmp/report.md --json-out tmp/out.json
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any
from unittest.mock import patch

from alphabee.midterm import score_engine
from alphabee.midterm.marginal_shadow import dedupe_frames, load_frames, shadow_report
from alphabee.midterm.models import ExpectationFactor, FundamentalFactor
from alphabee.midterm.score_engine import (
    KNOWN_MARGINAL_MODES,
    KNOWN_OCF_PENALTY_BASES,
    KNOWN_RECURRING_MODES,
    KNOWN_SEGMENT_BONUS_MODES,
    fundamental_breakdown,
)

SCHEMA_VERSION = "1"

#: 网格（设计 §15.2；值为浮点，便于 JSON 稳定）。
GRID_VALUES: dict[str, tuple[float, ...]] = {
    "_FUNDAMENTAL_YOY_SCALE": (20.0, 30.0, 40.0),
    "F_ACCEL_SCALE": (15.0, 25.0, 35.0),
    "_OCF_NEGATIVE_PENALTY": (0.3, 0.5, 0.7),
    "F_MARGIN_DELTA_SCALE": (3.0, 4.0, 6.0),
}

#: 样本门槛（设计 §15.2）。
SAMPLE_MIN_FRAMES = 50
SAMPLE_MIN_SYMBOLS = 20
SAMPLE_MIN_QUARTERS = 4

INSUFFICIENT_VERDICT = "样本不足，不校准"
REPORT_VERDICT = "仅报告，不自动改常量"

_DEFAULT_STATE_DIR = "data/midterm/state"
_DEFAULT_OUT = "docs/implementation/midterm/F_CALIBRATION_REPORT.md"
_DEFAULT_JSON_OUT = "tmp/f_calibration.json"

_SOURCE_STATE = "state"
_SOURCE_TASK_RECORDS = "task-records"
_SOURCE_BOTH = "both"
_SOURCES = (_SOURCE_STATE, _SOURCE_TASK_RECORDS, _SOURCE_BOTH)

_QUARTERS = {"0331": "Q1", "0630": "Q2", "0930": "Q3", "1231": "Q4"}


# ─────────────────────────────────────────────────────────────────────────────
# 样本与口径（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def _fundamental_of(frame: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = frame.get("factor_snapshot")
    if not isinstance(snapshot, dict):
        return None
    fundamental = snapshot.get("fundamental")
    return fundamental if isinstance(fundamental, dict) else None


def _report_period_of(frame: dict[str, Any]) -> str | None:
    fundamental = _fundamental_of(frame)
    raw = (fundamental or {}).get("report_period") or frame.get("report_period")
    if raw is None:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else None


def sample_of(frames: list[dict[str, Any]]) -> dict[str, Any]:
    """样本统计（真实帧 / 标的数 / 报告季度数）+ 门槛判读。"""
    symbols = {str(frame.get("symbol") or "").strip() for frame in frames}
    symbols.discard("")
    quarters = {
        f"{period[:4]}{_QUARTERS[period[4:8]]}"
        for frame in frames
        if (period := _report_period_of(frame)) is not None and period[4:8] in _QUARTERS
    }
    sufficient = (
        len(frames) >= SAMPLE_MIN_FRAMES and len(symbols) >= SAMPLE_MIN_SYMBOLS and len(quarters) >= SAMPLE_MIN_QUARTERS
    )
    return {
        "n_frames": len(frames),
        "n_symbols": len(symbols),
        "n_report_quarters": len(quarters),
        "report_quarters": sorted(quarters),
        "thresholds": {
            "min_frames": SAMPLE_MIN_FRAMES,
            "min_symbols": SAMPLE_MIN_SYMBOLS,
            "min_quarters": SAMPLE_MIN_QUARTERS,
        },
        "sufficient": sufficient,
    }


def grid_combos() -> list[dict[str, float]]:
    """笛卡尔网格（确定性顺序：3×3×3×3 = 81 组）。"""
    names = list(GRID_VALUES)
    return [dict(zip(names, values)) for values in product(*(GRID_VALUES[name] for name in names))]


def _frame_mode(frame: dict[str, Any], field: str, known: tuple[str, ...], default: str) -> str:
    value = str(frame.get(field) or "").strip().lower()
    return value if value in known else default


def recompute_frames(frames: list[dict[str, Any]], combo: dict[str, float]) -> list[dict[str, Any]]:
    """在网格参数下重算每帧的 ``score_breakdown``（临时 patch 常量，不写文件）。

    只对 ``fundamental`` 可解析的帧重算；解析失败/缺 fundamental 的帧原样保留。
    模式取帧内 provenance（``f_marginal_mode`` / ``ocf_penalty_basis`` /
    ``f_recurring_mode`` / ``segment_bonus_mode``），缺失回落生产默认。
    """
    recomputed: list[dict[str, Any]] = []
    with ExitStack() as stack:
        for name, value in combo.items():
            stack.enter_context(patch.object(score_engine, name, value))
        for frame in frames:
            raw_fundamental = _fundamental_of(frame)
            if raw_fundamental is None:
                recomputed.append(frame)
                continue
            try:
                fundamental = FundamentalFactor.model_validate(raw_fundamental)
                snapshot = frame.get("factor_snapshot") or {}
                expectation = ExpectationFactor.model_validate(snapshot.get("expectation") or {})
                breakdown = fundamental_breakdown(
                    fundamental,
                    expectation,
                    marginal_mode=_frame_mode(frame, "f_marginal_mode", KNOWN_MARGINAL_MODES, "shadow"),
                    ocf_penalty_basis=_frame_mode(frame, "ocf_penalty_basis", KNOWN_OCF_PENALTY_BASES, "ttm"),
                    f_recurring_mode=_frame_mode(frame, "f_recurring_mode", KNOWN_RECURRING_MODES, "shadow"),
                    segment_bonus_mode=_frame_mode(
                        frame, "segment_bonus_mode", KNOWN_SEGMENT_BONUS_MODES, "period_aligned"
                    ),
                )
            except Exception:  # noqa: BLE001 - 单帧坏数据不中断整批校准（原样保留）
                recomputed.append(frame)
                continue
            new_frame = copy.deepcopy(frame)
            new_frame.setdefault("factor_snapshot", {})["fundamental"] = {
                **raw_fundamental,
                "score_breakdown": breakdown or {},
            }
            recomputed.append(new_frame)
    return recomputed


def _stability(flip_rate: float | None) -> float | None:
    return None if flip_rate is None else 1.0 - flip_rate


def evaluate_grid(frames: list[dict[str, Any]], combos: list[dict[str, float]] | None = None) -> list[dict[str, Any]]:
    """对每组网格参数重算并汇总 S1–S4/G-8 指标（只报告，不改常量）。"""
    results: list[dict[str, Any]] = []
    for combo in combos if combos is not None else grid_combos():
        report = shadow_report(recompute_frames(frames, combo))
        results.append(
            {
                "params": dict(combo),
                "metrics": {
                    "median_abs_delta_ex_q1": report["median_abs_delta_ex_q1"],
                    "coverage_ex_q1": report["coverage_ex_q1"],
                    "flip_rate": report["flip_rate"],
                    "dangerous_flip_rate": report["dangerous_flip_rate"],
                    "state_stability": _stability(report["flip_rate"]),
                    "g8_pass": report["g8_pass"],
                },
            }
        )
    return results


def baseline_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """报告 JSON 的 ``metrics``：S1–S4 + S5–S8 + 状态稳定性（口径与 shadow_report 一致）。"""
    return {
        "n_frames": report["n_frames"],
        "abs_delta_f_median": report["abs_delta_f_median"],
        "abs_delta_f_p90": report["abs_delta_f_p90"],
        "median_abs_delta_ex_q1": report["median_abs_delta_ex_q1"],
        "coverage": report["coverage"],
        "coverage_ex_q1": report["coverage_ex_q1"],
        "flip_rate": report["flip_rate"],
        "dangerous_flip_rate": report["dangerous_flip_rate"],
        "state_stability": _stability(report["flip_rate"]),
        "g8_pass": report["g8_pass"],
        "s5_ocf_hit_rate": report["s5_ocf_hit_rate"],
        "s6_beat_miss_match": report["s6_beat_miss_match"],
        "s7_recurring_gap": report["s7_recurring_gap"],
        "s8_segment": report["s8_segment"],
    }


def forward_returns_info() -> dict[str, Any]:
    """前向收益可用性（不联网、不猜 API）：可用则记录，否则显式跳过。"""
    try:
        available = importlib.util.find_spec("alphabee.market_regime.forward_returns") is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        return {"available": False, "attached": False, "note": "market_regime.forward_returns 不可用，显式跳过"}
    return {
        "available": True,
        "attached": False,
        "note": "forward_returns 模块可用，但校准帧未携带行情序列，未计算（显式跳过）",
    }


def build_payload(
    frames: list[dict[str, Any]],
    *,
    source: str,
    skipped: int,
    duplicates: int,
) -> dict[str, Any]:
    """组装稳定 JSON（schema_version / generated_at / sample / metrics / grid / verdict）。"""
    sample = sample_of(frames)
    report = shadow_report(frames)
    sufficient = bool(sample["sufficient"])
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "skipped": skipped,
        "duplicates": duplicates,
        "sample": sample,
        "metrics": baseline_metrics(report),
        "grid": evaluate_grid(frames) if sufficient else [],
        "verdict": REPORT_VERDICT if sufficient else INSUFFICIENT_VERDICT,
        "forward_returns": forward_returns_info(),
        "note": "只出报告；不修改 score_engine 常量，不自动开权重",
    }
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# 渲染与写盘（薄壳）
# ─────────────────────────────────────────────────────────────────────────────

_METRIC_LABELS: tuple[tuple[str, str], ...] = (
    ("median_abs_delta_ex_q1", "S1 median|ΔF|（ex-Q1）"),
    ("coverage_ex_q1", "S4 覆盖率（ex-Q1）"),
    ("flip_rate", "S2 翻转率"),
    ("dangerous_flip_rate", "S3 危险翻转率"),
    ("state_stability", "状态稳定性"),
)


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(payload: dict[str, Any]) -> str:
    """确定性 Markdown 报告（样本不足时只说明门槛，不产网格结论）。"""
    sample = payload["sample"]
    lines = [
        "# F 口径校准报告（P2-3）",
        "",
        f"- schema_version: `{payload['schema_version']}`",
        f"- generated_at: `{payload['generated_at']}`",
        f"- source: `{payload['source']}`  帧数: {sample['n_frames']}"
        f"  标的数: {sample['n_symbols']}  报告季度数: {sample['n_report_quarters']}",
        f"- 样本门槛: 帧 ≥ {SAMPLE_MIN_FRAMES}、标的 ≥ {SAMPLE_MIN_SYMBOLS}、季度 ≥ {SAMPLE_MIN_QUARTERS}",
        "",
        f"**判读：{payload['verdict']}**",
        "",
    ]
    if not sample["sufficient"]:
        lines += [
            "样本不足，不校准：本次不产网格结论；待真实帧/标的/季度补足后重跑。",
            "",
        ]
        return "\n".join(lines) + "\n"

    metrics = payload["metrics"]
    lines += ["## 当前口径指标（S1–S8）", ""]
    for key, label in _METRIC_LABELS:
        lines.append(f"- {label}: {_fmt(metrics.get(key))}")
    lines.append(f"- G-8 判读: {_fmt(metrics.get('g8_pass'))}")
    lines.append("")
    lines += ["## 网格（只报告，不改常量）", ""]
    header = "| " + " | ".join([*GRID_VALUES, "S1 median|ΔF|（ex-Q1）", "S4 覆盖率", "S2 翻转率", "状态稳定性"]) + " |"
    divider = "|" + "---|" * (len(GRID_VALUES) + 4)
    lines += [header, divider]
    for row in payload["grid"]:
        params = row["params"]
        grid_metrics = row["metrics"]
        cells = [f"{params[name]:g}" for name in GRID_VALUES]
        cells += [
            _fmt(grid_metrics.get("median_abs_delta_ex_q1")),
            _fmt(grid_metrics.get("coverage_ex_q1")),
            _fmt(grid_metrics.get("flip_rate")),
            _fmt(grid_metrics.get("state_stability")),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        f"- 前向收益：{payload['forward_returns'].get('note', '—')}",
        f"- 说明：{payload['note']}",
        "",
    ]
    return "\n".join(lines) + "\n"


def _write_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    return _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# 数据源加载（与 f_marginal_shadow 同一套既有 I/O 口径）
# ─────────────────────────────────────────────────────────────────────────────


def _load_state_frames(paths: list[str] | None, state_dir: str) -> tuple[list[dict[str, Any]], int, list[str]]:
    state_sources = list(paths) if paths else [state_dir]
    frames, skipped = load_frames(state_sources)
    return frames, skipped, [str(source) for source in state_sources]


def _load_task_record_frames(task_records_dir: str | None) -> tuple[list[dict[str, Any]], int, str]:
    from alphabee.task_records.shadow import load_task_record_frames as _adapt
    from alphabee.task_records.store import TaskStore

    store = TaskStore(base_dir=task_records_dir)
    total = store.count()
    records = store.load_all(limit=total) if total else []
    frames, skipped = _adapt(records)
    return frames, skipped, f"task_records:{store.base_dir}"


def collect_frames(args: argparse.Namespace) -> tuple[list[dict[str, Any]], int, int, list[str]]:
    """按 ``--source`` 汇总帧；``--source both`` 先 state 后 task-records 并按 symbol-day 去重。"""
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="F 口径校准报告（P2-3；只报告，不改常量，不联网）")
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
        help="显式指定 state 帧文件/目录（给出时替代 --state-dir）",
    )
    parser.add_argument(
        "--task-records-dir",
        default=None,
        help="task_records 根目录（缺省 TaskStore 默认目录；仅 --source task-records/both 使用）",
    )
    parser.add_argument("--no-dedupe", action="store_true", help="关闭 (symbol, as_of_date) 去重（仅排查用）")
    parser.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help=f"Markdown 输出路径（默认 {_DEFAULT_OUT}）",
    )
    parser.add_argument(
        "--json-out",
        default=_DEFAULT_JSON_OUT,
        help=f"JSON 输出路径（默认 {_DEFAULT_JSON_OUT}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.source == _SOURCE_TASK_RECORDS and args.paths:
        parser.error("--paths 属于 state 源；--source task-records 时请改用 --task-records-dir")

    frames, skipped, duplicates, _sources = collect_frames(args)
    payload = build_payload(frames, source=args.source, skipped=skipped, duplicates=duplicates)
    _write_text(args.out, render_markdown(payload))
    _write_json(args.json_out, payload)
    if payload["verdict"] == INSUFFICIENT_VERDICT:
        print(INSUFFICIENT_VERDICT)
    else:
        print(f"校准报告已生成：{args.out}（{len(payload['grid'])} 组网格，仅报告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
