"""P1-5：roe_level interpretation 分档感知文案（消除「12.8% 判 excellent 但文案写 ≥15%」伪冲突）。

根因：interpretation.excellent 写死「ROE ≥ 15%」，但三级回退链
（peer_avg_roe*1.5 → industry_avg_roe*1.5 → 0.15）下 12.8% 可经 industry 相对档判
excellent——判定正确、文案矛盾，在 300037 轮直接催生 high 级已验证冲突。

修复：interpretation 改为分档感知文案（相对口径优先、绝对阈值仅作回退线），
thresholds 判定逻辑逐字未动。
"""

from __future__ import annotations

from alphabee.agents.derived_facts.registry import RULES, load_rules


def _interpretation(facts: dict[str, float]) -> str:
    load_rules()
    return RULES["roe_level"].compute(dict(facts), interpretation=True)["interpretation"]


# ── 三级回退链各命中一档时，interpretation 与档位/判定口径一致 ──────────────


def test_industry_relative_excellent_no_absolute_claim():
    """industry_avg_roe*1.5 命中 excellent（12.8% vs 行业均值 8%）⇒ 文案不得出现「≥15%」。"""
    text = _interpretation({"net_profit": 0.128, "avg_shareholders_equity": 1.0, "industry_avg_roe": 0.08})
    assert "≥15%" not in text and "≥ 15%" not in text  # 两种写死口径均不得出现
    assert "1.5" in text  # 相对口径（1.5 倍）在文案中可辨识


def test_peer_relative_excellent_no_absolute_claim():
    """peer_avg_roe*1.5 命中 excellent（10% vs 对标组均值 6%）⇒ 文案不得出现「≥15%」。"""
    text = _interpretation(
        {"net_profit": 0.10, "avg_shareholders_equity": 1.0, "peer_avg_roe": 0.06, "industry_avg_roe": 0.05}
    )
    assert "≥15%" not in text and "≥ 15%" not in text
    assert "对标组" in text  # peer 档口径在文案中可辨识


def test_absolute_excellent_mentions_15pct_only_as_fallback_line():
    """绝对回退线命中 excellent（18%，无 peer/industry 字段）⇒ 文案保留 15% 回退线但非写死口径。"""
    text = _interpretation({"net_profit": 0.18, "avg_shareholders_equity": 1.0})
    assert "15%" in text
    assert "≥15%" not in text and "≥ 15%" not in text
    assert "1.5" in text  # 相对口径仍是文案主体（判别力：旧写死文案无 1.5）


def test_good_band_interpretation_matches_thresholds():
    """good 档文案与 threshold 表达式（0.8–1.5 倍 / 8%–15%）不矛盾。"""
    text = _interpretation({"net_profit": 0.06, "avg_shareholders_equity": 1.0, "industry_avg_roe": 0.05})
    assert "0.8" in text and "1.5" in text
    assert "8%" in text and "15%" in text


def test_weak_band_interpretation_matches_thresholds():
    """weak 档文案与 threshold 表达式（<0.8 倍 / <8%）不矛盾。"""
    text = _interpretation({"net_profit": 0.06, "avg_shareholders_equity": 1.0})
    assert "0.8" in text and "8%" in text


def test_all_three_bands_have_distinct_band_aware_texts():
    """三档文案互不相同且均不含写死的「≥15%」绝对宣称（判别力承载之一）。"""
    texts = {
        _interpretation({"net_profit": 0.18, "avg_shareholders_equity": 1.0}),
        _interpretation({"net_profit": 0.06, "avg_shareholders_equity": 1.0, "industry_avg_roe": 0.05}),
        _interpretation({"net_profit": 0.06, "avg_shareholders_equity": 1.0}),
    }
    assert len(texts) == 3
    assert all("≥15%" not in text and "≥ 15%" not in text for text in texts)
