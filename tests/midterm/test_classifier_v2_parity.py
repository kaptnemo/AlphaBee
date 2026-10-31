"""classifier_v2 与 v1 的 parity 钉 + §8.2 五个变异杀伤。

两部分：

1. **常量/算子 parity（§11.2 R4）**：v1/v2 是两份刻意独立的实现，共享阈值、迁移集合与中性
   纯函数必须同值 —— 否则「把 v1 的阈值手动调一下，v1/v2 对比就失去可比性」这种漂移会静默
   发生。另钉 v1 的**数值 golden**（design §1.1 的 trigram 帧分布/熵）与本步新增的两个
   additive 字段的「只透出、不改数值」口径。
2. **五个变异杀伤（§8.2）**：每个变异都用**测试内局部重算 / 等价替换**实现（生产代码只有一份
   正确实现，不落变异代码），断言「真实实现过、变异实现必红」。

`factor_direction` 的 v2 口径落在 `decision_model`（本步不接线），故这里用局部纯函数钉住
「0.7×mean(F,E)+0.3×mean(F,E,T) 与 v1 六因子均值在 600760 帧上差 ≥0.4」；接线后的场景方向
断言归 §8.4（后续步骤）。
"""

from __future__ import annotations

import itertools

import pytest

from alphabee.midterm import classifier as etf
from alphabee.midterm import classifier_tef as tef
from alphabee.midterm import classifier_v2 as v2
from alphabee.midterm.models import Consistency, StateBelief, VariableScores

_V1_MODULES = (etf, tef)
_STATES = ("S0", "S1", "S2", "S3", "S4", "S5")
_GRID_5 = (-1.0, -0.5, 0.0, 0.5, 1.0)
# 各状态证据达到结构上限的输入（与 test_classifier_v2.py 的 I4 同口径）
_CAP_FRAMES = (
    ("tef", "S0", (0.0, 0.0, 0.0, 0.0, 0.0)),
    ("tef", "S1", (1.0, 0.0, -1.0, 1.0, 0.0)),
    ("tef", "S2", (1.0, 0.0, 1.0, 0.0, 0.0)),
    ("etf", "S2", (1.0, 1.0, 0.0, 0.0, 0.0)),
    ("tef", "S3", (1.0, 1.0, 1.0, -1.0, 1.0)),
    ("tef", "S4", (0.0, 0.0, -1.0, -1.0, -1.0)),
    ("tef", "S5", (-1.0, -1.0, 1.0, 0.0, 0.0)),
)


def _scores(f=None, e=None, t=None, v=None, c=None, r=None) -> VariableScores:
    return VariableScores(
        f_fundamental_trend=f,
        e_revision=e,
        t_relative_strength=t,
        v_valuation_percentile=v,
        c_crowding=c,
        r_risk=r,
    )


def _p_top(distribution_map: dict[str, float]) -> float:
    return max(distribution_map.values())


def _dist(evidence: dict[str, float]) -> dict[str, float]:
    return v2.distribution(v2.normalized_evidence(evidence))


def _max_abs_dp(left: dict[str, float], right: dict[str, float]) -> float:
    return max(abs(left[state] - right[state]) for state in _STATES)


def _critical_cell_bear_share(evidence_fn) -> tuple[int, float]:
    """关键格（F≤−0.5 ∧ E≤−0.3 ∧ T≥0.3）中看空质量 ≥0.5 的比例（§8.1 I3b 口径）。"""
    cells = 0
    bear_dominant = 0
    for frame in itertools.product(_GRID_5, repeat=6):
        f, e, t = frame[0], frame[1], frame[2]
        if f <= -0.5 and e <= -0.3 and t >= 0.3:
            cells += 1
            distribution_map = _dist(evidence_fn(_scores(*frame)))
            if distribution_map["S4"] + distribution_map["S5"] >= 0.5:
                bear_dominant += 1
    return cells, bear_dominant / cells


