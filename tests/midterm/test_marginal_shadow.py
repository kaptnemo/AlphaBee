"""F 边际改善影子期观测的单元测试（设计 §5.2 / §7.1 G-8 / §9 PR-3）。

全部合成帧 + 临时目录文件，不读真实 ``data/`` 语料、不联网。断言均为**具体数值/布尔**，
覆盖：有/无影子键的帧、危险翻转、两种落盘格式、坏行/坏文件计数、覆盖率分母、
以及 G-8 判读的通过/不通过两态。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from alphabee.midterm.classifier_dispatch import classify_state
from alphabee.midterm.marginal_shadow import (
    G8_MAX_ABS_DELTA_F_MEDIAN,
    G8_MAX_DANGEROUS_FLIP_RATE,
    G8_MAX_FLIP_RATE,
    G8_MIN_COVERAGE,
    G8_MIN_FRAMES,
    _quantile,
    frame_stats,
    load_frames,
    shadow_report,
)
from alphabee.midterm.models import VariableScores

# 600760 口径的其余五因子方向分（与本仓 v2 用例同源），让 F 成为状态的主导变量。
_OTHER_SCORES: dict[str, float] = {
    "e_revision": -0.5169,
    "t_relative_strength": 1.0,
    "v_valuation_percentile": 0.6015,
    "c_crowding": -0.1902,
    "r_risk": -0.08741,
}


# ─────────────────────────────────────────────────────────────────────────────
# 合成帧构造
# ─────────────────────────────────────────────────────────────────────────────


def _scores(f: float, **overrides: float) -> dict[str, Any]:
    payload: dict[str, Any] = {"m": {"market_score": 60.0}, "f_fundamental_trend": f, **_OTHER_SCORES}
    payload.update(overrides)
    return payload


def _state_of(scores: dict[str, Any], *, order: str = "etf", formula: str = "v1") -> str:
    """用生产分类器算 argmax（单一来源；测试不复制状态机逻辑）。"""
    result = classify_state(VariableScores.model_validate(scores), order=order, formula=formula)
    return str(result.state.argmax_state)


def _frame(
    *,
    f: float = -0.8,
    f_shadow: float | None = None,
    source: int | None = None,
    breakdown: bool = True,
    fundamental: bool = True,
    formula: str = "v1",
    order: str = "etf",
    argmax: str | None = None,
    scores: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一帧 artifact dict（``state.argmax_state`` 缺省按帧内 scores 真算）。"""
    payload_scores = scores if scores is not None else _scores(f)
    if argmax is None:
        argmax = _state_of(payload_scores, order=order, formula=formula)
    frame: dict[str, Any] = {
        "symbol": "600760.SH",
        "as_of_date": "2026-11-01",
        "state": {"argmax_state": argmax, "distribution": {argmax: 1.0}, "entropy": 0.0},
        "variable_scores": payload_scores,
        "classifier_formula": formula,
        "classifier_order": order,
    }
    if fundamental:
        payload: dict[str, Any] = {}
        if breakdown:
            payload["score_breakdown"] = {"f": f, "f_shadow": f_shadow, "f_marginal_source": source}
        frame["factor_snapshot"] = {"fundamental": payload}
    return frame


# ─────────────────────────────────────────────────────────────────────────────
# 单帧统计：ΔF / 翻转 / 危险翻转
# ─────────────────────────────────────────────────────────────────────────────


def test_frame_stats_marginal_frame_flip():
    """有完整影子键：ΔF 数值、S1→S5 翻转（非危险：现行不是 S5）。"""
    frame = _frame(f=0.3, f_shadow=-0.3, source=7)
    assert _state_of(_scores(0.3)) == "S1"  # 帧内当前状态（真实分类器 etf/v1）
    assert _state_of(_scores(-0.3)) == "S5"  # 影子状态

    stats = frame_stats(frame)
    assert stats["has_fundamental"] is True
    assert stats["f"] == pytest.approx(0.3)
    assert stats["f_shadow"] == pytest.approx(-0.3)
    assert stats["delta_f"] == pytest.approx(-0.6)
    assert stats["f_marginal_source"] == 7
    assert stats["current_state"] == "S1"
    assert stats["shadow_state"] == "S5"
    assert stats["flipped"] is True
    assert stats["dangerous_flip"] is False  # 现行不是 S5 ⇒ 不属于危险翻转


