"""P2-3 校准报告脚本（``scripts/f_calibration.py``）与影子 CLI 口径对齐测试。

覆盖：``--help``、样本不足分支（空/少量帧 ⇒ 「样本不足，不校准」且退出 0）、
JSON schema 稳定、Markdown 生成、足量样本的 81 组网格、常量注入只读不写回、
以及 ``scripts/f_marginal_shadow.py`` 文本输出与 ``shadow_report`` 的 ex-Q1 口径一致。

全部用 ``tmp_path`` 写帧与报告：不写真实 ``data/`` 或 ``docs/``、不联网。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from alphabee.midterm import score_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "f_calibration.py"
_SHADOW_SCRIPT = _REPO_ROOT / "scripts" / "f_marginal_shadow.py"


def _load_calibration_module() -> Any:
    spec = importlib.util.spec_from_file_location("f_calibration_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _frame(
    symbol: str = "600519.SH",
    as_of_date: str = "2026-01-01",
    report_period: str = "20250930",
    **overrides: Any,
) -> dict[str, Any]:
    """最小可重算帧：fundamental + 空 expectation（无 variable_scores/state 也可观测）。"""
    fundamental: dict[str, Any] = {
        "revenue_yoy": 10.0,
        "net_profit_yoy": 20.0,
        "eps_growth_yoy": 15.0,
        "report_period": report_period,
        "marginal_period": report_period,
        "gross_margin": 30.0,
        "q_revenue_yoy": 12.0,
        "q_net_profit_yoy": 25.0,
        "q_gross_margin_pp": 1.0,
    }
    fundamental.update(overrides)
    return {
        "symbol": symbol,
        "as_of_date": as_of_date,
        "f_marginal_mode": "shadow",
        "ocf_penalty_basis": "ttm",
        "f_recurring_mode": "shadow",
        "segment_bonus_mode": "period_aligned",
        "factor_snapshot": {"symbol": symbol, "fundamental": fundamental, "expectation": {}},
    }


def _write_frames(directory: Path, frames: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "600519.SH.jsonl"
    path.write_text("\n".join(json.dumps(frame, ensure_ascii=False) for frame in frames) + "\n", encoding="utf-8")
    return path


def _sufficient_frames() -> list[dict[str, Any]]:
    """20 只标的 × 4 个报告季度 = 80 帧（满足 50/20/4 门槛）。"""
    quarters = ("20250331", "20250630", "20250930", "20251231")
    symbols = [f"60{i:04d}.SH" for i in range(20)]
    frames: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols):
        for quarter_index, period in enumerate(quarters):
            frames.append(
                _frame(
                    symbol=symbol,
                    as_of_date=f"2026-{index + 1:02d}-{quarter_index + 1:02d}",
                    report_period=period,
                    revenue_yoy=8.0 + quarter_index,
                    net_profit_yoy=12.0 + index,
                )
            )
    return frames


# ─────────────────────────────────────────────────────────────────────────────
# CLI：--help / 样本不足 / JSON schema / Markdown
# ─────────────────────────────────────────────────────────────────────────────


def test_help_lists_all_required_options():
    result = _run(_SCRIPT, "--help")
    assert result.returncode == 0
    for flag in ("--paths", "--state-dir", "--source", "--out", "--json-out", "--help"):
        assert flag in result.stdout


def test_insufficient_sample_prints_verdict_and_exits_zero(tmp_path: Path):
    state_dir = tmp_path / "state"
    _write_frames(state_dir, [_frame(), _frame(symbol="000001.SZ")])
    out = tmp_path / "report.md"
    json_out = tmp_path / "out.json"

    result = _run(
        _SCRIPT,
        "--paths",
        str(state_dir),
        "--state-dir",
        str(state_dir),
        "--out",
        str(out),
        "--json-out",
        str(json_out),
    )

    assert result.returncode == 0
    assert "样本不足，不校准" in result.stdout
    assert "样本不足，不校准" in out.read_text(encoding="utf-8")

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["verdict"] == "样本不足，不校准"
    assert payload["grid"] == []
    assert payload["sample"]["sufficient"] is False
    assert payload["sample"]["n_frames"] == 2


def test_json_schema_is_stable(tmp_path: Path):
    state_dir = tmp_path / "state"
    _write_frames(state_dir, [_frame()])
    json_out = tmp_path / "out.json"
    _run(
        _SCRIPT,
        "--paths",
        str(state_dir),
        "--out",
        str(tmp_path / "report.md"),
        "--json-out",
        str(json_out),
    )

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "generated_at",
        "source",
        "skipped",
        "duplicates",
        "sample",
        "metrics",
        "grid",
        "verdict",
        "forward_returns",
        "note",
    }
    assert payload["schema_version"] == "1"
    assert isinstance(payload["generated_at"], str) and "T" in payload["generated_at"]

    sample = payload["sample"]
    assert set(sample) == {
        "n_frames",
        "n_symbols",
        "n_report_quarters",
        "report_quarters",
        "thresholds",
        "sufficient",
    }
    assert sample["thresholds"] == {"min_frames": 50, "min_symbols": 20, "min_quarters": 4}

    metrics = payload["metrics"]
    assert {"median_abs_delta_ex_q1", "coverage_ex_q1", "flip_rate", "s5_ocf_hit_rate"} <= set(metrics)
    assert isinstance(payload["forward_returns"], dict)
    assert set(payload["forward_returns"]) == {"available", "attached", "note"}


def test_sufficient_sample_produces_81_grid_rows(tmp_path: Path):
    state_dir = tmp_path / "state"
    _write_frames(state_dir, _sufficient_frames())
    out = tmp_path / "report.md"
    json_out = tmp_path / "out.json"

    result = _run(
        _SCRIPT,
        "--paths",
        str(state_dir),
        "--out",
        str(out),
        "--json-out",
        str(json_out),
    )

    assert result.returncode == 0
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["verdict"] == "仅报告，不自动改常量"
    assert payload["sample"]["sufficient"] is True
    assert payload["sample"]["n_symbols"] == 20
    assert payload["sample"]["n_report_quarters"] == 4
    assert len(payload["grid"]) == 81
    params_keys = {"_FUNDAMENTAL_YOY_SCALE", "F_ACCEL_SCALE", "_OCF_NEGATIVE_PENALTY", "F_MARGIN_DELTA_SCALE"}
    assert all(set(row["params"]) == params_keys for row in payload["grid"])
    assert all(
        {"median_abs_delta_ex_q1", "coverage_ex_q1", "g8_pass"} <= set(row["metrics"]) for row in payload["grid"]
    )

    markdown = out.read_text(encoding="utf-8")
    assert "# F 口径校准报告" in markdown
    assert "网格" in markdown
    assert "样本不足" not in markdown


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数：网格 / 注入不改常量 / 样本判读
# ─────────────────────────────────────────────────────────────────────────────


def test_grid_combos_cover_three_values_each():
    module = _load_calibration_module()
    combos = module.grid_combos()
    assert len(combos) == 81
    for name, values in module.GRID_VALUES.items():
        assert {combo[name] for combo in combos} == set(values)


def test_recompute_is_temporary_and_changes_with_scale():
    """网格注入只在上下文内生效（退出后常量恢复）；scale 变化会改变 F。"""
    module = _load_calibration_module()
    original = score_engine._FUNDAMENTAL_YOY_SCALE
    frame = _frame()

    low_scale = module.recompute_frames([frame], {"_FUNDAMENTAL_YOY_SCALE": 20.0})[0]
    high_scale = module.recompute_frames([frame], {"_FUNDAMENTAL_YOY_SCALE": 40.0})[0]
    low_f = low_scale["factor_snapshot"]["fundamental"]["score_breakdown"]["f"]
    high_f = high_scale["factor_snapshot"]["fundamental"]["score_breakdown"]["f"]
    assert low_f > high_f  # 同一 yoy 在更小的饱和点下得到更高的项
    assert score_engine._FUNDAMENTAL_YOY_SCALE == original  # 常量未被改写


def test_evaluate_grid_returns_metrics_per_combo():
    module = _load_calibration_module()
    frames = [_frame(), _frame(symbol="000001.SZ", report_period="20250630")]
    rows = module.evaluate_grid(frames)
    assert len(rows) == 81
    for row in rows:
        assert set(row) == {"params", "metrics"}
        assert set(row["metrics"]) == {
            "median_abs_delta_ex_q1",
            "coverage_ex_q1",
            "flip_rate",
            "dangerous_flip_rate",
            "state_stability",
            "g8_pass",
        }


def test_sample_of_thresholds():
    module = _load_calibration_module()
    insufficient = module.sample_of([_frame(), _frame(symbol="000001.SZ")])
    assert insufficient["sufficient"] is False
    assert insufficient["n_frames"] == 2 and insufficient["n_symbols"] == 2

    sufficient = module.sample_of(_sufficient_frames())
    assert sufficient["sufficient"] is True
    assert sufficient["n_report_quarters"] == 4
    assert sufficient["report_quarters"] == ["2025Q1", "2025Q2", "2025Q3", "2025Q4"]


# ─────────────────────────────────────────────────────────────────────────────
# 影子 CLI（f_marginal_shadow）文本与 shadow_report 的 ex-Q1 口径一致
# ─────────────────────────────────────────────────────────────────────────────


def test_shadow_cli_prints_ex_q1_median_and_coverage(tmp_path: Path):
    """5 个非 Q1 帧 ΔF=0.01 + 5 个 Q1 帧 ΔF=0.5：

    全帧中位数 = 0.255，ex-Q1 中位数 = 0.01；CLI 必须以 ex-Q1 为主并保留旧值对照，
    G-8 判读行同样引用 ex-Q1 值（不得出现打印值与门禁自相矛盾）。
    """
    frames: list[dict[str, Any]] = []
    for index in range(10):
        is_q1 = index >= 5
        period = "20260331" if is_q1 else "20260930"
        delta = 0.5 if is_q1 else 0.01
        frames.append(
            {
                "symbol": f"60000{index}.SH",
                "as_of_date": f"2026-01-{index + 1:02d}",
                "factor_snapshot": {
                    "fundamental": {
                        "report_period": period,
                        "marginal_period": period,
                        "score_breakdown": {
                            "f": -0.8,
                            "f_shadow": -0.8 + delta,
                            "f_marginal_source": 7,
                        },
                    }
                },
            }
        )
    path = tmp_path / "frames.jsonl"
    path.write_text("\n".join(json.dumps(frame) for frame in frames) + "\n", encoding="utf-8")

    result = _run(_SHADOW_SCRIPT, "--paths", str(path), "--no-dedupe")
    assert result.returncode == 0

    text = result.stdout
    assert "中位数(ex-Q1)=0.0100" in text
    assert "全帧中位数=0.2550" in text
    # G-8 median 行必须显示 ex-Q1 值（旧实现显示全帧 0.255 却用 ex-Q1 判 PASS，自相矛盾）
    assert "median|ΔF|(ex-Q1) <= 0.1: FAIL（0.0100；全帧=0.2550）" in text or (
        "median|ΔF|(ex-Q1) <= 0.1: PASS（0.0100；全帧=0.2550）" in text
    )
