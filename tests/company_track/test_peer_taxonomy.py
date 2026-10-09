"""判定 E（分类学召回池 + 特征）测试：查表、残差桶降级、确定性、绝不硬闸。

覆盖设计 §3.1/§3.3/§6 Step 3 与 t10 契约 acceptance 第 1–4 条：

- ``same_l3`` / ``same_l2`` 查表（命中 / 不命中 / **代码不在快照 ⇒ None**，不得误判为 False）；
- 残差桶两个**各自独立**的触发条件（L3 名以「其他」开头 / 成分数 < 下限）各自有用例；
- ``taxonomy_reliable`` 与 ``same_*`` 的**确定性**（同输入同输出）；
- 召回池：可信 ⇒ 同 L3；不可信 ⇒ 同 L2 降级；无分类学数据 ⇒ 空池；
- 数据来源纪律：只读仓库内静态快照（**无网络**），target 与候选同取一份快照；
- **E 绝不硬闸**：``gate_candidates`` 内不得出现任何以分类学取值为条件的剔除分支（AST 证据）。

密闭性：快照通过 monkeypatch 指向 ``tmp_path`` 合成 CSV（不依赖真实 ``data/``，不联网）。
"""

from __future__ import annotations

import ast
import csv
from pathlib import Path

import pytest

import alphabee.company_track.peer_taxonomy as taxo
from alphabee import PROJECT_ROOT
from alphabee.company_track.peer_group_build import gate_candidates

_QUALITY_MODULE = PROJECT_ROOT / "alphabee" / "company_track" / "peer_group_build.py"

_HEADER = [
    "stock_code",
    "symbol",
    "company_name",
    "area",
    "industry",
    "cnspell",
    "market",
    "list_date",
    "act_name",
    "act_ent_type",
    "sw_l1_code",
    "sw_l1_name",
    "sw_l2_code",
    "sw_l2_name",
    "sw_l3_code",
    "sw_l3_name",
]


@pytest.fixture(autouse=True)
def _taxonomy_cache_isolation():
    """每个用例前后清空分类学快照缓存（合成快照的用例不得污染后续用例的缓存）。"""
    import alphabee.company_track.peer_taxonomy as taxo

    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()
    yield
    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()


def _row(
    code: str,
    name: str,
    *,
    l1: tuple[str, str] = ("801080.SI", "电子"),
    l2: tuple[str, str] = ("801083.SI", "元件"),
    l3: tuple[str, str] = ("850822.SI", "印制电路板"),
) -> dict[str, str]:
    return {
        "stock_code": code,
        "symbol": code.split(".")[0],
        "company_name": name,
        "area": "广东",
        "industry": "元器件",
        "cnspell": "X",
        "market": "主板",
        "list_date": "20100101",
        "act_name": "某",
        "act_ent_type": "自然人",
        "sw_l1_code": l1[0],
        "sw_l1_name": l1[1],
        "sw_l2_code": l2[0],
        "sw_l2_name": l2[1],
        "sw_l3_code": l3[0],
        "sw_l3_name": l3[1],
    }


def _write_snapshot(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_HEADER)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    """合成快照：一个可信大 L3、一个残差桶 L3、一个成分不足的小 L3、以及快照外代码。"""
    path = tmp_path / "all_stocks.csv"
    rows: list[dict[str, str]] = []
    # 可信 L3「印制电路板」：20 只（≥ 15）
    for index in range(20):
        rows.append(_row(f"{index:06d}.SZ", f"PCB{index}"))
    # 残差桶 L3「其他专用设备」：20 只（成分足但名字是残差桶）
    for index in range(20):
        rows.append(
            _row(
                f"3{index:05d}.SZ",
                f"OTHER{index}",
                l1=("801890.SI", "机械设备"),
                l2=("801072.SI", "专用设备"),
                l3=("850751.SI", "其他专用设备"),
            )
        )
    # 成分不足 L3「镍」：3 只（< 15）
    for index in range(3):
        rows.append(
            _row(
                f"6{index:05d}.SZ",
                f"NICKEL{index}",
                l1=("801050.SI", "有色金属"),
                l2=("801051.SI", "小金属"),
                l3=("850501.SI", "镍"),
            )
        )
    _write_snapshot(path, rows)
    monkeypatch.setattr(taxo, "TAXONOMY_CSV", path)
    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()
    yield path
    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()


# ── 1. 查表与 same_l3 / same_l2（含未知 ⇒ None） ────────────────────────────