# ─────────────────────────────────────────────────────────────────────────────
# 1) 常量 / 算子 / 迁移集合 parity（§11.2 R4）
# ─────────────────────────────────────────────────────────────────────────────

_SHARED_SCALARS = ("_UP_T", "_DOWN_T", "_ACTIVE_T", "_DRIFT_EPS")
_SHARED_STRUCTURES = ("_STATES", "_FACTOR_ORDER", "_FORWARD", "_BACKWARD", "_REOPEN")


def test_shared_threshold_constants_equal_v1():
    for name in _SHARED_SCALARS:
        for module in _V1_MODULES:
            assert getattr(v2, name) == getattr(module, name), name
    # S0 基线：沿用 v1 常量（§4.6「parity 钉」），v2 以 S0_BASELINE 为唯一来源
    assert v2.S0_BASELINE == pytest.approx(0.30)
    for module in _V1_MODULES:
        assert v2.S0_BASELINE == module._S0_BASELINE  # noqa: SLF001 - parity 钉 v1 私有常量
    assert v2._S0_BASELINE == v2.S0_BASELINE  # noqa: SLF001 - v1 私有名同值别名
    assert v2.DEFAULT_PARAMS.s0_baseline == v2.S0_BASELINE


def test_shared_structures_equal_v1():
    for name in _SHARED_STRUCTURES:
        for module in _V1_MODULES:
            assert getattr(v2, name) == getattr(module, name), name


def test_shared_neutral_operators_equal_v1():
    """中性纯函数同值：up/dn 逐点相等；not_yet 在 w=1.0 时退化为 v1 的 ``_not_up``。"""
    for x in (None, -1.0, -0.3, 0.0, 0.3, 1.0):
        for module in _V1_MODULES:
            assert v2.up(x) == module._up(x)  # noqa: SLF001
            assert v2.dn(x) == module._down(x)  # noqa: SLF001
            assert v2.not_yet(x, 1.0) == module._not_up(x)  # noqa: SLF001
    for x in (-1.0, -0.5, 0.0, 0.4, 1.0):
        for module in _V1_MODULES:
            assert v2.clip01(x) == module._clip01(x)  # noqa: SLF001


def test_caps_are_structural_maxima_not_parameterised():
    """上限是结构性常量（§4.4）：S5 = max(1.5, λ)；S0 = S0_BASELINE；S3 = 3（三共振）。"""
    assert v2.EVIDENCE_CAPS["S5"] == max(1.5, v2.BREAK_LAMBDA)
    assert v2.EVIDENCE_CAPS["S0"] == pytest.approx(v2.S0_BASELINE)
    assert v2.EVIDENCE_CAPS["S3"] == pytest.approx(3.0)
    assert set(v2.EVIDENCE_CAPS) == set(_STATES)


def test_v1_numeric_golden_unchanged_by_additive_fields():
    """v1 数值零回归：design §1.1 的 600760 帧分布/熵（v1 模块只多两个 additive 字段的透出）。"""
    scores = _scores(f=-1.0, e=-0.5169, t=1.0, v=0.6015, c=-0.1902, r=-0.08741)
    tef_result = tef.classify_state(scores)
    assert tef_result.state.distribution == pytest.approx(
        {"S0": 0.1298, "S1": 0.1300, "S2": 0.5264, "S3": 0.0712, "S4": 0.0712, "S5": 0.0712}, abs=1e-4
    )
    assert tef_result.state.entropy == pytest.approx(1.4327, abs=1e-4)
    etf_result = etf.classify_state(scores)
    assert etf_result.state.distribution == pytest.approx(
        {"S0": 0.1235, "S1": 0.1237, "S2": 0.0678, "S3": 0.0678, "S4": 0.0992, "S5": 0.5181}, abs=1e-4
    )
    assert etf_result.state.entropy == pytest.approx(1.4516, abs=1e-4)


