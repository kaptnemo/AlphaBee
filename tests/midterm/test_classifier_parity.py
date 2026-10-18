"""classifier_tef 与 classifier 的 parity 与差异方向钉（TEF design §7.2）。

- 中性口径相等：网格扫描（含缺失/边界）钉住 S0/S1/S3 证据逐值相等；在顺序
  决定性位置未触发的点上，entropy 与 argmax 也逐值相等。
- 差异方向钉：S2/S4/S5 的 TEF/ETF 差异方向符合 §3.2（不钉具体数值，钉方向）。
- 迁移口径钉：同一 previous + 同一 scores，两套 transition.legal/kind 一致。
- 三变异杀伤：把 TEF S4 改回 E、S5 改加法式、背离 case ② 翻转回 E↑T↓ 必红。
"""

from __future__ import annotations

import itertools

import pytest

from alphabee.midterm import classifier as etf
from alphabee.midterm import classifier_tef as tef
from alphabee.midterm.models import Consistency, StateBelief, VariableScores

_STATES = ("S0", "S1", "S2", "S3", "S4", "S5")
_GRID = (None, -1.0, -0.3, 0.0, 0.3, 1.0)


def _scores(f=None, e=None, t=None, v=None, c=None, r=None) -> VariableScores:
    return VariableScores(
        f_fundamental_trend=f,
        e_revision=e,
        t_relative_strength=t,
        v_valuation_percentile=v,
        c_crowding=c,
        r_risk=r,
    )


def _belief(argmax: str) -> StateBelief:
    return StateBelief(distribution={argmax: 1.0}, argmax_state=argmax, entropy=0.0)


def _iter_grid_scores():
    for f, e, t, v, c in itertools.product(_GRID, repeat=5):
        yield _scores(f=f, e=e, t=t, v=v, c=c)


def _tef_evidence(scores: VariableScores) -> dict[str, float]:
    return tef._state_evidence(scores)  # noqa: SLF001


def _etf_evidence(scores: VariableScores) -> dict[str, float]:
    return etf._state_evidence(scores)  # noqa: SLF001


# ─────────────────────────────────────────────────────────────────────────────
# 中性口径相等钉（§7.2 第一条）
# ─────────────────────────────────────────────────────────────────────────────


def test_grid_s0_s1_s3_evidence_and_invariant_entropy_argmax_parity():
    invariant_points = 0
    for scores in _iter_grid_scores():
        tef_ev = _tef_evidence(scores)
        etf_ev = _etf_evidence(scores)
        for state in ("S0", "S1", "S3"):
            assert tef_ev[state] == pytest.approx(etf_ev[state]), (state, scores)

        # 顺序决定性位置（S2/S4/S5）不触发的输入上，两套完整分布逐值相等，
        # 因此 entropy / argmax 也必须逐值相等（钉软状态机器的中性口径）。
        if all(abs(tef_ev[s] - etf_ev[s]) < 1e-9 for s in _STATES):
            invariant_points += 1
            tef_result = tef.classify_state(scores)
            etf_result = etf.classify_state(scores)
            assert tef_result.state.entropy == pytest.approx(etf_result.state.entropy)
            assert tef_result.state.argmax_state == etf_result.state.argmax_state

    assert invariant_points > 0  # 网格必须含顺序无关点（全缺失 / 三共振等）


# ─────────────────────────────────────────────────────────────────────────────
# 差异方向钉（§7.2 第二条，不钉数值、钉方向）
# ─────────────────────────────────────────────────────────────────────────────


def test_s2_difference_direction():
    scores = _scores(f=1.0, e=0.0, t=1.0)
    assert _tef_evidence(scores)["S2"] > _etf_evidence(scores)["S2"]


