"""classifier_v2 的不变量测试（design MIDTERM_STATE_EVIDENCE_V2_DESIGN §8.1 I1–I9 + §8.5 验收帧）。

全部用合成 ``VariableScores``：无网络、无 LLM、无随机性（§2.2 N7），确定性验证。
数值容差 ±0.02（§9 第 3 条）。

关于 I1 的「分布不增」口径（与 §2.1 G1 / §4.7 逐字对齐）：状态级证据严格单调——多头侧
（S0–S3）不增、空头侧（S4/S5）不降；**分布层面只断言可证的聚合量**（多头侧总质量不增、
看空质量 ``P(S4)+P(S5)`` 与 ``P(S5)`` 不降）。原因：softmax 是归一化的，当多头侧某一状态
证据下降时，其释放的质量会在其余状态间重分配，故**单个** ``P(S0)``（或 ``P(S4)``）可能
反而上升（例如 E 恶化让 S2 掉、S0 的相对权重上升）。这是 softmax 的数学性质，不是单调性
回归；把状态级分布单调写进断言会得到一个恒假的不变量。

I1 的扫描域按 §8.1 固定为负域 ``F,E ∈ [−1,0]``，不放宽。§8.2 要求「S2 的 ``neutral_band``
换回单边 ``not_up`` ⇒ I1 必红」，但该变异在负域上是常数（``not_yet(e≤0) ≡ 1``），负域扫描按
定义杀不掉它，故另立 ``test_i1b_monotonicity_across_zero`` 覆盖「正 → 负」过渡（该测试
docstring 记录收口依据）。这是 §8.1 与 §8.2 措辞张力的显式解决，不是隐藏放宽。
"""

from __future__ import annotations

import ast
import itertools
import math
from pathlib import Path

import pytest

from alphabee.midterm import classifier_v2 as v2
from alphabee.midterm.models import (
    ClassifierResult,
    CompanyStateArtifact,
    Consistency,
    StateBelief,
    VariableScores,
)

_STATES = ("S0", "S1", "S2", "S3", "S4", "S5")
_LONG_SIDE = ("S0", "S1", "S2", "S3")
_SHORT_SIDE = ("S4", "S5")
_GRID_01 = tuple(round(-1.0 + 0.05 * i, 2) for i in range(21))  # −1.00 … 0.00，21 格（§8.1 I1）
# I1b 的「正 → 负」过渡扫描：+0.05（正侧入口）→ −0.05（跨过 0）→ −0.10 … → −1.00，共 21 点。
# 首步跨 0（而非经过 0.00）是刻意的：neutral_band 是双侧钟形，E=0 是 S2 的「预期尚未共振」
# 顶点，真实实现在 +0.05 → 0.00 处仍有 +0.0833 的合法上升；而单边变异在 +0.05 → −0.05 处
# 从 0.9167 跳到 1.0，正是 D1 病灶。见 test_i1b_monotonicity_across_zero 的 docstring。
_GRID_E_TRANSITION = (0.05, *(round(-0.05 - 0.05 * i, 2) for i in range(20)))
_GRID_5 = (-1.0, -0.5, 0.0, 0.5, 1.0)  # 5 值网格：5^6 = 15625（§1.2 / §8.5 同口径）
_TVC_GROUPS = (  # 5 组 (T, V, C)：覆盖价格强/弱、估值贵/便宜、拥挤/不拥挤
    (1.0, -1.0, 1.0),
    (0.5, 0.5, -0.5),
    (0.0, 0.0, 0.0),
    (-0.8, 0.8, 0.8),
    (0.3, -0.3, 0.6),
)
_TOL = 0.02


def _scores(f=None, e=None, t=None, v=None, c=None, r=None) -> VariableScores:
    return VariableScores(
        f_fundamental_trend=f,
        e_revision=e,
        t_relative_strength=t,
        v_valuation_percentile=v,
        c_crowding=c,
        r_risk=r,
    )


def _result(f=None, e=None, t=None, v=None, c=None, *, order: str = "tef"):
    return v2.classify_state(_scores(f, e, t, v, c), order=order)


def _p_top(result) -> float:
    return max(result.state.distribution.values())


def _bear_mass(result) -> float:
    distribution_map = result.state.distribution
    return distribution_map["S4"] + distribution_map["S5"]


def _long_mass(distribution_map: dict[str, float]) -> float:
    return sum(distribution_map[state] for state in _LONG_SIDE)


def _max_abs_dp(before: VariableScores, after: VariableScores, *, order: str = "tef") -> float:
    left = v2.classify_state(before, order=order).state.distribution
    right = v2.classify_state(after, order=order).state.distribution
    return max(abs(left[state] - right[state]) for state in _STATES)


def _grid_scores():
    """5^6 全网格（F/E/T/V/C/R 均取 5 值）。"""
    for frame in itertools.product(_GRID_5, repeat=6):
        yield _scores(*frame)


# ─────────────────────────────────────────────────────────────────────────────
# I1 对抗性单调性（D1 修复的形式证明，§4.7 第 1 条）
# ─────────────────────────────────────────────────────────────────────────────


