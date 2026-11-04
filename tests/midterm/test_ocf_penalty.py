"""P1-2/P1-5：OCF 惩罚改 TTM + 金融股跳过 的评分层单元测试。

口径依据 ``MIDTERM_F_DATA_GOVERNANCE_DESIGN`` §8/§11：

- ``basis="sign"``（score_engine 函数缺省）= 报告期累计 OCF 符号（旧行为，回滚口径）；
- ``basis="ttm"``（生产默认，由 decision_model 显式传入）= 滚动 12 个月 OCF：
  601668-like（累计 −453.93 亿、TTM +446.23 亿）⇒ 0；601012-like（TTM −9.27 亿）⇒ −0.5；
  600036-like（金融股）⇒ 0（跳过）；TTM 缺失 ⇒ 回落 sign 并留痕 ``ocf_penalty_basis=0``。

全部合成数据，无网络；直接调用 score_engine（函数缺省 sign），TTM 由 keyword 显式开启。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.models import ExpectationFactor, FactorSnapshot, FundamentalFactor
from alphabee.midterm.score_engine import (
    _OCF_NEGATIVE_PENALTY,
    _marginal,
    compress_scores,
    fundamental_breakdown,
)

# 设计 §8.2 实测（单位：元）：累计 vs TTM。
_ZHONGGUOJIANTOU_CUM = -453.93e8  # 601668 累计 OCF
_ZHONGGUOJIANTOU_TTM = 446.23e8  # 601668 TTM OCF（正 ⇒ 无惩罚）
_LONGI_CUM = -34.67e8  # 601012 累计 OCF
_LONGI_TTM = -9.27e8  # 601012 TTM OCF（真实负 ⇒ 仍惩罚）
_BANK_CUM = -500.0e8  # 600036 累计 OCF（金融股）
_BANK_TTM = -400.0e8  # 600036 TTM OCF（金融股）


def _factor(**overrides) -> FundamentalFactor:
    """最小可用 F 输入：三项 YoY 均为 0（L=0），只让 OCF 修正项影响 F。"""
    values: dict = {"revenue_yoy": 0.0, "net_profit_yoy": 0.0, "eps_growth_yoy": 0.0}
    values.update(overrides)
    return FundamentalFactor(**values)


def _bd(f: FundamentalFactor, *, basis: str | None = None) -> dict:
    e = ExpectationFactor()
    if basis is None:
        return fundamental_breakdown(f, e)
    return fundamental_breakdown(f, e, ocf_penalty_basis=basis)


# ─────────────────────────────────────────────────────────────────────────────
# 四类实测用例（§8.2）
# ─────────────────────────────────────────────────────────────────────────────


def test_601668_like_ttm_positive_no_penalty():
    """601668-like：累计 −453.93 亿但 TTM +446.23 亿 ⇒ TTM 口径无惩罚（旧 sign 有）。"""
    f = _factor(operating_cashflow=_ZHONGGUOJIANTOU_CUM, operating_cashflow_ttm=_ZHONGGUOJIANTOU_TTM)

    ttm = _bd(f, basis="ttm")
    assert ttm["ocf_penalty"] == 0.0
    assert ttm["f"] == pytest.approx(0.0)  # L=0，无 OCF 惩罚
    assert ttm["ocf_ttm"] == pytest.approx(_ZHONGGUOJIANTOU_TTM)
    assert ttm["ocf_penalty_basis"] == 1.0  # TTM 口径生效
    assert ttm["financial_sector"] == 0.0

    sign = _bd(f, basis="sign")
    assert sign["ocf_penalty"] == pytest.approx(-_OCF_NEGATIVE_PENALTY)  # 旧口径：累计为负
    assert sign["f"] == pytest.approx(-_OCF_NEGATIVE_PENALTY)
    assert sign["ocf_penalty_basis"] == 0.0
    assert "ocf_ttm" in sign  # 诊断值仍随帧落盘（口径 provenance 独立于惩罚）
    # 函数缺省 = sign（旧行为零变化）
    assert _bd(f) == sign


def test_601012_like_ttm_negative_still_penalized():
    """601012-like：累计 −34.67 亿、TTM −9.27 亿（真实负）⇒ TTM 口径仍 −0.5。"""
    f = _factor(operating_cashflow=_LONGI_CUM, operating_cashflow_ttm=_LONGI_TTM)
    ttm = _bd(f, basis="ttm")

    assert ttm["ocf_penalty"] == pytest.approx(-_OCF_NEGATIVE_PENALTY)
    assert ttm["f"] == pytest.approx(-_OCF_NEGATIVE_PENALTY)
    assert ttm["ocf_ttm"] == pytest.approx(_LONGI_TTM)
    assert ttm["ocf_penalty_basis"] == 1.0


def test_600036_like_financial_skips_ttm_penalty():
    """600036-like：金融股 + TTM 负 ⇒ TTM 口径 0（跳过），financial_sector=1。"""
    f = _factor(
        operating_cashflow=_BANK_CUM,
        operating_cashflow_ttm=_BANK_TTM,
        is_financial=True,
    )
    ttm = _bd(f, basis="ttm")

    assert ttm["ocf_penalty"] == 0.0
    assert ttm["f"] == pytest.approx(0.0)
    assert ttm["ocf_penalty_basis"] == 1.0  # TTM 口径生效（金融股跳过）
    assert ttm["financial_sector"] == 1.0

    # 回滚口径 sign：金融股仍按旧符号惩罚（§11.3 config 回滚语义）
    sign = _bd(f, basis="sign")
    assert sign["ocf_penalty"] == pytest.approx(-_OCF_NEGATIVE_PENALTY)
    assert sign["ocf_penalty_basis"] == 0.0


def test_ttm_missing_falls_back_to_sign_and_marks_basis_zero():
    """TTM 缺失（缺 FY(prev)/cum(t−4)）⇒ 回落累计符号，ocf_penalty_basis=0、不写 ocf_ttm。"""
    f = _factor(operating_cashflow=_LONGI_CUM, operating_cashflow_ttm=None)
    bd = _bd(f, basis="ttm")

    assert bd["ocf_penalty"] == pytest.approx(-_OCF_NEGATIVE_PENALTY)  # 不静默免罚
    assert bd["ocf_penalty_basis"] == 0.0  # 回落 sign 留痕
    assert "ocf_ttm" not in bd  # 缺失不写（不把 None 伪装成数值）


@pytest.mark.parametrize("basis", ["sign", "SIGN", "bogus", "", None])
def test_invalid_or_sign_basis_keeps_legacy_behaviour(basis):
    """非法/缺失口径 ⇒ 归一为 sign（旧行为），不得静默升级为 TTM。"""
    f = _factor(operating_cashflow=_ZHONGGUOJIANTOU_CUM, operating_cashflow_ttm=_ZHONGGUOJIANTOU_TTM)
    bd = _bd(f, basis=basis)

    assert bd["ocf_penalty"] == pytest.approx(-_OCF_NEGATIVE_PENALTY)
    assert bd["ocf_penalty_basis"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 金融股边际层显式降级（§11.2）
# ─────────────────────────────────────────────────────────────────────────────


def _marginal_ready_factor(**overrides) -> FundamentalFactor:
    values: dict = {
        "revenue_yoy": 10.0,
        "net_profit_yoy": 10.0,
        "eps_growth_yoy": 10.0,
        "q_revenue_yoy": 40.0,  # Δ_rev = 30pp
        "q_net_profit_yoy": 40.0,  # Δ_np = 30pp
        "q_gross_margin_pp": 2.0,  # 质量门满格
    }
    values.update(overrides)
    return FundamentalFactor(**values)


def test_financial_stock_marginal_is_explicitly_none():
    """金融股 ⇒ _marginal=None（显式，不依赖毛利率缺失的偶然性），且不写任何边际键。"""
    financial = _marginal_ready_factor(is_financial=True)
    non_financial = _marginal_ready_factor(is_financial=False)

    assert _marginal(financial) is None
    assert _marginal(non_financial) is not None  # 对照组：同样 q_* 下非金融可算

    for mode in ("shadow", "on"):
        bd = fundamental_breakdown(financial, ExpectationFactor(), marginal_mode=mode)
        assert bd is not None
        assert not any(key.startswith(("marginal", "f_shadow", "f_marginal_source")) for key in bd)
        assert bd["financial_sector"] == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# compress_scores 透传（生产路径由 decision_model 显式传入 ttm）
# ─────────────────────────────────────────────────────────────────────────────


def _snapshot(fundamental: FundamentalFactor) -> FactorSnapshot:
    return FactorSnapshot(symbol="601668.SH", fundamental=fundamental)


def test_compress_scores_passes_ocf_basis():
    """compress_scores(ocf_penalty_basis=...) 透传到 F；缺省 sign 保持旧行为。"""
    f = _factor(operating_cashflow=_ZHONGGUOJIANTOU_CUM, operating_cashflow_ttm=_ZHONGGUOJIANTOU_TTM)
    snap = _snapshot(f)

    f_ttm = compress_scores(snap, ocf_penalty_basis="ttm").f_fundamental_trend
    f_sign = compress_scores(snap, ocf_penalty_basis="sign").f_fundamental_trend
    f_default = compress_scores(snap).f_fundamental_trend

    assert f_ttm == pytest.approx(0.0)
    assert f_sign == pytest.approx(-_OCF_NEGATIVE_PENALTY)
    assert f_default == pytest.approx(f_sign)  # 函数缺省仍是旧口径
    assert f_ttm - f_sign == pytest.approx(_OCF_NEGATIVE_PENALTY)
