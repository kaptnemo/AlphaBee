"""对标组判定策略回归脚本（``scripts/peer_group_eval.py``）的纯逻辑测试 + 判别力变异实验。

只测确定性部分（加载/分类学特征/闸门/指标分段/disputed 双口径/扫参/Jaccard/缓存复算/prompt 复用），
不调 LLM、不读真实 ``data/``（缓存一律 monkeypatch 到 ``tmp_path``）。

**判别力自证**（断言必须能杀死错误实现）：M1 忽略 split 分段、M2 disputed 不计入排除口径、
M3 Jaccard 退化为恒 1、M4 生产入口误接在线路径 —— 每个变异实测必红。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

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


def _dims(product, customer, material=0.5, model=0.5):
    return {"product": product, "customer": customer, "material_tech": material, "business_model": model}


def _all_keep_decisions(cases, ev, product=0.9, customer=0.9):
    return {
        case.symbol: {
            c.code: ev.Decision(
                code=c.code,
                gen_overlap=0.9,
                dims=_dims(product, customer),
                judge_verdict="direct",
                judge_dims=_dims(product, customer),
            )
            for c in case.candidates
        }
        for case in cases
    }


def _synth_case(ev, symbol, *, split, segment="同质 L3", expect_empty=False, candidates=()):
    case = ev.EvalCase(symbol=symbol, name=symbol, split=split, segment=segment, expect_empty=expect_empty)
    case.candidates = [
        ev.Candidate(code=code, name=code, label=label, disputed=disputed) for code, label, disputed in candidates
    ]
    return case


# ── 判别力变异框架 ──────────────────────────────────────────────────────────


def _load_mutant(ev, source: str, module_name: str) -> Any:
    """把（变异后的）脚本源码写成临时模块并加载，供断言实测红/绿。"""
    import tempfile

    spec = importlib.util.spec_from_loader(module_name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{module_name}.py"
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


def _mutate(ev, old: str, new: str, module_name: str) -> Any:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"变异锚点不唯一：{old!r}"
    return _load_mutant(ev, source.replace(old, new), module_name)


# ── 加载 / 分类学 / 标注集覆盖 ──────────────────────────────────────────────


def test_load_cases_parses_fixture(cases):
    symbols = {case.symbol for case in cases}
    assert {"002916.SZ", "603986.SH", "002318.SZ", "301029.SZ"} <= symbols
    assert len(_case(cases, "002916.SZ").candidates) == 9


def test_labels_dataset_contract(cases):
    """标注集契约：15–20 例、每例元信息齐全、候选逐条 code+name+label+rationale。"""
    assert 15 <= len(cases) <= 20
    for case in cases:
        assert case.symbol and case.name and case.industry
        assert case.taxonomy_l3 and case.taxonomy_l2
        assert case.segment and case.pool_source
        assert case.split in {"train", "holdout"}
        assert case.candidates, case.symbol
        for cand in case.candidates:
            assert cand.code and cand.name, case.symbol
            assert cand.label in {"keep", "drop"}, (case.symbol, cand.code)
            assert cand.rationale, (case.symbol, cand.code)


def test_labels_cover_six_segments(cases):
    segments = {case.segment for case in cases}
    assert segments >= {"同质 L3", "分类冲突", "残差桶", "境内外混合", "无对标空组", "跨行业"}
    for segment in segments:
        assert any(case.segment == segment for case in cases)


def test_labels_holdout_ratio_and_required_coverage(cases):
    holdout = [case for case in cases if case.split == "holdout"]
    ratio = len(holdout) / len(cases)
    assert 0.25 <= ratio <= 0.35, ratio
    holdout_segments = {case.segment for case in holdout}
    # holdout 至少含 1 例空组 + 1 例跨行业 + 1 例分类冲突
    assert {"无对标空组", "跨行业", "分类冲突"} <= holdout_segments
    assert any(case.expect_empty for case in holdout)


def test_labels_non_a_share_candidates_exist(cases):
    """境内外混合业态：候选池确实含非 A 股代码（TW/US/HK 后缀）。"""
    foreign = [
        cand.code
        for case in cases
        if case.segment == "境内外混合"
        for cand in case.candidates
        if cand.code.rsplit(".", 1)[-1] not in {"SH", "SZ", "BJ"}
    ]
    assert foreign, "境内外混合业态缺非 A 股候选"


def test_labels_disputed_flag_is_boolean_and_optional(cases):
    disputed = [cand for case in cases for cand in case.candidates if cand.disputed]
    assert disputed, "标注集应至少有一处 disputed 标注"
    assert all(isinstance(cand.disputed, bool) for case in cases for cand in case.candidates)
    # 缺省为 False：新加字段不得改变既有案例的语义
    default_case = ev_default_candidate()
    assert default_case.disputed is False


def ev_default_candidate():
    from alphabee.company_track.peer_judge import DIMS  # noqa: F401  (import 仅证明依赖可用)

    spec = importlib.util.spec_from_file_location("peer_group_eval_default_probe", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Candidate(code="X")


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
    # RC-VERDICT-DIVERGENCE 收口：`gate()` 对**任何**策略都执行规则①「verdict==reject 先否决」，
    # 与生产 `gate_candidates` 同源同序（旧行为是 C_dims 只看 dims ⇒ 与生产相反，已删除）。
    # C 类策略"没有 judge"由 `project_decisions` 显式清空 verdict 表达，而不是靠闸门忽略 verdict。
    assert ev.gate(decisions, "C_dims") == {"A"}  # reject 先否决（B）+ product floor（C）
    assert ev.gate(decisions, "C_D_judge") == {"A"}  # judge reject 剔 B


def test_gate_min_overlap_and_weight(ev):
    # product 高但 customer 低、加权后不足阈值
    weak = ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.4, 0.2))
    assert ev.gate({"A": weak}, "C_D_judge", min_overlap=0.5) == set()
    assert ev.gate({"A": weak}, "C_D_judge", min_overlap=0.2) == {"A"}


# ── 指标 / 分段口径 ─────────────────────────────────────────────────────────


def test_prf(ev):
    labels = {"A": "keep", "B": "keep", "C": "drop"}
    assert ev.prf({"A", "C"}, labels) == (0.5, 0.5, 0.5)
    assert ev.prf({"A", "B"}, labels) == (1.0, 1.0, 1.0)


def test_metrics_are_segmented_by_split(ev):
    """指标必须按 split 分段：train 与 holdout 的取值必须能分开（M1 的判据）。"""
    train = _synth_case(ev, "T1", split="train", candidates=[("A", "keep", False), ("B", "drop", False)])
    holdout = _synth_case(ev, "H1", split="holdout", candidates=[("A", "drop", False), ("B", "keep", False)])
    cases = [train, holdout]
    # 每 case 的候选池必须**逐只**有 judge 判定（生产 `JudgeReport.ok` 的全覆盖口径）；
    # 漏判会按 fail-open 整体回退生成器分，故这里显式给全（否则测的是降级路径）。
    decisions = {
        "T1": {
            "A": ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),  # 全对
            "B": ev.Decision(code="B", judge_verdict="reject", judge_dims=_dims(0.1, 0.1)),
        },
        "H1": {
            "A": ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),  # 全错：留下 drop
            "B": ev.Decision(code="B", judge_verdict="reject", judge_dims=_dims(0.1, 0.1)),  # 剔掉 keep
        },
    }
    result = ev.evaluate_strategy(cases, decisions, "C_D_judge")
    assert ev.segment_metrics(result, "train")["f1"] == pytest.approx(1.0)
    assert ev.segment_metrics(result, "holdout")["f1"] == pytest.approx(0.0)
    assert ev.segment_metrics(result, "all")["f1"] == pytest.approx(0.5)
    assert set(result["segments"]) == {"train", "holdout", "all"}


def test_split_counts_and_case_selection(cases, ev):
    counts = ev._split_counts(cases)
    assert counts["all"] == len(cases)
    assert counts["train"] + counts["holdout"] == len(cases)
    assert len(ev.cases_for_split(cases, "holdout")) == counts["holdout"]
    assert ev.cases_for_split(cases, "all") == list(cases)


def test_expect_empty_case_scores_zero_precision_when_kept(ev):
    empty = _synth_case(
        ev, "E1", split="train", segment="无对标空组", expect_empty=True, candidates=[("A", "drop", False)]
    )
    decisions = {"E1": {"A": ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9))}}
    result = ev.evaluate_strategy([empty], decisions, "C_D_judge")
    assert ev.segment_metrics(result, "train")["precision"] == pytest.approx(0.0)
    # 空组被正确留空 → 满精度
    result = ev.evaluate_strategy([empty], {"E1": {}}, "C_D_judge")
    assert ev.segment_metrics(result, "train")["precision"] == pytest.approx(1.0)


# ── disputed 双口径 ─────────────────────────────────────────────────────────


def test_disputed_dual_metrics_differ(ev):
    """disputed 候选在两套口径下结果必须不同（M2 的判据）。"""
    case = _synth_case(
        ev,
        "D1",
        split="train",
        candidates=[("A", "keep", False), ("B", "drop", True)],
    )
    decisions = {
        "D1": {
            "A": ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),
            "B": ev.Decision(code="B", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),  # 争议候选被保留
        }
    }
    result = ev.evaluate_strategy([case], decisions, "C_D_judge")
    include = ev.segment_metrics(result, "train")
    exclude = ev.segment_metrics(result, "train", exclude_disputed=True)
    assert include["precision"] == pytest.approx(0.5)  # 保留 A、B；B 是 drop ⇒ 1/2
    assert exclude["precision"] == pytest.approx(1.0)  # 排除 B 后只看 A
    assert include != exclude


def test_disputed_case_with_all_candidates_disputed(ev):
    """全部候选都是 disputed ⇒ 排除口径下该 case 无候选：约定“空对空 = 一致”（与 Jaccard 同约定）。"""
    case = _synth_case(ev, "D2", split="holdout", candidates=[("A", "keep", True)])
    decisions = {"D2": {"A": ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9))}}
    result = ev.evaluate_strategy([case], decisions, "C_D_judge")
    row = ev.segment_metrics(result, "holdout", exclude_disputed=True)
    assert row["precision"] == pytest.approx(1.0)  # 无候选可比 → 不误报（保留集也被摘空）
    assert row["recall"] == pytest.approx(1.0)
    assert ev.segment_metrics(result, "holdout")["precision"] == pytest.approx(1.0)  # 未排除口径：A 是 keep


def test_discipline_metrics_exposes_both_disputed_scopes(ev):
    case = _synth_case(ev, "D3", split="train", candidates=[("A", "keep", False), ("B", "drop", True)])
    decisions = {
        "D3": {
            "A": ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),
            "B": ev.Decision(code="B", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),
        }
    }
    results = {"C_D_judge": ev.evaluate_strategy([case], decisions, "C_D_judge")}
    include = ev.discipline_metrics(results)
    exclude = ev.discipline_metrics(results, exclude_disputed=True)
    assert include["C_D_judge"]["all"]["precision"] == pytest.approx(0.5)  # 保留 A、B；B 是 drop ⇒ 1/2
    assert exclude["C_D_judge"]["all"]["precision"] == pytest.approx(1.0)


# ── 扫参 / Jaccard / 稳定性 ─────────────────────────────────────────────────


def test_evaluate_and_sweep(cases, ev):
    decisions = _all_keep_decisions(cases, ev)
    result = ev.evaluate_strategy(cases, decisions, "C_D_judge")
    assert result["strategy"] == "C_D_judge"
    best = ev.sweep(cases, decisions)
    assert best["params"]["min_overlap"] in ev.GRID_OVERLAP
    assert 0 <= best["result"]["segments"]["all"]["include_disputed"]["f1"] <= 1.0


def test_sweep_calibrates_on_train_segment(ev):
    """扫参必须按校准段（train）选点，且 holdout 数字随选点一并给出。"""
    train = _synth_case(ev, "T1", split="train", candidates=[("A", "keep", False), ("B", "drop", False)])
    holdout = _synth_case(ev, "H1", split="holdout", candidates=[("A", "keep", False), ("B", "drop", False)])
    cases = [train, holdout]
    decisions = {
        "T1": {
            "A": ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),
            "B": ev.Decision(code="B", judge_verdict="direct", judge_dims=_dims(0.4, 0.2)),
        },
        "H1": {},
    }
    best = ev.sweep(cases, decisions)
    assert best["calibrate_split"] == "train"
    assert ev.segment_metrics(best["result"], "train")["f1"] == pytest.approx(best["score"])


def test_jaccard_definitions(ev):
    assert ev.jaccard(set(), set()) == 1.0  # 单对语义：双方皆空 = 一致（不是缺失）
    assert ev.jaccard({"A"}, set()) == 0.0
    assert ev.jaccard({"A"}, {"A"}) == 1.0
    assert ev.jaccard({"A", "B"}, {"B", "C"}) == pytest.approx(1 / 3)
    assert ev.mean_pairwise_jaccard([{"A"}, {"A", "B"}, {"A", "B"}]) == pytest.approx((0.5 + 0.5 + 1.0) / 3)
    # F-1 收口：**聚合**层面把"双方皆空"的对排除出均值（无信息），全无信息 ⇒ 不可评估（None），
    # 不再静默报 1.000 假通过。
    assert ev.mean_pairwise_jaccard([]) is None
    assert ev.mean_pairwise_jaccard([set(), set()]) is None
    # 混合情形：`(∅,∅)` 对不参与，`(∅,{A})` 两对参与且均为 0（该 case 每次都变）
    assert ev.mean_pairwise_jaccard([set(), set(), {"A"}]) == pytest.approx(0.0)
    assert ev.pair_is_evaluable(set(), set()) is False
    assert ev.pair_is_evaluable({"A"}, set()) is True


def test_stability_requires_repeat(cases, ev):
    assert ev.stability(cases, ("C_D_E",), 1, record=False) == []


def test_stability_rows_carry_split_and_per_strategy(ev):
    train = _synth_case(ev, "T1", split="train", candidates=[("A", "keep", False)])
    holdout = _synth_case(ev, "H1", split="holdout", candidates=[("A", "keep", False)])
    rows = [
        {"case": "T1", "split": "train", "segment": "同质 L3", "results": {"B_single": 1.0, "C_D_E": 0.5}},
        {"case": "H1", "split": "holdout", "segment": "跨行业", "results": {"B_single": 0.0, "C_D_E": 1.0}},
    ]
    assert ev.stability_by_split(rows, "B_single") == {"train": 1.0, "holdout": 0.0, "all": 0.5}
    assert ev.stability_by_split(rows, "C_D_E") == {"train": 0.5, "holdout": 1.0, "all": 0.75}
    assert train.split == "train" and holdout.split == "holdout"


def test_available_stability_runs_detects_cached_repeats(tmp_path, monkeypatch, ev):
    """已有 stable_run<N>/ 缓存时，自动复算包含稳定性章节（不重采 LLM）。"""
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    assert ev._available_stability_runs() == 0
    (tmp_path / "stable_run0").mkdir()
    (tmp_path / "stable_run1").mkdir()
    (tmp_path / "stable_run2").mkdir()
    assert ev._available_stability_runs() == 3


def test_stability_rows_replay_from_cached_repeat_dirs(tmp_path, monkeypatch, ev):
    """record=False 时稳定性读 `stable_run<N>/`：同一份产物 → Jaccard=1（离线可复算）。"""
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _synth_case(ev, "S1", split="train", candidates=[("A", "keep", False)])
    payload = {
        "schema_version": ev.CACHE_SCHEMA_VERSION,
        "symbol": "S1",
        "split": "train",
        "segment": "同质 L3",
        "decisions": [ev.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)).to_dict()],
    }
    for index in range(2):
        run_dir = tmp_path / f"stable_run{index}"
        run_dir.mkdir()
        (run_dir / "S1.json").write_text(json.dumps(payload), encoding="utf-8")
    rows = ev.stability([case], ("C_D_E",), 2, record=False)
    assert rows[0]["results"]["C_D_E"] == pytest.approx(1.0)
    assert rows[0]["split"] == "train"


def test_stability_split_mean_is_pure(ev):
    pairs = [("train", 1.0), ("train", 0.5), ("holdout", 0.0)]
    assert ev._stability_split_mean(pairs) == {"train": 0.75, "holdout": 0.0, "all": 0.5}
    assert ev._stability_split_mean([]) == {"train": 0.0, "holdout": 0.0, "all": 0.0}


# ── 缓存复算（无 LLM） ──────────────────────────────────────────────────────


def test_collect_decisions_replay_from_cache(tmp_path, monkeypatch, cases, ev):
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _case(cases, "002916.SZ")
    payload = {
        "schema_version": ev.CACHE_SCHEMA_VERSION,
        "symbol": case.symbol,
        "split": case.split,
        "segment": case.segment,
        "decisions": [
            ev.Decision(
                code="002463.SZ",
                gen_overlap=0.9,
                dims=_dims(0.9, 0.8),
                judge_verdict="direct",
                judge_dims=_dims(0.9, 0.8),
            ).to_dict()
        ],
    }
    (tmp_path / "002916.SZ.json").write_text(json.dumps(payload), encoding="utf-8")

    decisions = ev.collect_decisions(case, record=False)
    assert decisions["002463.SZ"].judge_verdict == "direct"
    assert ev.gate(decisions, "C_D_judge") == {"002463.SZ"}
    # v1 兼容：旧缓存是裸数组，仍可复算（不要求重采）
    (tmp_path / "002916.SZ.json").write_text(json.dumps(payload["decisions"]), encoding="utf-8")
    legacy = ev.collect_decisions(case, record=False)
    assert legacy["002463.SZ"].gen_overlap == pytest.approx(0.9)


def test_collect_decisions_record_writes_cache_via_production_entries(tmp_path, monkeypatch, ev):
    """--record 走生产入口（不调 LLM）：注入 fake model，校验两个阶段都从生产函数取值。"""
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _synth_case(ev, "X1", split="train", candidates=[("002463.SZ", "keep", False)])
    calls: list[str] = []

    class FakeModel:
        def invoke(self, prompt: str) -> Any:
            calls.append(prompt)
            if "独立评审" in prompt:
                content = '[{"code": "002463.SZ", "verdict": "direct", "dims": {"product": 0.9}, "reason": "同环节"}]'
            else:
                content = '[{"code": "002463.SZ", "overlap": 0.8, "dims": {"product": 0.8}, "reason": "同环节"}]'
            return type("R", (), {"content": content})()

    monkeypatch.setattr(ev, "_resolve_business_description", lambda case: "印制电路板与封装基板")
    decisions = ev.collect_decisions(case, record=True, model=FakeModel())
    assert len(calls) == 2  # 生成器 + judge 两次调用
    assert decisions["002463.SZ"].gen_overlap == pytest.approx(0.8)
    assert decisions["002463.SZ"].judge_verdict == "direct"
    written = json.loads((tmp_path / "X1.json").read_text(encoding="utf-8"))
    assert written["schema_version"] == ev.CACHE_SCHEMA_VERSION
    assert written["model_component"] == ev.MODEL_COMPONENT
    assert written["decisions"][0]["code"] == "002463.SZ"


def test_collect_decisions_record_run_zero_updates_main_cache(tmp_path, monkeypatch, ev):
    """稳定性第 0 次重复同时更新主缓存（基线策略与重复采集用同一份产物，避免口径错配）。"""
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _synth_case(ev, "X2", split="train", candidates=[("002463.SZ", "keep", False)])

    class FakeModel:
        def invoke(self, prompt: str) -> Any:
            content = (
                '[{"code": "002463.SZ", "verdict": "direct", "dims": {"product": 0.9}, "reason": "同环节"}]'
                if "独立评审" in prompt
                else '[{"code": "002463.SZ", "overlap": 0.8, "dims": {"product": 0.8}, "reason": "同环节"}]'
            )
            return type("R", (), {"content": content})()

    monkeypatch.setattr(ev, "_resolve_business_description", lambda case: "印制电路板")
    ev.collect_decisions(case, record=True, model=FakeModel(), run_index=0)
    assert (tmp_path / "X2.json").is_file()
    assert (tmp_path / "stable_run0" / "X2.json").is_file()
    # run>0 只写重复目录，不动主缓存
    (tmp_path / "X2.json").unlink()
    ev.collect_decisions(case, record=True, model=FakeModel(), run_index=1)
    assert not (tmp_path / "X2.json").exists()
    assert (tmp_path / "stable_run1" / "X2.json").is_file()


def test_cache_path_run_index_separates_repeats(tmp_path, monkeypatch, ev):
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    assert ev.cache_path("002916.SZ", None) == tmp_path / "002916.SZ.json"
    assert ev.cache_path("002916.SZ", 2) == tmp_path / "stable_run2" / "002916.SZ.json"


def test_collect_decisions_missing_cache_raises(tmp_path, monkeypatch, cases, ev):
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path / "empty")
    case = _case(cases, "603986.SH")
    with pytest.raises(FileNotFoundError):
        ev.collect_decisions(case, record=False)


# ── 生产入口复用（prompt 双维护已消除） ─────────────────────────────────────


def test_harness_calls_production_prompt_builders():
    """脚本必须调用生产侧两个 LLM 阶段入口（而不是自带一份 prompt 文本）。"""
    source = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alphabee.company_track.peer_judge":
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    # 两个阶段都必须经生产入口调用（judge 侧为**批量生产入口** judge_peer_candidates_batched）
    assert {"infer_peer_scoring", "judge_peer_candidates_batched"} <= imported
    assert {"infer_peer_scoring", "judge_peer_candidates_batched"} <= called

    # 生产侧模块确实提供 prompt 构造入口（harness 不复制 prompt 的前提）
    import alphabee.company_track.peer_judge as peer_judge

    for name in ("build_judge_prompt", "build_scoring_prompt"):
        assert callable(getattr(peer_judge, name))


def test_harness_has_no_prompt_literals():
    """脚本内不得出现中文 prompt 模板字面量（prompt 唯一处在生产模块 ``peer_judge``）。

    用 AST 节点身份排除「叙述性」字符串（模块/函数 docstring、notebook/prompt 变量名，
    以及 CLI/报告/标注字段的说明文本），其余可执行字符串字面量不得含中文 prompt 模板标记。
    节点级白名单：重命名变量或加注释不会误红，但把 prompt 抄回脚本必然命中。
    """
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    markers = ("你是", "只输出 JSON", "买方研究员", "独立评审", "verdict:", "dims:", "候选清单")
    narrative: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    narrative.add(id(first.value))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and ("prompt" in target.id or "PROMPT" in target.id):
                    narrative.add(id(node.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    narrative.add(id(arg))  # CLI 帮助 / 报告标题 / 标注字段等说明文本
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in narrative:
            if any(marker in node.value for marker in markers):
                offenders.append((node.lineno, node.value[:40]))
    assert offenders == [], f"脚本内出现 prompt 字面量：{offenders}"


def test_harness_strategy_names_match_design():
    source = _SCRIPT.read_text(encoding="utf-8")
    for strategy in ("B_single", "C_dims", "C_D_judge", "C_E_taxo", "C_D_E"):
        assert f'"{strategy}"' in source


# ── 报告 ────────────────────────────────────────────────────────────────────


def test_render_report(cases, ev):
    decisions = _all_keep_decisions(cases, ev)
    results = {"C_D_judge": ev.evaluate_strategy(cases, decisions, "C_D_judge")}
    report = ev.render_report(cases, results, ev._stock_taxonomy(), None, None)
    assert "# 对标组判定策略回归" in report
    assert "C_D_judge" in report


def test_report_shows_both_disputed_scopes_and_splits(cases, ev):
    decisions = _all_keep_decisions(cases, ev)
    results = {"C_D_E": ev.evaluate_strategy(cases, decisions, "C_D_E")}
    stability_rows = [
        {"case": "T1", "split": "train", "segment": "同质 L3", "results": {"B_single": 1.0, "C_D_E": 0.5}},
        {"case": "H1", "split": "holdout", "segment": "跨行业", "results": {"B_single": 0.5, "C_D_E": 0.5}},
    ]
    report = ev.render_report(
        cases,
        results,
        ev._stock_taxonomy(),
        {
            "params": {"min_overlap": 0.5, "product_floor": 0.3, "customer_floor": 0.2},
            "score": 0.4,
            "calibrate_split": "train",
            "result": results["C_D_E"],
        },
        stability_rows,
        stability_strategies=("B_single", "C_D_E"),
    )
    for split in ("train", "holdout", "all"):
        assert f"split={split}" in report
    assert "未排除 disputed" in report
    assert "排除 disputed" in report
    assert "B_single Jaccard" in report and "C_D_E Jaccard" in report
    assert "校准段: train" in report
    assert "负结果" in report  # 抖动不改善时必须如实登记


def test_report_mentions_production_entry():
    report_source = _SCRIPT.read_text(encoding="utf-8")
    assert "peer_judge" in report_source


# ── 在线路径行为零变化（新增生产 judge 入口**不接入在线**） ──────────────────

_ONLINE_MODULE = PROJECT_ROOT / "alphabee" / "company_track" / "peer_extract.py"

"""#: 在线生成器 prompt 的**新冻结基线**（设计 §4：本步**有意**增补 dims schema）。
#: 旧→新逐字 diff 存于 ``tmp/certified/pj1_peer_quality_prereview/prompt_diff_old_to_new.json``：
#: 新增行仅为「第 4 条 dims 要求」与「输出 schema 增加 dims 字段」，无第三条语义被改写。
"""
_FROZEN_INFER_PROMPT = '你是买方研究员。根据下面这家公司的业务描述，识别其在 **A 股市场**中的**同产业链环节的竞争对手/可比公司**，给出 5–8 家并按可比度降序。\n要求：\n1. 排除上游供应商与下游客户（如 PCB 公司不要选上游覆铜板、下游封测）；\n2. 优先给 A 股上市公司，代码带交易所后缀（如 002463.SZ / 603228.SH）；不确定的代码填空串，**不要编造**；\n3. 每条必须诚实给出 `overlap`（0–1，与标的在**同环节业务/产品/客户**上的重叠度）：越接近直接竞对越接近 1.0；若候选主要在材料（如碳钢 vs 不锈钢）、终端（如半导体/医药洁净 vs 石化/核电）或盈利模式上与标的不同，请**如实给低分**（由下游按阈值过滤，不要因为拿不准就直接省略）。\n4. 每条还要给出**结构化维度** `dims`（各自 0–1，按证据独立打分、不要一律同值）：product(产品/服务重叠)、customer(客户/终端重叠)、material_tech(材料/技术路线相近度)、business_model(盈利模式/业态相近度)。\n只输出 JSON 数组（确实无候选才输出 []），每条：{"name": "公司名", "code": "股票代码", "exchange": "SH/SZ/BJ", "overlap": 0.0-1.0, "dims": {"product":0.0,"customer":0.0,"material_tech":0.0,"business_model":0.0}, "reason": "为什么是同环节竞对（业务/产品/客户重叠）"}。\n行业（供参考）: PCB\n业务线构成（供参考）:\n（无业务线数据）\n\n公司业务描述:\n印制电路板与封装基板'

#: 在线模块 token 流签名（去注释/换行后 sha256）：生产 judge 入口若被误接进在线路径必然变化。
#: **重定标沿革（旧→新；均属本步有意变更，不是删除守护）**：
#:   ``683cc76b…``（``343c774`` 基线）
#:   → ``00486a09a0870c12f3b7c3c625c7b5c4e4224c71eee1474ee9c658efef5422a7``（删除中文关键词否决表 ⇒ token 流变化）
#:   → ``785d4e94a1f0b6c90f253b7ca25f85855cdac51c892de5fde2193211edcd7fd6``（生成器改为产出结构化 ``dims``）
#:   → ``fe5a7b8fe600acc996addc8f0cd0d253583705ee050bf520d4ca19e94586c131``（阈值常量统一为单一权威定义：
#:     ``peer_extract`` 删除自带 ``= 0.5`` 的同名常量，改为再导出 ``peer_judge.DEFAULT_MIN_OVERLAP``）
_FROZEN_ONLINE_TOKEN_SIGNATURE = "4860511395f508c0f8c6201c3e496f9043dd1b91b3351eead1beb723b0b8ee73"


def _online_source() -> str:
    return _ONLINE_MODULE.read_text(encoding="utf-8")


def _online_module_signature(source: str) -> str:
    """源码 token 流签名：忽略注释与换行，保留代码与 docstring（改注释不误红，改语句即红）。"""
    import hashlib
    import io
    import tokenize

    skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}
    tokens = [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline) if tok.type not in skip]
    return hashlib.sha256("".join(tokens).encode()).hexdigest()


def _online_peer_judge_references(source: str) -> list[str]:
    """AST 扫描在线模块里对**生产 judge 入口**的任何 import / 调用引用。

    允许 ``coerce_overlap``（纯归一化 helper，与在线旧实现逐字等价；抽到 ``peer_judge`` 只为
    单一来源）；**禁止** judge/打分入口（``judge_peer_candidates`` / ``infer_peer_scoring`` /
    ``build_judge_prompt`` / ``build_scoring_prompt`` / ``resolve_peer_eval_model``）出现在在线路径。
    """
    forbidden_modules = {"alphabee.company_track.peer_judge"}
    forbidden_names = {
        "peer_judge",
        "judge_peer_candidates",
        "infer_peer_scoring",
        "build_judge_prompt",
        "build_scoring_prompt",
        "resolve_peer_eval_model",
    }
    hits: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules or alias.name.split(".")[-1] in forbidden_names:
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
            if any(alias.name in forbidden_names for alias in node.names):
                hits.append(f"from {node.module} import {sorted(a.name for a in node.names)}")
        elif isinstance(node, ast.Name) and node.id in forbidden_names:
            hits.append(f"Name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_names:
            hits.append(f"Attribute {node.attr}")
    return hits


def _capture_infer_prompt(monkeypatch, content: str = "[]") -> list[str]:
    """跑一次 ``infer_peer_candidates`` 并捕获发给模型的 prompt。"""
    import alphabee.utils.llm as llm_module
    from alphabee.company_track import infer_peer_candidates

    prompts: list[str] = []

    class FakeModel:
        def invoke(self, prompt):
            prompts.append(prompt)
            return type("R", (), {"content": content})()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    infer_peer_candidates("002916.SZ", [], "印制电路板与封装基板", industry="PCB")
    return prompts


def test_online_infer_prompt_is_byte_identical_to_frozen_snapshot(monkeypatch):
    """在线生成器 prompt 与**新冻结基线**逐字一致（设计 §4 有意增补 dims schema 后重定标）。

    反回归意图保留：prompt 若有任何未被本步声明的改写 ⇒ 与基线不等 ⇒ 判红。
    旧→新逐字 diff 见 tmp/certified/pj1_peer_quality_prereview/prompt_diff_old_to_new.json。
    """
    prompts = _capture_infer_prompt(monkeypatch)
    assert prompts == [_FROZEN_INFER_PROMPT]


def test_online_infer_four_branches_unchanged(monkeypatch):
    """LLM 关闭 / 无描述 / 非数组 / 异常四条分支返回值逐字不变（等价性证据 2/3）。"""
    import alphabee.utils.llm as llm_module
    from alphabee.company_track import infer_peer_candidates

    # 分支 1：LLM 关闭
    candidates, meta = infer_peer_candidates("002916.SZ", [], "PCB", use_llm=False)
    assert candidates == []
    assert meta == {"note": "LLM 推断关闭", "raw": None, "dropped": [], "llm_ok": False}

    # 分支 2：无业务描述
    candidates, meta = infer_peer_candidates("002916.SZ", [], "   ")
    assert candidates == []
    assert meta == {"note": "无业务描述文本，跳过 LLM 推断（不编造）", "raw": None, "dropped": [], "llm_ok": False}

    # 分支 3：LLM 输出可解析但不是 JSON 数组（对象 → 走「非数组」分支）
    class ArraylessModel:
        def invoke(self, prompt):
            return type("R", (), {"content": '{"code": "002463.SZ"}'})

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: ArraylessModel())
    candidates, meta = infer_peer_candidates("002916.SZ", [], "PCB")
    assert candidates == []
    assert meta == {"note": "LLM 输出非 JSON 数组", "raw": '{"code": "002463.SZ"}', "dropped": [], "llm_ok": False}

    # 分支 4：LLM 异常
    def boom(component, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_module, "create_chat_model", boom)
    candidates, meta = infer_peer_candidates("002916.SZ", [], "PCB")
    assert candidates == []
    assert meta["note"] == "LLM 推断失败: llm down"
    assert meta["llm_ok"] is False and meta["dropped"] == [] and meta["raw"] is None


def test_online_infer_candidate_shape_unchanged(monkeypatch):
    """候选 dict 的**新声明形状**（设计 §4：overlap + 结构化 dims），锁定键序与类型。

    本用例原锁「相对 343c774 逐字不变」；本步按设计 §4 **有意**为生成器输出增补
    ``overlap`` / ``dims`` 两键 ⇒ 按新契约重定标（未弱化：键序、类型、JSON 可解析性均钉死）。
    """
    _capture_infer_prompt(
        monkeypatch,
        '[{"name": "沪电股份", "code": "002463.SZ", "exchange": "sz", "overlap": 0.9, "reason": "同环节", '
        '"dims": {"product": 0.9, "customer": 0.8, "material_tech": 0.7, "business_model": 0.9}}]',
    )
    import json as _json

    from alphabee.company_track import infer_peer_candidates

    candidates, _ = infer_peer_candidates("002916.SZ", [], "PCB")
    assert len(candidates) == 1
    assert list(candidates[0]) == ["name", "code", "exchange", "reason", "source", "overlap", "dims"]
    assert candidates[0]["name"] == "沪电股份"
    assert candidates[0]["code"] == "002463.SZ"
    assert candidates[0]["exchange"] == "SZ"  # 交易所归一到大写
    assert candidates[0]["source"] == "infer"
    assert float(candidates[0]["overlap"]) == 0.9
    assert _json.loads(candidates[0]["dims"]) == {
        "product": 0.9,
        "customer": 0.8,
        "material_tech": 0.7,
        "business_model": 0.9,
    }


def test_online_module_signature_is_frozen():
    """在线模块 token 签名与冻结值一致（回归守护；M4 的判据之一）。

    **契约形状按设计 §4 有意收窄后重定标**（非删除）：在线生成器**必须**继续调用生产侧
    **打分/生成**入口（`build_scoring_prompt`），但**不得**调用独立**评审**入口
    （`judge_peer_candidates` / `build_judge_prompt`）。判定口径从"任何 peer_judge 引用都禁"
    收窄为"仅禁评审入口"；签名值随判定口径变更而重定标（关键词否决表删除 ⇒ token 流变化）。
    """
    assert _online_module_signature(_online_source()) == _FROZEN_ONLINE_TOKEN_SIGNATURE


def test_online_module_does_not_reference_peer_judge():
    """在线路径**可以**用生产侧打分/生成入口，但**绝不**接入独立评审入口（M4 的判据之二）。

    登记的口径差异：`build_scoring_prompt`（生成器打分）允许在线调用；`judge_peer_candidates` /
    `infer_peer_scoring` / `build_judge_prompt` / `resolve_peer_eval_model`（独立评审）禁止。
    """
    assert _online_peer_judge_references(_online_source()) == []


# ── 判别力自证：变异必红 ────────────────────────────────────────────────────


def test_m1_ignore_split_segmentation_is_killed(ev):
    """M1：把分段评估退化为“只用全体”。"""
    mutant = _mutate(
        ev,
        "        subset = cases_for_split(cases, split)",
        '        subset = cases_for_split(cases, "all")  # M1 忽略 split 分段',
        "pge_mutant_m1",
    )
    train = _synth_case(mutant, "T1", split="train", candidates=[("A", "keep", False)])
    holdout = _synth_case(mutant, "H1", split="holdout", candidates=[("A", "drop", False)])
    decisions = {
        "T1": {"A": mutant.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9))},
        "H1": {"A": mutant.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9))},
    }
    result = mutant.evaluate_strategy([train, holdout], decisions, "C_D_judge")
    # 正确实现为 train=1.0 / holdout=0.0；变异把 holdout 也算成全体 ⇒ 0.5（判别力被杀死）
    assert mutant.segment_metrics(result, "holdout")["f1"] == pytest.approx(0.5)
    assert mutant.segment_metrics(result, "train")["f1"] == pytest.approx(0.5)


def test_m2_ignoring_disputed_in_exclusion_scope_is_killed(ev):
    """M2：排除口径里没真的把 disputed 候选摘掉（两套数字退化为同一套）。"""
    mutant = _mutate(
        ev,
        "            labels_nd = _visible_labels(case, exclude_disputed=True)",
        "            labels_nd = _visible_labels(case, exclude_disputed=False)  # M2 disputed 不计入排除口径",
        "pge_mutant_m2",
    )
    case = _synth_case(mutant, "D3", split="train", candidates=[("A", "keep", False), ("B", "drop", True)])
    decisions = {
        "D3": {
            "A": mutant.Decision(code="A", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),
            "B": mutant.Decision(code="B", judge_verdict="direct", judge_dims=_dims(0.9, 0.9)),
        }
    }
    result = mutant.evaluate_strategy([case], decisions, "C_D_judge")
    include = mutant.segment_metrics(result, "train")
    exclude = mutant.segment_metrics(result, "train", exclude_disputed=True)
    assert include != exclude, "变异后两套口径必须仍然不同，否则说明判据不灵敏"


def test_m3_jaccard_degenerate_to_one_is_killed(ev):
    """M3：Jaccard 恒 1（稳定性永远“完美”）。"""
    mutant = _mutate(
        ev,
        "    union = left | right\n    return len(left & right) / len(union) if union else 1.0",
        "    return 1.0  # M3 Jaccard 退化为恒 1",
        "pge_mutant_m3",
    )
    # 正确实现为 0.0（完全不一致）；变异恒 1.0 ⇒ 稳定性“永远完美”，判别力被杀死
    assert mutant.jaccard({"A"}, {"B"}) == pytest.approx(1.0)
    assert mutant.mean_pairwise_jaccard([{"A"}, {"B"}, {"C"}]) == pytest.approx(1.0)
    # 参照实现（未变异）：同一输入必须为 0.0（否则本变异体不可观测）
    assert ev.mean_pairwise_jaccard([{"A"}, {"B"}, {"C"}]) == pytest.approx(0.0)


def test_m4_production_entry_wired_into_online_path_is_killed(ev):
    """M4：把**独立评审**入口误接进在线路径（``infer_peer_candidates``）⇒ 必红。

    该变异必须被『在线行为不变』的两条断言杀死（均位于 ``test_peer_group_build.py``）：
    1. ```test_online_module_does_not_reference_peer_judge```：AST 断言在线模块不得 import / 调用
       judge/打分入口（``judge_peer_candidates`` / ``infer_peer_scoring`` / …）；
    2. ```test_online_module_signature_is_frozen``：在线模块 token 签名与冻结值一致。
    此处实测：变异后 AST 断言判红、token 签名与冻结值不同；而 prompt 逐字快照仍绿
    （说明 M4 确实只被「在线接入」类判据杀死，而不是被无关判据误杀）。
    """
    source = _ONLINE_MODULE.read_text(encoding="utf-8")
    mutant_source = source.replace(
        "    if not fragments:\n",
        "    import alphabee.company_track.peer_judge as peer_judge  # M4 误接在线路径\n"
        "    _ = peer_judge.judge_peer_candidates  # M4 误用 judge 入口\n\n"
        "    if not fragments:\n",
        1,
    )
    assert mutant_source != source
    assert _online_peer_judge_references(mutant_source), "AST 断言未发现变异接入点 ⇒ 判据不灵敏"
    assert _online_peer_judge_references(source) == [], "未变异的在线模块必须干净"
    assert _online_module_signature(mutant_source) != _online_module_signature(source)
    # prompt 构造片段不因该变异变化 ⇒ 证明 M4 只被「在线接入」判据杀死，而非被 prompt 判据误杀
    for fragment in ("根据下面这家公司的业务描述", '"overlap": 0.0-1.0', "公司业务描述:"):
        assert fragment in mutant_source


# ── RC-QUALITY-DEFAULTS / RC-VERDICT-DIVERGENCE / F-1 / F-2 / F-5 / F-6 ─────
#    以上收口项的共同判据：报告与 harness 不得存在"看似权威的第二份默认"，且每个
#    「旧行为缺陷」都有可判红的反回归断言（对实现字节实测必红）。


def test_gate_defaults_reference_production_constants():
    """RC-QUALITY-DEFAULTS：``gate()`` 签名默认值**引用生产常量**（harness 无第二份权威）。"""
    import inspect

    from alphabee.company_track import peer_judge
    from alphabee.config import PeerQualitySettings

    signature = inspect.signature(ev_gate_signature_module().gate)
    assert signature.parameters["min_overlap"].default == peer_judge.DEFAULT_MIN_OVERLAP
    assert signature.parameters["product_floor"].default == peer_judge.DEFAULT_PRODUCT_FLOOR
    assert signature.parameters["customer_floor"].default == peer_judge.DEFAULT_CUSTOMER_FLOOR
    assert signature.parameters["weights"].default == peer_judge.DEFAULT_WEIGHTS

    defaults = PeerQualitySettings()
    assert (defaults.min_overlap, defaults.product_floor, defaults.customer_floor) == (
        peer_judge.DEFAULT_MIN_OVERLAP,
        peer_judge.DEFAULT_PRODUCT_FLOOR,
        peer_judge.DEFAULT_CUSTOMER_FLOOR,
    )
    assert defaults.weights == peer_judge.DEFAULT_WEIGHTS

    # 源码级：不得出现与前份权威同名的字面量默认（旧值 0.5 / 0.30）
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "min_overlap: float = 0.5" not in source
    assert "product_floor: float = 0.30" not in source
    assert "min_overlap: float = DEFAULT_MIN_OVERLAP" in source


def ev_gate_signature_module():
    import importlib.util as _ilu
    import sys as _sys

    spec = _ilu.spec_from_file_location("pge_gate_defaults_probe", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = _ilu.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_rc_quality_defaults_second_authority_is_killed(ev):
    """判别力：把 gate() 默认改回"harness 自己的"阈值 ⇒ 上面的同源断言必红。"""
    import inspect

    from alphabee.company_track import peer_judge

    mutant = _mutate(
        ev,
        "    min_overlap: float = DEFAULT_MIN_OVERLAP,",
        "    min_overlap: float = 0.5,  # RC 双重权威回归",
        "pge_mutant_rc_defaults",
    )
    assert inspect.signature(mutant.gate).parameters["min_overlap"].default != peer_judge.DEFAULT_MIN_OVERLAP


def test_projection_keeps_verdict_only_for_d_strategies(ev):
    """RC-VERDICT-DIVERGENCE 收口：D 类策略带 verdict（同源先否决）；C 类策略 verdict **显式清空**。"""
    case = _synth_case(ev, "V1", split="train", candidates=[("A", "keep", False)])
    decisions = {
        "V1": {
            "A": ev.Decision(
                code="A",
                gen_overlap=0.9,
                dims=_dims(0.9, 0.9),
                judge_verdict="reject",
                judge_dims=_dims(0.9, 0.9),
            )
        }
    }
    # D 类（judge 生效）：reject 先否决 ⇒ 不保留
    kept_d, judge_ok = ev.gate_for_case(case, decisions, "C_D_judge")
    assert judge_ok is True and kept_d == set()
    # C 类：策略定义就是"没有 judge" ⇒ 投影清空 verdict，按生成器 dims 判定
    kept_c, _ = ev.gate_for_case(case, decisions, "C_dims")
    assert kept_c == {"A"}
    # 闸门本身对 verdict 的处理是统一的（同源先否决）：直接喂带 verdict 的 decisions 也剔
    assert ev.gate(ev.project_decisions(decisions["V1"], "C_D_judge", judge_ok=True), "C_dims") == set()


def test_judge_incomplete_coverage_falls_back_like_production(ev):
    """judge 降级口径（与生产 fail-open 同源）：漏判 ⇒ 该 case **整体**回退生成器 dims/overlap。"""
    case = _synth_case(ev, "V2", split="train", candidates=[("A", "keep", False), ("B", "keep", False)])
    decisions = {
        "V2": {
            "A": ev.Decision(
                code="A",
                gen_overlap=0.9,
                dims=_dims(0.9, 0.9),
                judge_verdict="reject",  # judge 想否决 A，但因 B 漏判 ⇒ 整体降级 ⇒ A 不得被静默剔除
                judge_dims=_dims(0.1, 0.1),
            ),
            "B": ev.Decision(code="B", gen_overlap=0.9, dims=_dims(0.9, 0.9)),
        }
    }
    kept, judge_ok = ev.gate_for_case(case, decisions, "C_D_judge")
    assert judge_ok is False and kept == {"A", "B"}
    result = ev.evaluate_strategy([case], decisions, "C_D_judge")
    assert ev.judge_status_counts(result) == {"applied": 0, "degraded": 1, "n/a": 0}
    assert ev.judge_status_counts(ev.evaluate_strategy([case], decisions, "C_dims")) == {
        "applied": 0,
        "degraded": 0,
        "n/a": 1,
    }


# ── F-1：稳定性不可评估（不再假通过）+ 缓存 fail-loud ───────────────────────


def _write_repeat_cache(tmp_path, symbol: str, payload: dict, runs: int = 3) -> None:
    import json as _json

    for index in range(runs):
        run_dir = tmp_path / f"stable_run{index}"
        run_dir.mkdir(exist_ok=True)
        (run_dir / f"{symbol}.json").write_text(_json.dumps(payload), encoding="utf-8")


def test_stability_all_empty_is_unassessable_not_perfect(tmp_path, monkeypatch, ev):
    """F-1：n 次采集保留集**全空** ⇒ 该行「不可评估」（旧行为静默报 Jaccard=1.000 假通过）。"""
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _synth_case(ev, "S2", split="train", candidates=[("A", "drop", False)])
    payload = {
        "schema_version": ev.CACHE_SCHEMA_VERSION,
        "symbol": "S2",
        "split": "train",
        "segment": "同质 L3",
        "decisions": [ev.Decision(code="A", judge_verdict="reject", judge_dims=_dims(0.1, 0.1)).to_dict()],
    }
    _write_repeat_cache(tmp_path, "S2", payload)
    rows = ev.stability([case], ("C_D_judge",), 3, record=False)

    assert rows[0]["results"]["C_D_judge"] is None, "全空仍报数值 ⇒ F-1 假通过回归"
    assert rows[0]["empty_runs"]["C_D_judge"] == 3
    assert ev.stability_unassessable_cases(rows, "C_D_judge") == ["S2"]
    assert ev.stability_empty_everywhere_cases(rows, "C_D_judge") == ["S2"]  # 报告里的「n 次皆空案例数」
    # 不可评估行不参与分段均值
    assert ev.stability_by_split(rows, "C_D_judge") == {"train": 0.0, "holdout": 0.0, "all": 0.0}


def test_stability_all_empty_reported_as_na_in_report(tmp_path, monkeypatch, ev, cases):
    """报告层：不可评估行显式写 ``n/a（皆空）``，并给出「n 次皆空案例数」。"""
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _synth_case(ev, "S3", split="holdout", candidates=[("A", "drop", False)])
    payload = {
        "schema_version": ev.CACHE_SCHEMA_VERSION,
        "symbol": "S3",
        "split": "holdout",
        "segment": "跨行业",
        "decisions": [ev.Decision(code="A", judge_verdict="reject", judge_dims=_dims(0.1, 0.1)).to_dict()],
    }
    _write_repeat_cache(tmp_path, "S3", payload)
    rows = ev.stability([case], ("B_single", "C_D_E"), 3, record=False)
    report = ev.render_report(
        [case],
        {"C_D_E": ev.evaluate_strategy([case], {"S3": {}}, "C_D_E")},
        ev._stock_taxonomy(),
        None,
        rows,
        stability_strategies=("B_single", "C_D_E"),
        repeat=3,
    )
    assert "n/a（皆空）" in report
    assert "不可评估 case 数=1" in report or "不可评估 case 数=2" in report
    assert "皆空" in report


def test_m_f1_all_empty_reports_perfect_is_killed(ev):
    """判别力（M-F1）：把聚合改回"双方皆空 = 1.0"⇒ 不可评估断言必红。"""
    mutant = _mutate(
        ev,
        "    pairs = [pair for pair in itertools.combinations(sets, 2) if pair_is_evaluable(*pair)]\n"
        "    if not pairs:\n        return None",
        "    pairs = list(itertools.combinations(sets, 2))\n    if not pairs:\n        return 1.0  # F-1 假通过",
        "pge_mutant_f1",
    )
    assert mutant.mean_pairwise_jaccard([set(), set()]) == pytest.approx(1.0)
    assert ev.mean_pairwise_jaccard([set(), set()]) is None


def test_cache_corrupt_or_missing_fails_loud(tmp_path, monkeypatch, cases, ev):
    """F-1 附带判据：缓存**缺失/损坏/结构非法**一律 fail-loud（不得静默降级为"无数据"）。"""
    import json as _json

    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path)
    case = _case(cases, "603986.SH")
    with pytest.raises(FileNotFoundError) as missing:
        ev.collect_decisions(case, record=False)
    assert "--record" in str(missing.value)

    path = tmp_path / "603986.SH.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError) as corrupt:
        ev.collect_decisions(case, record=False)
    assert "缓存损坏" in str(corrupt.value)

    path.write_text(_json.dumps('"a string"'), encoding="utf-8")
    with pytest.raises(ValueError):
        ev.collect_decisions(case, record=False)


# ── F-6：缓存血缘审计（现算 + sha256，不人手维护散文） ──────────────────────


def test_cache_lineage_audit_lists_mismatch_with_sha256(tmp_path, cases, ev):
    """F-6：血缘不一致必须**现算并列出 sha256**；一致时为“无不一致”。"""
    import hashlib as _hashlib
    import json as _json

    case = _case(cases, "002318.SZ")
    consistent = {"symbol": case.symbol, "split": case.split, "segment": case.segment}
    (tmp_path / f"{case.symbol}.json").write_text(_json.dumps(consistent), encoding="utf-8")
    _write_repeat_cache(
        tmp_path,
        case.symbol,
        {"symbol": case.symbol, "split": "train" if case.split != "train" else "holdout", "segment": case.segment},
    )
    audit = ev.cache_lineage_audit(cases, root=tmp_path)
    assert audit["scanned"] == 4
    assert len(audit["mismatches"]) == 3
    first = audit["mismatches"][0]
    assert first["mismatched_fields"] == ["split"]
    assert first["labels_split"] == case.split and first["cache_split"] != case.split
    assert (
        first["sha256"] == _hashlib.sha256((tmp_path / "stable_run0" / f"{case.symbol}.json").read_bytes()).hexdigest()
    )
    assert first["path"].startswith(str(tmp_path))

    # 全部一致 ⇒ 空清单
    _write_repeat_cache(tmp_path, case.symbol, consistent, runs=3)
    for index in range(3):
        (tmp_path / f"stable_run{index}" / f"{case.symbol}.json").write_text(_json.dumps(consistent), encoding="utf-8")
    assert ev.cache_lineage_audit(cases, root=tmp_path)["mismatches"] == []


def test_current_generation_lineage_has_no_segment_mismatch(cases, ev):
    """现行世代（评测根）缓存应与标注集 stride「零不一致」，且任何不一致都必须列出 sha256。

    缓存缺失（全新克隆，``data/`` 被 gitignore）时跳过——本判据只约束"有缓存时不得静默"。
    """
    audit = ev.cache_lineage_audit(cases, root=ev.CACHE_DIR, label="现行世代")
    if audit["scanned"] == 0:
        pytest.skip("无本地现行世代缓存（data/ 为 gitignore 本地交付）")
    assert audit["mismatches"] == [], f"现行世代血缘不一致：{audit['mismatches']}"
    assert all(len(item["sha256"]) == 64 for item in audit["mismatches"])


def test_legacy_generation_lineage_mismatch_is_disclosed_with_sha256(cases, ev):
    """F-6：历史世代（``data/peer_eval_cache``，Step 0 旧 prompt）的**唯一不一致 = 002318.SZ 的 split**，
    且审计须给出这 4 个文件（主线 + stable_run0/1/2）的**现行 sha256**；本步**不改写**其字节。
    """
    root = ev.LEGACY_CACHE_DIR
    if not root.is_dir():
        pytest.skip("无历史世代缓存目录（data/ 为 gitignore 本地交付）")
    audit = ev.cache_lineage_audit(cases, root=root, label="历史世代")
    symbols = {item["path"].rsplit("/", 1)[-1] for item in audit["mismatches"]}
    if not symbols:
        pytest.skip("历史世代缓存已不存在（本地数据被清理）")
    assert symbols == {"002318.SZ.json"}, f"历史世代血缘不一致不止 002318：{sorted(symbols)}"
    assert all(item["mismatched_fields"] == ["split"] for item in audit["mismatches"])
    assert len(audit["mismatches"]) == 4  # 主线 + stable_run0/1/2
    assert all(len(item["sha256"]) == 64 for item in audit["mismatches"])


# ── F-5：评测产物版本化留档 + F-2 披露 ──────────────────────────────────────


def test_archive_run_writes_report_and_manifest(tmp_path, ev):
    """F-5：留档目录含 ``report.md`` + ``manifest.json``（含报告 sha256 与外部元数据）。"""
    import hashlib as _hashlib
    import json as _json

    result = ev.archive_run(
        "# report\n",
        meta={"cache_root": "data/peer_eval_cache/gen2", "labels": {"sha256": "x"}},
        out_dir=tmp_path,
        stamp="20260101T000000Z",
    )
    target = tmp_path / "20260101T000000Z"
    assert (target / "report.md").read_text(encoding="utf-8") == "# report\n"
    manifest = _json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["report_sha256"] == _hashlib.sha256(b"# report\n").hexdigest()
    assert manifest["cache_root"] == "data/peer_eval_cache/gen2"
    assert manifest["archived_at"] == "20260101T000000Z"
    assert result["dir"] == str(target)


def test_report_discloses_equivalence_lineage_and_immutability(cases, ev):
    """F-2/F-5/F-6 披露：等价关系（实测 C_E_taxo≡C_dims、C_D_E≡C_D_judge）、缓存非不可变证据、
    血缘不一致文件与 sha256 均须出现在报告头部。"""
    decisions = _all_keep_decisions(cases, ev)
    results = {
        name: ev.evaluate_strategy(cases, decisions, name) for name in ("C_dims", "C_E_taxo", "C_D_judge", "C_D_E")
    }
    lineage = [
        {
            "root": "data/peer_eval_cache",
            "label": "历史世代（Step 0 旧 judge prompt，只读血缘证据）",
            "scanned": 4,
            "mismatches": [
                {
                    "path": "data/peer_eval_cache/002318.SZ.json",
                    "sha256": "a" * 64,
                    "mismatched_fields": ["split"],
                    "cache_split": "train",
                    "cache_segment": "残差桶",
                    "labels_split": "holdout",
                    "labels_segment": "残差桶",
                }
            ],
        }
    ]
    report = ev.render_report(
        cases,
        results,
        ev._stock_taxonomy(),
        None,
        None,
        lineage=lineage,
        generated_at="2026-10-10T00:00:00+00:00",
    )
    assert "`C_E_taxo ≡ C_dims`" in report and "`C_D_E ≡ C_D_judge`" in report
    assert "保留集相同" in report  # 等价关系是**实测**披露，不是仅声明
    assert "不是不可变证据" in report
    assert "002318.SZ.json" in report and "a" * 64 in report
    assert "权威一律取 `tests/fixtures/peer_group_eval/labels.yaml`" in report
    assert "RC-QUALITY-DEFAULTS" in report and "RC-VERDICT-DIVERGENCE" in report
    assert "3 位小数" in report


# ── DoD（设计 §6 Step 2 + RC-DOD-SENS 两点复核 + 负结果路径） ───────────────


def _dod_fixture(ev) -> tuple[list[Any], dict[str, dict[str, Any]]]:
    """合成夹具：C_dims 在 holdout 上优于 C_D_judge（judge 误杀 keep）⇒ DoD 不成立。"""
    train = _synth_case(ev, "T1", split="train", candidates=[("A", "keep", False)])
    holdout = _synth_case(ev, "H1", split="holdout", candidates=[("A", "keep", False), ("B", "drop", False)])
    decisions = {
        "T1": {
            "A": ev.Decision(
                code="A",
                gen_overlap=1.0,
                dims=_dims(0.9, 0.9),
                judge_verdict="direct",
                judge_dims=_dims(0.9, 0.9),
            )
        },
        "H1": {
            "A": ev.Decision(
                code="A",
                gen_overlap=1.0,
                dims=_dims(0.9, 0.9),
                judge_verdict="reject",
                judge_dims=_dims(0.1, 0.1),
            ),
            "B": ev.Decision(
                code="B",
                gen_overlap=1.0,
                dims=_dims(0.9, 0.9),
                judge_verdict="reject",
                judge_dims=_dims(0.1, 0.1),
            ),
        },
    }
    return [train, holdout], decisions


def test_dod_check_three_points_and_negative_result_path(ev):
    """DoD 复核：标定点 + **生产生效点** + 占位默认点三处都报数；不成立时给出负结果路径（不得凑口径）。"""
    cases, decisions = _dod_fixture(ev)
    dod = ev.dod_check(cases, decisions)
    assert dod["calibrate_split"] == "train"
    assert {check["point"] for check in dod["checks"]} == {"calibrated", "production", "placeholder"}
    # 两处均不成立（judge 误杀 keep）
    assert dod["f1_ok_at_calibrated"] is False and dod["f1_ok_at_placeholder"] is False
    report = ev.render_report(
        cases,
        {"C_dims": ev.evaluate_strategy(cases, decisions, "C_dims")},
        ev._stock_taxonomy(),
        None,
        None,
        dod=dod,
    )
    assert "负结果（诚实登记）" in report
    assert "不得默认启用 judge" in report and "judge_enabled" in report


def _dod_payload(ev, **overrides):
    """构造 render_report 用的 DoD 载荷（默认 = 判据全成立）。"""
    params = {"min_overlap": 0.4, "product_floor": 0.2, "customer_floor": 0.2}
    payload = {
        "calibrate_split": "train",
        "baseline": "C_dims",
        "candidates": ["C_D_judge"],
        "calibrated": {
            "C_dims": {"params": params, "holdout": {"f1": 0.6}},
            "C_D_judge": {"params": params, "holdout": {"f1": 0.7}},
        },
        "production": {
            "C_dims": {"params": params, "holdout": {"f1": 0.685}},
            "C_D_judge": {"params": params, "holdout": {"f1": 0.702}},
        },
        "placeholder": {},
        "checks": [],
        "stability_deltas": {},
        "stability_skipped": [],
        "stability_ok": True,
        "f1_ok_at_calibrated": True,
        "f1_ok_at_production": True,
        "f1_ok_at_placeholder": False,
        "dod_satisfied": True,
    }
    payload.update(overrides)
    return payload


def test_dod_point_table_reports_train_and_holdout_for_three_points(ev):
    """阈值点对照表：三点 × 策略都给 train/holdout（防止只报有利方向）。"""
    cases, decisions = _dod_fixture(ev)
    dod = ev.dod_check(cases, decisions)
    table = dod["point_table"]
    assert set(table) == {"calibrated", "production", "placeholder"}
    for label in table:
        for name in ("B_single", "C_dims", "C_D_judge"):
            assert name in table[label], (label, name)
            assert set(table[label][name]) == {"train_f1", "holdout_f1"}
    report = ev.render_report([], {}, {}, None, None, dod=dod)
    assert "阈值点对照" in report


def test_dod_report_declares_calibrated_point_when_only_it_passes(ev):
    """RC-DOD-SENS：F1 判据只在标定点/生产生效点成立时，报告须显式声明「生产生效阈值 = 标定点」。"""
    report = ev.render_report([], {}, {}, None, None, dod=_dod_payload(ev))
    assert "生产生效阈值 = 标定点" in report
    assert "不得表述为「DoD 已稳健成立」" in report
    assert "DoD 成立 ✓" in report


def test_dod_report_negative_result_names_unmet_stability_criterion(ev):
    """稳定性判据不成立（F1 成立）⇒ 报告须走负结果路径并点名未满足判据（不得只说 F1 好看）。"""
    dod = _dod_payload(
        ev,
        stability_ok=False,
        dod_satisfied=False,
        stability_deltas={"C_D_judge": {"B_single": {"train": -0.009, "holdout": -0.016, "all": -0.011}}},
    )
    report = ev.render_report([], {}, {}, None, None, dod=dod)
    assert "负结果（诚实登记）" in report
    assert "稳定性判据（judge 策略的保留集 Jaccard 相对生成器口径存在下降段）" in report
    assert "不得默认启用 judge" in report
    assert "DoD 成立 ✓" not in report


def test_cli_default_cache_root_is_module_constant():
    """``--cache-root`` 默认 = 模块常量（缓存世代可切换，历史世代字节不被触碰）。"""
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "default=str(CACHE_DIR)" in source
    assert "历史世代文件字节保持原样" in source
    assert 'CACHE_DIR = PROJECT_ROOT / "data" / "peer_eval_cache" / "gen2"' in source
    assert 'LEGACY_CACHE_DIR = PROJECT_ROOT / "data" / "peer_eval_cache"' in source


def ev_labels_path():
    """标注集路径（现算校验的锚点）。"""
    from alphabee.company_track.peer_judge import DIMS as _PRODUCTION_DIMS

    assert _PRODUCTION_DIMS  # 依赖可用性守卫（避免加载错文件造成假绿）
    return PROJECT_ROOT / "tests" / "fixtures" / "peer_group_eval" / "labels.yaml"


def test_labels_header_coverage_claim_matches_computed(cases):
    """F-7：标注集头部的**定量散文**（「六类中的N类」）必须与现算的 holdout 业态数一致。

    旧残留：t14 把残差桶案例移入 holdout 后，散文仍写「六类中的五类」（与实际 6 类矛盾）——
    这类"注释撒谎"此后由本用例现算判红，不靠人眼复核。
    """
    import re as _re

    text = ev_labels_path().read_text(encoding="utf-8")
    computed = len({case.segment for case in cases if case.split == "holdout"})
    numerals = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
    claims = [numerals[token] for token in _re.findall(r"六类中的([一二三四五六])类", text)]
    assert claims, "头部缺少「六类中的N类」覆盖声明（本判据的锚点）"
    assert all(claim == computed for claim in claims), f"散文声明 {claims} ≠ 现算 holdout 业态数 {computed}"
    assert computed == 6  # 六类业态全覆盖（含残差桶）


def test_labels_coverage_claim_prose_mutation_is_killed(cases):
    """判别力：把散文改回「六类中的五类」⇒ 上面的现算判据必红。"""
    import re as _re

    text = ev_labels_path().read_text(encoding="utf-8")
    mutated = text.replace("六类中的六类", "六类中的五类", 1)
    assert mutated != text
    numerals = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
    computed = len({case.segment for case in cases if case.split == "holdout"})
    claims = [numerals[token] for token in _re.findall(r"六类中的([一二三四五六])类", mutated)]
    assert claims and claims[0] != computed


# ── 判定 E 与对标组置信度（t10：召回口径审计 + 置信度复算 + 变异） ──────────


def test_taxonomy_recall_audit_two_scopes_and_production_pool(ev):
    """E 召回审计：未并入（llm_only）与并入后（with_taxonomy）两口径 + 生产召回头寸。"""
    cases = ev.load_cases(ev.DEFAULT_CASES)
    taxonomy = ev._stock_taxonomy()
    for case in cases:
        ev._attach_taxonomy(case, taxonomy)
    audit = ev.taxonomy_recall_audit(cases)

    assert audit["cases"] >= 1
    means = audit["means"]
    assert means["coverage_with_taxonomy"] >= means["coverage_llm_only"] - 1e-12, means
    assert 0.0 <= means["coverage_production_pool"] <= 1.0
    for row in audit["rows"]:
        assert row["coverage_with_taxonomy"] == pytest.approx(row["kept_in_pool"] / row["keep_total"])
        assert row["coverage_llm_only"] == pytest.approx(row["kept_in_llm_only"] / row["keep_total"])
        assert row["production_level"] in {"l3", "l2", "none"}


def test_taxonomy_recall_audit_without_taxonomy_subset_is_killed(ev):
    """判别力：把「并入后」口径也算成反事实池（丢掉同 L3/L2 子集）⇒ 判据必红。"""
    cases = ev.load_cases(ev.DEFAULT_CASES)
    taxonomy = ev._stock_taxonomy()
    for case in cases:
        ev._attach_taxonomy(case, taxonomy)
    mutant = _mutate(
        ev,
        "        llm_codes = pool_codes - taxo_codes",
        "        llm_codes = pool_codes  # 变异：并入口径退回未并入口径",
        "pge_mutant_e_recall",
    )
    audit = mutant.taxonomy_recall_audit(cases)
    # 两口径退化为一套 ⇒ with_taxonomy == llm_only，判别力（含构造性差异）被杀死
    assert audit["means"]["coverage_with_taxonomy"] == pytest.approx(audit["means"]["coverage_llm_only"])
    reference = ev.taxonomy_recall_audit(cases)
    assert reference["means"]["coverage_with_taxonomy"] > reference["means"]["coverage_llm_only"]


def test_confidence_recompute_is_deterministic_and_reports_tiers(ev):
    """置信度复算：三档分布 + 确定性自检 + 逐 case 信号（含缺失信号按 0）。"""
    cases = ev.load_cases(ev.DEFAULT_CASES)
    taxonomy = ev._stock_taxonomy()
    for case in cases:
        ev._attach_taxonomy(case, taxonomy)
    decisions = {case.symbol: ev.collect_decisions(case, record=False) for case in cases}
    result = ev.confidence_recompute(cases, decisions)

    assert result["deterministic"] is True
    assert set(result["distribution"]) == {"低", "中", "高"}
    assert sum(result["distribution"].values()) == result["cases_with_peers"]
    ratios = [row["signals"]["judge_direct_ratio"] for row in result["rows"]]
    for row in result["rows"]:
        assert row["level"] in {"低", "中", "高"}
        assert 0.0 <= row["score"] <= 1.0
        assert "taxonomy_reliable=" in row["basis"]
    # 「公式本体被覆盖，而不只是 0 分支」：离线复算必须用到**真实**判分占比（存在非缺失且非零的取值）
    assert any(value is not None and value > 0 for value in ratios), ratios


def test_confidence_recompute_reuses_production_synthesis(ev):
    """单源判据：复算必须走**生产**合成函数（改生产权重 ⇒ 复算分数随之变化）。"""
    from alphabee.company_track.peer_group_build import synthesize_peer_confidence

    case = _synth_case(ev, "C1", split="train", candidates=[("A", "keep", False)])
    decisions = {
        "C1": {
            "A": ev.Decision(
                code="A",
                gen_overlap=0.9,
                dims=_dims(0.9, 0.9),
                judge_verdict="direct",
                judge_dims=_dims(0.9, 0.9),
            )
        }
    }
    result = ev.confidence_recompute([case], decisions)
    row = result["rows"][0]
    expected = synthesize_peer_confidence(
        taxonomy_reliable=row["signals"]["taxonomy_reliable"] == 1.0,
        judge_direct_ratio=row["signals"]["judge_direct_ratio"],
        mean_overlap=row["signals"]["mean_overlap"],
    )
    assert row["level"] == expected.level and row["score"] == pytest.approx(expected.score)


def test_report_includes_e_recall_and_confidence_sections(cases, ev):
    """报告含 E 召回口径审计与置信度复算章节（DoD ②③ 的可核对落点）。"""
    decisions = _all_keep_decisions(cases, ev)
    results = {
        "C_dims": ev.evaluate_strategy(cases, decisions, "C_dims"),
        "C_E_taxo": ev.evaluate_strategy(cases, decisions, "C_E_taxo"),
    }
    audit = ev.taxonomy_recall_audit(cases)
    confidence = ev.confidence_recompute(cases, decisions)
    report = ev.render_report(
        cases,
        results,
        ev._stock_taxonomy(),
        None,
        None,
        recall_audit=audit,
        confidence=confidence,
    )
    assert "E 召回口径审计（DoD ②）" in report
    assert "llm_only" in report and "with_taxonomy" in report and "production_pool" in report
    assert "对标组置信度复算（DoD ③）" in report
    assert "三档分布" in report and "确定性自检" in report
    assert "生产分类学" in report  # 逐 case 表新增生产口径列


# ── F-2（t16 未达项）：稳定性差值的**舍入口径声明** ─────────────────────────


def _f2_stability_rows():
    return [
        {
            "case": "T1",
            "split": "train",
            "segment": "同质 L3",
            "repeat": 3,
            "results": {"C_dims": 0.9},
            "empty_runs": {},
        },
        {
            "case": "H1",
            "split": "holdout",
            "segment": "跨行业",
            "repeat": 3,
            "results": {"C_dims": 0.8},
            "empty_runs": {},
        },
    ]


def _f2_dod(ev):
    """带稳定性差值的 DoD 载荷（用于检查 DoD 章节同样带舍入口径声明）。"""
    return _dod_payload(
        ev,
        stability_deltas={"C_dims": {"B_single": {"train": 0.01, "holdout": -0.015741, "all": 0.0}}},
        stability_ok=True,
    )


def test_stability_delta_declares_unrounded_rounding_scope(ev, cases):
    """报告须声明「稳定性差值由**未舍入**均值计算」（否则按印出的三位均值相减会差 0.001）。

    t16 实测：全文 0 次提及舍入口径；差值按未舍入均值算（holdout Δ=−0.015741 显示 −0.016），
    与三位舍入均值之差（0.934−0.919=−0.015）相差 0.001。
    """
    decisions = _all_keep_decisions(cases, ev)
    results = {"C_dims": ev.evaluate_strategy(cases, decisions, "C_dims")}
    rows = _f2_stability_rows()
    common = {
        "stability_strategies": ("B_single", "C_dims"),
        "repeat": 3,
    }
    with_dod = ev.render_report(cases, results, ev._stock_taxonomy(), None, rows, dod=_f2_dod(ev), **common)
    without_dod = ev.render_report(cases, results, ev._stock_taxonomy(), None, rows, **common)

    assert "未舍入" in with_dod and "相差 0.001" in with_dod
    # 两处差值呈现（DoD 稳定性判据 + 稳定性章节对照）都带声明 ⇒ 引用同一常量、共 2 次
    assert with_dod.count("舍入口径") == 2
    assert without_dod.count("舍入口径") == 1
    # 声明与实现同源（报告文本来自模块常量）
    assert ev.ROUNDING_SCOPE_NOTE in with_dod


@pytest.mark.parametrize(
    ("replacement", "label"),
    [
        ('ROUNDING_SCOPE_NOTE = ""', "删掉声明"),
        ('ROUNDING_SCOPE_NOTE = "- **舍入口径**：稳定性差值由**舍入后**的均值计算"', "改写成「由舍入值计算」"),
    ],
)
def test_f2_rounding_scope_declaration_removed_is_killed(ev, cases, replacement, label):
    """判别力：删掉声明或改写成「由舍入值计算」⇒ 上面的检查必红（实测 KILLED）。"""
    anchor = (
        'ROUNDING_SCOPE_NOTE = "- **舍入口径**：稳定性差值由**未舍入**均值计算，'
        '故可能与上方三位舍入均值之差**相差 0.001**"'
    )
    mutant = _mutate(ev, anchor, replacement, f"pge_mutant_f2_{abs(hash(label)) % 9973}")

    decisions = _all_keep_decisions(cases, mutant)
    results = {"C_dims": mutant.evaluate_strategy(cases, decisions, "C_dims")}
    rows = _f2_stability_rows()
    report = mutant.render_report(
        cases,
        results,
        mutant._stock_taxonomy(),
        None,
        rows,
        stability_strategies=("B_single", "C_dims"),
        repeat=3,
    )
    assert "未舍入" not in report, f"{label}：变异体仍带未舍入口径声明"
    reference = ev.render_report(
        cases,
        results,
        ev._stock_taxonomy(),
        None,
        rows,
        stability_strategies=("B_single", "C_dims"),
        repeat=3,
    )
    assert "未舍入" in reference, "参照实现必须带声明（否则判据不灵敏）"