def test_v1_exposes_only_two_additive_fields():
    """v1 只新增两个「透出」字段；另三个 v2 专属字段保持默认值（不改变任何数值行为）。"""
    scores = _scores(f=1.0, e=-0.5, t=-0.5)
    for module in _V1_MODULES:
        result = module.classify_state(scores)
        deltas = {delta.factor: delta for delta in result.factor_deltas}
        assert result.factor_consistency == deltas["F"].consistency.value == Consistency.DIVERGENT.value
        assert result.divergence_reason == "业绩好但股价不涨：F 强而 E/T 弱"
        assert result.entropy_normalized is None
        assert result.adverse_divergence is None
        assert result.state_evidence == {}

    # 无背离帧：两个字段分别是 independent / 空串（不是 None，下游按 str 消费）
    for module in _V1_MODULES:
        result = module.classify_state(_scores(f=1.0, e=1.0, t=1.0))
        assert result.factor_consistency == Consistency.RESONANT.value
        assert result.divergence_reason == ""


# ─────────────────────────────────────────────────────────────────────────────
# 2) §8.2 五个变异杀伤（变异全部在测试内局部重算，不落地生产代码）
# ─────────────────────────────────────────────────────────────────────────────


def _mutant_evidence_s2_single_sided(scores: VariableScores, *, order: str = "tef") -> dict[str, float]:
    """变异 1：S2 的 ``neutral_band`` 换回单边 ``not_up``（v1 的 D1 缺陷）。"""
    evidence = dict(v2.state_evidence(scores, order=order))
    f_up = v2.up(scores.f_fundamental_trend)
    if order == "tef":
        evidence["S2"] = v2.up(scores.t_relative_strength) * (f_up + v2.not_yet(scores.e_revision, v2.W_E))
    else:
        evidence["S2"] = v2.up(scores.e_revision) * (f_up + v2.not_yet(scores.t_relative_strength, v2.W_T))
    return evidence


