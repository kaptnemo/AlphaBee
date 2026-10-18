"""classifier_dispatch.py 的调度、接线与 provenance 持久化钉（TEF design §7.3）。

调度面（§5.1）：fail-open（缺段/坏值/配置异常/坏显式值一律回落 etf）、显式
``classifier_order`` 优先于配置、``None`` 跟随配置；真路由哨兵与
``classifier.py`` / ``models.py`` 的 ``ClassifierResult`` re-export 同一对象。
接线面（§4.5）：``evaluate`` 先 resolve 一次再路由、``get_decision`` 透传。
provenance 持久化面（§6.1）：钉住 ``CompanyStateArtifact.classifier_order`` 由
``evaluate`` 写入解析后的确定值，且存量旧帧无该字段 load 时回落 ``"etf"``。
"""

from __future__ import annotations

import json
import logging

from alphabee.midterm import classifier as etf
from alphabee.midterm import classifier_dispatch as dispatch
from alphabee.midterm import classifier_tef as tef
from alphabee.midterm.models import CompanyStateArtifact, VariableScores


def _scores(f=None, e=None, t=None, v=None, c=None, r=None) -> VariableScores:
    return VariableScores(
        f_fundamental_trend=f,
        e_revision=e,
        t_relative_strength=t,
        v_valuation_percentile=v,
        c_crowding=c,
        r_risk=r,
    )


class _ConfigStub:
    def __init__(self, *, has_midterm: bool = True, classifier_order: str | None = None, raise_exc: bool = False):
        self._has_midterm = has_midterm
        self._classifier_order = classifier_order
        self._raise_exc = raise_exc
        if has_midterm:
            self.midterm = _MidtermStub(classifier_order)


class _MidtermStub:
    def __init__(self, classifier_order: str | None):
        self.classifier_order = classifier_order


def _set_config(monkeypatch, *, has_midterm=True, classifier_order=None, raise_exc=False):
    def _get_settings():
        if raise_exc:
            raise RuntimeError("config boom")
        return _ConfigStub(has_midterm=has_midterm, classifier_order=classifier_order)

    monkeypatch.setattr("alphabee.config.get_settings", _get_settings)


# ─────────────────────────────────────────────────────────────────────────────
# fail-open（缺段 / 坏值 / 异常 → etf + warning，绝不抛）
# ─────────────────────────────────────────────────────────────────────────────


def test_fail_open_missing_midterm_section(monkeypatch, caplog):
    _set_config(monkeypatch, has_midterm=False)
    with caplog.at_level(logging.WARNING, logger="alphabee.midterm.classifier_dispatch"):
        assert dispatch.resolve_classifier_order(None) == "etf"
    assert any("回落 etf" in r.message for r in caplog.records)


def test_fail_open_bad_config_value(monkeypatch, caplog):
    _set_config(monkeypatch, classifier_order="xxx")
    with caplog.at_level(logging.WARNING, logger="alphabee.midterm.classifier_dispatch"):
        assert dispatch.resolve_classifier_order(None) == "etf"
    assert any("非法" in r.message for r in caplog.records)


def test_fail_open_config_exception(monkeypatch, caplog):
    _set_config(monkeypatch, raise_exc=True)
    with caplog.at_level(logging.WARNING, logger="alphabee.midterm.classifier_dispatch"):
        assert dispatch.resolve_classifier_order(None) == "etf"
    assert any("不可用" in r.message for r in caplog.records)