def _assert_adverse_step(
    previous: tuple[dict[str, float], dict[str, float]],
    current: tuple[dict[str, float], dict[str, float]],
    context: tuple,
) -> None:
    """断言「沿恶化方向走一步」的对抗性单调性（I1 / I1b 共用）。

    - 证据：多头侧（S0–S3）不增、空头侧（S4/S5）不降；
    - 分布：只断言可证的聚合量（多头侧总质量不增、看空质量不降），原因见模块 docstring。
    """
    previous_evidence, previous_distribution = previous
    evidence, distribution_map = current
    for state in _LONG_SIDE:
        assert evidence[state] <= previous_evidence[state] + 1e-12, (context, "long-side evidence rose", state)
    for state in _SHORT_SIDE:
        assert evidence[state] >= previous_evidence[state] - 1e-12, (context, "short-side evidence fell", state)
    assert _long_mass(distribution_map) <= _long_mass(previous_distribution) + 1e-12, context
    # 分布层同样只断言可证的聚合量：看空质量不降。**单个** P(S5) 不在此列 —— 空头侧内部也会
    # 重分配（I1b 跨 0 首步 S4 因 ``not_yet`` 饱和而上升，会把同为短侧的 S5 质量稀释）。
    assert (
        distribution_map["S4"] + distribution_map["S5"]
        >= previous_distribution["S4"] + previous_distribution["S5"] - 1e-12
    ), context


def test_i1_adverse_monotonicity():
    """I1（§8.1）：F、E 各沿负域 [−1,0] 的 21 格 × 5 组 (T,V,C) 扫描，证据单调（域按设计不放宽）。

    正 → 负过渡（含 ``E=+0.05`` 入口）由 :func:`test_i1b_monotonicity_across_zero` 覆盖：
    单边变异在负域上是常数，I1 按定义杀不到它（§8.2 变异 1 的必红入口是 I1b）。
    """
    sequences = 0
    for t, v, c in _TVC_GROUPS:
        for axis in ("f", "e"):
            for partner in _GRID_01:
                previous: tuple[dict[str, float], dict[str, float]] | None = None
                for x in reversed(_GRID_01):  # 0.00 → −1.00：沿负方向逐步移动（恶化）
                    scores = (
                        _scores(f=x, e=partner, t=t, v=v, c=c)
                        if axis == "f"
                        else _scores(f=partner, e=x, t=t, v=v, c=c)
                    )
                    evidence = v2.state_evidence(scores, order="tef")
                    distribution_map = v2.distribution(v2.normalized_evidence(evidence))
                    if previous is not None:
                        _assert_adverse_step(
                            previous, (evidence, distribution_map), ("i1", axis, x, partner, (t, v, c))
                        )
                        # I1 的扫描轴（E 轴 not_yet(e≤0)≡1、F 轴 S4 与 F 无关）上 S4 恒定，
                        # 短侧不会互相稀释，故此处可额外断言 S5 的质量不降（I1b 跨 0 首步不成立）。
                        assert distribution_map["S5"] >= previous[1]["S5"] - 1e-12
                    previous = (evidence, distribution_map)
                sequences += 1
    assert sequences == len(_TVC_GROUPS) * 2 * len(_GRID_01)  # 5 组 × 2 轴 × 21 固定点