def test_frame_stats_no_flip_negative_delta():
    frame = _frame(f=-0.8, f_shadow=-0.9, source=7)
    stats = frame_stats(frame)
    assert stats["delta_f"] == pytest.approx(-0.1)
    assert stats["current_state"] == "S5" and stats["shadow_state"] == "S5"
    assert stats["flipped"] is False
    assert stats["dangerous_flip"] is False


def test_frame_stats_dangerous_flip_s5_to_s1():
    """S5 现判被影子判到 S1 ⇒ 危险翻转（顶背离被抹掉的失败模式）。"""
    frame = _frame(f=-0.3, f_shadow=0.3, source=7)
    assert _state_of(_scores(-0.3)) == "S5"
    assert _state_of(_scores(0.3)) == "S1"
    stats = frame_stats(frame)
    assert stats["flipped"] is True
    assert stats["dangerous_flip"] is True


def test_frame_stats_old_frame_without_breakdown():
    """影子层上线前的旧帧：无 score_breakdown ⇒ 影子相关字段全 None，但仍计入帧数。"""
    frame = _frame(f=-0.8, f_shadow=None, source=None, breakdown=False)
    stats = frame_stats(frame)
    assert stats["has_fundamental"] is True
    assert stats["f"] is None
    assert stats["f_shadow"] is None
    assert stats["delta_f"] is None
    assert stats["f_marginal_source"] is None
    assert stats["current_state"] == "S5"  # 当前状态仍可观测
    assert stats["shadow_state"] is None
    assert stats["flipped"] is None
    assert stats["dangerous_flip"] is None


def test_frame_stats_frame_without_fundamental():
    frame = _frame(fundamental=False)
    frame["state"] = {"argmax_state": "S3"}
    stats = frame_stats(frame)
    assert stats["has_fundamental"] is False
    assert stats["f"] is None and stats["f_shadow"] is None
    assert stats["shadow_state"] is None and stats["flipped"] is None


def test_frame_stats_uses_recorded_formula():
    """翻转重算必须沿用帧内 classifier_formula（v1/v2 对同一帧会给出不同 argmax）。"""
    scores = _scores(-0.2)
    assert _state_of(scores, formula="v1") == "S5"
    assert _state_of(scores, formula="v2") == "S0"  # 口径差 ⇒ 判别力

    frame = _frame(f=-0.2, f_shadow=-0.2, source=7, formula="v2", argmax="S0", scores=scores)
    stats = frame_stats(frame)
    assert stats["shadow_state"] == "S0"
    assert stats["flipped"] is False  # 若误用 v1 会算成 S5 → 假翻转


def test_frame_stats_uses_recorded_order():
    """翻转重算必须沿用帧内 classifier_order（etf/tef 对同一帧会给出不同 argmax）。"""
    scores = _scores(0.3, e_revision=-0.5, t_relative_strength=0.5)
    assert _state_of(scores, order="etf") == "S1"
    assert _state_of(scores, order="tef") == "S2"  # 口径差 ⇒ 判别力

    frame = _frame(f=0.3, f_shadow=0.3, source=7, order="tef", argmax="S2", scores=scores)
    stats = frame_stats(frame)
    assert stats["shadow_state"] == "S2"
    assert stats["flipped"] is False  # 若误用 etf 会算成 S1 → 假翻转


