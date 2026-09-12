"""bayes.py 的单元测试：log-odds 置信度更新、State×Confidence 三情景概率、缺失契约。

全部用合成 ``EvidenceEvent`` / 状态字符串，不发起网络请求，确定性验证。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.bayes import (
    ScenarioProbability,
    scenario_probability,
    update_confidence,
)
from alphabee.midterm.models import EvidenceEvent


def _ev(effect: str, delta: float, idx: int = 0) -> EvidenceEvent:
    return EvidenceEvent(
        id=f"e{idx}",
        date="2024-01-01",
        kind="expectation",
        description="",
        effect_on_thesis=effect,
        confidence_delta=delta,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Confidence（log-odds 更新）
# ─────────────────────────────────────────────────────────────────────────────


def test_confirming_raises_posterior():
    post = update_confidence([_ev("confirming", 0.5)], prior=0.5)
    assert post == pytest.approx(0.75)


def test_refuting_lowers_posterior():
    post = update_confidence([_ev("refuting", 0.5)], prior=0.5)
    # 改造 C：refuting 放大 1.2×，比对称（0.25）更低
    assert post == pytest.approx(0.2111, abs=1e-3)


def test_neutral_unchanged():
    post = update_confidence([_ev("neutral", 0.5)], prior=0.6)
    assert post == pytest.approx(0.6)


def test_posterior_in_unit_interval():
    for effect in ("confirming", "refuting", "neutral"):
        post = update_confidence([_ev(effect, 0.9), _ev("confirming", 0.8)], prior=0.3)
        assert post is not None
        assert 0.0 < post < 1.0


def test_multiple_events_accumulate():
    post = update_confidence([_ev("confirming", 0.5), _ev("confirming", 0.5)], prior=0.5)
    assert post > 0.75  # 两条确认证据比单条更强


def test_no_evidence_returns_prior():
    assert update_confidence([], prior=0.7) == pytest.approx(0.7)
    assert update_confidence(None, prior=0.7) == pytest.approx(0.7)


def test_no_evidence_no_prior_returns_none():
    # 无证据且无先验 → None，不静默假设 0.5
    assert update_confidence([], prior=None) is None
    assert update_confidence(None, prior=None) is None


def test_refuting_outweighs_equal_confirming():
    # 改造 C：refuting 放大 1.2×，同样强度的一正一反不再回到先验，而是略低于先验
    post = update_confidence([_ev("confirming", 0.5), _ev("refuting", 0.5)], prior=0.5)
    assert post < 0.5
    assert post == pytest.approx(0.4453, abs=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# 改造 C：置信度校准（clamp 防饱和 + refuting 加权）
# ─────────────────────────────────────────────────────────────────────────────


def test_confidence_clamped_below_max():
    # 多条强 confirming → sigmoid 饱和到 1.0 之前被 clamp 到 0.95
    post = update_confidence([_ev("confirming", 0.9) for _ in range(5)], prior=0.7)
    assert post == pytest.approx(0.95)


def test_confidence_clamped_above_min():
    # 多条强 refuting → sigmoid 饱和到 0 之前被 clamp 到 0.05
    post = update_confidence([_ev("refuting", 0.9) for _ in range(5)], prior=0.3)
    assert post == pytest.approx(0.05)


def test_refuting_weighted_stronger_than_confirming():
    # 同样强度 0.5：refuting 的 log-odds 幅度（1.2×）大于 confirming，反证更有信息量
    conf = update_confidence([_ev("confirming", 0.5)], prior=0.5)
    ref = update_confidence([_ev("refuting", 0.5)], prior=0.5)
    assert (conf - 0.5) < (0.5 - ref)  # 0.25 < 0.2889


# ─────────────────────────────────────────────────────────────────────────────
# ScenarioProbability（§5.2 表）
# ─────────────────────────────────────────────────────────────────────────────


def test_sums_to_one():
    for state in ("S0", "S1", "S2", "S3", "S4", "S5"):
        for conf in (None, 0.1, 0.5, 0.9):
            sp = scenario_probability(state, confidence=conf)
            assert sp.p_bull + sp.p_base + sp.p_bear == pytest.approx(1.0)


def test_s3_highest_bull():
    # 同一 confidence 下，S3 的 bull 概率最高（§5.2 表）
    conf = 0.7
    bulls = {s: scenario_probability(s, confidence=conf).p_bull for s in ("S1", "S2", "S3", "S4")}
    assert bulls["S3"] == max(bulls.values())


def test_s2_rising_above_s1():
    conf = 0.7
    assert scenario_probability("S2", confidence=conf).p_bull > scenario_probability("S1", confidence=conf).p_bull


def test_s4_declining_below_s3():
    conf = 0.7
    assert scenario_probability("S4", confidence=conf).p_bull < scenario_probability("S3", confidence=conf).p_bull


def test_higher_confidence_boosts_s3_bull():
    low = scenario_probability("S3", confidence=0.3).p_bull
    high = scenario_probability("S3", confidence=0.9).p_bull
    assert high > low


def test_higher_confidence_lowers_s5_bull():
    low = scenario_probability("S5", confidence=0.3).p_bull
    high = scenario_probability("S5", confidence=0.9).p_bull
    assert high < low  # 确信退出 → 更看空


def test_probability_source_state_prior():
    sp = scenario_probability("S3", confidence=None)
    assert sp.probability_source == "state_prior"


def test_no_evidence_conservative_prior():
    # 无证据 → 保守先验：S3 bull 显著下修（不再无脑 0.55），向 base/bear 收缩
    sp = scenario_probability("S3", confidence=None)
    assert sp.probability_source == "state_prior"
    assert sp.p_bull < 0.55
    assert sp.p_bull == pytest.approx(0.44, abs=1e-2)  # 0.55 → 0.44
    assert sp.p_bear > 0.15  # 保守：bear 上修


def test_has_evidence_false_forces_state_prior():
    # 显式 has_evidence=False：即使给了 confidence 也走保守先验，不标 bayes_posterior
    sp = scenario_probability("S3", confidence=0.7, has_evidence=False)
    assert sp.probability_source == "state_prior"
    assert sp.p_bull < 0.55


def test_probability_source_bayes_posterior():
    sp = scenario_probability("S3", confidence=0.7)
    assert sp.probability_source == "bayes_posterior"


def test_as_scenarios_bridge():
    sp = scenario_probability("S3", confidence=0.7)
    scenarios = sp.as_scenarios()
    assert [s.scenario for s in scenarios] == ["bull", "base", "bear"]
    assert sum(s.probability for s in scenarios) == pytest.approx(1.0)
    assert all(s.expected_return is None for s in scenarios)


def test_unknown_state_raises():
    with pytest.raises(ValueError):
        scenario_probability("S9", confidence=0.5)


# ─────────────────────────────────────────────────────────────────────────────
# 因子条件化情景概率（scenario_probability(factor_direction)，修正改造 B 证据方向符号错误）
# ─────────────────────────────────────────────────────────────────────────────


def test_s4_bullish_factor_direction_reduces_bear():
    # S4 状态先验看空（state_dir=-1），但因子方向分看多（factor_dir=+1）
    # → direction=0.5×(-1)+0.5×(+1)=0，bear 不再被状态表锁死到 0.67。
    sp = scenario_probability("S4", confidence=1.0, factor_direction=1.0)
    assert sp.p_bear < 0.5
    assert sp.p_bull > 0.3


def test_s4_bearish_factor_direction_raises_bear():
    # S4 + 因子方向分看空（factor_dir=-1）→ direction=-1，比回退状态方向（-1）等价；
    # 与无因子方向分（direction=-1）同为最看空，验证因子方向分不会把看空状态「中和」。
    bearish = scenario_probability("S4", confidence=1.0, factor_direction=-1.0)
    neutral = scenario_probability("S4", confidence=1.0, factor_direction=None)
    assert bearish.p_bear == pytest.approx(neutral.p_bear)


def test_factor_direction_none_falls_back_to_state_dir():
    # factor_direction=None 与不传等价（方向回退 state_dir，向后兼容）
    assert scenario_probability("S4", confidence=1.0) == scenario_probability(
        "S4", confidence=1.0, factor_direction=None
    )


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数 / 确定性
# ─────────────────────────────────────────────────────────────────────────────


def test_deterministic():
    events = [_ev("confirming", 0.3), _ev("refuting", 0.2)]
    assert update_confidence(events, prior=0.5) == update_confidence(events, prior=0.5)
    assert scenario_probability("S3", confidence=0.7) == scenario_probability("S3", confidence=0.7)


def test_scenario_probability_is_dataclass():
    sp = scenario_probability("S2", confidence=0.6)
    assert isinstance(sp, ScenarioProbability)