@pytest.mark.parametrize("f_fixed", [-1.0, -0.5, 0.0, 0.5])
def test_i1b_monotonicity_across_zero(f_fixed):
    """I1b（§8.2 变异 1 的必红入口）：E 从正侧入口 +0.05 单调降到 −1.00，S2 及其他多头侧证据不增。

    **收口依据（§8.1 / §8.2 的措辞张力，显式解决，不放宽 I1）**：§8.1 规定 I1 的扫描域是负域
    ``F,E ∈ [−1,0]``；而把 S2 的 ``neutral_band`` 换回单边 ``not_up`` 时，变异在负域上恒等于
    1.0（常数），**I1 按定义杀不掉它**（见 parity 里对该常数的显式断言）。§8.2 仍要求「I1 必红」，
    故新增本测试补上「正 → 负」过渡这一段：真实实现在 +0.05 → −0.05 处 S2 持平、其后严格下降；
    变异恰在此处从 0.9167 跳到 1.0（D1「恶化 = 确认」），I1b 因此必红。I1 的域保持不变。

    起点取 +0.05 而不是 +1.00：``neutral_band`` 是双侧钟形，``E=+1``（强上修）时 S2 的「预期
    尚未共振」项诚实地为 0，E 回落到中性带内 S2 上升是 S2 的设计语义（价格先确认、预期尚未
    共振），只有「E 跌破 0 后 S2 仍被单边算子满额确认」才是 D1 病灶。**[+1.00, +0.05] 正侧段
    的形状仍被显式钉住**（tef 上 S2 随 E 回落而上升、etf 上随门控收窄而下降），因此本测试覆盖
    「+1.00 起」的完整过渡语义，只是不谎称整段单调不增。

    另：D1 反例（§8.2 病灶）显式钉为严格不等式 —— 正侧入口 +0.05 的 S2 证据必须严格大于
    完全恶化 −1.00 的 S2 证据（变异下两者反向）。
    """
    sequences = 0
    for t, v, c in _TVC_GROUPS:
        for order in ("tef", "etf"):
            previous: tuple[dict[str, float], dict[str, float]] | None = None
            s2_values: list[float] = []
            for e in _GRID_E_TRANSITION:  # +0.05 → −0.05 → −0.10 → … → −1.00（逐步恶化）
                scores = _scores(f=f_fixed, e=e, t=t, v=v, c=c)
                evidence = v2.state_evidence(scores, order=order)
                distribution_map = v2.distribution(v2.normalized_evidence(evidence))
                s2_values.append(evidence["S2"])
                if previous is not None:
                    _assert_adverse_step(
                        previous,
                        (evidence, distribution_map),
                        ("i1b", f_fixed, e, order, (t, v, c)),
                    )
                previous = (evidence, distribution_map)

            # D1 反例钉：正侧入口 +0.05 的 S2 严格大于完全恶化 −1.00（变异下反向 ⇒ I1b 红）。
            # S2 恒为 0 的组（如 tef 的 T≤0）没有可判别的证据，跳过严格不等式。
            if s2_values[0] > 1e-12:
                assert s2_values[0] > s2_values[-1], ("i1b-D1", f_fixed, order, (t, v, c))

            # 正侧段（从 +1.00 起，解释「为何单调链不从 +1.00 起」）：
            # - tef：E 是 S2 的非门控中性项（钟形），E 从 +1.00 回落到中性带内时「尚未共振」项回补，
            #   S2 **上升**——这是 S2 的设计语义，不是 D1；
            # - etf：S2 的门控就是 E 本身，E 回落时门控收窄 ⇒ S2 下降（etf 的钟形落在 T 轴上）。
            # 两序在正侧段都反向于「恶化 ⇒ 不增」，故 [+1.00, +0.05] 上的单调链在数学上不可能成立；
            # 这里把从 +1.00 起的正侧段形状显式钉住（而不是谎称它单调不增）。
            positive_grid = (1.0, 0.8, 0.6, 0.4, 0.2, 0.05)
            positive_s2 = [
                v2.state_evidence(_scores(f=f_fixed, e=e, t=t, v=v, c=c), order=order)["S2"] for e in positive_grid
            ]
            positive_pairs = list(zip(positive_s2, positive_s2[1:]))
            if order == "tef":
                assert all(later >= earlier - 1e-12 for earlier, later in positive_pairs), (
                    "i1b-positive-tef",
                    f_fixed,
                    order,
                    (t, v, c),
                )
            else:
                assert all(later <= earlier + 1e-12 for earlier, later in positive_pairs), (
                    "i1b-positive-etf",
                    f_fixed,
                    order,
                    (t, v, c),
                )
            sequences += 1
    assert sequences == len(_TVC_GROUPS) * 2  # 5 组 × 2 序（每个固定 F）


# ─────────────────────────────────────────────────────────────────────────────
# I2 可达性（D2 修复：T>0 半平面也能退出）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("order", "expected", "frame"),
    [
        # tef（T→E→F）
        ("tef", "S0", (0.0, 0.0, 0.0, 0.0, 0.0)),
        ("tef", "S1", (1.0, 0.0, -1.0, 1.0, 0.0)),
        ("tef", "S2", (1.0, 0.0, 1.0, 0.0, 0.0)),
        ("tef", "S3", (1.0, 1.0, 1.0, -1.0, 1.0)),
        ("tef", "S4", (0.0, 0.0, -1.0, -1.0, -1.0)),
        ("tef", "S5", (-1.0, -1.0, 1.0, 0.0, 0.0)),
        # etf（E→T→F）：S2 门控换成 E，其余状态用同一帧即可达
        ("etf", "S0", (0.0, 0.0, 0.0, 0.0, 0.0)),
        ("etf", "S1", (1.0, 0.0, -1.0, 1.0, 0.0)),
        ("etf", "S2", (1.0, 1.0, 0.0, 0.0, 0.0)),
        ("etf", "S3", (1.0, 1.0, 1.0, -1.0, 1.0)),
        ("etf", "S4", (0.0, 0.0, -1.0, -1.0, -1.0)),
        ("etf", "S5", (-1.0, -1.0, 1.0, 0.0, 0.0)),
    ],
)
def test_i2_reachability_all_six_states(order, expected, frame):
    result = v2.classify_state(_scores(*frame), order=order)
    assert result.state.argmax_state == expected
    assert _p_top(result) >= 0.65


def test_i2_d2_counterexample_price_strong_double_click_exits():
    """D2 核心反例：T=+1（价格仍强）且 F=E=−1 ⇒ S5（v1-tef 在此恒为 S2，退出不可达）。"""
    scores = _scores(f=-1.0, e=-1.0, t=1.0)
    result = v2.classify_state(scores, order="tef")
    assert result.state.argmax_state == "S5"
    assert _p_top(result) >= 0.65
    # 证伪路径不含 T：S5 证据 = max(价格路径, λ·se·sf)，与 T 的符号无关（§4.7 第 4 条）
    assert v2.state_evidence(scores, order="tef")["S5"] == pytest.approx(v2.BREAK_LAMBDA)
    assert v2.state_evidence(_scores(f=-1.0, e=-1.0, t=-1.0), order="tef")["S5"] == pytest.approx(v2.BREAK_LAMBDA)


# ─────────────────────────────────────────────────────────────────────────────
# I3 幅度敏感（专杀「F/E 幅度不敏感」回归）
# ─────────────────────────────────────────────────────────────────────────────