def test_frame_stats_injected_classify_receives_mode_and_scores():
    """classify 可注入：验证拿到的是「F 换成 f_shadow 的 scores」+ 帧内口径。"""
    calls: list[tuple[float | None, str, str]] = []

    def fake_classify(scores: VariableScores, *, order: str, formula: str) -> Any:
        calls.append((scores.f_fundamental_trend, order, formula))
        state = "S3" if (scores.f_fundamental_trend or 0.0) > -0.5 else "S5"
        return SimpleNamespace(state=SimpleNamespace(argmax_state=state))

    stats = frame_stats(_frame(f=-0.8, f_shadow=-0.1, source=7), classify=fake_classify)
    assert calls == [(-0.1, "etf", "v1")]
    assert stats["shadow_state"] == "S3"
    assert stats["flipped"] is True
    assert stats["dangerous_flip"] is True  # 现判 S5（f<−0.5 的表内状态）→ S3 属危险落点


def test_frame_stats_classify_failure_degrades_to_unjudgeable():
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("classifier exploded")

    stats = frame_stats(_frame(f=-0.8, f_shadow=-0.1, source=7), classify=boom)
    assert stats["shadow_state"] is None
    assert stats["flipped"] is None
    assert stats["dangerous_flip"] is None


def test_frame_stats_empty_dict_is_all_none():
    stats = frame_stats({})
    assert stats["has_fundamental"] is False
    assert all(value is None for key, value in stats.items() if key != "has_fundamental")


# ─────────────────────────────────────────────────────────────────────────────
# 分位口径
# ─────────────────────────────────────────────────────────────────────────────


def test_quantile_linear_interpolation():
    assert _quantile([], 0.5) is None
    assert _quantile([5.0], 0.9) == 5.0  # n=1 直接返回
    assert _quantile([0.0, 1.0], 0.5) == pytest.approx(0.5)  # 与中位数同源
    assert _quantile([0.0, 1.0], 0.9) == pytest.approx(0.9)
    assert _quantile([0.1, 0.7], 0.9) == pytest.approx(0.64)  # 0.1 + 0.9×(0.7−0.1)


# ─────────────────────────────────────────────────────────────────────────────
# 报告聚合：数值与 G-8 判读
# ─────────────────────────────────────────────────────────────────────────────


def test_shadow_report_aggregates_numeric_metrics():
    """混合语料：翻转帧 + 不翻转帧 + 旧帧（无分解）+ 无 fundamental 帧。"""
    frames = [
        _frame(f=0.3, f_shadow=-0.3, source=7),  # 翻转（S1→S5），|ΔF|=0.6
        _frame(f=-0.8, f_shadow=-0.9, source=5),  # 不翻转（S5→S5），|ΔF|=0.1
        _frame(f=-0.8, breakdown=False),  # 旧帧：有 fundamental 但无分解
        _frame(f=-0.8, fundamental=False),  # 无 fundamental：不计入覆盖率分母
    ]
    report = shadow_report(frames)

    assert report["n_frames"] == 4
    assert report["n_fundamental_frames"] == 3
    assert report["n_with_marginal"] == 2
    assert report["coverage"] == pytest.approx(2 / 3)
    assert report["n_delta_frames"] == 2
    assert report["abs_delta_f_median"] == pytest.approx(0.35)  # median(0.6, 0.1)
    assert report["abs_delta_f_p90"] == pytest.approx(0.55)  # 0.1 + 0.9×(0.6−0.1)
    assert report["n_flip_frames"] == 2
    assert report["n_flips"] == 1
    assert report["flip_rate"] == pytest.approx(0.5)
    assert report["n_dangerous_flips"] == 0
    assert report["dangerous_flip_rate"] == pytest.approx(0.0)
    # G-8 判读：帧数/中位数/翻转率/覆盖率四项不达标，危险翻转率达标
    assert report["n_frames_ok"] is False
    assert report["median_ok"] is False
    assert report["flip_ok"] is False
    assert report["dangerous_ok"] is True
    assert report["coverage_ok"] is False
    assert report["g8_pass"] is False


