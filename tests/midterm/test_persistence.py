"""persistence.py 追加式落盘单测（D3-1）。

覆盖 append→load→latest→drop round-trip、幂等 append、跨标的隔离、嵌套结构
（StateBelief / FactorSnapshot / ExpectedValue / PositionDecision）round-trip。
"""

import pytest

from alphabee.midterm.models import (
    CompanyStateArtifact,
    ExpectedValue,
    FactorSnapshot,
    FundamentalFactor,
    PositionDecision,
    ScenarioOutcome,
    StateBelief,
    VariableScores,
)
from alphabee.midterm.persistence import append_artifact, drop_artifact, latest_artifact, load_artifacts


def _artifact(
    symbol: str = "000977",
    as_of_date: str = "2026-01-01",
    thesis_confidence: float = 0.7,
) -> CompanyStateArtifact:
    return CompanyStateArtifact(
        schema_version="1",
        symbol=symbol,
        as_of_date=as_of_date,
        state=StateBelief(distribution={"S1": 0.3, "S2": 0.5, "S3": 0.2}, argmax_state="S2", entropy=1.0),
        thesis_confidence=thesis_confidence,
        variable_scores=VariableScores(e_revision=0.5, f_fundamental_trend=0.2),
        factor_snapshot=FactorSnapshot(
            symbol=symbol,
            as_of_date=as_of_date,
            fundamental=FundamentalFactor(revenue_yoy=12.5, net_profit_yoy=8.0, roe=15.0),
        ),
        expected_value=ExpectedValue(
            scenarios=[
                ScenarioOutcome(scenario="bull", probability=0.4, expected_return=20.0),
                ScenarioOutcome(scenario="base", probability=0.4, expected_return=5.0),
                ScenarioOutcome(scenario="bear", probability=0.2, expected_return=-10.0),
            ],
            ev=8.0,
            risk_adjusted_ev=2.0,
            probability_source="bayes_posterior",
        ),
        position=PositionDecision(stock_weight=0.10, actual_weight=0.08, portfolio_exposure=0.8, position_band="试探"),
    )


def test_append_returns_id(tmp_path):
    id_ = append_artifact(_artifact(), data_dir=tmp_path)
    assert id_ == "000977:2026-01-01"


def test_append_load_round_trip_nested(tmp_path):
    artifact = _artifact()
    append_artifact(artifact, data_dir=tmp_path)
    loaded = load_artifacts("000977", data_dir=tmp_path)
    assert len(loaded) == 1
    # 嵌套结构 round-trip：model_dump 语义相等
    assert loaded[0].model_dump() == artifact.model_dump()
    assert loaded[0].state.argmax_state == "S2"
    assert loaded[0].factor_snapshot.fundamental.revenue_yoy == pytest.approx(12.5)
    assert loaded[0].expected_value.ev == pytest.approx(8.0)
    assert loaded[0].position.position_band == "试探"


def test_append_is_idempotent(tmp_path):
    first = append_artifact(_artifact(), data_dir=tmp_path)
    second = append_artifact(_artifact(), data_dir=tmp_path)  # 同 symbol:date 重复追加
    assert first == second
    assert len(load_artifacts("000977", data_dir=tmp_path)) == 1  # 不产生重复行


def test_latest_artifact(tmp_path):
    append_artifact(_artifact(as_of_date="2026-01-01"), data_dir=tmp_path)
    append_artifact(_artifact(as_of_date="2026-01-05", thesis_confidence=0.8), data_dir=tmp_path)
    latest = latest_artifact("000977", data_dir=tmp_path)
    assert latest.as_of_date == "2026-01-05"
    assert latest.thesis_confidence == pytest.approx(0.8)


def test_load_empty_symbol(tmp_path):
    assert load_artifacts("600519", data_dir=tmp_path) == []
    assert latest_artifact("600519", data_dir=tmp_path) is None


def test_drop_artifact(tmp_path):
    id_ = append_artifact(_artifact(), data_dir=tmp_path)
    assert drop_artifact(id_, data_dir=tmp_path) is True
    assert load_artifacts("000977", data_dir=tmp_path) == []
    # 再次 drop 未命中 → False
    assert drop_artifact(id_, data_dir=tmp_path) is False


def test_drop_isolated_by_symbol(tmp_path):
    append_artifact(_artifact(symbol="000977", as_of_date="2026-01-01"), data_dir=tmp_path)
    append_artifact(_artifact(symbol="600519", as_of_date="2026-01-01"), data_dir=tmp_path)
    assert drop_artifact("000977:2026-01-01", data_dir=tmp_path) is True
    # 另一标的的历史不受影响
    assert len(load_artifacts("600519", data_dir=tmp_path)) == 1


def test_load_sorted_by_date(tmp_path):
    # 故意乱序追加，load 仍按日期升序
    append_artifact(_artifact(as_of_date="2026-01-05"), data_dir=tmp_path)
    append_artifact(_artifact(as_of_date="2026-01-01"), data_dir=tmp_path)
    loaded = load_artifacts("000977", data_dir=tmp_path)
    assert [a.as_of_date for a in loaded] == ["2026-01-01", "2026-01-05"]