def test_lookup_hit_and_fields(snapshot):
    entry = taxo.taxonomy_entry("000003.SZ")
    assert entry is not None
    assert (entry.l1_name, entry.l2_name, entry.l3_name) == ("电子", "元件", "印制电路板")
    assert entry.has_l3 and entry.has_l2 and not entry.is_residual_l3_name


def test_lookup_miss_returns_none(snapshot):
    assert taxo.taxonomy_entry("999999.SZ") is None  # 不在快照 ⇒ None（未知，不伪造）


def test_same_levels_hit_miss_and_unknown(snapshot):
    pairs = taxo.same_levels("000001.SZ", ["000002.SZ", "600001.SZ", "999999.SZ"])
    assert pairs["000002.SZ"] == (True, True)  # 同 L3 同 L2
    assert pairs["600001.SZ"] == (False, False)  # 同 L1 不同 L2/L3 ⇒ False（类别不同）
    assert pairs["999999.SZ"] == (None, None)  # 不在快照 ⇒ None（**不得**误判为 False）


def test_same_levels_unknown_target_is_none(snapshot):
    assert taxo.same_levels("999999.SZ", ["000001.SZ"]) == {"000001.SZ": (None, None)}


def test_same_levels_same_l3_only_when_reliable(snapshot):
    """残差桶降级（``l3_reliable=False``）⇒ 特征降权：``same_l3`` 一律 None，``same_l2`` 照常。"""
    reliable = taxo.same_levels("300001.SZ", ["300002.SZ"])
    downgraded = taxo.same_levels("300001.SZ", ["300002.SZ"], l3_reliable=False)
    assert reliable["300002.SZ"] == (True, True)
    assert downgraded["300002.SZ"] == (None, True)


def test_annotate_same_levels_is_order_stable_and_non_mutating(snapshot):
    candidates = [{"code": "000002.SZ", "name": "B"}, {"code": "999999.SZ", "name": "X"}]
    original = [dict(item) for item in candidates]
    annotated = taxo.annotate_same_levels("000001.SZ", candidates)
    assert candidates == original, "不得就地修改入参"
    assert [item["code"] for item in annotated] == ["000002.SZ", "999999.SZ"]  # 保序
    assert annotated[0]["same_l3"] is True and annotated[0]["same_l2"] is True
    assert annotated[1]["same_l3"] is None and annotated[1]["same_l2"] is None


# ── 2. 残差桶自适应：两个触发条件各自独立 ────────────────────────────────────


def test_reliability_ok_for_large_normal_l3(snapshot):
    reliability = taxo.assess_reliability("000001.SZ")
    assert reliability.reliable is True
    assert reliability.recall_level == "l3"
    assert reliability.l3_constituents == 20


def test_reliability_residual_by_name(snapshot):
    """触发条件 1：L3 名以「其他」开头（成分数足够也不算可信）。"""
    reliability = taxo.assess_reliability("300001.SZ")
    assert reliability.reliable is False
    assert "残差桶" in reliability.reason and "其他" in reliability.reason
    assert reliability.l3_constituents == 20  # 成分数并不少 ⇒ 证明是**名字**触发
    assert reliability.recall_level == "l2"


def test_reliability_residual_by_small_l3(snapshot):
    """触发条件 2：同 L3 成分数 < 下限（名字正常也不算可信）。"""
    reliability = taxo.assess_reliability("600001.SZ")
    assert reliability.reliable is False
    assert "成分数" in reliability.reason and reliability.l3_constituents == 3
    assert reliability.recall_level == "l2"
    # 阈值可配置：下限调到 3 ⇒ 该桶转为可信（证明判据可配、非硬编码）
    assert taxo.assess_reliability("600001.SZ", min_constituents=3).reliable is True


def test_reliability_unknown_symbol_is_unreliable(snapshot):
    reliability = taxo.assess_reliability("999999.SZ")
    assert reliability.reliable is False and reliability.recall_level == "none"
    assert "不在分类学快照" in reliability.reason


# ── 3. 召回池（可信 L3 / 残差桶降级 L2 / 无数据空池） ────────────────────────


def test_recall_pool_uses_l3_when_reliable(snapshot):
    codes, level = taxo.recall_pool("000001.SZ")
    assert level == "l3" and len(codes) == 20 and "000001.SZ" in codes
    assert codes.index("000001.SZ") < codes.index("000019.SZ")  # 快照顺序保序