def test_shadow_report_empty_corpus_is_not_pass():
    report = shadow_report([])
    assert report["n_frames"] == 0
    assert report["coverage"] is None
    assert report["abs_delta_f_median"] is None
    assert report["abs_delta_f_p90"] is None
    assert report["flip_rate"] is None
    assert report["dangerous_flip_rate"] is None
    assert report["n_frames_ok"] is False
    assert report["g8_pass"] is False  # 空语料绝不放行


def _clean_frames(n: int, *, flips: int = 0, dangerous: int = 0) -> list[dict[str, Any]]:
    """n 帧干净语料：前 ``flips`` 帧翻转，其中前 ``dangerous`` 帧为 S5→S2。"""
    frames: list[dict[str, Any]] = []
    for index in range(n):
        if index < dangerous:
            frames.append(_frame(f=-0.3, f_shadow=0.3, source=7))  # S5 → S2（危险）
        elif index < flips:
            frames.append(_frame(f=0.3, f_shadow=-0.3, source=7))  # S1 → S5（普通翻转，现行非 S5）
        else:
            frames.append(_frame(f=-0.8, f_shadow=-0.79, source=7))  # 不翻转，|ΔF|=0.01
    return frames


def test_shadow_report_g8_pass_with_fifty_clean_frames():
    """刚好 50 帧且各指标达标 ⇒ g8_pass=True（门槛常量与判读一致）。"""
    report = shadow_report(_clean_frames(G8_MIN_FRAMES))
    assert G8_MIN_FRAMES == 50
    assert report["n_frames"] == 50
    assert report["coverage"] == pytest.approx(1.0)
    assert report["abs_delta_f_median"] == pytest.approx(0.01, abs=1e-9)
    assert report["flip_rate"] == pytest.approx(0.0)
    assert report["dangerous_flip_rate"] == pytest.approx(0.0)
    assert report["n_frames_ok"] is True
    assert report["median_ok"] is True
    assert report["flip_ok"] is True
    assert report["dangerous_ok"] is True
    assert report["coverage_ok"] is True
    assert report["g8_pass"] is True


def test_shadow_report_flip_rate_breach_fails_g8():
    """6/50 = 12% 翻转率 > 10% ⇒ flip_ok=False、g8_pass=False。"""
    report = shadow_report(_clean_frames(50, flips=6))
    assert report["n_flips"] == 6
    assert report["flip_rate"] == pytest.approx(0.12)
    assert report["flip_rate"] > G8_MAX_FLIP_RATE
    assert report["flip_ok"] is False
    assert report["median_ok"] is True  # 其余指标仍达标
    assert report["g8_pass"] is False


def test_shadow_report_dangerous_flip_rate_breach_fails_g8():
    """2/50 = 4% 危险翻转 > 3% ⇒ dangerous_ok=False、g8_pass=False。"""
    report = shadow_report(_clean_frames(50, flips=2, dangerous=2))
    assert report["n_dangerous_flips"] == 2
    assert report["dangerous_flip_rate"] == pytest.approx(0.04)
    assert report["dangerous_flip_rate"] > G8_MAX_DANGEROUS_FLIP_RATE
    assert report["dangerous_ok"] is False
    assert report["flip_ok"] is True  # 4% ≤ 10%（普通翻转率仍达标）
    assert report["g8_pass"] is False


def test_shadow_report_coverage_breach_fails_g8():
    """半数是旧帧（无边际键）⇒ 覆盖率 < 85% ⇒ coverage_ok=False。"""
    frames = [_frame(f=-0.8, f_shadow=-0.79, source=7) for _ in range(25)]
    frames += [_frame(f=-0.8, f_shadow=None, source=None) for _ in range(25)]
    report = shadow_report(frames)
    assert report["coverage"] == pytest.approx(0.5)
    assert report["coverage"] < G8_MIN_COVERAGE
    assert report["coverage_ok"] is False
    assert report["g8_pass"] is False


