"""position.py 的单元测试：三轴正交合成、软状态期望、分层相乘、赔率门槛、单股上限。

全部用合成 ``StateBelief`` / 数值，不发起网络请求，确定性验证。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.models import StateBelief
from alphabee.midterm.position import build_position


def _belief(argmax: str, distribution: dict[str, float] | None = None, entropy: float = 0.0) -> StateBelief:
    return StateBelief(
        distribution=distribution or {argmax: 1.0},
        argmax_state=argmax,
        entropy=entropy,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 动作类型（§6.3）与软状态期望（§2b.3）
# ─────────────────────────────────────────────────────────────────────────────


def test_position_band_from_actual_weight():
    """position_band 由折减后实际仓位（actual_weight）映射，而非裸 argmax_state。"""
    # S3 满置信 + 满暴露 → actual_weight=0.175 → 核心
    core = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, market_exposure=1.0)
    assert core.actual_weight == pytest.approx(0.175)
    assert core.position_band == "核心"

    # S3 满置信 + 0.6 暴露 → actual_weight=0.105 → 加仓
    add = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, market_exposure=0.6)
    assert add.position_band == "加仓"

    # S3 半置信 + 0.4 暴露 → actual_weight=0.035 → 观察（微小仓位）
    observe = build_position(_belief("S3", {"S3": 1.0}), confidence=0.5, market_exposure=0.4)
    assert observe.position_band == "观察"


def test_band_not_core_when_confidence_zero_bear_market():
    """S3 但 Confidence=0 + M熊市 → band 不再是核心（折减后为 0 → 清仓）。"""
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=0.0, market_exposure=0.3)
    assert d.stock_weight == 0.0
    assert d.actual_weight == 0.0
    assert d.position_band != "核心"
    assert d.position_band == "清仓"


def test_tiny_actual_weight_not_core():
    """折减后微小仓位（≈1%）应标观察/试探，而非核心。"""
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=0.2, market_exposure=0.3)
    assert d.actual_weight == pytest.approx(0.3 * 0.175 * 0.2)  # ≈1.05%
    assert d.actual_weight < 0.05
    assert d.position_band in ("观察", "试探")
    assert d.position_band != "核心"


def test_action_type_separated_from_position_band():
    """动作类型（argmax 粗粒度意图）与 position_band（折减后实际仓位）语义分离，有 rationale。"""
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=0.0, market_exposure=0.3)
    assert any("动作类型=核心" in r for r in d.rationale)  # argmax 意图仍为「核心」
    assert d.position_band == "清仓"  # 但折减后实际仓位为 0 → 清仓
    assert any("position_band=清仓" in r and "actual_weight" in r for r in d.rationale)


def test_band_fallback_to_stock_weight_without_exposure():
    """无市场暴露 → actual_weight=None，position_band 回退到 stock_weight（置信度折减后）。"""
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0)
    assert d.actual_weight is None
    assert d.stock_weight == pytest.approx(0.175)
    assert d.position_band == "核心"  # stock_weight=0.175 ≥ 0.15


def test_expected_weight_peaked_s3():
    # S3 单一分布 → 期望仓位带 = 核心带中点 17.5%
    d = build_position(_belief("S3", {"S3": 1.0}))
    assert d.stock_weight == pytest.approx(0.175 * 1.0 * 0.5 * 1.0)  # base × 0.175 × 0.5(中性conf) × 1.0(风险)


def test_expected_weight_over_distribution():
    # S2.5 过渡态：0.4×S2 + 0.6×S3 → 介于加仓与核心之间
    dist = {"S2": 0.4, "S3": 0.6}
    expected = 0.4 * 0.125 + 0.6 * 0.175
    d = build_position(_belief("S3", dist))
    assert d.stock_weight == pytest.approx(expected * 0.5)


def test_high_entropy_more_conservative():
    # 分布宽（熵高）→ 期望仓位低于尖峰分布的 argmax 仓位（§2b.3 无需额外规则）
    spread = {"S1": 0.2, "S2": 0.3, "S3": 0.3, "S4": 0.2}
    peaked = build_position(_belief("S3", {"S3": 1.0}))
    wide = build_position(_belief("S3", spread, entropy=1.3))
    assert wide.stock_weight < peaked.stock_weight


# ─────────────────────────────────────────────────────────────────────────────
# 三轴正交：Confidence 力度 + EV 门槛
# ─────────────────────────────────────────────────────────────────────────────


def test_confidence_scales_weight():
    base = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0).stock_weight
    half = build_position(_belief("S3", {"S3": 1.0}), confidence=0.5).stock_weight
    assert base == pytest.approx(half * 2.0)


def test_confidence_missing_neutral():
    d = build_position(_belief("S3", {"S3": 1.0}))
    assert d.stock_weight == pytest.approx(0.175 * 0.5)


def test_ev_gate_zeroes_weight():
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, risk_adjusted_ev=0.5)
    assert d.stock_weight == 0.0


def test_ev_sufficient_no_gate():
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, risk_adjusted_ev=1.5)
    assert d.stock_weight > 0.0


def test_ev_missing_no_gate():
    # EV 缺失 → 不设门槛，仓位不为 0（理由中注明）
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, risk_adjusted_ev=None)
    assert d.stock_weight > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 分层相乘 + 风险调整
# ─────────────────────────────────────────────────────────────────────────────


def test_risk_up_reduces_weight():
    base = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, risk_adjustment=0.0).stock_weight
    risky = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, risk_adjustment=-1.0).stock_weight
    assert risky < base


def test_risk_down_allows_more():
    base = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, risk_adjustment=0.0).stock_weight
    safe = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, risk_adjustment=1.0).stock_weight
    assert safe > base


def test_actual_weight_is_exposure_times_stock():
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, market_exposure=0.6)
    assert d.actual_weight == pytest.approx(0.6 * d.stock_weight)
    assert d.portfolio_exposure == pytest.approx(0.6)


def test_actual_weight_none_without_exposure():
    d = build_position(_belief("S3", {"S3": 1.0}))
    assert d.actual_weight is None


# ─────────────────────────────────────────────────────────────────────────────
# 单股上限 / 集中度 → restricted（§7.2）
# ─────────────────────────────────────────────────────────────────────────────


def test_single_stock_cap_restricts():
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0, single_stock_cap=0.10)
    assert d.restricted is True
    assert d.stock_weight == pytest.approx(0.10)


def test_no_cap_not_restricted():
    d = build_position(_belief("S3", {"S3": 1.0}), confidence=1.0)
    assert d.restricted is False


def test_s5_zero_weight():
    d = build_position(_belief("S5", {"S5": 1.0}), confidence=1.0)
    assert d.stock_weight == 0.0
    assert d.position_band == "清仓"


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数 / 确定性 / 反模式检查
# ─────────────────────────────────────────────────────────────────────────────


def test_deterministic():
    b = _belief("S3", {"S2": 0.3, "S3": 0.7}, entropy=0.6)
    assert (
        build_position(b, confidence=0.8, risk_adjusted_ev=1.2).model_dump()
        == build_position(b, confidence=0.8, risk_adjusted_ev=1.2).model_dump()
    )


def test_no_hardcoded_state_branch_in_module():
    # 反模式检查：源码中不出现 `if state == "S2"` 这类硬编码买卖分支
    src = open("alphabee/midterm/position.py", encoding="utf-8").read()
    assert 'if state == "' not in src
    assert 'if argmax == "' not in src
