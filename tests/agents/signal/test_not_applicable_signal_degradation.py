"""Signal 层对"不适用（None）"衍生事实的降级契约测试。

背景：`valuation_risk` 的 PEG 分支依赖 `peg_ratio`，而 PEG 在净利润非正增长 /
亏损基期 / 数据不足时为 `not_applicable`（值以 None 落地）。
旧行为：条件 `peg_ratio > 3` 遇到 None 抛 TypeError → 整条信号变成 invalid，
        连 valuation_compression / pb_roe_match 两个分支一起丢掉。

契约：
- 条件引用了"键存在但值为 None"的输入 → 该档位不触发，其余条件继续评估；
- 结果里记录 unavailable_inputs，便于报告说明口径；
- 真正写错的条件（引用了不存在的字段）仍然判 invalid，不静默吞掉。
"""

from pathlib import Path

from alphabee.agents.signal.engine import SignalEngine
from alphabee.agents.signal.registry import SignalRule, load_signal_rules

RULES_DIR = Path("alphabee/agents/signal/rules")


class _FakeDerivedFactsEngine:
    def __init__(self, results):
        self._results = results

    def run(self, rule_names, fact_values):
        return {name: self._results[name] for name in rule_names}


def _valuation_risk_engine(peg_ratio, valuation_compression, pb_roe_match):
    engine = SignalEngine()
    engine._df_engine = _FakeDerivedFactsEngine(
        {
            "peg_ratio": (
                {"peg_ratio": peg_ratio, "level": "overvalued"}
                if peg_ratio is not None
                else {"peg_ratio": None, "level": "not_applicable", "error": "non_positive_pe_or_profit_growth"}
            ),
            "valuation_compression": {"valuation_compression": valuation_compression, "level": "premium"},
            "pb_roe_match": {"pb_roe_match": pb_roe_match, "level": "overvalued"},
        }
    )
    return engine


def test_valuation_risk_keeps_other_branches_when_peg_not_applicable():
    load_signal_rules()
    engine = _valuation_risk_engine(peg_ratio=None, valuation_compression=1.5, pb_roe_match=2.5)

    result = engine.run(
        ["valuation_risk"],
        {"pe_ttm": 30.0, "net_profit_yoy": -20.0},
    )["valuation_risk"]

    # PEG 不可用，但估值压缩 + PB-ROE 分支仍然命中 high
    assert result["level"] == "high"
    assert result["unavailable_inputs"] == ["peg_ratio"]


def test_valuation_risk_reports_unavailable_input_on_none_level():
    load_signal_rules()
    engine = _valuation_risk_engine(peg_ratio=None, valuation_compression=0.5, pb_roe_match=1.0)

    result = engine.run(
        ["valuation_risk"],
        {"pe_ttm": 30.0, "net_profit_yoy": -20.0},
    )["valuation_risk"]

    assert result["level"] == "none"
    assert result["unavailable_inputs"] == ["peg_ratio"]
    assert "peg_ratio" in result["interpretation"]


def test_valuation_risk_compression_branch_survives_without_peg():
    # PEG 不适用时，估值压缩分支仍按自身阈值命中 low
    load_signal_rules()
    engine = _valuation_risk_engine(peg_ratio=None, valuation_compression=0.9, pb_roe_match=1.0)

    result = engine.run(
        ["valuation_risk"],
        {"pe_ttm": 30.0, "net_profit_yoy": -20.0},
    )["valuation_risk"]

    assert result["level"] == "low"
    assert result["unavailable_inputs"] == ["peg_ratio"]


def test_valuation_risk_uses_peg_when_available():
    load_signal_rules()
    engine = _valuation_risk_engine(peg_ratio=3.5, valuation_compression=0.9, pb_roe_match=1.0)

    result = engine.run(
        ["valuation_risk"],
        {"pe_ttm": 30.0, "net_profit_yoy": 10.0},
    )["valuation_risk"]

    assert result["level"] == "high"
    assert "unavailable_inputs" not in result


def test_condition_referencing_absent_field_still_invalid():
    rule = SignalRule(RULES_DIR / "valuation_risk.yaml")
    result = rule.evaluate({"pe_ttm": 30.0, "net_profit_yoy": 10.0})  # 缺 peg_ratio 等衍生事实
    assert result["level"] in {"missing_fact", "invalid"}
