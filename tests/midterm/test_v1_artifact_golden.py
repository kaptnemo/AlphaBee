"""§8.3 两段式：v1 artifact 数值零回归（golden）+ 新增字段定值（状态证据 V2 design）。

**Golden 的生成方式（冻结于本步改动前的代码）**：用本文件的 :func:`_snapshot` / :func:`_evidence`
确定性输入（固定 ``as_of_date``、固定证据、显式 ``classifier_order="etf"``，不依赖本地
``config.yaml``），调用改动前的 ``evaluate()`` **两次确认逐字节一致**，再剔除 8 个新增字段、
以 ``json.dumps(..., ensure_ascii=False, sort_keys=True, indent=2) + "\\n"`` 写入
``tests/midterm/golden/v1_artifact_golden.json``（254 行 / 7343 字节，
sha256 ``d3d2406d390e687487249bc4e1fb62c40d6bbc83a3bf99ce5487ec7302a8a691``）。

两段式断言（避免「additive 字段」与「逐字节相等」自相矛盾）：

- **断言 A（数值零回归）**：默认公式（``classifier_formula=None`` → 配置缺省 ``"v1"``）产出的
  artifact JSON **剔除 8 个新增字段后**与 golden 逐字节相等；
- **断言 B（新增字段定值）**：v1 下新增字段取「无 v2 语义」的确定值，且
  ``uncertain`` / ``factor_consistency`` / ``divergence_reason`` 与 v1 分类器内部判决逐值相同。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from alphabee.midterm import classifier as etf_classifier
from alphabee.midterm.decision_model import evaluate
from alphabee.midterm.models import (
    AuditSnapshot,
    CompanyStateArtifact,
    CrowdingFactor,
    EvidenceEvent,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
    MarketFactor,
    RiskFactor,
    TrendFactor,
    ValuationFactor,
)

# 本步新增的 8 个 artifact 字段（设计 §6.1）。golden 里不包含它们；断言 B 逐个钉定值。
_NEW_FIELDS = (
    "classifier_formula",
    "entropy_normalized",
    "uncertain",
    "factor_consistency",
    "divergence_reason",
    "adverse_divergence",
    "state_evidence",
    "factor_direction",
)
_GOLDEN_PATH = Path(__file__).with_name("golden") / "v1_artifact_golden.json"
_THESIS = "核心假设：份额提升与盈利改善"
_SIX_FACTORS = (
    "f_fundamental_trend",
    "e_revision",
    "t_relative_strength",
    "v_valuation_percentile",
    "c_crowding",
    "r_risk",
)


def _snapshot() -> FactorSnapshot:
    """确定性输入：全量非空因子 + 固定 as_of_date（golden 的输入，禁止改字段值）。"""
    return FactorSnapshot(
        symbol="600519.SH",
        as_of_date="2026-10-30",
        fundamental=FundamentalFactor(
            revenue_yoy=12.0,
            net_profit_yoy=18.0,
            eps_growth_yoy=15.0,
            segment_fastest_yoy=30.0,
            segment_slowest_yoy=4.0,
            roe=18.5,
        ),
        expectation=ExpectationFactor(
            eps_fy1_revision_1m=3.0,
            revision_breadth=0.6,
            coverage_count=5,
            forecast_revision_anchor=2.0,
            rating_mean=2.4,
        ),
        trend=TrendFactor(
            rs_stock_market_20d=8.0,
            rs_stock_market_60d=6.0,
            rs_stock_industry_20d=5.0,
            price_change_pct=1.2,
            industry_breadth_20=60.0,
        ),
        valuation=ValuationFactor(pe_ttm_5y_percentile=0.35, pb_5y_percentile=0.4, percentile=0.35, pe_ttm=22.0),
        crowding=CrowdingFactor(
            holder_count_change=5.0, hot_rank=80, turnover_rate_percentile=0.4, amount_pct_of_market=1.5
        ),
        risk=RiskFactor(
            pledge_ratio=10.0,
            debt_to_assets=40.0,
            audit=AuditSnapshot(audit_opinion="标准无保留意见", audit_agency="某会计师事务所"),
        ),
        market=MarketFactor(market_score=60.0, position_low=0.5, position_high=0.8, regime="震荡"),
    )


def _evidence() -> list[EvidenceEvent]:
    """确定性证据（日期落在 as_of_date 前 365 天内，避免被时效过滤）。"""
    return [
        EvidenceEvent(
            id="golden-e1",
            date="2026-09-30",
            kind="expectation",
            subject="600519:revision:eps_fy1_revision_1m",
            description="一致预期上修",
            effect_on_thesis="confirming",
            confidence_delta=0.3,
        ),
        EvidenceEvent(
            id="golden-e2",
            date="2026-10-15",
            kind="fundamental",
            description="毛利率小幅下滑",
            effect_on_thesis="refuting",
            confidence_delta=0.1,
        ),
    ]


def _canonical(payload: dict) -> str:
    """剔除 8 个新增字段后的规范化 JSON（断言 A 的逐字节比较口径，与 golden 生成时一致）。"""
    trimmed = {key: value for key, value in payload.items() if key not in _NEW_FIELDS}
    return json.dumps(trimmed, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _golden_text() -> str:
    return _GOLDEN_PATH.read_text(encoding="utf-8")


def _golden_payload() -> dict:
    return json.loads(_golden_text())


def _pin_default_midterm_config(monkeypatch, *, formula_revision: str = "v1", classifier_order: str = "etf") -> None:
    """钉住配置缺省（两条 resolve 都在调用时 ``from alphabee.config import get_settings``）。"""
    stub = SimpleNamespace(
        midterm=SimpleNamespace(classifier_order=classifier_order, formula_revision=formula_revision)
    )
    monkeypatch.setattr("alphabee.config.get_settings", lambda: stub)


def _evaluate_golden_input():
    return evaluate(_snapshot(), _evidence(), prior_confidence=0.5, thesis=_THESIS, classifier_order="etf")


# ─────────────────────────────────────────────────────────────────────────────
# 断言 A：v1 数值零回归（剔除 8 个新增字段后逐字节相等）
# ─────────────────────────────────────────────────────────────────────────────


def test_v1_artifact_golden_numeric_zero_regression(monkeypatch):
    _pin_default_midterm_config(monkeypatch)
    artifact = _evaluate_golden_input()
    payload = artifact.model_dump(mode="json")

    # 8 个新增字段确实存在（否则「剔除后相等」会退化成「根本不写」的假通过）
    assert set(_NEW_FIELDS) <= set(payload)
    canonical = _canonical(payload)
    assert canonical.strip(), "golden 比较对象不得为空壳"
    assert canonical == _golden_text()
    # 剔除字段之外的关键结构仍在各自位置（golden 不是只比了空壳）
    assert payload["state"]["argmax_state"] == "S3"
    assert payload["classifier_order"] == "etf"
    assert payload["schema_version"] == "1"


def test_v1_artifact_golden_is_stable_across_runs(monkeypatch):
    """同一输入两次 evaluate ⇒ 剔除 8 字段后逐字节一致（golden 的可复现前提）。"""
    _pin_default_midterm_config(monkeypatch)
    first = _canonical(_evaluate_golden_input().model_dump(mode="json"))
    second = _canonical(_evaluate_golden_input().model_dump(mode="json"))
    assert first == second == _golden_text()


def test_old_frame_without_new_fields_loads_with_defaults():
    """旧 JSON（golden 即旧形状）可 model_validate：8 个字段回落默认，schema_version 仍 "1"。"""
    artifact = CompanyStateArtifact.model_validate(_golden_payload())
    assert artifact.schema_version == "1"
    assert artifact.classifier_formula == "v1"
    assert artifact.entropy_normalized is None
    assert artifact.uncertain is False
    assert artifact.factor_consistency == ""
    assert artifact.divergence_reason == ""
    assert artifact.adverse_divergence is None
    assert artifact.state_evidence == {}
    assert artifact.factor_direction is None


# ─────────────────────────────────────────────────────────────────────────────
# 断言 B：v1 下 8 个新增字段的定值
# ─────────────────────────────────────────────────────────────────────────────


def test_v1_artifact_new_fields_are_fixed(monkeypatch):
    _pin_default_midterm_config(monkeypatch)
    artifact = _evaluate_golden_input()

    # v2 专属语义在 v1 下必须为空（不制造 v2 证据）
    assert artifact.classifier_formula == "v1"
    assert artifact.entropy_normalized is None
    assert artifact.adverse_divergence is None
    assert artifact.state_evidence == {}

    # factor_direction = 改动前六因子均值：用 golden 里的方向分独立算一遍对比
    golden_scores = _golden_payload()["variable_scores"]
    available = [golden_scores[name] for name in _SIX_FACTORS if golden_scores[name] is not None]
    assert available, "golden 必须包含至少一个方向分"
    assert artifact.factor_direction == pytest.approx(sum(available) / len(available))
    # 且与 artifact 自带的 variable_scores 一致（不依赖 golden 的浮点序列化）
    scores = artifact.variable_scores
    six = [
        scores.f_fundamental_trend,
        scores.e_revision,
        scores.t_relative_strength,
        scores.v_valuation_percentile,
        scores.c_crowding,
        scores.r_risk,
    ]
    six_available = [value for value in six if value is not None]
    assert artifact.factor_direction == pytest.approx(sum(six_available) / len(six_available))

    # uncertain / factor_consistency / divergence_reason 等于 v1 分类器内部已算出的值
    internal = etf_classifier.classify_state(scores)
    assert artifact.uncertain is internal.uncertain
    assert artifact.factor_consistency == internal.factor_consistency
    assert artifact.divergence_reason == internal.divergence_reason
    # 本帧（等权均值/熵高）确实触发 v1 的 uncertain，且无背离（独立）
    assert artifact.uncertain is True
    assert artifact.factor_consistency == "independent"
    assert artifact.divergence_reason == ""