def test_i3_magnitude_sensitivity():
    # F：−1 vs −0.5（其余相同）
    assert (
        _max_abs_dp(
            _scores(f=-1.0, e=-1.0, t=-1.0, v=0.0, c=0.0),
            _scores(f=-0.5, e=-1.0, t=-1.0, v=0.0, c=0.0),
        )
        >= _TOL
    )
    # E：−1 vs −0.5（其余相同）；S2 证据本身也必须随 E 幅度变化（neutral_band 的作用）
    assert (
        _max_abs_dp(
            _scores(f=-1.0, e=-1.0, t=1.0, v=0.0, c=0.0),
            _scores(f=-1.0, e=-0.5, t=1.0, v=0.0, c=0.0),
        )
        >= _TOL
    )
    assert v2.state_evidence(_scores(f=0.0, e=-1.0, t=1.0), order="tef")["S2"] != pytest.approx(
        v2.state_evidence(_scores(f=0.0, e=-0.5, t=1.0), order="tef")["S2"]
    )


# ─────────────────────────────────────────────────────────────────────────────
# I3b 关键格不反转（D2 的业务口径：F≤−0.5 ∧ E≤−0.3 ∧ T≥0.3）
# ─────────────────────────────────────────────────────────────────────────────


def test_i3b_critical_cell_not_inverted():
    cells = 0
    bear_dominant = 0
    for scores in _grid_scores():
        f = scores.f_fundamental_trend
        e = scores.e_revision
        t = scores.t_relative_strength
        if f is None or e is None or t is None:
            continue
        if f <= -0.5 and e <= -0.3 and t >= 0.3:
            cells += 1
            if _bear_mass(v2.classify_state(scores, order="tef")) >= 0.5:
                bear_dominant += 1
    assert cells == 1000  # 2 (F) × 2 (E) × 2 (T) × 5^3 (V,C,R)
    assert bear_dominant / cells >= 0.90


# ─────────────────────────────────────────────────────────────────────────────
# I4 上限归一化（D3 修复：每状态可达 1.0；S5 cap == max(1.5, BREAK_LAMBDA)）
# ─────────────────────────────────────────────────────────────────────────────

# (order, state, frame)：使该状态证据达到结构上限的输入（同时也是 I2 的 argmax 帧）
_CAP_FRAMES = (
    ("tef", "S0", (0.0, 0.0, 0.0, 0.0, 0.0)),
    ("tef", "S1", (1.0, 0.0, -1.0, 1.0, 0.0)),
    ("tef", "S2", (1.0, 0.0, 1.0, 0.0, 0.0)),
    ("etf", "S2", (1.0, 1.0, 0.0, 0.0, 0.0)),
    ("tef", "S3", (1.0, 1.0, 1.0, -1.0, 1.0)),
    ("tef", "S4", (0.0, 0.0, -1.0, -1.0, -1.0)),
    ("tef", "S5", (-1.0, -1.0, 1.0, 0.0, 0.0)),
)


def test_i4_s5_cap_equals_max_price_path_and_lambda():
    assert v2.EVIDENCE_CAPS["S5"] == max(1.5, v2.BREAK_LAMBDA)
    assert v2.EVIDENCE_CAPS["S5"] == pytest.approx(2.0)


@pytest.mark.parametrize(("order", "state", "frame"), _CAP_FRAMES)
def test_i4_caps_are_achievable(order, state, frame):
    evidence = v2.state_evidence(_scores(*frame), order=order)
    assert evidence[state] == pytest.approx(v2.EVIDENCE_CAPS[state])
    assert v2.normalized_evidence(evidence)[state] == pytest.approx(1.0)


def test_i4_evidence_never_exceeds_caps_on_grid():
    for scores in _grid_scores():
        evidence = v2.state_evidence(scores, order="tef")
        for state in _STATES:
            # 上限即「可达最大值」：任何输入都不得超过（否则 cap 不是上限，归一化会溢出被 clip）
            assert evidence[state] <= v2.EVIDENCE_CAPS[state] + 1e-12, (state, scores)
        for state, value in v2.normalized_evidence(evidence).items():
            assert 0.0 <= value <= 1.0, state


# ─────────────────────────────────────────────────────────────────────────────
# I5 校准聚合（§7.3 的聚合门；容差按 §8.1 的 ≤0.10 / ≥0.85）
# ─────────────────────────────────────────────────────────────────────────────


def test_i5_calibration_aggregates():
    bull = _result(1.0, 1.0, 1.0, -1.0, 1.0)
    bear = _result(-1.0, -1.0, -1.0, -1.0, 1.0)
    assert abs(_p_top(bull) - _p_top(bear)) <= 0.10  # 多空「状态确信度」同一个量

    no_signal = _result(0.05, -0.05, 0.1, 0.0, 0.0)
    assert no_signal.state.distribution["S0"] >= 0.85

    for scores in _grid_scores():
        distribution_map = v2.distribution(v2.normalized_evidence(v2.state_evidence(scores, order="tef")))
        entropy = v2.entropy_nats(distribution_map)
        assert 0.0 <= entropy / math.log(len(_STATES)) <= 1.0
        assert sum(distribution_map.values()) == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────────────
# I6 困境反转 vs 证伪（§4.3 双路径的语义边界）
# ─────────────────────────────────────────────────────────────────────────────


