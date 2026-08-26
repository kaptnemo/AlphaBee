from alphabee.agents.anomaly.engine import AnomalyEngine
from alphabee.agents.anomaly.registry import CROSS_RULES, ensure_loaded
from alphabee.agents.facts.models import FinancialFacts, FinancialSnapshot


def _facts(*snapshots: FinancialSnapshot) -> FinancialFacts:
    return FinancialFacts(stock_code="002130.SZ", snapshots=list(snapshots))


def _codir_facts(current_cash: float, current_debt: float, fc_spike: bool = False) -> FinancialFacts:
    """构造 high_cash_high_debt 的测试快照：cash/负债历史平稳、本期飙升。

    fc_spike=False 时 financing_cashflow 全程缺失（None），用于验证“数据缺失不阻断”；
    fc_spike=True 时 financing_cashflow 历史平稳、本期飙到 200，用于验证公司事件命中。
    """
    periods = ["20241231", "20231231", "20221231", "20211231", "20201231"]
    history_cash = [100.0, 105.0, 95.0, 100.0]
    history_debt = [50.0, 52.0, 48.0, 50.0]
    history_fc = [50.0, 45.0, 55.0, 50.0] if fc_spike else [None, None, None, None]

    snaps: list[FinancialSnapshot] = []
    for i, period in enumerate(periods):
        if i == 0:
            fc = 200.0 if fc_spike else None
            snaps.append(
                FinancialSnapshot(
                    period=period,
                    cash=current_cash,
                    interest_bearing_debt=current_debt,
                    financing_cashflow=fc,
                )
            )
        else:
            snaps.append(
                FinancialSnapshot(
                    period=period,
                    cash=history_cash[i - 1],
                    interest_bearing_debt=history_debt[i - 1],
                    financing_cashflow=history_fc[i - 1],
                )
            )
    return _facts(*snaps)


def test_codir_marks_synthetic_baseline_and_component_z():
    ensure_loaded()
    engine = AnomalyEngine()
    facts = _codir_facts(current_cash=130.0, current_debt=60.0)

    anomaly = engine._evaluate_codir(CROSS_RULES["high_cash_high_debt"], facts.snapshots, {})

    assert anomaly is not None
    # 合成基线必须显式标注，不能再伪装成“历史基线”。
    assert anomaly.baseline_kind == "synthetic_codir"
    assert anomaly.component_z is not None
    assert set(anomaly.component_z.keys()) == {"cash", "interest_bearing_debt"}
    # 无 financing_cashflow → 不命中公司事件。
    assert anomaly.regime_change is False

    # to_dict 契约：下游报告端能读到这三个新字段。
    d = anomaly.to_dict()
    assert d["baseline_kind"] == "synthetic_codir"
    assert d["component_z"] is not None
    assert d["regime_change"] is False


def test_codir_detects_corporate_event_and_downgrades_level():
    ensure_loaded()
    engine = AnomalyEngine()
    # 现金 + 有息负债 + 融资现金流同时正向飙升 → 命中“公司事件”，等级降一档。
    facts = _codir_facts(current_cash=130.0, current_debt=60.0, fc_spike=True)

    anomaly = engine._evaluate_codir(CROSS_RULES["high_cash_high_debt"], facts.snapshots, {})

    assert anomaly is not None
    assert anomaly.regime_change is True
    # 无公司事件时本应是 high，命中后降一级到 medium。
    assert anomaly.level == "medium"


def test_detect_corporate_event_returns_false_on_missing_financing_cashflow():
    ensure_loaded()
    engine = AnomalyEngine()
    # financing_cashflow 全程缺失：detect 静默返回 False，且不阻断 codir 主流程。
    facts = _codir_facts(current_cash=130.0, current_debt=60.0)

    assert engine._detect_corporate_event(facts.snapshots, {}) is False

    anomaly = engine._evaluate_codir(CROSS_RULES["high_cash_high_debt"], facts.snapshots, {})
    assert anomaly is not None
    assert anomaly.regime_change is False
    # 未命中公司事件 → 保持原始 high 判定，不做降级。
    assert anomaly.level == "high"
