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
    report_period: str | None = None,
    marginal_period: str | None = None,
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
        if report_period is not None:
            payload["report_period"] = report_period
        if marginal_period is not None:
            payload["marginal_period"] = marginal_period
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


def test_frame_stats_report_period_and_is_q1():
    """P0-3：report_period 随帧可读；is_q1 由 marginal_period 判定、缺失回落 report_period。"""
    q1 = _frame(f=-0.8, report_period="20260331", marginal_period="20260331")
    q1_stats = frame_stats(q1)
    assert q1_stats["report_period"] == "20260331"
    assert q1_stats["is_q1"] is True

    annual = _frame(f=-0.8, report_period="20261231", marginal_period="20261231")
    annual_stats = frame_stats(annual)
    assert annual_stats["report_period"] == "20261231"
    assert annual_stats["is_q1"] is False

    # marginal_period 缺失 ⇒ 回落 report_period（带分隔符也可判定）
    fallback = _frame(f=-0.8, report_period="2026-03-31")
    assert frame_stats(fallback)["is_q1"] is True

    # 旧帧无期间字段 ⇒ 明确 None（无法判定，不获得 Q1 豁免）
    old_stats = frame_stats(_frame(f=-0.8))
    assert old_stats["report_period"] is None
    assert old_stats["is_q1"] is None


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
    assert report["coverage_ex_q1"] is None
    assert report["abs_delta_f_median"] is None
    assert report["abs_delta_f_p90"] is None
    assert report["median_abs_delta_ex_q1"] is None
    assert report["flip_rate"] is None
    assert report["dangerous_flip_rate"] is None
    assert report["n_frames_ok"] is False
    assert report["g8_pass"] is False  # 空语料绝不放行


def test_shadow_report_ex_q1_coverage_denominator():
    """P0-3 S4：Q1 帧（结构性无边际键）不计入 ex-Q1 覆盖率分母；旧 coverage 保留对照。"""
    frames = [_frame(f=-0.8, f_shadow=-0.79, source=7) for _ in range(40)]
    frames += [_frame(f=-0.8, source=None, marginal_period="20260331") for _ in range(10)]

    report = shadow_report(frames)

    assert report["n_q1_frames"] == 10
    assert report["n_fundamental_frames"] == 50
    assert report["n_fundamental_frames_ex_q1"] == 40
    assert report["n_with_marginal_ex_q1"] == 40
    assert report["coverage"] == pytest.approx(40 / 50)  # 旧口径：被 Q1 稀释
    assert report["coverage_ex_q1"] == pytest.approx(1.0)  # ex-Q1：分母排除 Q1
    assert report["coverage"] < G8_MIN_COVERAGE
    assert report["coverage_ok"] is True  # 门禁用 ex-Q1 口径


def test_shadow_report_ex_q1_median_excludes_q1_delta():
    """P0-3 S1：Q1 帧的 ΔF（结构性/污染值）不进入 ex-Q1 中位数；旧中位数保留对照。"""
    # 40 帧 Q1 带「修复前污染」的 ΔF=0.3 + 20 帧非 Q1 ΔF=0.01
    frames = [_frame(f=-0.8, f_shadow=-0.5, source=7, marginal_period="20260331") for _ in range(40)]
    frames += [_frame(f=-0.8, f_shadow=-0.79, source=7) for _ in range(20)]

    report = shadow_report(frames)

    assert report["n_delta_frames"] == 60
    assert report["n_delta_frames_ex_q1"] == 20
    assert report["abs_delta_f_median"] == pytest.approx(0.3)  # 旧口径：Q1 污染主导
    assert report["median_abs_delta_ex_q1"] == pytest.approx(0.01)  # ex-Q1：只看非 Q1
    assert report["median_ok"] is True


