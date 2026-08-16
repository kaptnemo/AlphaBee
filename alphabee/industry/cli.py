"""行业研究工作流 CLI（industry-context Phase 1）。

用法::

    # 按标的解析行业并生成知识快照（推荐）
    python -m alphabee.industry.cli --symbol 600519.SH

    # 直接指定行业
    python -m alphabee.industry.cli --standard sw_l1 --code 801120.SI --name 白酒

    # 可选开启 LLM 定性合成
    python -m alphabee.industry.cli --symbol 600519.SH --qualitative llm

    # 存储管理
    python -m alphabee.industry.cli --list
    python -m alphabee.industry.cli --show --standard sw_l1 --code 801120.SI
"""

from __future__ import annotations

import argparse
import json
import sys

from alphabee.industry.contracts import IndustryTarget
from alphabee.industry.persistence import IndustryProfileStore


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m alphabee.industry.cli",
        description="AlphaBee 行业知识研究工作流（离线/准离线）",
    )
    parser.add_argument("--symbol", default=None, help="股票代码，如 600519 或 600519.SH")
    parser.add_argument("--standard", default=None, help="分类体系：sw_l1 / sw_l2 / ths / custom")
    parser.add_argument("--code", default=None, help="行业代码（匹配键，sw_l1 → 801120.SI）")
    parser.add_argument("--name", default="", help="行业展示名（如 白酒）")
    parser.add_argument(
        "--qualitative",
        default="none",
        choices=("none", "llm"),
        help="定性合成模式（v1 默认 none，保持轻量）",
    )
    parser.add_argument("--peer-limit", type=int, default=20, help="成分股抽样上限")
    parser.add_argument("--as-of-date", default=None, help="数据截止日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--data-dir", default=None, help="知识存储根目录（默认 data/industry_profiles）")
    parser.add_argument("--list", action="store_true", help="列出全部行业知识快照")
    parser.add_argument("--show", action="store_true", help="展示指定行业快照（需 --standard/--code）")
    parser.add_argument(
        "--crosscheck",
        action="store_true",
        help="多来源行业交叉校验（申万/同花顺/东方财富；配合 --symbol 或 --name）",
    )
    return parser.parse_args(argv)


def _store(args: argparse.Namespace) -> IndustryProfileStore:
    return IndustryProfileStore(root=args.data_dir)


def _target(args: argparse.Namespace) -> IndustryTarget:
    if args.symbol:
        return IndustryTarget(symbol=args.symbol)
    if args.standard and args.code:
        return IndustryTarget(
            classification_standard=args.standard,
            industry_code=args.code,
            industry_name=args.name,
        )
    raise SystemExit("需要 --symbol，或 --standard + --code（可加 --name）")


def _print_run_result(state) -> None:
    from alphabee.industry.contracts import IndustryContextArtifact

    print("=" * 64)
    print("行业知识快照生成")
    print("=" * 64)
    if state.review.status == "rejected":
        print("  ✗ 行业身份不可得，未产出快照（review=rejected）")
        for note in state.review.notes:
            print(f"    - {note}")
        return
    artifact: IndustryContextArtifact = state.artifact
    print(
        f"  行业            : {artifact.industry or '-'}（{artifact.classification_standard}:{artifact.industry_code}）"
    )
    print(f"  数据截止        : {artifact.as_of_date}")
    print(f"  成分股数        : {artifact.peer_count or 0}")
    print(f"  审核状态        : {artifact.review_status}（confidence={artifact.confidence}）")
    print(f"  过期日          : {artifact.stale_after or '-'}")
    print(
        f"  降级            : {'是' if artifact.degraded else '否'}{' — ' + artifact.degraded_reason if artifact.degraded else ''}"
    )
    benchmarks = artifact.all_benchmarks()
    if benchmarks:
        print("  数值基准        :")
        for key, value in sorted(benchmarks.items()):
            if value is not None:
                print(f"    {key:36s} {value}")
    print(f"  快照文件        : {state.persist_path}")
    for note in artifact.review_notes:
        print(f"  ⚠ {note}")
    print("=" * 64)


def _print_list(store: IndustryProfileStore) -> None:
    profiles = store.list_profiles()
    if not profiles:
        print("  ℹ 暂无行业知识快照。先运行：python -m alphabee.industry.cli --symbol 600519.SH")
        return
    print(f"{'标准':<8} {'行业代码':<14} {'行业':<12} {'数据截止':<12} {'审核':<12} {'过期':<6} 降级")
    print("-" * 76)
    for info in profiles:
        print(
            f"{info.classification_standard:<8} {info.industry_code:<14} "
            f"{info.industry[:12]:<12} {info.as_of_date:<12} "
            f"{(info.review_status or '-'):<12} {'是' if info.stale else '否':<6} "
            f"{'是' if info.degraded else '否'}"
        )


def _print_show(args: argparse.Namespace, store: IndustryProfileStore) -> None:
    if not (args.standard and args.code):
        raise SystemExit("--show 需要 --standard + --code")
    artifact = store.load(args.standard, args.code)
    if artifact is None:
        print(f"  ℹ 未找到 {args.standard}:{args.code} 的快照")
        return
    payload = artifact.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_industry_name(args: argparse.Namespace) -> str:
    """解析交叉校验用的行业名：--name 直接给定；--symbol 经 get_industry_fact。"""
    if args.name:
        return args.name
    if args.symbol:
        from alphabee.agents.facts.tools.industry_fact import get_industry_fact

        ind_fact = get_industry_fact(args.symbol) or {}
        industry = str(ind_fact.get("industry") or "").strip()
        if industry:
            return industry
        raise SystemExit(f"--symbol {args.symbol} 无法解析行业名")
    raise SystemExit("--crosscheck 需要 --name 或 --symbol")


def _print_crosscheck(industry: str) -> None:
    from alphabee.industry.crosscheck import fetch_industry_crosscheck

    print(f"  🔀 多来源行业交叉校验: {industry}")
    result = fetch_industry_crosscheck(industry)
    print(f"     命中来源: {result.sources_hit}/3")
    for match in result.matches:
        detail = f"{match.source}: {match.industry}"
        if match.code:
            detail += f" ({match.code}{f' {match.level}' if match.level else ''})"
        if match.valuation:
            vals = "  ".join(f"{key}={value:.2f}" for key, value in match.valuation.items() if value is not None)
            if vals:
                detail += f" | {vals}"
        print(f"       {detail}")
    for warning in result.warnings:
        print(f"       ⚠ {warning}")
    print(f"      canonical: {result.canonical_name or '—'}")
    facts = result.as_facts()
    print(
        "      facts: "
        + "  ".join(
            f"{key}={value}" for key, value in facts.items() if key not in ("query", "warnings") and value is not None
        )
    )
    print()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = _store(args)

    if args.list:
        _print_list(store)
        return 0
    if args.show:
        _print_show(args, store)
        return 0
    if args.crosscheck:
        _print_crosscheck(_resolve_industry_name(args))
        return 0

    from alphabee.industry import IndustryContextWorkflow

    target = _target(args)
    result = IndustryContextWorkflow(store=store).run(
        target,
        qualitative_mode=args.qualitative,
        as_of_date=args.as_of_date,
        peer_limit=args.peer_limit,
    )
    _print_run_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
