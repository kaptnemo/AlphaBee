#!/usr/bin/env python3
"""离线报告 A/B：同一 ``ReportGenerationPayload`` 分别生成 legacy / memo_lite 报告（设计 §3.2.6）。

流程：

1. 读入 payload JSON（``generate_report`` 在 ``report.capture_payload=true`` 时落盘的输入）；
2. 对每个 mode 调用 :func:`alphabee.orchestrator.reporter._generate_report_value`
   （同一 payload、同一模型参数，各跑一次）；失败时回退
   :func:`~alphabee.orchestrator.reporter.build_deterministic_report` 并在 metrics 标注；
3. 写 ``<mode>.json``（ReportOutput）、``<mode>.md``（复用 ``task_report.render_report_dict``）
   与 ``metrics.json``（主文/附录字符、数字 token、章节数，按 ``report_layout.report_shape`` 口径）。

路径纪律：``--out`` 必须在工作区内（否则报错退出），脚本只写 ``--out`` 下的文件。
不联网；模型调用由调用方（测试）提供假实现。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from alphabee import PROJECT_ROOT
from alphabee.apps.cli.report_layout import report_shape
from alphabee.apps.cli.task_report import render_report_dict
from alphabee.orchestrator import reporter
from alphabee.orchestrator.contracts import ReportGenerationPayload

#: 支持的报告模式（Q1-lite：legacy = 现状 13 节；memo_lite = 投资备忘录）。
SUPPORTED_MODES: tuple[str, ...] = ("legacy", "memo_lite")
DEFAULT_MODES = ",".join(SUPPORTED_MODES)
#: 数字 token 口径（与 ``scripts/report_baseline.py`` 一致，仅用于 A/B 指标）。
NUMERIC_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?%?")

#: 路径守卫基线（测试可 monkeypatch 到 tmp 目录）；默认 = 仓库根。
WORKSPACE_ROOT = PROJECT_ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线报告 A/B：同一 payload 分别生成 legacy / memo_lite 报告并输出指标",
        epilog="示例：python scripts/report_ab.py --payload data/report_payloads/<run>.json "
        "--modes legacy,memo_lite --out tmp/ab/<symbol>/",
    )
    parser.add_argument("--payload", required=True, metavar="JSON", help="ReportGenerationPayload JSON 路径")
    parser.add_argument(
        "--modes",
        default=DEFAULT_MODES,
        metavar="MODES",
        help=f"逗号分隔的模式列表（默认 {DEFAULT_MODES}）",
    )
    parser.add_argument("--out", required=True, metavar="DIR", help="输出目录（必须在工作区内）")
    return parser


def _parse_modes(raw: str) -> list[str]:
    modes = [mode.strip() for mode in (raw or "").split(",") if mode.strip()]
    if not modes:
        raise ValueError("--modes 不能为空")
    unknown = [mode for mode in modes if mode not in SUPPORTED_MODES]
    if unknown:
        raise ValueError(f"不支持的 mode: {', '.join(unknown)}（可选 {', '.join(SUPPORTED_MODES)}）")
    return modes


def _resolve_out_dir(out: str) -> Path:
    """``--out`` 必须在工作区内；返回绝对路径（拒绝工作区外路径）。"""
    resolved = Path(out).expanduser().resolve()
    root = Path(WORKSPACE_ROOT).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"--out 必须位于工作区内（{root}）：{resolved}")
    return resolved


def _load_payload(path: Path) -> ReportGenerationPayload:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload JSON 顶层必须是对象")
    return ReportGenerationPayload.model_validate(raw)


def _generate_mode(payload: ReportGenerationPayload, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """生成单个模式的报告；失败回退确定性报告并在 meta 标注。"""
    try:
        return reporter._generate_report_value(payload, mode), {"degraded": False, "error": ""}
    except Exception as exc:  # noqa: BLE001 - A/B 必须产出可比对产物，失败降级并标注
        report = reporter.build_deterministic_report(
            payload,
            failure_reason=f"{type(exc).__name__}: {exc}",
            mode=mode,
        )
        return report, {"degraded": True, "error": f"{type(exc).__name__}: {exc}"}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(payload: ReportGenerationPayload, modes: list[str], out_dir: Path) -> dict[str, Any]:
    """执行 A/B 并把产物写入 ``out_dir``，返回 metrics（纯 IO 编排，便于测试）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, Any] = {"payload": {"symbol": payload.company.symbol, "query": payload.company.query}}
    for mode in modes:
        report, meta = _generate_mode(payload, mode)
        layout = "legacy" if mode == "legacy" else "memo"
        _write_json(out_dir / f"{mode}.json", report)
        markdown = render_report_dict(report)
        (out_dir / f"{mode}.md").write_text(markdown, encoding="utf-8")
        shape = report_shape(markdown, layout)
        metrics[mode] = {
            **shape,
            "numeric_tokens": len(NUMERIC_TOKEN_RE.findall(markdown)),
            "degraded": meta["degraded"],
            "error": meta["error"],
        }
    _write_json(out_dir / "metrics.json", metrics)
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        modes = _parse_modes(args.modes)
        out_dir = _resolve_out_dir(args.out)
        payload = _load_payload(Path(args.payload))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    metrics = run(payload, modes, out_dir)
    summary = "，".join(
        f"{mode}: main={metrics[mode]['main_chars']} appendix={metrics[mode]['appendix_chars']}"
        + ("（降级）" if metrics[mode]["degraded"] else "")
        for mode in modes
    )
    print(f"报告 A/B 已生成：{out_dir}（{summary}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
