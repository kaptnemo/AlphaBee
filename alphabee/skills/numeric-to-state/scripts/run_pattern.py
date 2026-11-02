#!/usr/bin/env python
"""按输入方向分计算状态分布（配合 numeric-to-state Skill 使用）。

加载一个暴露模块级 ``SPEC`` 的 Python 文件，构造 :class:`alphabee.pattern.Engine`，
对给定 ``scores`` 做分类并输出 JSON（分布 / argmax / P_top / 熵 / 迁移等）。

用法::

    python run_pattern.py --spec ./my_spec.py --scores '{"momentum": 0.8, "volatility": -0.3}'
    python run_pattern.py --spec ./my_spec.py --scores-file scores.json
    python run_pattern.py --spec ./my_spec.py --scores-file now.json \
        --previous-scores-file prev.json     # 额外输出 transition / drift

注意：``distribution`` 是归一化模式证据的 softmax 权重，**不是频率概率或后验概率**。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from alphabee.pattern import Engine, PatternSpec, validate_spec
from alphabee.pattern.types import PatternResult

_WEIGHT_DISCLAIMER = "distribution 是归一化模式证据的 softmax 权重，不是频率概率或后验概率。"


def _load_spec(spec_path: Path) -> PatternSpec:
    """从 ``spec_path`` 导入并返回模块级 ``SPEC``。"""
    if not spec_path.is_file():
        raise SystemExit(f"spec 文件不存在：{spec_path}")

    module_name = f"_numeric_to_state_spec_{spec_path.stem}"
    loader_spec = importlib.util.spec_from_file_location(module_name, spec_path)
    if loader_spec is None or loader_spec.loader is None:
        raise SystemExit(f"无法加载 spec 文件：{spec_path}")

    # 允许 spec 模块 import 同目录下的辅助模块。
    parent = str(spec_path.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    module = importlib.util.module_from_spec(loader_spec)
    loader_spec.loader.exec_module(module)

    spec = getattr(module, "SPEC", None)
    if not isinstance(spec, PatternSpec):
        raise SystemExit(f"{spec_path} 必须暴露模块级 SPEC（PatternSpec），实际：{type(spec)!r}")
    return spec


def _parse_scores(text: str, *, source: str) -> dict[str, float | None]:
    """把 JSON 字符串解析为 ``{axis: 方向分或 None}``。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{source} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{source} 必须是 JSON 对象（axis → 数值/None），实际：{type(payload).__name__}")

    scores: dict[str, float | None] = {}
    for key, value in payload.items():
        if value is None:
            scores[str(key)] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SystemExit(f"{source} 中 {key!r} 必须是数值或 null，实际：{value!r}")
        scores[str(key)] = float(value)
    return scores


def _transition_payload(result: PatternResult) -> dict[str, Any] | None:
    transition = result.transition
    if transition is None:
        return None
    return {
        "from_state": transition.from_state,
        "to_state": transition.to_state,
        "legal": transition.legal,
        "kind": transition.kind,
        "trigger_factors": list(transition.trigger_factors),
    }


def _result_payload(result: PatternResult) -> dict[str, Any]:
    belief = result.belief
    return {
        "argmax_state": belief.argmax_state,
        "distribution": dict(belief.distribution),
        "p_top": result.p_top,
        "uncertain": result.uncertain,
        "entropy": belief.entropy,
        "entropy_normalized": result.entropy_normalized,
        "raw_evidence": dict(result.raw_evidence),
        "normalized_evidence": dict(result.normalized_evidence),
        "drift": dict(belief.drift) if belief.drift is not None else None,
        "transition": _transition_payload(result),
    }


def _warnings(result: PatternResult) -> list[str]:
    warnings = [_WEIGHT_DISCLAIMER]
    if result.uncertain:
        warnings.append("P_top 低于 uncertain_p_top：本次结果标记为 uncertain，不要强行下结论。")
    transition = result.transition
    if transition is not None and not transition.legal:
        warnings.append(
            f"迁移 {transition.from_state!r} -> {transition.to_state!r} 不在迁移图中（legal=false, kind=illegal）。"
        )
    return warnings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按输入方向分计算 alphabee.pattern 状态分布")
    parser.add_argument("--spec", required=True, type=Path, help="暴露模块级 SPEC（PatternSpec）的 Python 文件")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scores", help='当前帧 JSON，如 \'{"momentum": 0.8, "volatility": -0.3}\'')
    group.add_argument("--scores-file", type=Path, help="当前帧 JSON 文件路径")
    parser.add_argument("--previous-scores", help="上一帧 JSON（可选，用于输出 transition / drift）")
    parser.add_argument("--previous-scores-file", type=Path, help="上一帧 JSON 文件路径（可选）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    scores_text = args.scores if args.scores is not None else args.scores_file.read_text(encoding="utf-8")
    scores = _parse_scores(scores_text, source="scores")

    previous: dict[str, float | None] | None = None
    if args.previous_scores is not None:
        previous = _parse_scores(args.previous_scores, source="previous-scores")
    elif args.previous_scores_file is not None:
        previous = _parse_scores(
            args.previous_scores_file.read_text(encoding="utf-8"),
            source="previous-scores-file",
        )

    spec = _load_spec(args.spec)
    report = validate_spec(spec)
    if not report.ok:
        issues = [f"[{issue.code}] {issue.message}" for issue in report.issues]
        raise SystemExit("非法 PatternSpec：\n" + "\n".join(issues))

    engine = Engine(spec)
    previous_result = engine.classify(previous) if previous is not None else None
    result = engine.classify(scores, previous=previous_result)

    payload = {
        "scores": scores,
        "previous_scores": previous,
        **_result_payload(result),
        "warnings": _warnings(result),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