def test_shadow_report_g8_uses_ex_q1_metrics():
    """P0-3 G-8：Q1 帧拉低旧 coverage/中位数，但门禁用 ex-Q1 口径 ⇒ 仍可 pass。"""
    frames = _clean_frames(G8_MIN_FRAMES)  # 50 帧非 Q1、ΔF=0.01、source=7
    frames += [_frame(f=-0.8, source=None, marginal_period="20260331") for _ in range(10)]

    report = shadow_report(frames)

    assert report["n_frames"] == 60
    assert report["n_q1_frames"] == 10
    assert report["coverage"] == pytest.approx(50 / 60)  # 旧口径 < 85%
    assert report["median_abs_delta_ex_q1"] == pytest.approx(0.01)
    assert report["coverage_ex_q1"] == pytest.approx(1.0)
    assert report["median_ok"] is True
    assert report["coverage_ok"] is True
    assert report["g8_pass"] is True


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


# ─────────────────────────────────────────────────────────────────────────────
# 帧去重：样本单位 = symbol-day（G-8 分母口径）
# ─────────────────────────────────────────────────────────────────────────────


def test_dedupe_frames_symbol_day_keeps_first_and_counts_drops():
    from alphabee.midterm.marginal_shadow import DEDUPE_SYMBOL_DAY, dedupe_frames

    first = _frame(f=-0.8, f_shadow=-0.9, source=7)
    same_day = dict(first, f_shadow=-0.7)  # 同 symbol + as_of_date
    next_day = dict(first, as_of_date="2026-11-02")
    other_symbol = dict(first, symbol="600519.SH")

    kept, dropped = dedupe_frames([first, same_day, next_day, other_symbol], scope=DEDUPE_SYMBOL_DAY)
    assert kept == [first, next_day, other_symbol]
    assert dropped == 1


def test_dedupe_frames_keeps_frames_without_key():
    """缺 symbol / as_of_date 的帧无法判定同日 ⇒ 保留（不静默丢）。"""
    from alphabee.midterm.marginal_shadow import dedupe_frames

    no_symbol = {"as_of_date": "2026-11-01", "state": {"argmax_state": "S1"}}
    no_date = {"symbol": "600760.SH", "state": {"argmax_state": "S1"}}
    frames = [no_symbol, dict(no_symbol), no_date, dict(no_date)]
    kept, dropped = dedupe_frames(frames)
    assert kept == frames
    assert dropped == 0


def test_dedupe_frames_scope_none_and_invalid():
    from alphabee.midterm.marginal_shadow import DEDUPE_NONE, dedupe_frames

    a = _frame(f=-0.8, f_shadow=-0.9, source=7)
    b = dict(a, f_shadow=-0.7)
    kept, dropped = dedupe_frames([a, b], scope=DEDUPE_NONE)
    assert kept == [a, b]
    assert dropped == 0

    with pytest.raises(ValueError):
        dedupe_frames([a], scope="week")


# ─────────────────────────────────────────────────────────────────────────────
# S5–S8（P2-3 / 设计 §15.1）：合成帧的分组、分母、缺失显式 None
# ─────────────────────────────────────────────────────────────────────────────


def _breakdown_frame(**breakdown: Any) -> dict[str, Any]:
    """构造带自定义 score_breakdown 的帧（用于 S5–S8 口径测试）。"""
    frame = _frame(f=-0.8, f_shadow=-0.79, source=7)
    frame["factor_snapshot"]["fundamental"]["score_breakdown"] = dict(breakdown)
    return frame