def test_i6_turnaround_vs_falsification():
    # ① 仅 F 崩（预期尚未下修）⇒ 反转候选：不退出
    only_f = _result(-1.0, -0.2, 1.0, 0.3, 0.0)
    assert only_f.state.distribution["S5"] <= 0.2
    # ② F+E 双击（预期已被砍）⇒ 证伪：退出
    double_click = _result(-1.0, -1.0, 1.0, 0.0, 0.0)
    assert double_click.state.distribution["S5"] >= 0.7
    # ③ 无信号 ⇒ S0
    no_signal = _result(0.0, 0.0, 0.0, 0.0, 0.0)
    assert no_signal.state.argmax_state == "S0"
    # 两行差异只来自 E：同 F/T/V/C 下 E 从 −0.2 → −1.0，S5 从 0 跳到 0.923
    assert only_f.state.distribution["S5"] < double_click.state.distribution["S5"]


# ─────────────────────────────────────────────────────────────────────────────
# I7 顶背离对象（§6.2）
# ─────────────────────────────────────────────────────────────────────────────

_600760 = (-1.0, -0.5169, 1.0, 0.6015, -0.1902)  # F, E, T, V, C（§1.1 实帧）


def test_i7_adverse_divergence_object():
    result = v2.classify_state(_scores(*_600760), order="tef")
    adverse = result.adverse_divergence
    assert adverse is not None
    assert adverse.kind == "fundamental_adverse_vs_price_strength"
    assert adverse.fundamental_severity == pytest.approx(1.0)  # sf = severe(−1) 饱和
    assert adverse.expectation_severity == pytest.approx(0.7626, abs=1e-3)  # se = severe(−0.5169)
    assert adverse.price_strength == pytest.approx(1.0)  # t_up = up(+1)
    assert adverse.severity == pytest.approx(0.76, abs=_TOL)
    assert adverse.severity == pytest.approx(
        adverse.fundamental_severity * adverse.expectation_severity * adverse.price_strength
    )
    assert "顶背离" in adverse.description
    # 同帧分布与归一化熵（§8.5）
    assert result.state.argmax_state == "S5"
    assert result.state.distribution["S5"] == pytest.approx(0.795, abs=_TOL)
    assert result.state.entropy == pytest.approx(0.800, abs=_TOL)
    assert result.entropy_normalized == pytest.approx(0.447, abs=_TOL)
    assert result.factor_consistency == Consistency.DIVERGENT.value
    assert result.divergence_reason == "基本面与预期同时实质恶化而价格仍强（顶背离）"


@pytest.mark.parametrize(
    "frame",
    [
        (1.0, 1.0, 1.0, -1.0, 1.0),  # 三共振：无背离
        (-1.0, -1.0, -1.0, -1.0, 1.0),  # 价格也破位（T<0）：不是「价格仍强」，无顶背离
        (0.05, -0.05, 0.1, 0.0, 0.0),  # 无信号
        (0.0, -0.5, 0.8, 0.0, 0.0),  # 价格先行背离：F 不恶化，不构成顶背离
        (1.0, -0.5, -0.5, 0.0, 0.0),  # 业绩好股价不涨：F 强，不构成顶背离
        (-1.0, -0.2, 1.0, 0.3, 0.0),  # 仅 F 崩：E 未进入严重区（死区），不构成顶背离
    ],
)
def test_i7_non_adverse_frames_have_no_object(frame):
    assert v2.classify_state(_scores(*frame), order="tef").adverse_divergence is None


# ─────────────────────────────────────────────────────────────────────────────
# I8 uncertain 语义（D4-A：顶背离是「确定的坏消息」，不算 uncertain）
# ─────────────────────────────────────────────────────────────────────────────


def test_i8_uncertain_semantics():
    # ① P_top < 阈值 ⇒ uncertain（无状态主导）
    vague = v2.classify_state(_scores(-1.0, -0.2, 1.0, 0.3, 0.0), order="tef")
    assert _p_top(vague) < v2.UNCERTAIN_P_TOP
    assert vague.uncertain is True
    assert any(task.id == "research-uncertain" for task in vague.research_tasks)

    # ② 顶背离 ⇒ 不是 uncertain（P_top 高且 adverse 非 None 两条都不成立）
    adverse = v2.classify_state(_scores(*_600760), order="tef")
    assert _p_top(adverse) >= v2.UNCERTAIN_P_TOP
    assert adverse.adverse_divergence is not None
    assert adverse.uncertain is False
    assert any(task.id == "research-adverse" for task in adverse.research_tasks)

    # ③ 价格先行背离（非顶背离）⇒ uncertain + research-divergent
    price_lead = v2.classify_state(_scores(0.0, -0.5, 0.8, 0.0, 0.0), order="tef")
    assert price_lead.factor_consistency == Consistency.DIVERGENT.value
    assert price_lead.adverse_divergence is None
    assert price_lead.uncertain is True
    assert any(task.id == "research-divergent" for task in price_lead.research_tasks)

    # ④ 三共振且 P_top 高 ⇒ 不是 uncertain
    resonant = v2.classify_state(_scores(1.0, 1.0, 1.0, -1.0, 1.0), order="tef")
    assert resonant.factor_consistency == Consistency.RESONANT.value
    assert resonant.uncertain is False


