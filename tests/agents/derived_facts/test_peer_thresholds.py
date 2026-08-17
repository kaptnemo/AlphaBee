"""衍生规则对标组三级回退链测试（COMPANY_TRACK Phase D，D5）。

契约：``peer_*``（对标组）→ ``industry_*``（申万基线）→ 绝对阈值；
前序表达式引用的字段缺失时抛异常 → 顺延下一级（registry.py 表达式列表语义）。
「无对标组」行为与 Phase 0/1 完全一致（向后兼容）。
"""

from alphabee.agents.derived_facts.registry import RULES, load_rules


def _compute(rule_name: str, facts: dict[str, float]) -> str:
    load_rules()
    return RULES[rule_name].compute(dict(facts))["level"]


# ── debt_ratio：peer → industry → 绝对 ─────────────────────────────────────


def test_debt_ratio_peer_relative_aggressive():
    # 值 0.70 vs peer 均值 0.50 → >0.60 → aggressive（peer 相对判断生效）
    facts = {
        "total_liabilities": 0.70,
        "total_assets": 1.0,
        "peer_avg_debt_ratio": 0.50,
    }
    assert _compute("debt_ratio", facts) == "aggressive"


def test_debt_ratio_same_level_peer_priority():
    # 同一档位内 peer 表达式先于 industry：值 0.30 vs peer 0.45（<0.36 → conservative）
    # 即便 industry 0.10 会判 moderate，peer 的 conservative 在档位扫描中先命中。
    # 注：跨档位冲突（peer 判 aggressive 而 industry 判 moderate）按档位扫描序
    # conservative→moderate→aggressive 解析（表达式列表回退链机制语义，见路线图 D5 说明）。
    facts = {
        "total_liabilities": 0.30,
        "total_assets": 1.0,
        "peer_avg_debt_ratio": 0.45,
        "industry_avg_debt_ratio": 0.10,
    }
    assert _compute("debt_ratio", facts) == "conservative"


def test_debt_ratio_peer_relative_moderate():
    facts = {
        "total_liabilities": 0.50,
        "total_assets": 1.0,
        "peer_avg_debt_ratio": 0.45,  # 0.36 <= 0.50 <= 0.54 → moderate
    }
    assert _compute("debt_ratio", facts) == "moderate"


def test_debt_ratio_no_peer_falls_back_to_industry():
    # 无 peer → 顺延 industry（Phase 1 行为不变）
    facts = {
        "total_liabilities": 0.50,
        "total_assets": 1.0,
        "industry_avg_debt_ratio": 0.45,
    }
    assert _compute("debt_ratio", facts) == "moderate"


def test_debt_ratio_no_peer_no_industry_absolute():
    # 无 peer 无 industry → 绝对阈值（Phase 0 行为不变）
    assert _compute("debt_ratio", {"total_liabilities": 0.72, "total_assets": 1.0}) == "aggressive"
    assert _compute("debt_ratio", {"total_liabilities": 0.50, "total_assets": 1.0}) == "moderate"
    assert _compute("debt_ratio", {"total_liabilities": 0.30, "total_assets": 1.0}) == "conservative"


# ── roe_level：peer → industry → 绝对 ──────────────────────────────────────


def test_roe_level_peer_first_overrides_industry():
    # 值 0.09：peer 0.05（≥0.075 → excellent）会覆盖 industry 0.10（good）
    facts = {
        "net_profit": 0.09,
        "avg_shareholders_equity": 1.0,
        "peer_avg_roe": 0.05,
        "industry_avg_roe": 0.10,
    }
    assert _compute("roe_level", facts) == "excellent"


def test_roe_level_peer_relative_good():
    facts = {
        "net_profit": 0.06,
        "avg_shareholders_equity": 1.0,
        "peer_avg_roe": 0.05,  # 0.04 <= 0.06 < 0.075 → good
    }
    assert _compute("roe_level", facts) == "good"


def test_roe_level_peer_weak_when_below_peer():
    facts = {
        "net_profit": 0.03,
        "avg_shareholders_equity": 1.0,
        "peer_avg_roe": 0.05,  # 0.03 < 0.04 → weak（对标组视角）
    }
    assert _compute("roe_level", facts) == "weak"


def test_roe_level_no_peer_no_industry_absolute():
    assert _compute("roe_level", {"net_profit": 0.06, "avg_shareholders_equity": 1.0}) == "weak"
    assert _compute("roe_level", {"net_profit": 0.18, "avg_shareholders_equity": 1.0}) == "excellent"
