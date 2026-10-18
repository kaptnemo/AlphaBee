"""离线标定 tracking 阈值（P0 设计 §7.1）：只读回放 ``data/tracking/alerts/*.jsonl``。

统计各触发 kind 频次、PRICE_MOVE 的 |price_change_pct| 分布、tv_distance 分布，
供人工评审后决定是否登记新默认值。**只读、不改阈值、不写库**；空数据目录也能运行。
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

DEFAULT_ALERT_DIR = Path("data") / "tracking" / "alerts"


def _load_frames(alert_dir: Path) -> list[dict]:
    frames: list[dict] = []
    if not alert_dir.exists():
        return frames
    for path in sorted(alert_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return frames


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线标定 tracking 阈值（只读）")
    parser.add_argument("--alert-dir", default=str(DEFAULT_ALERT_DIR), help="告警 JSONL 目录")
    args = parser.parse_args(argv)

    frames = _load_frames(Path(args.alert_dir))
    print(f"alert_dir={args.alert_dir} frames={len(frames)}")
    if not frames:
        print("无告警帧可标定（data/tracking/alerts/*.jsonl 为空/缺失）。标定结论需人工评审后再登记新默认值。")
        return 0

    kind_counts: dict[str, int] = {}
    price_changes: list[float] = []
    tv_values: list[float] = []
    for frame in frames:
        for trigger in frame.get("triggers", []) or []:
            kind = str(trigger.get("kind") or "unknown")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            payload = trigger.get("payload") or {}
            if kind == "price_move":
                pc = _num(payload.get("price_change_pct"))
                if pc is not None:
                    price_changes.append(abs(pc))
        tv = _num(frame.get("tv_distance"))
        if tv is not None:
            tv_values.append(tv)

    print(f"trigger_kind_counts={kind_counts}")
    if price_changes:
        print(
            f"price_move_abs_change: n={len(price_changes)} min={min(price_changes):.3f} "
            f"median={statistics.median(price_changes):.3f} max={max(price_changes):.3f}"
        )
    else:
        print("price_move_abs_change: n=0")
    if tv_values:
        print(
            f"tv_distance: n={len(tv_values)} min={min(tv_values):.4f} "
            f"median={statistics.median(tv_values):.4f} max={max(tv_values):.4f}"
        )
    else:
        print("tv_distance: n=0")
    print("口径说明：只读统计、不自动改阈值；新默认值须人工评审后登记 ROADMAP。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