def test_recall_pool_falls_back_to_l2_when_residual(snapshot):
    codes, level = taxo.recall_pool("300001.SZ")
    assert level == "l2" and len(codes) == 20
    assert all(code.startswith("3") for code in codes)  # L2「专用设备」成分（比 L3 桶大）


def test_recall_pool_empty_without_taxonomy(snapshot):
    assert taxo.recall_pool("999999.SZ") == ((), "none")


# ── 4. 确定性（同输入同输出） ───────────────────────────────────────────────


def test_taxonomy_functions_are_deterministic(snapshot):
    first = (
        taxo.same_levels("000001.SZ", ["000002.SZ", "600001.SZ"]),
        taxo.assess_reliability("600001.SZ"),
        taxo.recall_pool("300001.SZ"),
    )
    second = (
        taxo.same_levels("000001.SZ", ["000002.SZ", "600001.SZ"]),
        taxo.assess_reliability("600001.SZ"),
        taxo.recall_pool("300001.SZ"),
    )
    assert first == second
    assert taxo.stock_taxonomy() is taxo.stock_taxonomy()  # lru_cache：同一次读取同一对象


# ── 5. 数据来源纪律（本地快照、无网络、同源匹配） ───────────────────────────


def test_snapshot_columns_are_sw_taxonomy():
    """快照列契约：申万 2021 的 L1/L2/L3 代码与名称（**同源匹配**，不跨分类体系）。"""
    with taxo.TAXONOMY_CSV.open("r", encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    for column in ("sw_l1_code", "sw_l1_name", "sw_l2_code", "sw_l2_name", "sw_l3_code", "sw_l3_name"):
        assert column in header
    assert "stock_code" in header


def test_module_reads_local_snapshot_only():
    """无网络：模块只 import 本地读表所需（``csv``/``lru_cache``），不得出现网络/行情依赖。"""
    source = (PROJECT_ROOT / "alphabee" / "company_track" / "peer_taxonomy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "csv", "dataclasses", "functools", "typing", "alphabee"}, imported


def test_missing_snapshot_file_yields_empty_table(tmp_path, monkeypatch):
    monkeypatch.setattr(taxo, "TAXONOMY_CSV", tmp_path / "nope.csv")
    taxo.stock_taxonomy.cache_clear()
    taxo._members_by.cache_clear()
    try:
        assert taxo.stock_taxonomy() == {}
        assert taxo.taxonomy_entry("000001.SZ") is None
        assert taxo.recall_pool("000001.SZ") == ((), "none")
    finally:
        taxo.stock_taxonomy.cache_clear()
        taxo._members_by.cache_clear()


# ── 6. E 绝不硬闸（AST 证据 + 行为对照） ────────────────────────────────────


def test_gate_has_no_taxonomy_condition():
    """AST：``gate_candidates`` 内不得出现任何以分类学取值为条件的剔除分支（设计 §3.3/§6 Step 3）。"""
    source = _QUALITY_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "gate_candidates")
    offenders: list[str] = []
    for node in ast.walk(gate):
        if isinstance(node, ast.Name) and node.id in {"same_l3", "same_l2", "taxonomy_reliable"}:
            offenders.append(f"Name {node.id}@{node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in {"same_l3", "same_l2", "taxonomy_reliable"}:
            offenders.append(f"Attribute {node.attr}@{node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in {"same_l3", "same_l2", "taxonomy_reliable"}:
                offenders.append(f"Constant {node.value}@{node.lineno}")
    assert offenders == [], f"Gate 内出现分类学条件（E 硬闸回归）：{offenders}"


def test_taxonomy_features_do_not_affect_gate_outcome():
    """行为对照：只翻转 ``same_l3/same_l2/taxonomy_reliable``（其余不变）⇒ 保留集必须逐字相同。"""
    base = [
        {"code": "600001.SH", "name": "同 L3 但产品无关", "overlap": 0.9},
        {"code": "600002.SH", "name": "跨 L3 但同环节", "overlap": 0.9},
    ]
    with_features = [
        {**base[0], "same_l3": True, "same_l2": True},
        {**base[1], "same_l3": False, "same_l2": False, "taxonomy_reliable": False},
    ]
    kept_plain, dropped_plain, _, _ = gate_candidates(base)
    kept_feat, dropped_feat, _, _ = gate_candidates(with_features)
    assert [c["code"] for c in kept_plain] == [c["code"] for c in kept_feat]
    assert [d["code"] for d in dropped_plain] == [d["code"] for d in dropped_feat]