def test_i8_uncertain_frame_always_has_a_research_task():
    """无 dead-end：v2 的 uncertain 两个来源各自都必有研究任务（见 _research_tasks docstring）。"""
    for scores in _grid_scores():
        result = v2.classify_state(scores, order="tef")
        if result.uncertain:
            assert result.research_tasks, scores


# ─────────────────────────────────────────────────────────────────────────────
# I9 防「全零 ⇒ 假均分」（S0 底噪的作用）
# ─────────────────────────────────────────────────────────────────────────────


def test_i9_no_zero_evidence_uniform():
    for scores in _grid_scores():
        evidence = v2.state_evidence(scores, order="tef")
        assert max(evidence.values()) > 0.0, scores
        distribution_map = v2.distribution(v2.normalized_evidence(evidence))
        # 任何输入都不该出现 6 状态各 1/6 的「假均分」（全零证据的 softmax 结果）
        assert not all(abs(p - 1.0 / len(_STATES)) < 1e-12 for p in distribution_map.values())


# ─────────────────────────────────────────────────────────────────────────────
# §8.5 验收帧（六帧，P/H/bear_mass 容差 ±0.02）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("frame", "argmax", "p_top", "entropy"),
    [
        (_600760, "S5", 0.795, 0.800),
        ((1.0, 1.0, 1.0, -1.0, 1.0), "S3", 0.850, 0.597),
        ((-1.0, -1.0, -1.0, -1.0, 1.0), "S5", 0.773, 0.726),
        ((0.05, -0.05, 0.1, 0.0, 0.0), "S0", 0.892, 0.516),
        ((-1.0, -1.0, 1.0, 0.0, 0.0), "S5", 0.923, 0.381),
    ],
)
def test_acceptance_frames(frame, argmax, p_top, entropy):
    result = v2.classify_state(_scores(*frame), order="tef")
    assert result.state.argmax_state == argmax
    assert _p_top(result) == pytest.approx(p_top, abs=_TOL)
    assert result.state.entropy == pytest.approx(entropy, abs=_TOL)


def test_acceptance_frame_600760_bear_mass():
    result = v2.classify_state(_scores(*_600760), order="tef")
    assert _bear_mass(result) == pytest.approx(0.819, abs=_TOL)


def test_acceptance_frame_perfect_bear_mass():
    result = v2.classify_state(_scores(-1.0, -1.0, -1.0, -1.0, 1.0), order="tef")
    assert _bear_mass(result) == pytest.approx(0.943, abs=_TOL)


def test_acceptance_frame_turnaround_candidate():
    """困境反转候选：S2 为主但 P_top=0.365 ⇒ uncertain，且不退出（S5 ≤ 0.2）。"""
    result = v2.classify_state(_scores(-1.0, -0.2, 1.0, 0.3, 0.0), order="tef")
    assert result.state.argmax_state == "S2"
    assert _p_top(result) == pytest.approx(0.365, abs=_TOL)
    assert result.uncertain is True
    assert result.state.distribution["S5"] <= 0.2


# ─────────────────────────────────────────────────────────────────────────────
# 载体字段 / 参数 / 算子契约
# ─────────────────────────────────────────────────────────────────────────────


def test_entropy_normalized_is_entropy_over_ln6():
    result = v2.classify_state(_scores(*_600760), order="tef")
    assert result.entropy_normalized == pytest.approx(result.state.entropy / math.log(6))


def test_state_evidence_exposed_is_normalized_evidence():
    result = v2.classify_state(_scores(*_600760), order="tef")
    raw = v2.state_evidence(_scores(*_600760), order="tef")
    assert result.state_evidence == pytest.approx(v2.normalized_evidence(raw))
    assert set(result.state_evidence) == set(_STATES)


def test_research_task_contract():
    """研究任务触发口径（§5.4）：id / importance / decides / status 与 v1 一致，adverse 排最前。

    两条规则（与 ``_research_tasks`` docstring 逐条对应）：

    - ``research-divergent`` 对**任何** divergent 触发（含顶背离，v1 口径不变）；顶背离帧
      **同时**产出 ``research-adverse`` 与 ``research-divergent``；
    - ``research-uncertain`` 仅由「无状态主导（P_top < 阈值）」触发，不由背离触发。
    """
    # 顶背离帧：research-adverse 排最前，且与 research-divergent 同时产出（v1 结构不变）
    top = v2.classify_state(_scores(*_600760), order="tef")
    assert top.adverse_divergence is not None
    assert [task.id for task in top.research_tasks] == ["research-adverse", "research-divergent"]
    tasks = {task.id: task for task in top.research_tasks}
    assert tasks["research-divergent"].importance == "high"
    assert tasks["research-divergent"].decides == ["thesis_state"]
    assert tasks["research-divergent"].status == "open"
    assert "顶背离" in tasks["research-divergent"].unknown  # 背离文案与 adverse 同源
    adverse_task = tasks["research-adverse"]
    assert adverse_task.importance == "high"
    assert adverse_task.decides == ["thesis_state"]
    assert adverse_task.status == "open"
    assert "顶背离" in adverse_task.unknown

    # 背离但 P_top 仍高（「业绩好股价不涨」帧，P_top≈0.897）：uncertain=True 但只发
    # research-divergent —— research-uncertain 仅由无状态主导（P_top<阈值）触发
    confident_divergent = v2.classify_state(_scores(1.0, -0.5, -1.0, -1.0, -1.0), order="tef")
    assert confident_divergent.factor_consistency == Consistency.DIVERGENT.value
    assert confident_divergent.adverse_divergence is None
    assert max(confident_divergent.state.distribution.values()) >= v2.UNCERTAIN_P_TOP
    assert confident_divergent.uncertain is True
    assert [task.id for task in confident_divergent.research_tasks] == ["research-divergent"]

    # 无状态主导（P_top < 阈值）：只发 research-uncertain
    vague = v2.classify_state(_scores(-1.0, -0.2, 1.0, 0.3, 0.0), order="tef")
    assert [task.id for task in vague.research_tasks] == ["research-uncertain"]
    assert vague.research_tasks[0].importance == "medium"


