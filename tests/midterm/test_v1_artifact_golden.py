"""§8.3 两段式：v1 artifact 数值零回归（golden）+ 新增字段定值（状态证据 V2 design）。

**Golden 的生成方式（冻结于本步改动前的代码）**：用本文件的 :func:`_snapshot` / :func:`_evidence`
确定性输入（固定 ``as_of_date``、固定证据、显式 ``classifier_order="etf"``，不依赖本地
``config.yaml``），调用改动前的 ``evaluate()`` **两次确认逐字节一致**，再剔除 8 个新增字段、
以 ``json.dumps(..., ensure_ascii=False, sort_keys=True, indent=2) + "\\n"`` 写入
``tests/midterm/golden/v1_artifact_golden.json``（254 行 / 7343 字节，
sha256 ``d3d2406d390e687487249bc4e1fb62c40d6bbc83a3bf99ce5487ec7302a8a691``）。

两段式断言（避免「additive 字段」与「逐字节相等」自相矛盾）：

- **断言 A（数值零回归）**：默认公式（``classifier_formula=None`` → 配置缺省 ``"v1"``）产出的
  artifact JSON **剔除新增 additive 字段后**（口径见 :func:`_canonical`）与 golden 逐字节相等；
- **断言 B（新增字段定值）**：v1 下新增字段取「无 v2 语义」的确定值，且
  ``uncertain`` / ``factor_consistency`` / ``divergence_reason`` 与 v1 分类器内部判决逐值相同。

F 边际改善（``MIDTERM_F_MARGINAL_DESIGN``）：PR-1 在 ``FundamentalFactor`` 新增 4 个
``q_*`` 单季派生字段，PR-2 在 ``CompanyStateArtifact`` 新增 ``f_marginal_mode`` 并在
``score_breakdown`` 追加边际键。三者都是 **additive 诊断/provenance 字段**（golden 冻结时
尚不存在，且不参与任何方向分/分布计算），故与顶层 8 字段、``score_breakdown`` 同口径从
golden 比较对象中剔除；它们的存在性、定值与确定性由本文件的
:func:`test_v1_artifact_q_marginal_fields_are_none_and_excluded` /
:func:`test_v1_artifact_f_marginal_mode_default_and_keys` 单独钉住。

**F 边际缺省 shadow 不影响断言 A**：缺省 ``f_marginal_mode="shadow"`` 只多算分解键，
F 逐值不变（G3/G7）；golden 的输入无多期单季字段 ⇒ 降级，分解连边际键都不写，
``score_breakdown`` 与 off 逐键相同。
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
    # F 边际模式 provenance（F-Marginal §6.1，PR-2）：additive 字段，golden 冻结时尚不存在。
    "f_marginal_mode",
)
# F 边际改善（F-Marginal §3.3）新增的 4 个单季派生字段：与 score_breakdown 同属
# additive 诊断字段（golden 冻结时尚不存在、不参与方向分/分布计算），同口径剔除。
_Q_MARGINAL_FIELDS = (
    "q_revenue_yoy",
    "q_net_profit_yoy",
    "q_gross_margin",
    "q_gross_margin_pp",
)
# score_breakdown 里的边际分解键（F-Marginal §6.1）：shadow/on 且单季数据可用时出现。
_MARGINAL_KEYS = (
    "marginal_raw",
    "marginal_quality",
    "marginal",
    "f_shadow",
    "f_shadow_delta",
    "f_marginal_source",
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
    """剔除新增字段后的规范化 JSON（断言 A 的逐字节比较口径，与 golden 生成时一致）。

    剔除两类：① 9 个 artifact 顶层字段（V2 design §6.1 的 8 个 + F-Marginal §6.1 的
    ``f_marginal_mode``）；② ``factor_snapshot.fundamental`` 内的 ``score_breakdown``
    （L1，2026-11-01 的 F 可审计分解）与 4 个 ``q_*`` 单季派生字段（F-Marginal §3.3，PR-1）——
    后两者在 golden 冻结时尚不存在，且是**诊断/派生字段**（不参与任何方向分/分布计算），
    故与顶层字段同口径剔除；它们的存在性与确定性由
    ``test_f_breakdown_written_and_deterministic`` /
    ``test_v1_artifact_q_marginal_fields_are_none_and_excluded`` /
    ``test_v1_artifact_f_marginal_mode_default_and_keys`` 单独钉住。
    """
    trimmed = {key: value for key, value in payload.items() if key not in _NEW_FIELDS}
    factor_snapshot = trimmed.get("factor_snapshot")
    if isinstance(factor_snapshot, dict):
        fundamental = factor_snapshot.get("fundamental")
        if isinstance(fundamental, dict):
            dropped = {"score_breakdown", *_Q_MARGINAL_FIELDS}
            if dropped & set(fundamental):
                trimmed["factor_snapshot"] = {
                    **factor_snapshot,
                    "fundamental": {key: value for key, value in fundamental.items() if key not in dropped},
                }
    return json.dumps(trimmed, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _golden_text() -> str:
    return _GOLDEN_PATH.read_text(encoding="utf-8")


def _golden_payload() -> dict:
    return json.loads(_golden_text())


def _pin_default_midterm_config(monkeypatch, *, formula_revision: str = "v1", classifier_order: str = "etf") -> None:
    """钉住配置缺省（三条 resolve 都在调用时 ``from alphabee.config import get_settings``）。

    stub **不含** ``f_marginal_mode`` ⇒ 走「缺字段 ⇒ settings 默认值 shadow」分支（F 仍然逐值不变）。
    """
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

    # 新增 additive 字段确实存在（否则「剔除后相等」会退化成「根本不写」的假通过）
    assert set(_NEW_FIELDS) <= set(payload)
    canonical = _canonical(payload)
    assert canonical.strip(), "golden 比较对象不得为空壳"
    assert canonical == _golden_text()
    # 剔除字段之外的关键结构仍在各自位置（golden 不是只比了空壳）
    assert payload["state"]["argmax_state"] == "S3"
    assert payload["classifier_order"] == "etf"
    assert payload["schema_version"] == "1"


def test_v1_artifact_golden_is_stable_across_runs(monkeypatch):
    """同一输入两次 evaluate ⇒ 剔除 additive 字段后逐字节一致（golden 的可复现前提）。"""
    _pin_default_midterm_config(monkeypatch)
    first = _canonical(_evaluate_golden_input().model_dump(mode="json"))
    second = _canonical(_evaluate_golden_input().model_dump(mode="json"))
    assert first == second == _golden_text()


def test_f_breakdown_written_and_deterministic(monkeypatch):
    """L1（2026-11-01）：F 可审计分解随帧落盘、两次一致，且不改变方向分本身。

    golden 里不包含 ``score_breakdown``（诊断字段，见 ``_canonical`` 口径）；本用例钉
    「存在 + 确定性 + 与方向分单一来源 + 旧帧无该字段时回落空 dict」。
    """
    _pin_default_midterm_config(monkeypatch)
    first = _evaluate_golden_input()
    second = _evaluate_golden_input()
    breakdown = first.factor_snapshot.fundamental.score_breakdown
    assert breakdown, "F 分解必须落盘（报告依赖它，不得重算）"
    assert breakdown == second.factor_snapshot.fundamental.score_breakdown
    assert breakdown["f"] == pytest.approx(first.variable_scores.f_fundamental_trend)
    # golden fixture 无现金流 ⇒ 经营现金流惩罚为 0；自由现金流已移出 F（键不存在）
    assert breakdown["ocf_penalty"] == 0.0
    assert not any("free_cashflow" in key for key in breakdown)
    # 旧帧（golden 本身）无该字段 ⇒ 回落空 dict
    old = CompanyStateArtifact.model_validate(_golden_payload())
    assert old.factor_snapshot.fundamental.score_breakdown == {}


def test_v1_artifact_q_marginal_fields_are_none_and_excluded(monkeypatch):
    """F-Marginal §3.3（PR-1）：4 个 q_* 字段随帧落盘、缺省 None，且不进入 golden 比较口径。

    golden 的输入 ``_snapshot()`` 只给 canonical 单期字段（无多期 income/fina），
    因此派生字段必须全部 None——这正是「旧形状可回放」与「additive 不改变数值」的钉子。
    """
    _pin_default_midterm_config(monkeypatch)
    payload = _evaluate_golden_input().model_dump(mode="json")
    fundamental = payload["factor_snapshot"]["fundamental"]

    for name in _Q_MARGINAL_FIELDS:
        assert name in fundamental, f"{name} 必须随帧落盘（否则报告/跟踪无从消费）"
        assert fundamental[name] is None, f"{name} 无多期数据时必须为 None（不静默造值）"
    # 剔除口径生效：剔除后才与 golden 逐字节相等（断言 A 的前提）
    assert _canonical(payload) == _golden_text()


def _snapshot_with_marginal() -> FactorSnapshot:
    """带单季派生字段的确定性帧（600760 口径）：shadow 下应落 marginal 键、F 不变。"""
    snap = _snapshot()
    snap.fundamental = FundamentalFactor(
        revenue_yoy=-34.19,
        net_profit_yoy=-26.0,
        eps_growth_yoy=-28.0,
        roe=18.5,
        operating_cashflow=1.026e9,
        q_revenue_yoy=23.29,
        q_net_profit_yoy=137.0,
        q_gross_margin=9.63,
        q_gross_margin_pp=-3.4,
    )
    return snap


def test_v1_artifact_f_marginal_mode_default_and_keys(monkeypatch):
    """F-Marginal §6.1（PR-2）：缺省 shadow 落 marginal 键但不改 F；off 不落键；两次运行一致。

    断言 A/B 之外的另一半：**additive 字段确实被写入**（否则「剔除后相等」会退化成
    「根本不写」的假通过），且 off 与 shadow 的 F 逐值相同（G-7 影子不生效）。
    """
    _pin_default_midterm_config(monkeypatch)  # stub 无 f_marginal_mode 字段 ⇒ 缺省 shadow
    snap_shadow = _snapshot_with_marginal()
    shadow = evaluate(snap_shadow, _evidence(), prior_confidence=0.5, thesis=_THESIS, classifier_order="etf")
    again = evaluate(
        _snapshot_with_marginal(), _evidence(), prior_confidence=0.5, thesis=_THESIS, classifier_order="etf"
    )
    off = evaluate(
        _snapshot_with_marginal(),
        _evidence(),
        prior_confidence=0.5,
        thesis=_THESIS,
        classifier_order="etf",
        f_marginal_mode="off",
    )

    assert shadow.f_marginal_mode == "shadow"
    assert off.f_marginal_mode == "off"
    breakdown = shadow.factor_snapshot.fundamental.score_breakdown
    for key in _MARGINAL_KEYS:
        assert key in breakdown, f"shadow 帧缺边际键 {key}"
    assert breakdown["marginal"] == pytest.approx(0.2)  # M_raw=1 × Q=0.2 的质量门
    # 该 golden 帧无业绩预告区间 ⇒ 无 beat：f_shadow = 0.7×L(-0.9333) + 0.3×M(0.2) = -0.5933
    assert breakdown["f_shadow"] == pytest.approx(0.7 * (-0.933333) + 0.3 * 0.2, abs=1e-4)
    # G-7：shadow 只算不进 F —— F 与 off 逐值相等（abs=0）
    assert shadow.variable_scores.f_fundamental_trend == off.variable_scores.f_fundamental_trend
    assert breakdown["f"] == pytest.approx(off.factor_snapshot.fundamental.score_breakdown["f"])
    # off：分解不含任何边际键（旧口径逐键不变）
    off_breakdown = off.factor_snapshot.fundamental.score_breakdown
    assert not any(key.startswith(("marginal", "f_shadow", "f_marginal_source")) for key in off_breakdown)
    # 确定性：同一输入两次运行分解逐值一致
    assert again.factor_snapshot.fundamental.score_breakdown == breakdown
    assert again.f_marginal_mode == "shadow"
    # G-7 的强化口径：shadow 与 off 的 artifact 在剔除 additive 字段后**逐字节相同**
    # （边际只多诊断键，不改变任何方向分/状态/EV；不同输入才与 golden 逐字节相等）
    assert _canonical(shadow.model_dump(mode="json")) == _canonical(off.model_dump(mode="json"))


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
    # F 边际（F-Marginal §3.3/§6.1）：旧帧无 q_* / f_marginal_mode 键 ⇒ 回落 None / "off"
    fundamental = artifact.factor_snapshot.fundamental
    assert all(getattr(fundamental, name) is None for name in _Q_MARGINAL_FIELDS)
    assert artifact.f_marginal_mode == "off"  # 旧帧语义 = 未记录/未启用


# ─────────────────────────────────────────────────────────────────────────────
# 断言 B：v1 下 8 个新增字段的定值
# ─────────────────────────────────────────────────────────────────────────────


def test_v1_artifact_new_fields_are_fixed(monkeypatch):
    _pin_default_midterm_config(monkeypatch)
    artifact = _evaluate_golden_input()

    # v2 专属语义在 v1 下必须为空（不制造 v2 证据）
    assert artifact.classifier_formula == "v1"
    # F 边际 provenance（F-Marginal §6.1）：配置 stub 缺字段 ⇒ 缺省 shadow（F 仍逐值不变）
    assert artifact.f_marginal_mode == "shadow"
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
