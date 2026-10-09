"""对标组判定策略回归脚本（scripts/peer_group_eval.py）纯逻辑测试。

只测确定性部分（加载/分类学特征/闸门/指标/扫参/缓存复算），不调 LLM。
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from alphabee import PROJECT_ROOT

_SCRIPT = PROJECT_ROOT / "scripts" / "peer_group_eval.py"


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("peer_group_eval_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 的字符串注解解析需要模块在 sys.modules 中注册
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cases(ev):
    loaded = ev.load_cases(ev.DEFAULT_CASES)
    taxonomy = ev._stock_taxonomy()
    for case in loaded:
        ev._attach_taxonomy(case, taxonomy)
    return loaded


def _case(cases, symbol):
    return next(case for case in cases if case.symbol == symbol)


# ── 加载 / 分类学 ───────────────────────────────────────────────────────────


def test_load_cases_parses_fixture(cases):
    symbols = {case.symbol for case in cases}
    assert {"002916.SZ", "603986.SH", "002318.SZ", "301029.SZ"} <= symbols
    assert len(_case(cases, "002916.SZ").candidates) == 9


def test_attach_taxonomy_same_l3(cases):
    # 002916 的 6 个 keep 全部同 L3；600183（drop）也同 L3 ⇒ 证明 E 单用会把上游算进来
    case = _case(cases, "002916.SZ")
    by_code = {c.code: c for c in case.candidates}
    assert all(
        by_code[c].same_l3 for c in ("002463.SZ", "300476.SZ", "002436.SZ", "603228.SH", "001389.SZ", "603920.SH")
    )
    assert by_code["600183.SH"].same_l3 is True  # 上游 CCL 同 L3，E 无法区分
    assert by_code["600584.SH"].same_l3 is False  # 下游封测


def test_taxonomy_recall_false_negative_cases(cases, ev):
    # 002318：武进不锈是最直接对标却因申万归「普钢」落在 L3 外 ⇒ E 召回有漏
    case = _case(cases, "002318.SZ")
    assert ev.taxonomy_recall(case, "l3") == pytest.approx(2 / 4)
    # 301029：唯一 keep（鼎智）不是同 L3、仅同 L2
    case = _case(cases, "301029.SZ")
    assert ev.taxonomy_recall(case, "l3") == 0.0
    assert ev.taxonomy_recall(case, "l2") == 1.0


# ── 闸门 ────────────────────────────────────────────────────────────────────


def _dims(product, customer, material=0.5, model=0.5):
    return {"product": product, "customer": customer, "material_tech": material, "business_model": model}


def test_gate_strategies(ev):
    good = ev.Decision(
        code="A", gen_overlap=0.9, dims=_dims(0.9, 0.8), judge_verdict="direct", judge_dims=_dims(0.9, 0.8)
    )
    reject = ev.Decision(
        code="B", gen_overlap=0.9, dims=_dims(0.9, 0.8), judge_verdict="reject", judge_dims=_dims(0.9, 0.8)
    )
    low_product = ev.Decision(
        code="C", gen_overlap=0.9, dims=_dims(0.1, 0.9), judge_verdict="direct", judge_dims=_dims(0.1, 0.9)
    )
    decisions = {"A": good, "B": reject, "C": low_product}

    assert ev.gate(decisions, "B_single") == {"A", "B", "C"}  # 只看 gen_overlap
    assert ev.gate(decisions, "C_dims") == {"A", "B"}  # product floor 剔 C
    assert ev.gate(decisions, "C_D_judge") == {"A"}  # judge reject 剔 B


def test_gate_min_overlap_and_weight(ev):
    # product 高但 customer 低、加权后不足阈值
    weak = ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.4, 0.2))
    assert ev.gate({"A": weak}, "C_D_judge", min_overlap=0.5) == set()
    assert ev.gate({"A": weak}, "C_D_judge", min_overlap=0.2) == {"A"}


# ── 指标 / 扫参 ─────────────────────────────────────────────────────────────


def test_prf(ev):
    labels = {"A": "keep", "B": "keep", "C": "drop"}
    assert ev.prf({"A", "C"}, labels) == (0.5, 0.5, 0.5)
    assert ev.prf({"A", "B"}, labels) == (1.0, 1.0, 1.0)


def test_evaluate_and_sweep(cases, ev):
    decisions = {}
    for case in cases:
        decisions[case.symbol] = {
            c.code: ev.Decision(code=c.code, judge_verdict="direct", judge_dims=_dims(0.9, 0.9))
            for c in case.candidates
        }
    result = ev.evaluate_strategy(cases, decisions, "C_D_judge")
    assert result["strategy"] == "C_D_judge"
    best = ev.sweep(cases, decisions)
    assert best["params"]["min_overlap"] in ev.GRID_OVERLAP
    assert 0.0 <= best["result"]["f1"] <= 1.0


# ── 缓存复算（无 LLM） ──────────────────────────────────────────────────────


def test_collect_decisions_replay_from_cache(tmp_path, monkeypatch, cases, ev):
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _case(cases, "002916.SZ")
    payload = [
        ev.Decision(
            code="002463.SZ",
            gen_overlap=0.9,
            dims=_dims(0.9, 0.8),
            judge_verdict="direct",
            judge_dims=_dims(0.9, 0.8),
        ).to_dict()
    ]
    (tmp_path / "002916.SZ.json").write_text(json.dumps(payload), encoding="utf-8")

    decisions = ev.collect_decisions(case, record=False)
    assert decisions["002463.SZ"].judge_verdict == "direct"
    assert ev.gate(decisions, "C_D_judge") == {"002463.SZ"}


def test_collect_decisions_missing_cache_raises(tmp_path, monkeypatch, cases, ev):
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path / "empty")
    case = _case(cases, "603986.SH")
    with pytest.raises(FileNotFoundError):
        ev.collect_decisions(case, record=False)


def test_render_report(cases, ev):
    decisions = {
        case.symbol: {
            c.code: ev.Decision(code=c.code, judge_verdict="direct", judge_dims=_dims(0.9, 0.9))
            for c in case.candidates
        }
        for case in cases
    }
    results = {"C_D_judge": ev.evaluate_strategy(cases, decisions, "C_D_judge")}
    report = ev.render_report(cases, results, ev._stock_taxonomy(), None, None)
    assert "# 对标组判定策略回归" in report
    assert "C_D_judge" in report