def test_operator_missing_value_semantics():
    """缺失值口径（§4.2 尾段）：up/dn → 0；neutral_band/not_yet → 0.5；severe → 0。"""
    assert v2.up(None) == 0.0
    assert v2.dn(None) == 0.0
    assert v2.neutral_band(None, v2.W_E) == 0.5
    assert v2.not_yet(None, v2.W_E) == 0.5
    assert v2.severe(None) == 0.0
    assert v2.neutral_band(0.0, v2.W_E) == 1.0
    assert v2.neutral_band(v2.W_E, v2.W_E) == 0.0
    assert v2.neutral_band(v2.W_E / 2.0, v2.W_E) == pytest.approx(0.5)
    assert v2.not_yet(0.0, v2.W_E) == 1.0
    assert v2.not_yet(v2.W_E, v2.W_E) == 0.0
    assert v2.severe(0.0) == 0.0
    assert v2.severe(-v2.SEVERE_LO) == 0.0  # 死区
    assert v2.severe(-v2.SEVERE_HI) == 1.0
    assert v2.severe(-(v2.SEVERE_LO + v2.SEVERE_HI) / 2.0) == pytest.approx(0.5)

    # 全缺失帧：S0 底噪 + 0.5 口径仍给出非退化分布（不制造虚假证据，也不假均分）
    result = v2.classify_state(_scores(), order="tef")
    assert result.state.argmax_state == "S0"
    assert max(result.state_evidence["S0"], result.state_evidence["S1"]) > 0.0


@pytest.mark.parametrize("bad_order", ["", "ETF ", "TEF", "both", None])
def test_invalid_order_raises_value_error(bad_order):
    with pytest.raises(ValueError):
        v2.classify_state(_scores(), order=bad_order)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        v2.state_evidence(_scores(), order=bad_order)  # type: ignore[arg-type]


def test_etf_and_tef_share_non_gated_states():
    """顺序只翻转 S2/S4/S5 的门控落点；S0/S1/S3 逐值相等（沿用 v1 的 2×2 隔离口径）。"""
    for scores in _grid_scores():
        tef_evidence = v2.state_evidence(scores, order="tef")
        etf_evidence = v2.state_evidence(scores, order="etf")
        for state in ("S0", "S1", "S3"):
            assert tef_evidence[state] == pytest.approx(etf_evidence[state])


