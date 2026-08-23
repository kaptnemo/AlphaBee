"""task records 相关 CLI（从根 main.py 拆出）。"""

from __future__ import annotations

import argparse

from alphabee.apps.cli.colors import Color, color, hr


def handle_task_cli(args: argparse.Namespace) -> None:
    """处理 task records 相关的 CLI 命令。"""
    from alphabee.task_records import TaskAnalyzer, TaskStore, distill

    store = TaskStore()
    count = store.count()

    if count == 0:
        print(color("  ℹ 暂无运行记录。请先执行分析任务。", Color.DIM))
        return

    print()
    print(hr("═", 70, Color.CYAN))
    print(color("  📊 AlphaBee Task Records", Color.BOLD, Color.CYAN))
    print(color(f"  共 {count} 条记录", Color.DIM))
    print(hr("═", 70, Color.CYAN))
    print()

    if args.task_stats:
        analyzer = TaskAnalyzer(store)
        summary = analyzer.summary()

        print(color("  📈 执行概况", Color.BOLD, Color.WHITE))
        print(f"  总运行: {summary['run_count']} 次, 平均耗时: {summary['avg_duration_s']}s")
        print()

        print(color("  🚨 最高频问题类别", Color.BOLD, Color.WHITE))
        for cat, cnt in summary["top_issues"][:8]:
            print(f"  {cat:30s} {cnt:4d}")

        print()
        print(color("  🏷 最高频问题模式", Color.BOLD, Color.WHITE))
        for kw, cnt in summary["top_message_clusters"][:8]:
            print(f"  {kw:30s} {cnt:4d}")

        print()
        print(color("  📡 信号触发率", Color.BOLD, Color.WHITE))
        print(f"  {'信号':30s} {'触发率':>6s}  {'High%':>6s}  {'Med%':>6s}  {'Low%':>6s}  {'Block%':>6s}")
        for sid, stats in summary["signal_trigger_rates"].items():
            print(
                f"  {sid:30s} {stats['triggered_pct']:5.0f}%  "
                f"{stats['high_pct']:5.0f}%  {stats['medium_pct']:5.0f}%  "
                f"{stats['low_pct']:5.0f}%  {stats['blocked_pct']:5.0f}%"
            )

        print()
        print(color("  🎯 Flag 影响 (overall_confidence) ", Color.BOLD, Color.WHITE))
        fi = summary["flag_impact"]
        for group, data in fi.items():
            if data.get("count", 0) > 0:
                print(
                    f"  {group:20s}: H={data['high_pct']:5.1f}% M={data['medium_pct']:5.1f}% L={data['low_pct']:5.1f}% ({data['count']}次)"
                )

        print()
        print(color("  ⚠ 单证据维度", Color.BOLD, Color.WHITE))
        for dim, cnt in summary["single_evidence_dims"][:5]:
            print(f"  {dim:30s} {cnt:4d}")
        if not summary["single_evidence_dims"]:
            print("  (无)")

        print()
        print(color("  🏭 语境适配缺口行业", Color.BOLD, Color.WHITE))
        for ind, cnt in summary["context_gap_industries"][:5]:
            print(f"  {ind:30s} {cnt:4d}")
        if not summary["context_gap_industries"]:
            print("  (无)")

    if args.distill:
        print(color("  🔬 正在生成蒸馏分析报告...", Color.BOLD, Color.YELLOW))
        print()
        try:
            report = distill()
            print(report)
        except Exception as exc:
            print(color(f"  ❌ 蒸馏失败: {exc}", Color.RED))

    if args.task_history:
        target = args.task_history.strip()
        records = [r for r in store.load_all() if r.symbol == target]
        if not records:
            print(color(f"  ℹ 未找到标的 {target} 的历史记录", Color.DIM))
            return
        print(color(f"  📋 {target} 历史记录 ({len(records)} 条)", Color.BOLD, Color.WHITE))
        print()
        print(f"  {'时间':22s} {'置信度':8s} {'审查':14s} {'异常':4s} {'问题':4s} {'耗时'}")
        print(f"  {'-' * 22} {'-' * 8} {'-' * 14} {'-' * 4} {'-' * 4} {'-' * 6}")
        for r in records:
            print(
                f"  {r.timestamp[:19]:22s} {r.overall_confidence:8s} "
                f"{r.review_overall_status:14s} {r.anomaly_triggered_count:4d} "
                f"{len(r.issues):4d} {r.total_duration_s:5.0f}s"
            )

    if args.task_record:
        tid = args.task_record.strip()
        record = store.load(tid)
        if record is None:
            print(color(f"  ❌ 未找到记录: {tid}", Color.RED))
            return
        import json as _json

        print(_json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2))

    print()
    print(hr("═", 70, Color.CYAN))