def test_shadow_report_median_breach_fails_g8():
    """|ΔF| 中位数 0.2 > 0.10 ⇒ median_ok=False（其余指标达标）。"""
    frames = [_frame(f=-0.8, f_shadow=-0.6, source=7) for _ in range(50)]
    report = shadow_report(frames)
    assert report["abs_delta_f_median"] == pytest.approx(0.2)
    assert report["abs_delta_f_median"] > G8_MAX_ABS_DELTA_F_MEDIAN
    assert report["median_ok"] is False
    assert report["flip_ok"] is True
    assert report["g8_pass"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 帧加载：两种落盘格式 / 坏行计数 / 目录展开
# ─────────────────────────────────────────────────────────────────────────────


def _write(path: Any, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_load_frames_supports_pretty_whole_file_and_jsonl(tmp_path):
    """历史帧两种形态并存：整文件 pretty JSON 对象（1 帧）与逐行 JSONL（多帧）。"""
    pretty = _frame(f=0.3, f_shadow=-0.3, source=7)
    pretty["symbol"] = "603259.SH"
    _write(tmp_path / "a_603259.jsonl", json.dumps(pretty, ensure_ascii=False, indent=4))

    line1 = _frame(f=-0.8, f_shadow=-0.9, source=7)
    line1["symbol"] = "300760.SZ"
    line2 = _frame(f=-0.3, f_shadow=0.3, source=5)
    line2["symbol"] = "300760.SZ"
    _write(tmp_path / "b_300760.jsonl", json.dumps(line1) + "\n" + json.dumps(line2) + "\n")

    frames, skipped = load_frames(tmp_path)
    assert skipped == 0
    assert [frame["symbol"] for frame in frames] == ["603259.SH", "300760.SZ", "300760.SZ"]
    report = shadow_report(frames)
    assert report["n_frames"] == 3
    assert report["n_flips"] == 2  # S5→S0 与 S5→S2
    assert report["n_dangerous_flips"] == 1


def test_load_frames_skips_bad_lines_but_keeps_good_ones(tmp_path):
    good = _frame(f=-0.8, f_shadow=-0.9, source=7)
    _write(
        tmp_path / "mixed.jsonl",
        json.dumps(good) + "\n{broken json\n" + json.dumps(good) + "\n\n42\n",
    )
    frames, skipped = load_frames(tmp_path / "mixed.jsonl")
    assert len(frames) == 2  # 坏行不静默丢掉整份文件
    assert skipped == 2  # "{broken json" 与标量 42

    frames_missing, skipped_missing = load_frames(tmp_path / "not-exists.jsonl")
    assert frames_missing == []
    assert skipped_missing == 1  # 文件级失败计 1 次


def test_load_frames_whole_file_garbage_counts_lines(tmp_path):
    _write(tmp_path / "garbage.jsonl", "{not json\nstill not json\n")
    frames, skipped = load_frames(tmp_path / "garbage.jsonl")
    assert frames == []
    assert skipped == 2  # 逐行解析失败：每行计 1 次


def test_load_frames_accepts_json_array_and_counts_non_objects(tmp_path):
    frames_payload = [_frame(f=-0.8, f_shadow=-0.9, source=7), _frame(f=-0.3, f_shadow=0.3, source=7)]
    _write(tmp_path / "array.json", json.dumps([*frames_payload, 42]))
    frames, skipped = load_frames(tmp_path / "array.json")
    assert len(frames) == 2
    assert skipped == 1  # 数组里的非对象元素


def test_load_frames_empty_inputs(tmp_path):
    assert load_frames([]) == ([], 0)
    _write(tmp_path / "empty.jsonl", "")
    _write(tmp_path / "blank.jsonl", "\n  \n")
    assert load_frames(tmp_path / "empty.jsonl") == ([], 0)
    assert load_frames(tmp_path / "blank.jsonl") == ([], 0)