def test_module_is_self_contained():
    """§5.1：只 import ``alphabee.midterm.models``，不 import v1 分类器（对比隔离）。"""
    source = Path(v2.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "alphabee.midterm.models" in imported
    for forbidden in ("alphabee.midterm.classifier", "alphabee.midterm.classifier_tef", "classifier"):
        assert forbidden not in imported


# ─────────────────────────────────────────────────────────────────────────────
# 迁移 / drift 接线（复用 previous.argmax_state，与 v1 同构）
# ─────────────────────────────────────────────────────────────────────────────


def _belief(argmax: str, distribution: dict[str, float] | None = None) -> StateBelief:
    return StateBelief(
        distribution=distribution or {argmax: 1.0},
        argmax_state=argmax,
        entropy=0.0,
    )


def test_previous_drives_transition_and_drift():
    previous = _belief("S2", {state: (1.0 if state == "S2" else 0.0) for state in _STATES})
    result = v2.classify_state(_scores(f=-1.0, e=-1.0, t=1.0), order="tef", previous=previous)
    assert result.transition is not None
    assert result.transition.from_state == "S2"
    assert result.transition.to_state == "S5"
    assert result.transition.legal is False  # S2→S5 跳级非法（迁移集合与 v1 同值）
    assert result.transition.kind == "illegal"
    assert result.state.drift is not None
    assert result.state.drift["S5"] > 0.0
    assert result.state.drift["S2"] < 0.0

    # 无 previous ⇒ transition/drift 均为 None（与 v1 同口径）
    no_previous = v2.classify_state(_scores(f=-1.0, e=-1.0, t=1.0), order="tef")
    assert no_previous.transition is None
    assert no_previous.state.drift is None


# ─────────────────────────────────────────────────────────────────────────────
# 载体默认值 / 旧帧回落（models.py 的 additive 契约，本步一次落盘）
# ─────────────────────────────────────────────────────────────────────────────


def test_artifact_and_result_additive_defaults():
    old_artifact_payload = {"symbol": "600760.SH", "as_of_date": "2026-10-30", "classifier_order": "tef"}
    artifact = CompanyStateArtifact.model_validate(old_artifact_payload)
    assert artifact.schema_version == "1"  # additive ⇒ 不升版本
    assert artifact.classifier_formula == "v1"
    assert artifact.entropy_normalized is None
    assert artifact.uncertain is False
    assert artifact.factor_consistency == ""
    assert artifact.divergence_reason == ""
    assert artifact.adverse_divergence is None
    assert artifact.state_evidence == {}
    assert artifact.factor_direction is None

    # 新帧（JSON 往返）保持字段
    result = v2.classify_state(_scores(*_600760), order="tef")
    new_artifact = CompanyStateArtifact(
        symbol="600760.SH",
        as_of_date="2026-10-30",
        state=result.state,
        classifier_formula=v2.FORMULA_REVISION,
        entropy_normalized=result.entropy_normalized,
        uncertain=result.uncertain,
        factor_consistency=result.factor_consistency,
        divergence_reason=result.divergence_reason,
        adverse_divergence=result.adverse_divergence,
        state_evidence=result.state_evidence,
        factor_direction=-0.5826,
    )
    restored = CompanyStateArtifact.model_validate(new_artifact.model_dump(mode="json"))
    assert restored.adverse_divergence is not None
    assert restored.adverse_divergence.kind == "fundamental_adverse_vs_price_strength"
    assert restored.entropy_normalized == pytest.approx(result.entropy_normalized)
    assert restored.state_evidence == pytest.approx(result.state_evidence)

    # ClassifierResult 的 5 个 additive 字段全部有默认值 ⇒ 旧构造方式仍然合法
    legacy_result = ClassifierResult(
        state=result.state,
        transition=None,
        factor_deltas=[],
        uncertain=False,
    )
    assert legacy_result.factor_consistency == ""
    assert legacy_result.divergence_reason == ""
    assert legacy_result.entropy_normalized is None
    assert legacy_result.adverse_divergence is None
    assert legacy_result.state_evidence == {}


# ─────────────────────────────────────────────────────────────────────────────
# §5.2 背离分类学：五个优先级各自可命中（顺序型随 order 翻转）
# ─────────────────────────────────────────────────────────────────────────────

_ADVERSE_REASON = "基本面与预期同时实质恶化而价格仍强（顶背离）"


@pytest.mark.parametrize(
    ("order", "frame", "consistency", "reason"),
    [
        # ① 顶背离：价格仍强 + F/E 同时实质恶化（两序均判，顺序无关）
        ("tef", (-1.0, -0.6, 1.0, 0.0, 0.0), Consistency.DIVERGENT, _ADVERSE_REASON),
        ("etf", (-1.0, -0.6, 1.0, 0.0, 0.0), Consistency.DIVERGENT, _ADVERSE_REASON),
        # ② 业绩好但股价不涨（沿用 v1 文案）
        ("tef", (1.0, -0.5, -0.5, 0.0, 0.0), Consistency.DIVERGENT, "业绩好但股价不涨：F 强而 E/T 弱"),
        ("etf", (1.0, -0.5, -0.5, 0.0, 0.0), Consistency.DIVERGENT, "业绩好但股价不涨：F 强而 E/T 弱"),
        # ③ 顺序型：tef = T 先转强 / etf = E 先上修
        ("tef", (0.0, -0.5, 0.8, 0.0, 0.0), Consistency.DIVERGENT, "价格先行转强 vs 预期未跟上：T 转强而 E 未上修"),
        ("etf", (0.0, 0.8, -0.5, 0.0, 0.0), Consistency.DIVERGENT, "业绩上修 vs 股价相对走弱：E 强上修而 T 短期走弱"),
        # ④ 三共振
        ("tef", (1.0, 1.0, 1.0, 0.0, 0.0), Consistency.RESONANT, ""),
        ("etf", (1.0, 1.0, 1.0, 0.0, 0.0), Consistency.RESONANT, ""),
        # ⑤ 其余
        ("tef", (0.0, 0.0, 0.0, 0.0, 0.0), Consistency.INDEPENDENT, ""),
        ("etf", (0.0, 0.0, 0.0, 0.0, 0.0), Consistency.INDEPENDENT, ""),
        # ① 优先于 ③：F/E 都进入严重区时先判顶背离（不是「价格先行」观察格）
        ("tef", (-1.0, -1.0, 0.8, 0.0, 0.0), Consistency.DIVERGENT, _ADVERSE_REASON),
    ],
)
def test_divergence_taxonomy_priority(order, frame, consistency, reason):
    result = v2.classify_state(_scores(*frame), order=order)
    assert result.factor_consistency == consistency.value
    assert result.divergence_reason == reason
    assert (result.adverse_divergence is not None) == (reason == _ADVERSE_REASON)


@pytest.mark.parametrize(
    "frame", [(None, -1.0, 1.0, 0.0, 0.0), (-1.0, None, 1.0, 0.0, 0.0), (-1.0, -1.0, None, 0.0, 0.0)]
)
def test_divergence_missing_factor_is_independent(frame):
    """F/E/T 任一缺失 ⇒ 无法判定（independent / 空文案 / 无顶背离），不制造背离证据。"""
    result = v2.classify_state(_scores(*frame), order="tef")
    assert result.factor_consistency == Consistency.INDEPENDENT.value
    assert result.divergence_reason == ""
    assert result.adverse_divergence is None