def test_mutation_kill_s2_single_sided_breaks_i1b_and_i3():
    """变异 1（§8.2）：S2 的 ``neutral_band`` 换回单边 ``not_up`` ⇒ I1b、I3 必红。

    **收口依据（§8.1 / §8.2 的措辞张力，显式解决）**：§8.1 的 I1 只扫负域 ``F,E ∈ [−1,0]``，
    而单边变异在负域上恒等于 1.0（常数）——**I1 按定义杀不到它**（下方显式断言这一点，故不能
    声称「I1 必红」）。§8.2 的「I1 必红」由 I1b（正 → 负过渡，+0.05 → −1.00）承接：真实实现在
    +0.05 → −0.05 处持平、其后严格下降；变异恰在此处 0.9167 → 1.0 上升（D1「恶化 = 确认」），
    并破坏「S2(+0.05) > S2(−1.00)」这条 D1 反例钉。
    """
    # D1 反例钉（I1b 的严格不等式）：E 从正侧入口 +0.05 掉到完全恶化 −1.00
    positive = _scores(f=0.0, e=0.05, t=1.0)
    deteriorated = _scores(f=0.0, e=-1.0, t=1.0)
    real_positive = v2.state_evidence(positive, order="tef")["S2"]
    real_deteriorated = v2.state_evidence(deteriorated, order="tef")["S2"]
    mutant_positive = _mutant_evidence_s2_single_sided(positive)["S2"]
    mutant_deteriorated = _mutant_evidence_s2_single_sided(deteriorated)["S2"]
    assert real_positive > real_deteriorated  # I1b：正侧入口严格高于完全恶化（D1 病灶已修）
    assert mutant_positive < mutant_deteriorated  # 变异：恶化反而「确认」⇒ I1b 必红

    # I1b 的单调链（+0.05 → −0.05 → −0.10 → … → −1.00）：真实实现 S2 不增，变异在首步上升
    transition = [0.05, *(-0.05 - 0.05 * i for i in range(20))]
    real_chain = [v2.state_evidence(_scores(f=0.0, e=e, t=1.0), order="tef")["S2"] for e in transition]
    mutant_chain = [_mutant_evidence_s2_single_sided(_scores(f=0.0, e=e, t=1.0))["S2"] for e in transition]
    assert all(later <= earlier + 1e-12 for earlier, later in zip(real_chain, real_chain[1:]))
    assert any(later > earlier + 1e-12 for earlier, later in zip(mutant_chain, mutant_chain[1:]))  # ⇒ I1b 必红

    # 为什么必须补 I1b：I1 的负域扫描对变异「看不见」——not_yet(e≤0) ≡ 1，变异在负域上是常数
    negative_only = [-0.05 - 0.05 * i for i in range(20)]
    mutant_negative = [_mutant_evidence_s2_single_sided(_scores(f=0.0, e=e, t=1.0))["S2"] for e in negative_only]
    assert mutant_negative == pytest.approx([mutant_negative[0]] * len(mutant_negative))  # I1 杀不掉它
    real_negative = [v2.state_evidence(_scores(f=0.0, e=e, t=1.0), order="tef")["S2"] for e in negative_only]
    assert all(later <= earlier + 1e-12 for earlier, later in zip(real_negative, real_negative[1:]))

    # I3：E 幅度 −1 vs −0.5 —— 真实实现（中性带）对 S2 与分布都敏感，变异对 S2 完全无感
    e_low = _scores(f=0.0, e=-1.0, t=1.0)
    e_high = _scores(f=0.0, e=-0.5, t=1.0)
    assert v2.state_evidence(e_low, order="tef")["S2"] != pytest.approx(v2.state_evidence(e_high, order="tef")["S2"])
    assert _mutant_evidence_s2_single_sided(e_low)["S2"] == pytest.approx(
        _mutant_evidence_s2_single_sided(e_high)["S2"]
    )
    assert (
        _max_abs_dp(_dist(v2.state_evidence(e_low, order="tef")), _dist(v2.state_evidence(e_high, order="tef"))) >= 0.02
    )
    assert (
        _max_abs_dp(_dist(_mutant_evidence_s2_single_sided(e_low)), _dist(_mutant_evidence_s2_single_sided(e_high)))
        < 0.02
    )


def _mutant_evidence_s5_price_only(scores: VariableScores, *, order: str = "tef") -> dict[str, float]:
    """变异 2：删掉 S5 的 F×E 证伪路径（只剩价格路径 —— v1-tef 的 D2 缺陷）。"""
    evidence = dict(v2.state_evidence(scores, order=order))
    evidence["S5"] = v2.dn(scores.t_relative_strength) * (
        v2.dn(scores.e_revision) + 0.5 * v2.dn(scores.f_fundamental_trend)
    )
    return evidence


def test_mutation_kill_s5_without_falsification_breaks_i2_and_i3b():
    # I2 反例：F=E=−1, T=+1 ⇒ 真实 S5 可达；变异下价格路径为 0 ⇒ 退出不可达
    frame = _scores(f=-1.0, e=-1.0, t=1.0)
    assert v2.classify_state(frame, order="tef").state.argmax_state == "S5"
    mutant_evidence = _mutant_evidence_s5_price_only(frame)
    assert mutant_evidence["S5"] == 0.0
    assert max(_dist(mutant_evidence).items(), key=lambda item: item[1])[0] == "S0"  # ⇒ I2 必红

    # I3b：关键格看空侧 ≥0.5 的比例（真实 100%；变异下 T≥0.3 ⇒ S4=S5=0 ⇒ 必红）
    real_cells, real_share = _critical_cell_bear_share(lambda scores: v2.state_evidence(scores, order="tef"))
    mutant_cells, mutant_share = _critical_cell_bear_share(_mutant_evidence_s5_price_only)
    assert real_cells == mutant_cells == 1000
    assert real_share >= 0.90
    assert mutant_share < 0.90