def test_s5_ocf_hit_rate_groups_by_basis_and_quarter():
    """S5：分母=含 ocf_penalty 的帧；按 basis（1=ttm/0=sign）与报告季度分组。"""
    from alphabee.midterm.marginal_shadow import s5_ocf_hit_rate

    frames = [
        _breakdown_frame(f=-0.8, ocf_penalty=-0.5, ocf_penalty_basis=1),  # ttm 命中
        _breakdown_frame(f=-0.8, ocf_penalty=0.0, ocf_penalty_basis=1),  # ttm 未命中
        _breakdown_frame(f=-0.8, ocf_penalty=-0.5, ocf_penalty_basis=0),  # sign 命中
        _breakdown_frame(f=-0.8),  # 旧帧/缺 penalty ⇒ 不进分母
    ]
    frames[0]["factor_snapshot"]["fundamental"]["report_period"] = "20250930"
    frames[1]["factor_snapshot"]["fundamental"]["report_period"] = "20250930"
    frames[2]["factor_snapshot"]["fundamental"]["report_period"] = "20250630"

    metrics = s5_ocf_hit_rate(frames)
    assert metrics["n_frames"] == 4
    assert metrics["n_judgeable"] == 3
    assert metrics["n_hits"] == 2
    assert metrics["rate"] == pytest.approx(2 / 3)
    assert metrics["by_basis"]["ttm"] == {"n": 2, "hits": 1, "rate": pytest.approx(0.5)}
    assert metrics["by_basis"]["sign"] == {"n": 1, "hits": 1, "rate": pytest.approx(1.0)}
    assert metrics["n_basis_missing"] == 0
    assert metrics["by_quarter"]["2025Q3"] == {"n": 2, "hits": 1, "rate": pytest.approx(0.5)}
    assert metrics["by_quarter"]["2025Q2"] == {"n": 1, "hits": 1, "rate": pytest.approx(1.0)}
    # 行业字段不可得 ⇒ 显式 None（不编造分组）
    assert metrics["by_industry"] is None
    assert "行业" in metrics["industry_note"]


def test_s5_ocf_hit_rate_missing_basis_and_empty():
    """S5：basis 缺失的帧计入总体但不进分组；空输入不抛、比率为 None。"""
    from alphabee.midterm.marginal_shadow import s5_ocf_hit_rate

    metrics = s5_ocf_hit_rate([_breakdown_frame(ocf_penalty=-0.5)])
    assert metrics["n_judgeable"] == 1 and metrics["n_hits"] == 1
    assert metrics["n_basis_missing"] == 1
    assert metrics["by_basis"]["ttm"]["rate"] is None
    assert metrics["by_basis"]["sign"]["rate"] is None

    empty = s5_ocf_hit_rate([])
    assert empty["n_judgeable"] == 0
    assert empty["rate"] is None
    assert empty["by_quarter"] == {}


def test_s6_beat_miss_match_denominator_is_judgeable_frames():
    """S6：分母=含 forecast_period_matched 的帧；1 为命中；缺键/无分解帧不进分母。"""
    from alphabee.midterm.marginal_shadow import s6_beat_miss_match

    frames = [
        _breakdown_frame(forecast_period_matched=1),  # 入围 + 命中
        _breakdown_frame(forecast_period_matched=0),  # 入围
        _breakdown_frame(forecast_period_matched=1.0),  # 入围 + 命中
        _breakdown_frame(ocf_penalty=0.0),  # 有分解但缺 forecast 键 ⇒ n_missing
        {"symbol": "600760.SH"},  # 无 fundamental / 无分解 ⇒ 完全忽略
    ]
    metrics = s6_beat_miss_match(frames)
    assert metrics["n_judgeable"] == 3
    assert metrics["n_matched"] == 2
    assert metrics["n_missing"] == 1
    assert metrics["rate"] == pytest.approx(2 / 3)
    assert s6_beat_miss_match([])["rate"] is None