def test_fail_open_bad_explicit_value(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="alphabee.midterm.classifier_dispatch"):
        assert dispatch.resolve_classifier_order("yyy") == "etf"
    assert any("非法值" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# 显式优先级：order 覆盖配置
# ─────────────────────────────────────────────────────────────────────────────


def test_explicit_etf_overrides_config_tef(monkeypatch):
    _set_config(monkeypatch, classifier_order="tef")
    assert dispatch.resolve_classifier_order("etf") == "etf"


def test_explicit_tef_overrides_config_etf(monkeypatch):
    _set_config(monkeypatch, classifier_order="etf")
    assert dispatch.resolve_classifier_order("tef") == "tef"


def test_none_reads_config(monkeypatch):
    _set_config(monkeypatch, classifier_order="tef")
    assert dispatch.resolve_classifier_order(None) == "tef"


# ─────────────────────────────────────────────────────────────────────────────
# 真路由（模块属性延迟 import，monkeypatch 可证伪）
# ─────────────────────────────────────────────────────────────────────────────


def test_route_tef_hits_module_attribute(monkeypatch):
    monkeypatch.setattr(tef, "classify_state", lambda scores, previous=None: "TEF_SENTINEL")
    assert dispatch.classify_state(_scores(), order="tef") == "TEF_SENTINEL"


def test_route_etf_hits_module_attribute(monkeypatch):
    monkeypatch.setattr(etf, "classify_state", lambda scores, previous=None: "ETF_SENTINEL")
    assert dispatch.classify_state(_scores(), order="etf") == "ETF_SENTINEL"


def test_route_default_is_etf(monkeypatch):
    monkeypatch.setattr(etf, "classify_state", lambda scores, previous=None: "ETF_SENTINEL")
    monkeypatch.setattr(tef, "classify_state", lambda scores, previous=None: "TEF_SENTINEL")
    assert dispatch.classify_state(_scores(), order=None) == "ETF_SENTINEL"


def test_dispatch_default_matches_etf_classifier_field_for_field():
    # 默认配置（midterm.classifier_order=etf）下，dispatch 输出与 ETF 逐字段相等
    scores = _scores(f=0.8, e=0.8, t=-0.5, v=0.5)
    assert dispatch.classify_state(scores, order=None) == etf.classify_state(scores)


# ─────────────────────────────────────────────────────────────────────────────
# evaluate + get_decision 透传（两序同输入输出差异方向符合 §3.2）
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluate_classifier_order_routes_and_differs_by_design(monkeypatch):
    from alphabee.midterm import decision_model
    from alphabee.midterm.models import FactorSnapshot

    # S2 门控翻转输入：f_up=1, e_up=0, t_up=1 ⇒ ETF argmax=S1，TEF argmax=S2
    scores = _scores(f=1.0, e=0.0, t=1.0)
    monkeypatch.setattr(decision_model, "compress_scores", lambda snapshot: scores)

    etf_artifact = decision_model.evaluate(FactorSnapshot(symbol="600519.SH"), classifier_order="etf")
    tef_artifact = decision_model.evaluate(FactorSnapshot(symbol="600519.SH"), classifier_order="tef")

    assert etf_artifact.state.argmax_state == "S1"
    assert tef_artifact.state.argmax_state == "S2"


def test_get_decision_passes_classifier_order_through(monkeypatch):
    from alphabee.midterm import decision_model, factors
    from alphabee.midterm.models import FactorSnapshot

    captured = {}

    def _fake_evaluate(snapshot, evidence=None, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        factors, "get_factor_snapshot", lambda symbol, include_market=True: FactorSnapshot(symbol=symbol)
    )
    monkeypatch.setattr(decision_model, "evaluate", _fake_evaluate)

    decision_model.get_decision("600519.SH", classifier_order="tef")
    assert captured.get("classifier_order") == "tef"


# ─────────────────────────────────────────────────────────────────────────────
# ClassifierResult 迁移 re-export 兼容（§4.1）
# ─────────────────────────────────────────────────────────────────────────────


def test_midterm_init_export_surface():
    import alphabee.midterm as midterm

    # 既有导出语义逐字保留：classify_state 仍指向 ETF 版
    assert midterm.classify_state is etf.classify_state
    # 新增导出
    assert midterm.classify_state_tef is tef.classify_state
    assert midterm.classify_state_dispatch is dispatch.classify_state
    assert midterm.resolve_classifier_order is dispatch.resolve_classifier_order


def test_classifier_result_reexport_identity_and_fields():
    from alphabee.midterm.classifier import ClassifierResult as FromClassifier
    from alphabee.midterm.models import ClassifierResult as FromModels

    assert FromClassifier is FromModels

    result = etf.classify_state(_scores(f=0.8, e=0.8, t=0.8))
    assert isinstance(result, FromClassifier)
    assert isinstance(result, FromModels)
    # 字段逐字不变
    for field_name in ("state", "transition", "factor_deltas", "uncertain", "research_tasks", "rationale"):
        assert hasattr(result, field_name)


# ─────────────────────────────────────────────────────────────────────────────
# PR-2：artifact provenance + 持久化兼容（TEF design §6.1 / §7.3 持久化行）
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluate_writes_classifier_order_to_artifact(monkeypatch):
    from alphabee.midterm import decision_model
    from alphabee.midterm.models import FactorSnapshot

    scores = _scores(f=1.0, e=0.0, t=1.0)
    monkeypatch.setattr(decision_model, "compress_scores", lambda snapshot: scores)

    tef_artifact = decision_model.evaluate(FactorSnapshot(symbol="600519.SH"), classifier_order="tef")
    assert tef_artifact.classifier_order == "tef"

    etf_artifact = decision_model.evaluate(FactorSnapshot(symbol="600519.SH"), classifier_order="etf")
    assert etf_artifact.classifier_order == "etf"


def test_old_frame_without_classifier_order_loads_as_etf(tmp_path):
    """存量 JSONL 无 classifier_order 字段 ⇒ load 回落 "etf"（历史帧确由 ETF 产出）。"""
    from alphabee.midterm import persistence

    state_dir = tmp_path / "state"
    path = state_dir / "600519.SH.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    old_payload = CompanyStateArtifact(symbol="600519.SH", as_of_date="2026-01-01").model_dump(mode="json")
    old_payload.pop("classifier_order", None)  # 模拟存量旧帧
    path.write_text(
        json.dumps({"id": "600519.SH:2026-01-01", **old_payload}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    loaded = persistence.load_artifacts("600519.SH", state_dir)
    assert len(loaded) == 1
    assert loaded[0].classifier_order == "etf"


def test_new_frame_classifier_order_roundtrip(tmp_path):
    from alphabee.midterm import persistence

    artifact = CompanyStateArtifact(symbol="600519.SH", as_of_date="2026-01-02", classifier_order="tef")
    persistence.append_artifact(artifact, tmp_path / "state")

    loaded = persistence.load_artifacts("600519.SH", tmp_path / "state")
    assert len(loaded) == 1
    assert loaded[0].classifier_order == "tef"