def _mutant_distribution_without_normalization(scores: VariableScores, *, order: str = "tef") -> dict[str, float]:
    """变异 3：去掉上限归一化，直接 softmax 原始证据（v1 的 D3 缺陷）。"""
    return v2.distribution(v2.state_evidence(scores, order=order))


def test_mutation_kill_no_normalization_breaks_i4_and_i5():
    # I4：未归一化 ⇒ 各状态可达上限不齐（S3=3.0 / S5=2.0 / S0=0.30），
    # 「每个状态都可归一化到 1.0」的断言不再成立（缺的是那把共同的尺子）
    raw_maxima = [v2.state_evidence(_scores(*frame), order=order)[state] for order, state, frame in _CAP_FRAMES]
    normalized_maxima = [
        v2.normalized_evidence(v2.state_evidence(_scores(*frame), order=order))[state]
        for order, state, frame in _CAP_FRAMES
    ]
    assert len({round(value, 6) for value in raw_maxima}) > 1  # 变异：上限不齐 ⇒ I4 必红
    assert normalized_maxima == pytest.approx([1.0] * len(_CAP_FRAMES))

    # I5：对称性门（完美多空 P_top 差 ≤ 0.10）
    bull = _scores(f=1.0, e=1.0, t=1.0, v=-1.0, c=1.0)
    bear = _scores(f=-1.0, e=-1.0, t=-1.0, v=-1.0, c=1.0)
    real_diff = abs(
        _p_top(v2.classify_state(bull, order="tef").state.distribution)
        - _p_top(v2.classify_state(bear, order="tef").state.distribution)
    )
    mutant_diff = abs(
        _p_top(_mutant_distribution_without_normalization(bull))
        - _p_top(_mutant_distribution_without_normalization(bear))
    )
    assert real_diff <= 0.10
    assert mutant_diff > 0.10  # ⇒ I5 必红


def _mutant_evidence_without_s0_floor(scores: VariableScores, *, order: str = "tef") -> dict[str, float]:
    """变异 4：S0 去掉底噪 ``S0_FLOOR``（v1 的 S0 恒 0.30 的反面：改成无底噪的纯 neutrality）。"""
    evidence = dict(v2.state_evidence(scores, order=order))
    evidence["S0"] = (
        v2.S0_BASELINE
        * v2.neutral_band(scores.f_fundamental_trend, v2.W_F)
        * v2.neutral_band(scores.e_revision, v2.W_E)
        * v2.neutral_band(scores.t_relative_strength, v2.W_T)
    )
    return evidence


def test_mutation_kill_s0_without_floor_breaks_i9():
    # 反例帧：F=0, E=−1, T=+0.5（价格先行背离）⇒ 变异下六个状态证据全为 0
    frame = _scores(f=0.0, e=-1.0, t=0.5)
    assert max(v2.state_evidence(frame, order="tef").values()) > 0.0  # I9：底噪保证非全零
    mutant_evidence = _mutant_evidence_without_s0_floor(frame)
    assert max(mutant_evidence.values()) == 0.0  # 变异：全零 ⇒ I9 必红
    # 全零证据的 softmax 恰好是 1/6 假均分（I9 防的就是这个）
    assert _dist(mutant_evidence) == pytest.approx({state: 1.0 / 6.0 for state in _STATES})


def _factor_direction_v1(scores: VariableScores) -> float | None:
    """v1 口径：六因子等权均值（缺失按可用子集求均值，全缺 ⇒ None）。"""
    values = [
        scores.f_fundamental_trend,
        scores.e_revision,
        scores.t_relative_strength,
        scores.v_valuation_percentile,
        scores.c_crowding,
        scores.r_risk,
    ]
    available = [value for value in values if value is not None]
    return sum(available) / len(available) if available else None