def test_s7_recurring_gap_distribution_and_missing():
    """S7：np_term_recurring / 归母项 / 差值的 n、median、P90、missing 计数。"""
    from alphabee.midterm.marginal_shadow import s7_recurring_gap

    frames = [
        _breakdown_frame(np_term_recurring=0.5, net_profit_term=1.0),
        _breakdown_frame(np_term_recurring=0.3, net_profit_term=1.0),
        _breakdown_frame(net_profit_term=1.0),  # 仅归母项（旧口径 fallback）
        {"symbol": "600760.SH"},  # 无分解 ⇒ 不计入 n_with_breakdown
    ]
    metrics = s7_recurring_gap(frames)
    assert metrics["n_frames"] == 4
    assert metrics["n_with_breakdown"] == 3
    assert metrics["np_term_recurring"]["n"] == 2
    assert metrics["np_term_recurring"]["median"] == pytest.approx(0.4)
    assert metrics["np_term_recurring"]["p90"] == pytest.approx(0.48)
    assert metrics["np_term_recurring"]["missing"] == 1
    assert metrics["net_profit_term"]["n"] == 3
    assert metrics["net_profit_term"]["median"] == pytest.approx(1.0)
    assert metrics["net_profit_term"]["missing"] == 0
    assert metrics["diff"]["n"] == 2
    assert metrics["diff"]["median"] == pytest.approx(-0.6)
    assert metrics["diff"]["p90"] == pytest.approx(-0.52)


def test_s8_segment_rates_and_missing():
    """S8：bonus 触发率 / 期间错配率 / 占比门槛率；分母=含对应键的帧。"""
    from alphabee.midterm.marginal_shadow import s8_segment

    frames = [
        _breakdown_frame(segment_bonus=0.2, segment_period_mismatch=0, segment_share_below_min=0),
        _breakdown_frame(segment_bonus=0.0, segment_period_mismatch=1, segment_share_below_min=1),
        _breakdown_frame(),  # 三个键全缺
    ]
    metrics = s8_segment(frames)
    assert metrics["n_frames"] == 3
    assert metrics["segment_bonus"] == {
        "n_judgeable": 2,
        "n_triggered": 1,
        "rate": pytest.approx(0.5),
        "missing": 1,
    }
    assert metrics["segment_period_mismatch"]["rate"] == pytest.approx(0.5)
    assert metrics["segment_period_mismatch"]["n_mismatched"] == 1
    assert metrics["segment_share_below_min"]["rate"] == pytest.approx(0.5)
    assert metrics["segment_share_below_min"]["n_below_min"] == 1

    empty = s8_segment([])
    assert empty["segment_bonus"]["rate"] is None
    assert empty["segment_period_mismatch"]["missing"] == 0


def test_shadow_report_aggregates_s5_s8_and_keeps_existing_keys():
    """S5–S8 聚合进 shadow_report；既有 S1–S4 键与口径保持（值不变）。"""
    from alphabee.midterm.marginal_shadow import shadow_report

    frames = [
        _breakdown_frame(
            f=-0.8,
            f_shadow=-0.79,
            f_marginal_source=7,
            ocf_penalty=-0.5,
            ocf_penalty_basis=1,
            forecast_period_matched=1,
            np_term_recurring=0.5,
            net_profit_term=1.0,
            segment_bonus=0.2,
            segment_period_mismatch=0,
            segment_share_below_min=0,
        )
    ]
    report = shadow_report(frames)
    # 既有键仍在、值口径不变
    assert report["n_frames"] == 1
    assert report["median_abs_delta_ex_q1"] == pytest.approx(0.01)
    assert report["g8_pass"] is False  # 帧数不足
    # 新键
    assert report["s5_ocf_hit_rate"]["rate"] == pytest.approx(1.0)
    assert report["s6_beat_miss_match"]["rate"] == pytest.approx(1.0)
    assert report["s7_recurring_gap"]["diff"]["median"] == pytest.approx(-0.5)
    assert report["s8_segment"]["segment_bonus"]["rate"] == pytest.approx(1.0)


def test_shadow_report_empty_input_includes_safe_s5_s8():
    """空输入/旧帧安全：S5–S8 键存在且比率为 None，不抛异常。"""
    from alphabee.midterm.marginal_shadow import shadow_report

    report = shadow_report([])
    assert report["s5_ocf_hit_rate"]["rate"] is None
    assert report["s6_beat_miss_match"]["rate"] is None
    assert report["s7_recurring_gap"]["np_term_recurring"]["missing"] == 0
    assert report["s8_segment"]["segment_bonus"]["rate"] is None