def test_s4_difference_direction_gate_and_leading():
    gate = _scores(e=-1.0, t=0.0, v=-1.0, c=-1.0)
    assert _tef_evidence(gate)["S4"] < _etf_evidence(gate)["S4"]  # 价格未破位 → TEF 更保守

    leading = _scores(e=None, t=-1.0, v=-1.0)
    assert _tef_evidence(leading)["S4"] > _etf_evidence(leading)["S4"]  # 价格破位 → TEF 先导


def test_s5_difference_direction():
    gate = _scores(f=-1.0, e=-1.0, t=0.0)
    assert _tef_evidence(gate)["S5"] < _etf_evidence(gate)["S5"]  # 无价格确认 → TEF 不退出


# ─────────────────────────────────────────────────────────────────────────────
# 迁移口径钉（§7.2 第三条）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("previous", "scores"),
    [
        (_belief("S2"), _scores(f=0.8, e=0.8, t=0.8)),  # 前进 S2→S3
        (_belief("S3"), _scores(f=0.6, e=-0.5, t=-0.5, v=0.7)),  # reopen S3→S1'
        (_belief("S1"), _scores(f=0.8, e=0.8, t=0.8)),  # 跳级 S1→S3 illegal
    ],
)
def test_transition_caliber_parity(previous, scores):
    tef_transition = tef.classify_state(scores, previous=previous).transition
    etf_transition = etf.classify_state(scores, previous=previous).transition
    assert tef_transition is not None and etf_transition is not None
    assert tef_transition.legal == etf_transition.legal
    assert tef_transition.kind == etf_transition.kind


# ─────────────────────────────────────────────────────────────────────────────
# 三变异杀伤（§7.2 第四条）
# ─────────────────────────────────────────────────────────────────────────────


def test_mutation_kill_s4_revert_to_e_would_flip_gate_direction():
    # 变异：把 TEF S4 门控改回 E（等价 ETF）。真实 TEF 在价格未破位时 S4=0。
    scores = _scores(e=-1.0, t=0.0, v=-1.0, c=-1.0)
    real = _tef_evidence(scores)["S4"]
    mutant = _etf_evidence(scores)["S4"]  # S4 = e_not_up * (t_down + v_down + c_down)
    assert real == 0.0
    assert mutant > 0.0
    assert real != mutant  # 差异方向钉必红


def test_mutation_kill_s5_additive_would_flip_gate_direction():
    # 变异：把 TEF S5 改成加法式 t_down + e_down + 0.5*f_down。
    scores = _scores(f=-1.0, e=-1.0, t=0.0)
    real = _tef_evidence(scores)["S5"]
    mutant = (
        tef._down(scores.t_relative_strength)
        + tef._down(scores.e_revision)
        + 0.5
        * tef._down(  # noqa: SLF001
            scores.f_fundamental_trend
        )
    )
    assert real == 0.0
    assert mutant > 0.0
    assert real != mutant  # S5 门控钉必红


def test_mutation_kill_divergence_mirror_flip_would_break_mirror():
    # 变异：把 TEF 背离 case ② 翻转回 E↑+T↓（ETF 口径）。
    def mutant_divergent(f, e, t) -> bool:
        e_up = e is not None and e > tef._UP_T  # noqa: SLF001
        t_dn = t is not None and t < tef._DOWN_T  # noqa: SLF001
        return e_up and t_dn

    real_mirror = tef.classify_state(_scores(f=0.0, e=-0.5, t=0.8))
    real_independent = tef.classify_state(_scores(f=0.0, e=0.8, t=-0.5))

    assert any(d.consistency == Consistency.DIVERGENT for d in real_mirror.factor_deltas)
    assert not mutant_divergent(0.0, -0.5, 0.8)  # 变异在 T↑E↓ 上判不出背离
    independent_deltas = {d.factor: d for d in real_independent.factor_deltas}
    assert independent_deltas["E"].consistency == Consistency.INDEPENDENT
    assert independent_deltas["T"].consistency == Consistency.INDEPENDENT
    assert mutant_divergent(0.0, 0.8, -0.5)  # 变异会把 E↑T↓ 误判为背离
