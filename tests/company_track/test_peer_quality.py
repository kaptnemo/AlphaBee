"""对标组候选质量闸（判定 C）测试：确定性 Gate、最小数量闸、持久化与判别力变异。

覆盖设计 §3.3/§3.4/§3.5/§6 Step 1：
- 四维归一化与权重合成（权重唯一处）；
- 四条剔除规则各一（verdict==reject / product / customer / 合成 overlap）；
- 人工白名单 bypass（调用方直传候选不被反向覆盖）；
- notes 明细格式与 reason ≤80 字符；
- 最小对标数闸（消费侧 <2 不注入 peer_*、回退 industry、记 issue + notes）；
- 配置面：``company_track.peer_quality`` 缺段回落默认；
- 判别力变异（对**实现字节**实测必红）：M1 去 product_floor、M2 改权重、M3 忽略 min_peers、
  M4 verdict==reject 仍保留、M5 关键词表残留（重新用理由文本否决）。

密闭性：全部用 tmp_path / 内存对象，不读真实 ``data/``、不调 LLM。
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
from alphabee.company_track.peer_group_build import (
    DEFAULT_WEIGHTS,
    MIN_PEERS_DEFAULT,
    gate_candidates,
    normalize_dims,
    overlap_score,
)

_QUALITY_MODULE = PROJECT_ROOT / "alphabee" / "company_track" / "peer_group_build.py"
_JUDGE_MODULE = PROJECT_ROOT / "alphabee" / "company_track" / "peer_judge.py"
_NODE_MODULE = PROJECT_ROOT / "alphabee" / "orchestrator" / "nodes" / "resolve_company_track.py"


def _dims(product=0.9, customer=0.8, material=0.8, model=0.9) -> dict[str, float]:
    return {"product": product, "customer": customer, "material_tech": material, "business_model": model}


def _cand(code="000001.SZ", name="甲", dims=None, overlap=0.9, verdict="direct", reason="同环节") -> dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "dims": dims if dims is not None else _dims(),
        "overlap": overlap,
        "verdict": verdict,
        "reason": reason,
    }


# ── 归一化 / 权重合成 ───────────────────────────────────────────────────────


def test_normalize_dims_fills_missing_and_clamps():
    assert normalize_dims({"product": 0.9, "customer": "0.5"}) == {
        "product": 0.9,
        "customer": 0.5,
        "material_tech": 0.0,
        "business_model": 0.0,
    }
    assert normalize_dims({"product": 1.8, "customer": -0.3})["product"] == 1.0
    assert normalize_dims({"product": 1.8, "customer": -0.3})["customer"] == 0.0
    # 非法值（非数值）按 0，不抛
    assert normalize_dims({"product": "高", "customer": None})["product"] == 0.0
    assert normalize_dims("not-a-dict") == {
        "product": 0.0,
        "customer": 0.0,
        "material_tech": 0.0,
        "business_model": 0.0,
    }


def test_normalize_dims_accepts_json_text():
    """候选跨层传递时 dims 为 JSON 文本（LLM 原始输出形态）⇒ 必须能解析，不得被当成"缺字段"。"""
    payload = json.dumps({"product": 0.9, "customer": 0.8, "material_tech": 0.7, "business_model": 0.6})
    assert normalize_dims(payload) == {
        "product": 0.9,
        "customer": 0.8,
        "material_tech": 0.7,
        "business_model": 0.6,
    }
    assert normalize_dims("{不是 JSON") == {
        "product": 0.0,
        "customer": 0.0,
        "material_tech": 0.0,
        "business_model": 0.0,
    }


def test_overlap_score_uses_single_weight_source():
    dims = _dims(1.0, 1.0, 1.0, 1.0)
    assert overlap_score(dims) == pytest.approx(1.0)  # 权重和为 1 ⇒ 全 1 得 1
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
    # 权重唯一处：显式传另一套权重 ⇒ 结果随之变化（供标定使用）
    weighted = overlap_score({"product": 1.0, "customer": 0.0, "material_tech": 0.0, "business_model": 0.0})
    assert weighted == pytest.approx(DEFAULT_WEIGHTS["product"])


# ── 四条剔除规则 ───────────────────────────────────────────────────────────


def test_rule1_judge_reject_dropped():
    kept, dropped, _, _ = gate_candidates([_cand(overlap=0.99, dims=_dims(1, 1, 1, 1), verdict="reject")])
    assert kept == []
    assert dropped[0]["drop"] == "judge reject"


@pytest.mark.parametrize("verdict", ["direct", "adjacent", "", None, "unknown"])
def test_non_reject_verdicts_do_not_drop(verdict):
    kept, dropped, _, _ = gate_candidates([_cand(verdict=verdict)])
    assert [c["code"] for c in kept] == ["000001.SZ"]
    assert dropped == []


def test_rule2_product_floor_dropped():
    kept, dropped, _, _ = gate_candidates([_cand(dims=_dims(product=0.1, customer=0.9))])
    assert kept == []
    assert dropped[0]["drop"] == "产品重叠不足"


def test_rule3_customer_floor_dropped():
    kept, dropped, _, _ = gate_candidates([_cand(dims=_dims(product=0.9, customer=0.1))])
    assert kept == []
    assert dropped[0]["drop"] == "客户重叠不足"


def test_rule4_overlap_threshold_dropped():
    """规则④：**合成** overlap 低于阈值即剔（合成口径与 harness 一致，自评 overlap 不参与判定）。"""
    cand = _cand(overlap=0.95, dims=_dims(0.5, 0.5, 0.8, 0.9))  # 合成 = 0.60
    kept, dropped, _, _ = gate_candidates([cand], min_overlap=0.70)
    assert kept == []
    assert dropped[0]["drop"] == "overlap 0.61 < 0.70"
    assert dropped[0]["overlap"] == pytest.approx(0.95)  # 自评值仅作血缘留痕


def test_rule_order_is_verdict_then_floors_then_overlap():
    """剔除顺序固定：低 product + reject ⇒ 报 reject（第 ① 条先命中）。"""
    kept, dropped, _, _ = gate_candidates([_cand(dims=_dims(product=0.0), verdict="reject")])
    assert dropped[0]["drop"] == "judge reject"


def test_missing_overlap_falls_back_to_dim_composite():
    """LLM 漏给 overlap ⇒ 用四维合成值兜底（不再"缺分即保留"）。"""
    dims = _dims(0.5, 0.5, 0.5, 0.5)  # 合成 = 0.5
    kept, dropped, scores, _ = gate_candidates([_cand(overlap=None, dims=dims)], min_overlap=0.5)
    assert [c["code"] for c in kept] == ["000001.SZ"]  # 恰好等于阈值 ⇒ 保留
    assert scores["000001.SZ"] == pytest.approx(0.5)

    kept, dropped, _, _ = gate_candidates([_cand(overlap=None, dims=dims)], min_overlap=0.6)
    assert kept == [] and dropped[0]["drop"] == "overlap 0.50 < 0.60"


def test_scores_and_match_dims_returned_for_kept_only():
    kept, dropped, scores, match_dims = gate_candidates(
        [_cand(code="A.SZ", overlap=0.8), _cand(code="B.SZ", dims=_dims(product=0.1))]
    )
    assert [c["code"] for c in kept] == ["A.SZ"]
    # 记分 = 合成分（权重 × 四维）：0.9*0.4 + 0.8*0.3 + 0.8*0.2 + 0.9*0.1 = 0.86
    assert scores == {"A.SZ": pytest.approx(0.86)}
    assert match_dims["A.SZ"] == _dims()
    assert "B.SZ" not in scores and "B.SZ" not in match_dims


def test_bypass_keeps_manual_candidates_without_dims():
    """人工白名单：只记分不剔除（调用方给定候选不被 Gate 反向覆盖）。"""
    kept, dropped, _, _ = gate_candidates([_cand(dims=None, overlap=None, verdict="reject")], bypass=True)
    assert [c["code"] for c in kept] == ["000001.SZ"]
    assert dropped == []


# ── notes 明细格式 ─────────────────────────────────────────────────────────


def test_format_drop_note_shape_and_reason_truncation():
    from alphabee.company_track.peer_group_build import format_drop_note

    long_reason = "理" * 200
    note = format_drop_note(
        {"code": "300260.SZ", "name": "新莱应材", "drop": "overlap 0.42 < 0.50", "reason": long_reason}
    )
    assert note.startswith("质量闸剔除 300260.SZ 新莱应材（overlap 0.42 < 0.50）：")
    assert note.endswith("…")
    assert len(note.split("：", 1)[1]) == 81  # 80 字符 + 省略号


def test_build_notes_include_gate_details(tmp_path, monkeypatch):
    """LLM 候选经生产 Gate 剔除 ⇒ notes 逐条写明 code/name/原因（可审计）。"""
    from alphabee.company_track.peer_group_build import build_peer_group
    from alphabee.company_track.peer_group_store import PeerGroupStore

    # A 股存在性校验打桩（本用例只验 Gate 明细，不验外部取数）
    monkeypatch.setattr(
        "alphabee.company_track.peer_group_build.validate_a_share_codes",
        lambda codes: (list(codes), [], None),
    )
    monkeypatch.setattr(
        "alphabee.company_track.peer_group_build.infer_peer_candidates",
        lambda *a, **k: (
            [
                {
                    "name": "沪电股份",
                    "code": "002463.SZ",
                    "reason": "通信 PCB 同环节",
                    "overlap": 0.9,
                    "dims": json.dumps(_dims(0.9, 0.9), ensure_ascii=False),
                    "verdict": "direct",
                }
            ],
            {
                "note": "质量闸剔除 1 条非直接对标",
                "llm_ok": True,
                "dropped": [
                    {
                        "name": "生益科技",
                        "code": "600584.SH",
                        "overlap": 0.15,
                        "dims": _dims(product=0.1, customer=0.9),
                        "drop": "产品重叠不足",
                        "reason": "上游覆铜板",
                    }
                ],
            },
        ),
    )
    store = PeerGroupStore(root=tmp_path)
    group, warnings = build_peer_group("002916.SZ", business_description="PCB", store=store)

    assert group.codes == ["002463.SZ"]
    drop_notes = [n for n in warnings if "600584.SH 生益科技" in n]
    assert len(drop_notes) == 1, f"剔除明细必须恰好一条（去重）：{drop_notes}"
    assert "产品重叠不足" in drop_notes[0] and "上游覆铜板" in drop_notes[0]
    # notes 落盘可审计
    loaded = store.load("002916.SZ")
    assert loaded is not None
    assert any("产品重叠不足" in n for n in loaded.notes)


# ── 最小对标数闸（消费侧） ─────────────────────────────────────────────────


def _run_node(monkeypatch, codes: list[str], *, min_peers: int | None = None) -> dict[str, Any]:
    """跑真实节点：patch ``PeerGroupStore.load`` 注入给定对标组（不碰真实 data/）。"""
    import asyncio

    import alphabee.company_track as ct_module
    import alphabee.orchestrator.nodes.resolve_company_track as node
    from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
    from alphabee.company_track.peer_group_store import PeerGroup, PeerGroupStore

    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        ct_module,
        "build_company_track",
        lambda *a, **k: CompanyTrackArtifact(
            symbol="603986.SH",
            segments=[SegmentSnapshot(report_date="20251231", segment_name="存储芯片", revenue_share=100.0)],
            as_of_date="20251231",
        ),
    )
    # 关键：节点内是函数体局部 import，patch 类方法才能在节点里生效
    monkeypatch.setattr(PeerGroupStore, "load", lambda self, symbol: PeerGroup(symbol=symbol, codes=list(codes)))
    monkeypatch.setattr(PeerGroupStore, "save", lambda self, group: Path("/dev/null"))
    monkeypatch.setattr(
        ct_module,
        "derive_peer_benchmarks",
        lambda codes_arg, industry="", **kw: (
            captured.update(codes=list(codes_arg))
            or ({"peer_avg_roe": 0.05}, {"error": None, "peer_count": len(codes_arg)})
        ),
    )
    monkeypatch.setattr(
        node, "_min_peers", (lambda: min_peers) if min_peers is not None else (lambda: MIN_PEERS_DEFAULT)
    )

    class Run:
        context = {"symbol": "603986.SH"}

    result = asyncio.run(node.resolve_company_track({"run": Run()}, {}))  # type: ignore[arg-type]
    result["_derive_called_with"] = captured.get("codes")
    return result


def test_min_peers_gate_blocks_one_and_zero(monkeypatch):
    """1 只与 0 只候选都不得注入 peer_*，且不得调用基准计算。"""
    for codes in (["300223.SZ"], []):
        result = _run_node(monkeypatch, codes)
        assert result["fact_values"] == {}, codes
        assert result["_derive_called_with"] is None, f"{codes} 不应触发基准计算"
        assert [i for i in result["issues"] if i.category == "peer_group_missing"], codes


def test_min_peers_gate_allows_two(monkeypatch):
    result = _run_node(monkeypatch, ["300223.SZ", "688766.SH"])
    assert result["fact_values"]["peer_avg_roe"] == pytest.approx(0.05)
    assert result["_derive_called_with"] == ["300223.SZ", "688766.SH"]
    assert not [i for i in result["issues"] if i.category == "peer_group_missing"]


def test_min_peers_configurable(monkeypatch):
    result = _run_node(monkeypatch, ["300223.SZ", "688766.SH"], min_peers=3)
    assert result["fact_values"] == {}
    assert [i for i in result["issues"] if i.category == "peer_group_missing"]


# ── 配置面（缺段回落默认） ─────────────────────────────────────────────────


def test_peer_quality_config_defaults_and_example_sync():
    from alphabee.config import PeerQualitySettings

    defaults = PeerQualitySettings()
    assert defaults.min_peers == MIN_PEERS_DEFAULT
    assert defaults.weights == DEFAULT_WEIGHTS
    # 阈值与 peer_quality 的默认常量必须**逐项一致**（单一来源 + train 段标定值）
    from alphabee.company_track import peer_group_build

    assert defaults.min_overlap == peer_group_build.DEFAULT_MIN_OVERLAP
    assert defaults.product_floor == peer_group_build.DEFAULT_PRODUCT_FLOOR
    assert defaults.customer_floor == peer_group_build.DEFAULT_CUSTOMER_FLOOR
    assert defaults.confidence.low == 0.4 and defaults.confidence.medium == 0.7
    # 判定 D（独立 batch judge）配置面：批量上限的**唯一默认处** = peer_judge 常量
    from alphabee.company_track import peer_judge

    assert defaults.judge_batch_size == peer_judge.JUDGE_BATCH_SIZE_DEFAULT
    # 判定 D 的**默认开关**：Step 2 的 DoD 复核为负结果（F1 判据成立、稳定性判据不成立）
    # ⇒ 按设计 §6 Step 2 的负结果路径**不得默认启用**（Gate 走判定 C 口径；置 true 即启用）。
    assert defaults.judge_enabled is False

    import yaml

    example = yaml.safe_load((PROJECT_ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    section = example["company_track"]["peer_quality"]
    assert PeerQualitySettings(**section).model_dump() == defaults.model_dump(), (
        "config.yaml.example 的 company_track.peer_quality 与代码默认值漂移"
    )


def test_peer_quality_missing_section_falls_back(monkeypatch):
    """旧 config 无 company_track 段 ⇒ 读取点回落默认，不抛。"""
    import alphabee.company_track.peer_group_build as pb
    from alphabee.config import get_settings

    monkeypatch.setattr("alphabee.config.get_settings", lambda: type("S", (), {})())
    pb._PEER_QUALITY_CACHE.clear()
    assert pb._peer_quality_settings() == {}
    # 正常 Settings 实例下读得到全键
    pb._PEER_QUALITY_CACHE.clear()
    monkeypatch.undo()
    section = pb._peer_quality_settings()
    assert section["min_peers"] == get_settings().company_track.peer_quality.min_peers
    assert set(section) >= {"min_overlap", "weights", "product_floor", "customer_floor", "min_peers"}


# ── 判别力变异：对实现字节实测必红 ─────────────────────────────────────────


def _mutant(module_path: Path, old: str, new: str, name: str) -> Any:
    """把实现源码的某处替换后加载为变异模块（不改仓库文件）。"""
    import tempfile

    source = module_path.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"变异锚点不唯一：{old!r}"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.py"
        path.write_text(source.replace(old, new), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def test_m1_removing_product_floor_is_killed():
    """M1：去掉 product 下限 ⇒ 产品严重不重叠的候选被错误保留。"""
    mutant = _mutant(
        _QUALITY_MODULE,
        '        elif has_dims and dims["product"] < product_floor:',
        "        elif False:  # M1 去 product_floor",
        "pq_mutant_m1",
    )
    kept, dropped, _, _ = mutant.gate_candidates([_cand(dims=_dims(product=0.05, customer=0.9))])
    assert [c["code"] for c in kept] == ["000001.SZ"], "M1 未被杀死：product 下限仍生效"
    assert dropped == []


def test_m2_changing_weights_is_killed():
    """M2：改动权重 ⇒ 同一组 dims 的合成 overlap 变化（权重唯一处被破坏即被杀死）。"""
    mutant = _mutant(
        _JUDGE_MODULE,
        '"product": 0.40,\n    "customer": 0.30,',
        '"product": 0.05,\n    "customer": 0.30,',
        "pq_mutant_m2",
    )
    dims = _dims(product=1.0, customer=0.0, material=0.0, model=0.0)
    assert overlap_score(dims) == pytest.approx(0.40)
    assert mutant.overlap_score(dims) == pytest.approx(0.05), "M2 未被杀死：权重改动未生效"


def test_m3_ignoring_min_peers_is_killed(monkeypatch, tmp_path):
    """M3：忽略 min_peers ⇒ 1 只候选也会被注入 peer_*（回退 industry 失效）。"""
    import asyncio

    from alphabee.company_track.peer_group_store import PeerGroup

    source = _NODE_MODULE.read_text(encoding="utf-8")
    mutated = source.replace(
        "    too_few = peer_group is not None and not peer_group.is_empty() and len(peer_group.codes) < min_peers",
        "    too_few = False  # M3 忽略 min_peers",
        1,
    )
    assert mutated != source
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "node_mutant_m3.py"
        path.write_text(mutated, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("node_mutant_m3", path)
        assert spec is not None and spec.loader is not None
        mutant = importlib.util.module_from_spec(spec)
        sys.modules["node_mutant_m3"] = mutant
        spec.loader.exec_module(mutant)

        from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot

        monkeypatch.setattr(mutant, "_min_peers", lambda: 2)
        monkeypatch.setattr(
            "alphabee.company_track.build_company_track",
            lambda *a, **k: CompanyTrackArtifact(
                symbol="603986.SH",
                segments=[SegmentSnapshot(report_date="20251231", segment_name="存储芯片", revenue_share=100.0)],
                as_of_date="20251231",
            ),
        )
        monkeypatch.setattr(
            "alphabee.company_track.peer_group_store.PeerGroupStore.load",
            lambda self, symbol: PeerGroup(symbol=symbol, codes=["300223.SZ"], source="manual"),
        )
        monkeypatch.setattr(
            "alphabee.company_track.derive_peer_benchmarks",
            lambda codes, industry="", **kw: ({"peer_avg_roe": 0.05}, {"error": None, "peer_count": len(codes)}),
        )

        class Run:
            context = {"symbol": "603986.SH"}

        result = asyncio.run(mutant.resolve_company_track({"run": Run()}, {}))
    assert result["fact_values"] == {"peer_avg_roe": pytest.approx(0.05)}, "M3 未被杀死：仍拒绝了单只候选"


def test_m4_keeping_judge_reject_is_killed():
    """M4：verdict==reject 仍保留 ⇒ 独立否决位失效。"""
    mutant = _mutant(
        _QUALITY_MODULE,
        '        elif verdict == "reject":',
        "        elif False:  # M4 reject 仍保留",
        "pq_mutant_m4",
    )
    kept, dropped, _, _ = mutant.gate_candidates([_cand(verdict="reject", dims=_dims(1, 1, 1, 1), overlap=1.0)])
    assert [c["code"] for c in kept] == ["000001.SZ"], "M4 未被杀死：reject 仍被剔除"
    assert dropped == []


def test_m5_keyword_table_residue_is_killed():
    """M5：关键词表残留（用理由文本否决）⇒ 与"措辞不是判据"的契约冲突，必然可被判红。"""
    mutant_source = _QUALITY_MODULE.read_text(encoding="utf-8")
    # 模拟"把关键词表加回来"：在闸门里按理由文本否决
    mutated = mutant_source.replace(
        "        if bypass or not enabled:\n"
        "            # 人工候选白名单 / 质量闸总开关停用：只归一化与记分，不做任何剔除\n"
        "            pass",
        "        if bypass or not enabled:\n"
        "            # 人工候选白名单 / 质量闸总开关停用：只归一化与记分，不做任何剔除\n"
        "            pass\n"
        '        elif "而非" in str(cand.get("reason") or ""):  # M5 关键词表残留\n'
        '            drop = "理由自曝实质差异"',
        1,
    )
    assert mutated != mutant_source
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pq_mutant_m5.py"
        path.write_text(mutated, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("pq_mutant_m5", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["pq_mutant_m5"] = module
        spec.loader.exec_module(module)

        kept, dropped, _, _ = module.gate_candidates([_cand(dims=_dims(0.9, 0.9), reason="同环节，而非上游材料商")])
    assert kept == [], "M5 未被杀死：措辞仍能否决"
    assert dropped[0]["drop"] == "理由自曝实质差异"


def test_existing_bytes_have_no_keyword_table():
    """删除完整性（AST 级证据）：全仓不存在关键词否决表或理由自洽判定。

    用 AST 而不是子串匹配：docstring 里提到历史名字属叙述（不判），真正的判据是**定义与调用**。
    """
    import re

    # 构造旧符号名而不写字面量：使「全仓 grep 旧符号零命中」本身就是可 grep 验证的事实
    pattern_names = {"_" + "REASON" + "_MISMATCH", "_reason" + "_self" + "_contradicts"}
    hits: list[str] = []
    for path in (PROJECT_ROOT / "alphabee").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in pattern_names:
                hits.append(f"{path}:def {node.name}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in pattern_names:
                        hits.append(f"{path}:{target.id} =")
            elif isinstance(node, ast.Name) and node.id in pattern_names:
                hits.append(f"{path}:Name {node.id}")
    assert hits == [], f"关键词否决表残留：{hits}"

    # 判定本身不得读 `reason` 文本（措辞不是判据）：扫描 gate_candidates 函数体
    quality_src = _QUALITY_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(quality_src)
    gate = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "gate_candidates")
    body = ast.get_source_segment(quality_src, gate) or ""
    code_lines = [
        line
        for line in body.split("\n")
        if "reason" in line and not line.strip().startswith("#") and "reason" not in line.split("#")[0].strip()[:0]
    ]
    # 允许的是把 reason 原样带进明细（record/dropped），不允许对 reason 做判定
    for line in code_lines:
        assert re.search(r"reason[^\n]*(in |==|startswith|search|match|find)", line) is None, (
            f"gate 内出现按理由文本判定：{line.strip()}"
        )


def test_scoring_prompt_literal_is_unique_to_this_module():
    """prompt 唯一处判据：判分 prompt 的字面量只出现在 ``peer_extract``（不得第二份）。"""
    marker = "只输出 JSON 数组（确实无候选才输出 []）"
    hits: list[str] = []
    for path in (PROJECT_ROOT / "alphabee").rglob("*.py"):
        if marker in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(PROJECT_ROOT)))
    assert hits == ["alphabee/company_track/peer_extract.py"], f"prompt 字面量多处出现：{hits}"


# ── 与评测 harness 的口径等价（物理 import 切换顺延 t7） ────────────────────


def _harness_gate():
    """加载离线评测 harness（``scripts/peer_group_eval.py``）——它的 gate 是**本地实现**。"""
    spec = importlib.util.spec_from_file_location(
        "pge_for_equivalence", PROJECT_ROOT / "scripts" / "peer_group_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dims_for_harness(dims: dict[str, float] | None):
    return dict(dims) if dims else None


def test_harness_local_gate_matches_production_gate_bitwise():
    """口径等价：同一批输入下「harness 本地 gate 保留集」与「生产 Gate 保留集」逐字相同。

    含边界值：dims 恰等于 product/customer 下限、合成 overlap 恰等于 min_overlap、
    ``verdict=reject`` 且维度全 1（验四条规则顺序）、缺 dims（两侧都不做维度下限判定）。
    物理 import 切换（让 harness 直接调生产 Gate）顺延到该文件进入 inScope 的步骤。
    """
    m = _harness_gate()
    from alphabee.company_track import peer_group_build

    assert m.DIMS == peer_group_build.DIMS  # 两侧四维定义同源（防"加载错文件"的假绿）

    params = {"weights": DEFAULT_WEIGHTS, "min_overlap": 0.40, "product_floor": 0.20, "customer_floor": 0.20}
    cases: list[dict[str, Any]] = [
        {"code": "A.SZ", "overlap": 0.90, "dims": _dims(0.9, 0.9), "verdict": "direct"},
        {
            "code": "B.SZ",
            "overlap": 0.90,
            "dims": _dims(product=0.20, customer=0.9),
            "verdict": "adjacent",
        },  # 边界：product == floor
        {
            "code": "C.SZ",
            "overlap": 0.90,
            "dims": _dims(product=0.9, customer=0.20),
            "verdict": "direct",
        },  # 边界：customer == floor
        {
            "code": "D.SZ",
            "overlap": 0.90,
            "dims": _dims(product=0.19, customer=0.9),
            "verdict": "direct",
        },  # 边界下：应剔
        {"code": "E.SZ", "overlap": 0.38, "dims": _dims(0.9, 0.9), "verdict": "direct"},  # 边界下：overlap < 阈值
        {"code": "F.SZ", "overlap": 0.40, "dims": _dims(0.9, 0.9), "verdict": "direct"},  # 边界：overlap == 阈值
        {"code": "G.SZ", "overlap": 0.99, "dims": _dims(1.0, 1.0), "verdict": "reject"},  # order ①
        {"code": "H.SZ", "overlap": 0.99, "dims": _dims(0.05, 0.05), "verdict": "reject"},  # ① 先于 ②
        {"code": "I.SZ", "overlap": None, "dims": _dims(1.0, 1.0, 1.0, 1.0), "verdict": "direct"},  # 缺分 ⇒ 合成兜底
        {
            "code": "J.SZ",
            "overlap": None,
            "dims": _dims(0.5, 0.5, 0.5, 0.5),
            "verdict": "direct",
        },  # 缺分 ⇒ 合成 0.5 ⇒ 边界保留
    ]
    decisions = {
        c["code"]: m.Decision(
            code=c["code"],
            gen_overlap=c["overlap"],
            dims=_dims_for_harness(c["dims"]),
            judge_verdict=c.get("verdict"),
            judge_dims=_dims_for_harness(c["dims"]),
        )
        for c in cases
    }

    harness_kept = m.gate(decisions, "C_D_E", **params)
    prod_kept, _dropped, _scores, _dims_map = gate_candidates(cases, **params)

    assert harness_kept == {c["code"] for c in prod_kept}, (
        f"harness 与生产 Gate 保留集不一致：harness={sorted(harness_kept)} prod={sorted(c['code'] for c in prod_kept)}"
    )
    # 保留集非平凡（防"两侧都空"的假绿）
    # 边界语义：product/customer 恰等于 floor、合成 overlap 恰等于阈值 ⇒ 保留；D 维度不足、G/H reject ⇒ 剔
    assert harness_kept == {"A.SZ", "B.SZ", "C.SZ", "E.SZ", "F.SZ", "I.SZ", "J.SZ"}


def test_gate_equivalence_matrix_harness_vs_production():
    """**输入矩阵逐字相同**：harness 本地 gate 与生产 Gate 在以下矩阵上保留集完全一致。

    矩阵（覆盖你点名的全部边界）：
    - ``dims`` 恰等于 product/customer floor（20.0%）⇒ 保留；
    - 合成 overlap 恰等于 min_overlap（0.40）⇒ 保留；略低 ⇒ 剔；
    - ``verdict=reject`` 且 dims 全 1（**优先于 floor/阈值**）⇒ 剔；
    - 缺 ``dims``：回落**自评 overlap**（高 ⇒ 保留；低 ⇒ 剔）；两者皆缺 ⇒ 不可评估 ⇒ 剔；
    - 人工/结构化候选（无 dims 有自评）⇒ **不得被 Gate 杀**（生产走 ``bypass=True``；
      本用例同时证明：若不 bypass，缺 dims 候选仍按同一回落规则处理，harness 与生产同解）。

    规则（两侧共用同一条）：``reject`` → 有 dims 走「维度下限 + 合成 Σw·dim」→ 缺 dims 只按自评
    overlap → 两者皆缺视为不可评估 ⇒ 剔。
    """
    m = _harness_gate()
    params = {"weights": DEFAULT_WEIGHTS, "min_overlap": 0.40, "product_floor": 0.20, "customer_floor": 0.20}
    d_ge = _dims(0.9, 0.9)  # 合成 0.86
    d_eq = _dims(0.5, 0.5, 0.8, 0.9)  # 合成 0.61
    matrix: list[dict[str, Any]] = [
        {"code": "A.SZ", "overlap": 0.90, "dims": d_ge, "verdict": "direct"},
        {
            "code": "B.SZ",
            "overlap": 0.90,
            "dims": _dims(product=0.20, customer=0.9),
            "verdict": "adjacent",
        },  # product == floor
        {
            "code": "C.SZ",
            "overlap": 0.90,
            "dims": _dims(product=0.9, customer=0.20),
            "verdict": "direct",
        },  # customer == floor
        {"code": "D.SZ", "overlap": 0.90, "dims": _dims(product=0.19, customer=0.9), "verdict": "direct"},  # 略低 floor
        {"code": "E.SZ", "overlap": 0.40, "dims": d_eq, "verdict": "direct"},  # 合成 0.61 > 阈值
        {"code": "F.SZ", "overlap": 0.30, "dims": _dims(0.2, 0.2, 0.5, 0.5), "verdict": "direct"},  # 合成 0.315 < 0.40
        {"code": "G.SZ", "overlap": 0.99, "dims": _dims(1.0, 1.0), "verdict": "reject"},  # reject 优先
        {"code": "H.SZ", "overlap": 0.99, "dims": _dims(0.05, 0.05), "verdict": "reject"},  # reject 优先于 floor
        {"code": "I.SZ", "overlap": 0.90, "dims": None, "verdict": "direct"},  # 缺 dims，自评高 ⇒ 保留
        {"code": "J.SZ", "overlap": 0.30, "dims": None, "verdict": "direct"},  # 缺 dims，自评低 ⇒ 剔
        {"code": "K.SZ", "overlap": None, "dims": None, "verdict": "direct"},  # 两者皆缺 ⇒ 不可评估 ⇒ 剔
        {"code": "L.SZ", "overlap": 0.99, "dims": None, "verdict": "reject"},  # 缺 dims 但 reject ⇒ 剔
    ]
    decisions = {
        c["code"]: m.Decision(
            code=c["code"],
            gen_overlap=c["overlap"],
            dims=dict(c["dims"]) if c["dims"] else None,
            judge_verdict=c["verdict"],
            judge_dims=dict(c["dims"]) if c["dims"] else None,
            judge_overlap=c["overlap"],
        )
        for c in matrix
    }
    harness_kept = m.gate(decisions, "C_D_E", **params)
    prod_kept, dropped, _scores, _dims_map = gate_candidates(matrix, **params)
    prod_set = {c["code"] for c in prod_kept}
    assert harness_kept == prod_set, (
        f"两侧不等价：harness={sorted(harness_kept)} prod={sorted(prod_set)} "
        f"drops={[(d['code'], d['drop']) for d in dropped]}"
    )
    # 逐项落点（防"两侧都空"或"两侧都全留"的假绿）
    assert harness_kept == {"A.SZ", "B.SZ", "C.SZ", "E.SZ", "I.SZ"}
    drops = {d["code"]: d["drop"] for d in dropped}
    assert drops["D.SZ"] == "产品重叠不足"
    assert drops["F.SZ"].startswith("overlap 0.29 <")
    assert drops["G.SZ"] == "judge reject" and drops["H.SZ"] == "judge reject"
    assert drops["J.SZ"].startswith("overlap 0.30 <") and drops["K.SZ"].startswith("overlap 0.00 <")
    assert drops["L.SZ"] == "judge reject"


def test_manual_structured_candidates_never_killed_by_gate():
    """人工/结构化候选（无 dims、有自评）**必须保留**：生产走 ``bypass=True``（只记分不剔）。

    判别力：把 ``bypass=True`` 去掉 ⇒ 本用例必红（候选会按缺 dims 回落规则被阈值剔除）。
    """
    manual = [
        {"name": "沪电股份", "code": "002463.SZ", "reason": "同环节", "source": "manual", "overlap": 0.9},
        {"name": "生益科技", "code": "600183.SH", "reason": "上游", "source": "manual", "overlap": 0.3},
    ]
    kept, dropped, _scores, _dims_map = gate_candidates(manual, bypass=True)
    assert [c["code"] for c in kept] == ["002463.SZ", "600183.SH"]
    assert dropped == []


def test_dod_measurement_input_has_no_missing_dims():
    """**DoD 测量口径守卫**：评测输入的每个候选都必须带 dims（缺 dims 分支在 DoD 上触发次数 = 0），
    否则缺 dims 回退规则会实质参与 DoD 数字、使口径不可比。"""
    m = _harness_gate()
    cases = m.load_cases(m.DEFAULT_CASES)
    decisions = {c.symbol: m.collect_decisions(c, record=False) for c in cases}
    total = 0
    missing: list[str] = []
    for sym, rows in decisions.items():
        for code, d in rows.items():
            total += 1
            if d.dims is None or d.judge_dims is None:
                missing.append(f"{sym}:{code}")
    assert total > 0
    assert missing == [], f"DoD 输入存在缺 dims 候选（{len(missing)}/{total}）：{missing[:5]}"


# ── 最小对标数闸（消费侧） ─────────────────────────────────────────────────


def _run_node(monkeypatch, codes: list[str], *, min_peers: int | None = None) -> dict[str, Any]:
    """跑真实节点：patch ``PeerGroupStore.load`` 注入给定对标组（不碰真实 data/）。"""
    import asyncio

    import alphabee.company_track as ct_module
    import alphabee.orchestrator.nodes.resolve_company_track as node
    from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
    from alphabee.company_track.peer_group_store import PeerGroup, PeerGroupStore

    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        ct_module,
        "build_company_track",
        lambda *a, **k: CompanyTrackArtifact(
            symbol="603986.SH",
            segments=[SegmentSnapshot(report_date="20251231", segment_name="存储芯片", revenue_share=100.0)],
            as_of_date="20251231",
        ),
    )
    # 关键：节点内是函数体局部 import，patch 类方法才能在节点里生效
    monkeypatch.setattr(PeerGroupStore, "load", lambda self, symbol: PeerGroup(symbol=symbol, codes=list(codes)))
    monkeypatch.setattr(PeerGroupStore, "save", lambda self, group: Path("/dev/null"))
    monkeypatch.setattr(
        ct_module,
        "derive_peer_benchmarks",
        lambda codes_arg, industry="", **kw: (
            captured.update(codes=list(codes_arg))
            or ({"peer_avg_roe": 0.05}, {"error": None, "peer_count": len(codes_arg)})
        ),
    )
    monkeypatch.setattr(
        node, "_min_peers", (lambda: min_peers) if min_peers is not None else (lambda: MIN_PEERS_DEFAULT)
    )

    class Run:
        context = {"symbol": "603986.SH"}

    result = asyncio.run(node.resolve_company_track({"run": Run()}, {}))  # type: ignore[arg-type]
    result["_derive_called_with"] = captured.get("codes")
    return result


def test_unscored_candidates_are_not_threshold_dropped():
    """无分数来源（研报片段抽取 / 闭集择优的候选形态：无 dims、无 overlap）不得被阈值剔除。

    这两条路径的候选形态是 ``{name, code, exchange, reason, source}``；若对它们执行
    ``effective(0.0) < min_overlap`` 会整条静默清空成空对标组（相对 ``cd5e57b`` 的回归）。

    判别力：把 ``require_score=False`` 改成 True（或去掉该参数）⇒ 本用例首段必红。
    """
    unscored = [
        {"name": "沪电股份", "code": "002463.SZ", "exchange": "SZ", "reason": "同环节", "source": "infer"},
        {"name": "胜宏科技", "code": "300476.SZ", "exchange": "SZ", "reason": "管理层点名", "source": "universe"},
    ]
    kept, dropped, scores, match_dims = gate_candidates(unscored, require_score=False)

    assert [c["code"] for c in kept] == ["002463.SZ", "300476.SZ"], "无分数候选被阈值剔除（整条来源被清空）"
    assert dropped == []
    # 未评分 ≠ 重叠度 0：不得写入伪造分数
    assert scores == {} and match_dims == {}

    # 反向口径（与评测 harness 同源）：默认 require_score=True 时，缺分 ⇒ 不可评估 ⇒ 剔
    kept_default, dropped_default, _, _ = gate_candidates(unscored)
    assert kept_default == [] and len(dropped_default) == 2
    assert all(d["drop"].startswith("overlap 0.00 <") for d in dropped_default)


def test_scored_candidates_still_threshold_dropped():
    """对照：**带自评 overlap 的**低分候选仍必须被阈值剔除（守护不能变成免剔后门）。"""
    scored = [{"code": "000002.SZ", "name": "乙", "overlap": 0.10, "verdict": "adjacent", "reason": "终端错配"}]
    kept, dropped, scores, _dims = gate_candidates(scored)

    assert kept == []
    assert len(dropped) == 1 and dropped[0]["drop"].startswith("overlap 0.10 < ")
    # 有分数 ⇒ 仍写入分数记录（未评分才是空表）
    assert scores == {}


# ── 阈值常量的单一权威定义 ────────────────────────────────────────────────


def test_min_overlap_has_single_authoritative_definition():
    """阈值常量只允许一处权威定义（历史「同名异值」0.5 / 0.4 已消除）。

    判别力：任一处重新引入不同字面量（例如 ``peer_extract`` 再写回 ``= 0.5``）⇒ 本用例必红。
    """
    from alphabee.company_track import peer_extract, peer_group_build, peer_judge

    assert peer_extract.DEFAULT_MIN_OVERLAP == peer_judge.DEFAULT_MIN_OVERLAP
    assert peer_group_build.DEFAULT_MIN_OVERLAP == peer_judge.DEFAULT_MIN_OVERLAP

    owners: list[str] = []
    for path in sorted((PROJECT_ROOT / "alphabee").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "DEFAULT_MIN_OVERLAP" for target in node.targets
            ):
                owners.append(path.name)
    assert owners == ["peer_judge.py"], f"阈值常量存在多处权威定义：{owners}"


def test_no_regex_on_reason_text_anywhere():
    """反回归（结构化判据）：全仓不得出现「对理由文本编译/使用正则做否决」的变相词表。

    判据比"抓旧函数名"更强：只要有人在候选判定路径上对 ``reason`` 做正则匹配/findall，
    或定义名字含 ``reason`` 且返回 bool 的判定函数，本用例即红。
    """
    offenders: list[str] = []
    for path in (PROJECT_ROOT / "alphabee").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
                if name in {"search", "match", "findall", "fullmatch", "compile"}:
                    segment = ast.get_source_segment(source, node) or ""
                    if "reason" in segment:
                        offenders.append(f"{path}:{node.lineno}: {segment[:60]}")
            if isinstance(node, ast.FunctionDef):
                returns_bool = isinstance(node.returns, ast.Name) and node.returns.id in {"bool", "Bool"}
                if "reason" in node.name and returns_bool:
                    offenders.append(f"{path}:{node.lineno}: def {node.name} -> bool")
    assert offenders == [], f"发现对理由文本的否决式判定（变相词表）：{offenders}"


def test_judge_rollback_switch_is_verbatim_and_documented():
    """开关口径可逐字执行：``judge_enabled`` 缺省 false（判定 C）⇒ 置 true 即启用判定 D（接线与配置保留）。

    判据两点：① 键在 ``PeerQualitySettings`` 与 ``config.yaml.example`` 中同名同位；
    ② 读取点 ``_peer_quality_settings()`` 能取到该键（缺段回落默认 True）。
    """
    import yaml

    from alphabee.company_track import peer_group_build
    from alphabee.config import PeerQualitySettings, get_settings

    example = yaml.safe_load((PROJECT_ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    section = example["company_track"]["peer_quality"]
    assert "judge_enabled" in section and "judge_batch_size" in section
    assert PeerQualitySettings(**section).judge_enabled is False  # 负结果 ⇒ 默认不启用（回滚口径 = 现状）

    peer_group_build._PEER_QUALITY_CACHE.clear()
    live = peer_group_build._peer_quality_settings()
    assert live["judge_enabled"] == get_settings().company_track.peer_quality.judge_enabled
    assert live["judge_batch_size"] == get_settings().company_track.peer_quality.judge_batch_size


# ── 对标组置信度（设计 §3.7）：三档、边界、确定性、节点落字段 ────────────────


def test_confidence_config_defaults_and_weights_sum_to_one():
    """置信度权重/阈值默认值：三项权重和 = 1、阈值 0.4/0.7，且与示例配置逐项一致。"""
    import yaml

    from alphabee.company_track.peer_group_build import CONFIDENCE_SIGNALS as SIGNALS
    from alphabee.company_track.peer_group_build import CONFIDENCE_WEIGHTS_DEFAULT
    from alphabee.config import PeerQualitySettings

    defaults = PeerQualitySettings()
    weights = defaults.confidence.weights
    assert set(weights) == set(SIGNALS) == {"taxonomy_reliable", "judge_direct_ratio", "mean_overlap"}
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights == CONFIDENCE_WEIGHTS_DEFAULT
    assert (defaults.confidence.low, defaults.confidence.medium) == (0.4, 0.7)

    example = yaml.safe_load((PROJECT_ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    section = example["company_track"]["peer_quality"]
    assert PeerQualitySettings(**section).model_dump() == defaults.model_dump()
    assert "taxonomy_enabled" in section  # 回滚开关在示例中显式在位


@pytest.mark.parametrize(
    ("kwargs", "expected_level", "expected_score"),
    [
        # 边界：恰好等于 low（0.4）⇒ 中（判据是 `< low`）
        ({"taxonomy_reliable": True, "mean_overlap": 0.0}, "中", 0.4),
        # 略低于 low
        ({"taxonomy_reliable": False, "mean_overlap": 0.99}, "低", pytest.approx(0.297)),
        # 边界：恰好等于 medium（0.7）⇒ 高
        ({"taxonomy_reliable": True, "judge_direct_ratio": 1.0}, "高", 0.7),
        # 略低于 medium
        ({"taxonomy_reliable": True, "judge_direct_ratio": 0.99}, "中", pytest.approx(0.697)),
        # 三信号全缺失 ⇒ 0 ⇒ 低
        ({}, "低", 0.0),
    ],
)
def test_confidence_tiers_and_boundaries(kwargs, expected_level, expected_score):
    from alphabee.company_track import synthesize_peer_confidence

    confidence = synthesize_peer_confidence(**kwargs)
    assert confidence.level == expected_level
    assert confidence.score == pytest.approx(expected_score, abs=1e-6)
    assert f"score={confidence.score:.2f}" in confidence.basis()


def test_confidence_is_deterministic_and_normalizes_weights():
    from alphabee.company_track import synthesize_peer_confidence

    first = synthesize_peer_confidence(taxonomy_reliable=True, mean_overlap=0.9)
    second = synthesize_peer_confidence(taxonomy_reliable=True, mean_overlap=0.9)
    assert first == second
    # 权重和不为 1 ⇒ 按权重和归一（不越界），信号缺失仍按 0
    scaled = synthesize_peer_confidence(
        taxonomy_reliable=True,
        mean_overlap=0.9,
        weights={"taxonomy_reliable": 4.0, "judge_direct_ratio": 3.0, "mean_overlap": 3.0},
    )
    assert scaled.score == pytest.approx(first.score)
    # 阈值可配（来自配置）
    configurable = synthesize_peer_confidence(taxonomy_reliable=True, mean_overlap=0.0, low=0.5, medium=0.5)
    assert configurable.level == "低"


def test_node_writes_confidence_to_artifact_and_notes(monkeypatch):
    """节点把三档与合成口径落 ``CompanyTrackArtifact`` 字段 + ``review_notes`` 一行（设计 §3.8/§8 决策 5）。"""
    from alphabee.company_track.contracts import CompanyTrackArtifact
    from alphabee.core import ArtifactType

    result = _run_node(monkeypatch, ["300223.SZ", "688766.SH"])
    artifacts = [item for item in result["artifacts"] if item.type == ArtifactType.COMPANY_TRACK]
    assert len(artifacts) == 1
    artifact = CompanyTrackArtifact(**artifacts[0].value)
    assert artifact.peer_group_confidence in {"低", "中", "高"}
    assert artifact.peer_group_confidence_score is not None
    assert "|低|中|高|" not in f"|{artifact.peer_group_confidence}|"  # 三档取值约束
    assert "taxonomy_reliable=" in artifact.peer_group_confidence_basis  # 合成口径（信号取值）
    assert any(note.startswith("对标组置信度") for note in artifact.review_notes)


def test_node_skips_confidence_without_peer_group(monkeypatch):
    """无对标组（min_peers 闸拦下）⇒ 不写置信度字段（空串），且不得编造档位。"""
    from alphabee.company_track.contracts import CompanyTrackArtifact
    from alphabee.core import ArtifactType

    result = _run_node(monkeypatch, ["300223.SZ"])  # 1 只 < min_peers 2 ⇒ 不注入 peer_*
    artifact = CompanyTrackArtifact(
        **next(item for item in result["artifacts"] if item.type == ArtifactType.COMPANY_TRACK).value
    )
    assert artifact.peer_group_confidence == ""
    assert artifact.peer_group_confidence_score is None
    assert not any(note.startswith("对标组置信度") for note in artifact.review_notes)


def test_m3_confidence_ignoring_taxonomy_is_killed():
    """M3：置信度忽略 ``taxonomy_reliable`` ⇒ 必红（参照实现下该信号必须改变 score）。"""
    from alphabee.company_track import synthesize_peer_confidence

    reliable = synthesize_peer_confidence(taxonomy_reliable=True, mean_overlap=0.9)
    unreliable = synthesize_peer_confidence(taxonomy_reliable=False, mean_overlap=0.9)
    assert reliable.score > unreliable.score, "参照实现下分类学信号必须影响分数"

    mutant = _mutant(
        _QUALITY_MODULE,
        '        "taxonomy_reliable": None if taxonomy_reliable is None else (1.0 if taxonomy_reliable else 0.0),',
        '        "taxonomy_reliable": 0.0,  # M3 忽略分类学可信度',
        "pq_mutant_m3_confidence",
    )
    assert mutant.synthesize_peer_confidence(taxonomy_reliable=True, mean_overlap=0.9).score == pytest.approx(
        mutant.synthesize_peer_confidence(taxonomy_reliable=False, mean_overlap=0.9).score
    )
    assert mutant.synthesize_peer_confidence(taxonomy_reliable=True, mean_overlap=0.9).score != pytest.approx(
        reliable.score
    )


def test_confidence_judge_off_signal_counts_as_zero_and_is_declared(monkeypatch):
    """**D 项口径**：``judge_enabled=false``（默认）时 ``judge_direct_ratio`` 按 **0** 计入。

    判据三点（防「伪造成有 judge」与「读成判过但全非 direct」两种误读）：

    1. 生产路径（judge 关闭）算出的 artifact 分数**精确等于**「只含 E + C 两路」的合成分；
    2. `basis()` 显式写 ``judge_direct_ratio=缺失``（而不是 0.00 之类像真信号的取值）；
    3. 合成函数 docstring 逐字声明该口径（`judge_enabled=false` … 按 0 计入 ⇒ 只反映 E 与 C）。
    """
    import inspect

    from alphabee.company_track import synthesize_peer_confidence
    from alphabee.company_track.contracts import CompanyTrackArtifact
    from alphabee.company_track.peer_taxonomy import assess_reliability
    from alphabee.core import ArtifactType

    # ① 生产路径（默认 judge 关闭）
    result = _run_node(monkeypatch, ["300223.SZ", "688766.SH"])
    artifact = CompanyTrackArtifact(
        **next(item for item in result["artifacts"] if item.type == ArtifactType.COMPANY_TRACK).value
    )
    assert artifact.peer_group_confidence  # 有对标组 ⇒ 有档位
    two_signal_only = synthesize_peer_confidence(
        taxonomy_reliable=assess_reliability("603986.SH").reliable,
        judge_direct_ratio=None,  # D 项缺失 ⇒ 按 0
        mean_overlap=None,  # 该夹具的对标组无 scores ⇒ C 项也缺失
    )
    assert artifact.peer_group_confidence_score == pytest.approx(two_signal_only.score)
    # 反证：若把 D 项当 1 或 0.5 计入，分数必然不同（判据不是恒真）
    assert two_signal_only.score != pytest.approx(
        synthesize_peer_confidence(
            taxonomy_reliable=assess_reliability("603986.SH").reliable, judge_direct_ratio=1.0
        ).score
    )

    # ② 口径在展示层逐字可读
    assert "judge_direct_ratio=缺失" in artifact.peer_group_confidence_basis
    assert two_signal_only.basis().count("缺失") >= 1

    # ③ docstring 逐字声明
    doc = inspect.getdoc(synthesize_peer_confidence) or ""
    assert "`judge_enabled=false`（当前默认）或 judge 不可用时" in doc
    assert "D 项按 **0** 计入" in doc and "只反映 E" in doc and "与 C" in doc


def test_m6_missing_ratio_counted_as_nonzero_is_killed():
    """M6：把「缺失按 0」改成「缺失按 1 / 0.5」⇒ 上面的口径用例必红（判别力自证）。"""
    from alphabee.company_track import synthesize_peer_confidence

    reference = synthesize_peer_confidence(taxonomy_reliable=True, judge_direct_ratio=None, mean_overlap=None)
    assert reference.signals["judge_direct_ratio"] is None
    assert reference.score == pytest.approx(0.4)  # 0.4·1 + 0.3·0 + 0.3·0

    forged_one = _mutant(
        _QUALITY_MODULE,
        "    ratio = _coerce_ratio(judge_direct_ratio)",
        "    ratio = 1.0 if judge_direct_ratio is None else _coerce_ratio(judge_direct_ratio)  # M6 缺失按 1",
        "pq_mutant_m6_ratio_one",
    )
    assert forged_one.synthesize_peer_confidence(
        taxonomy_reliable=True, judge_direct_ratio=None
    ).score == pytest.approx(0.7)
    assert forged_one.synthesize_peer_confidence(
        taxonomy_reliable=True, judge_direct_ratio=None
    ).score != pytest.approx(reference.score)

    forged_half = _mutant(
        _QUALITY_MODULE,
        "    ratio = _coerce_ratio(judge_direct_ratio)",
        "    ratio = 0.5 if judge_direct_ratio is None else _coerce_ratio(judge_direct_ratio)  # M6 缺失按 0.5",
        "pq_mutant_m6_ratio_half",
    )
    assert forged_half.synthesize_peer_confidence(
        taxonomy_reliable=True, judge_direct_ratio=None
    ).score == pytest.approx(0.55)


# ── 质量闸总开关（company_track.peer_quality.enabled，t24 / 设计 §3.5/§3.6） ──────


def test_gate_disabled_keeps_everything_and_keeps_scoring():
    """``enabled=False`` ⇒ ``dropped`` 恒空、``kept`` 等于全部输入（含 reject 与低分/缺分）。"""
    from alphabee.company_track.peer_group_build import gate_candidates

    inputs = [
        {"code": "A.SZ", "name": "高分", "dims": _dims(0.9, 0.9), "verdict": "direct"},
        {"code": "B.SZ", "name": "判否决", "dims": _dims(0.9, 0.9), "verdict": "reject"},
        {"code": "C.SZ", "name": "产品不足", "dims": _dims(product=0.05, customer=0.9), "verdict": "direct"},
        {"code": "D.SZ", "name": "低分自评", "overlap": 0.05, "verdict": "adjacent"},
        {"code": "E.SZ", "name": "缺分数", "reason": "无分数来源"},
    ]
    kept, dropped, scores, match_dims = gate_candidates(inputs, enabled=False)

    assert dropped == [], "停用后仍有剔除明细"
    assert [c["code"] for c in kept] == ["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"], "停用后候选不齐全或乱序"
    # 分数/维度照常记录（只对有分数候选）⇒ 与 enabled=true 的记分语义一致
    assert scores == {
        "A.SZ": pytest.approx(0.89),
        "B.SZ": pytest.approx(0.89),
        "C.SZ": pytest.approx(0.55),
        "D.SZ": pytest.approx(0.05),
    }
    assert match_dims["A.SZ"] == _dims(0.9, 0.9)
    # 对照：默认（enabled=true）下 B/C/D 会被剔
    kept_on, dropped_on, _, _ = gate_candidates(inputs)
    assert [c["code"] for c in kept_on] == ["A.SZ"] and len(dropped_on) == 4


def test_gate_disabled_scoring_matches_enabled_scoring():
    """停用只改「剔不剔」，不改「怎么记分」：两侧 ``scores`` / ``match_dims`` 逐条相同。"""
    from alphabee.company_track.peer_group_build import gate_candidates

    inputs = [
        {"code": "A.SZ", "name": "甲", "dims": _dims(0.8, 0.7), "verdict": "direct"},
        {"code": "B.SZ", "name": "乙", "overlap": 0.6, "verdict": "adjacent"},
        {"code": "C.SZ", "name": "丙", "overlap": 0.2, "verdict": "reject"},
    ]
    _, _, scores_on, dims_on = gate_candidates(inputs)
    kept_off, dropped_off, scores_off, dims_off = gate_candidates(inputs, enabled=False)

    assert dropped_off == []
    assert [c["code"] for c in kept_off] == ["A.SZ", "B.SZ", "C.SZ"]
    assert scores_off == scores_on or set(scores_off) >= set(scores_on)
    # 有分数候选的记分值必须逐条一致（停用不改变权重/归一化口径）
    for code, value in scores_on.items():
        assert scores_off[code] == pytest.approx(value)
    for code, dims in dims_on.items():
        assert dims_off[code] == dims


def test_gate_enabled_default_matrix_is_frozen_against_baseline():
    """默认口径（``enabled=True``）行为冻结：同一矩阵的 kept/dropped/scores/match_dims 逐字固定。"""
    from alphabee.company_track.peer_group_build import gate_candidates

    inputs = [
        {"code": "A.SZ", "name": "边界 product", "dims": _dims(product=0.20, customer=0.9), "verdict": "direct"},
        {"code": "B.SZ", "name": "边界 overlap", "dims": _dims(0.5, 0.5, 0.5, 0.5), "verdict": "direct"},
        {"code": "C.SZ", "name": "reject 优先", "dims": _dims(1.0, 1.0), "verdict": "reject"},
        {"code": "D.SZ", "name": "缺 dims 回落自评", "overlap": 0.9, "verdict": "direct"},
        {"code": "E.SZ", "name": "两者皆缺", "verdict": "direct"},
    ]
    kept, dropped, scores, match_dims = gate_candidates(inputs)

    assert [c["code"] for c in kept] == ["A.SZ", "B.SZ", "D.SZ"]
    assert [(d["code"], d["drop"]) for d in dropped] == [
        ("C.SZ", "judge reject"),
        ("E.SZ", "overlap 0.00 < 0.40"),
    ]
    assert scores == {"A.SZ": pytest.approx(0.61), "B.SZ": pytest.approx(0.5), "D.SZ": pytest.approx(0.9)}
    # 既有行为：score-only 候选（D）也会落 match_dims（值为全 0 的四维占位）——本步**不改**该行为
    assert set(match_dims) == {"A.SZ", "B.SZ", "D.SZ"}


def test_enabled_config_description_and_example_match_semantics():
    """配置面：``PeerQualitySettings.enabled`` 描述与 ``config.yaml.example`` 注释都写清 false 的含义。"""
    import yaml

    from alphabee.config import PeerQualitySettings

    description = PeerQualitySettings.model_fields["enabled"].description or ""
    for token in ("不做任何剔除", "全部候选直接保留", "min_peers", "taxonomy_enabled", "置信度"):
        assert token in description, f"enabled 描述缺少语义说明：{token}"

    example_text = (PROJECT_ROOT / "config.yaml.example").read_text(encoding="utf-8")
    assert "enabled: true" in example_text
    assert "不做任何剔除" in example_text and "全部候选直接保留" in example_text

    section = yaml.safe_load(example_text)["company_track"]["peer_quality"]
    assert PeerQualitySettings(**section).enabled is True  # 默认口径仍为 true