def _factor_direction_v2(scores: VariableScores) -> float | None:
    """v2 口径（§6.3）：``0.7×mean(F,E) + 0.3×mean(F,E,T)``；缺失按可用子集求均值，全缺 ⇒ None。"""
    core = [value for value in (scores.f_fundamental_trend, scores.e_revision) if value is not None]
    trio = [
        value
        for value in (scores.f_fundamental_trend, scores.e_revision, scores.t_relative_strength)
        if value is not None
    ]
    parts = [(0.7, core), (0.3, trio)]
    total = 0.0
    weight = 0.0
    for part_weight, values in parts:
        if values:
            total += part_weight * sum(values) / len(values)
            weight += part_weight
    return total / weight if weight > 0.0 else None


def test_mutation_kill_factor_direction_v1_caliber_breaks_scenario_direction():
    """变异 5：``factor_direction`` 的 v2 口径换回六因子均值 ⇒ 600760 帧方向判反。

    v1 的等权均值让 ``T=+1``、``V=+0.60`` 把 ``F=−1``、``E=−0.52`` 抵消成 ≈−0.03（类别错误）；
    v2 口径只看 thesis 驱动因子（F/E）+ 时序修正（T），同帧为 −0.583。接线后的
    ``scenario_probability`` 断言归 §8.4（后续步骤），此处先钉口径差。
    """
    scores = _scores(f=-1.0, e=-0.5169, t=1.0, v=0.6015, c=-0.1902, r=-0.08741)
    v1_direction = _factor_direction_v1(scores)
    v2_direction = _factor_direction_v2(scores)
    assert v1_direction == pytest.approx(-0.0322, abs=1e-3)
    assert v2_direction == pytest.approx(-0.5826, abs=1e-4)
    assert abs(v2_direction - v1_direction) >= 0.4  # 变异（v1 口径）必红
    assert v1_direction > -0.1 and v2_direction < -0.5  # 符号/量级都判反
    # 全缺失 ⇒ None（不制造中性 0.0 假信号）
    assert _factor_direction_v2(_scores()) is None


# ─────────────────────────────────────────────────────────────────────────────
# 3) 迁移 / drift / 活跃因子与 v1 逐值同构（§5.4「复用同值常量、transition 用 previous.argmax_state」）
# ─────────────────────────────────────────────────────────────────────────────


def test_transition_rules_and_trigger_factors_equal_v1():
    scores = _scores(f=-1.0, e=-0.5, t=0.4, v=0.0, c=0.8, r=-0.4)
    for from_state in _STATES:
        for to_state in _STATES:
            v2_transition = v2._transition(  # noqa: SLF001 - parity 钉私有纯函数
                from_state, to_state, scores, Consistency.INDEPENDENT
            )
            for module in _V1_MODULES:
                v1_transition = module._transition(  # noqa: SLF001
                    from_state, to_state, scores, Consistency.INDEPENDENT
                )
                assert v2_transition.legal == v1_transition.legal, (from_state, to_state)
                assert v2_transition.kind == v1_transition.kind, (from_state, to_state)
                assert v2_transition.trigger_factors == v1_transition.trigger_factors


def test_drift_caliber_equal_v1():
    previous = StateBelief(
        distribution={"S0": 0.2, "S1": 0.2, "S2": 0.2, "S3": 0.2, "S4": 0.1, "S5": 0.1},
        argmax_state="S0",
        entropy=1.0,
    )
    distribution_map = {"S0": 0.1, "S1": 0.1, "S2": 0.3005, "S3": 0.4, "S4": 0.05, "S5": 0.0495}
    v2_drift = v2._drift(distribution_map, previous)  # noqa: SLF001
    for module in _V1_MODULES:
        assert v2_drift == module._drift(distribution_map, previous)  # noqa: SLF001
    assert v2._drift(distribution_map, previous) is not None  # noqa: SLF001
    assert v2._drift(distribution_map, None) is None  # noqa: SLF001
