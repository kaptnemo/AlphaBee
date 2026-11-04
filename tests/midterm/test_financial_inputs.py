"""P2-2：F 输入序列 PIT 快照（financial_inputs）单元测试。

口径依据 ``MIDTERM_F_DATA_GOVERNANCE_DESIGN`` §14：

- ``build_financial_inputs`` 纯函数：6 条序列、每条最多 8 期、稳定 content_hash；
- ``append`` 按 ``(symbol, as_of_date, report_period, content_hash)`` 幂等；
- ``load`` 按 as_of 升序、坏行/非对象行跳过；
- 默认目录 ``data/midterm/financial_inputs/<symbol>.jsonl``；
- 落盘开关缺省 false；``get_factor_snapshot`` 仅在 true 时写盘且失败 fail-open。

全部合成数据 + ``tmp_path``：不触网、不写真实 ``data/``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import alphabee.midterm.factors as factors_mod
import alphabee.midterm.financial_inputs as fi
from alphabee.midterm.factors import get_factor_snapshot
from alphabee.midterm.financial_inputs import (
    DEFAULT_DATA_DIR,
    MAX_PERIODS,
    FinancialInputSnapshot,
    append_financial_inputs,
    build_financial_inputs,
    load_financial_inputs,
    resolve_persist_financial_inputs,
)

_SERIES_FIELDS = (
    "revenue",
    "operating_revenue",
    "net_profit_attr_p",
    "net_profit_dedt",
    "gross_margin",
    "operating_cashflow",
)


def _fin_data() -> dict:
    """多期 fin_data（income/fina/cashflow 均是 canonical 字段名）。"""
    return {
        "stock_code": "600519.SH",
        "fina": [
            {
                "period": "20250930",
                "ann_date": "20251030",
                "revenue_yoy": 10.0,
                "net_profit_yoy": 12.0,
                "net_profit_dedt": 5.0,
                "gross_margin": 30.0,
            },
            {"period": "20250630", "ann_date": "20250820", "revenue_yoy": 8.0, "gross_margin": 28.0},
        ],
        "income": [
            {"period": "20250930", "operating_revenue": 100.0, "revenue": 110.0, "net_profit_attr_p": 12.0},
            {"period": "20250630", "operating_revenue": 90.0, "revenue": 99.0, "net_profit_attr_p": 10.0},
        ],
        "cashflow": [
            {"period": "20250930", "operating_cashflow": 20.0},
            {"period": "20250630", "operating_cashflow": 15.0},
        ],
    }


_MAX_PERIOD_RECORDS = [
    "20230331",
    "20230630",
    "20230930",
    "20231231",
    "20240331",
    "20240630",
    "20240930",
    "20241231",
    "20250331",
    "20250630",
]


# ─────────────────────────────────────────────────────────────────────────────
# build：纯函数 / 6 条序列 / 最多 8 期 / 稳定 hash
# ─────────────────────────────────────────────────────────────────────────────


def test_build_is_deterministic_and_hash_is_stable():
    """同输入两次 build ⇒ 完全相等（含 content_hash）；不写盘、不读系统时间。"""
    first = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")
    second = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")

    assert first == second
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64  # sha256 hex
    assert first.schema_version == "1"
    assert first.source == "tushare"


def test_build_series_six_fields_and_sparse_missing():
    """series 恰好 6 条（各自来源 income/fina/cashflow）；缺失字段不写空 dict。"""
    snapshot = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")
    assert set(snapshot.series) == set(_SERIES_FIELDS)
    assert snapshot.series["revenue"] == {"20250630": 99.0, "20250930": 110.0}
    assert snapshot.series["operating_cashflow"] == {"20250630": 15.0, "20250930": 20.0}

    partial = _fin_data()
    for record in partial["fina"]:
        record.pop("gross_margin", None)
    sparse = build_financial_inputs(partial, as_of_date="2026-02-01")
    assert "gross_margin" not in sparse.series  # 缺失 ⇒ 不写空 dict
    assert "revenue" in sparse.series


def test_build_caps_series_at_eight_periods():
    """每条序列最多 8 期（按报告期升序保留最新 8 期）。"""
    data = {
        "stock_code": "600519.SH",
        "income": [{"period": period, "revenue": float(idx)} for idx, period in enumerate(_MAX_PERIOD_RECORDS)],
    }
    snapshot = build_financial_inputs(data, as_of_date="2026-02-01")

    revenue = snapshot.series["revenue"]
    assert len(revenue) == MAX_PERIODS == 8
    assert list(revenue) == _MAX_PERIOD_RECORDS[-8:]  # 升序、最新 8 期
    assert "20230331" not in revenue


def test_build_report_period_and_ann_date():
    """report_period 取 fina 最新合法期；report_ann_date 取该期 ann_date（YYYY-MM-DD）。"""
    snapshot = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")
    assert snapshot.report_period == "20250930"
    assert snapshot.report_ann_date == "2025-10-30"
    assert snapshot.as_of_date == "2026-02-01"
    assert snapshot.symbol == "600519.SH"

    # fina 缺失 ⇒ 回落 income 最新期与 ann_date
    fallback = {
        "stock_code": "600519.SH",
        "income": [{"period": "20250930", "ann_date": "20251031", "revenue": 100.0}],
    }
    snapshot = build_financial_inputs(fallback, as_of_date="2026-02-01")
    assert snapshot.report_period == "20250930"
    assert snapshot.report_ann_date == "2025-10-31"


def test_build_hash_changes_with_as_of_and_values():
    """hash 对 as_of_date / 原始值敏感（同内容同 hash、不同内容不同 hash）。"""
    base = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")
    other_day = build_financial_inputs(_fin_data(), as_of_date="2026-02-02")
    changed = _fin_data()
    changed["income"][0]["revenue"] = 111.0
    other_value = build_financial_inputs(changed, as_of_date="2026-02-01")

    assert base.content_hash != other_day.content_hash
    assert base.content_hash != other_value.content_hash


def test_build_handles_empty_and_invalid_fin_data():
    """空/非法输入不抛：series 为空、报告期空串、hash 仍稳定。"""
    empty = build_financial_inputs(None, as_of_date="2026-02-01")
    assert empty.series == {}
    assert empty.report_period == ""

    invalid = build_financial_inputs(
        {"stock_code": "600519.SH", "income": [{"period": "bad", "revenue": float("nan")}], "fina": [{"period": "x"}]},
        as_of_date="2026-02-01",
    )
    assert invalid.series == {}
    assert invalid.report_period == ""


# ─────────────────────────────────────────────────────────────────────────────
# append / load：幂等、原子、排序、坏行跳过、文件名安全
# ─────────────────────────────────────────────────────────────────────────────


def test_append_is_idempotent_for_same_identity(tmp_path: Path):
    """同内容两次 append ⇒ 仅 1 行，第二次返回 False。"""
    snapshot = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")

    assert append_financial_inputs(snapshot, base_dir=tmp_path) is True
    assert append_financial_inputs(snapshot, base_dir=tmp_path) is False

    path = tmp_path / "600519.SH.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert load_financial_inputs("600519.SH", base_dir=tmp_path) == [snapshot]


def test_append_distinguishes_as_of_and_report_period(tmp_path: Path):
    """不同 as_of_date / report_period 不视为同帧 ⇒ 追加（不误去重）。"""
    first = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")
    next_day = build_financial_inputs(_fin_data(), as_of_date="2026-02-02")
    changed_report = _fin_data()
    changed_report["fina"][0]["period"] = "20251231"
    next_report = build_financial_inputs(changed_report, as_of_date="2026-02-01")

    assert append_financial_inputs(first, base_dir=tmp_path) is True
    assert append_financial_inputs(next_day, base_dir=tmp_path) is True
    assert append_financial_inputs(next_report, base_dir=tmp_path) is True
    assert len(load_financial_inputs("600519.SH", base_dir=tmp_path)) == 3


def test_load_sorts_by_as_of_and_skips_bad_lines(tmp_path: Path):
    """读回按 as_of_date 升序；坏 JSON/非对象行跳过不抛。"""
    later = build_financial_inputs(_fin_data(), as_of_date="2026-03-01")
    earlier = build_financial_inputs(_fin_data(), as_of_date="2026-01-01")
    append_financial_inputs(later, base_dir=tmp_path)
    append_financial_inputs(earlier, base_dir=tmp_path)

    path = tmp_path / "600519.SH.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
        handle.write("[1, 2, 3]\n")
        handle.write('{"symbol": "600519.SH"}\n')  # 非法（缺 report_period 允许，但缺字段集不完整）
        handle.write("\n")

    loaded = load_financial_inputs("600519.SH", base_dir=tmp_path)
    assert [item.as_of_date for item in loaded] == ["2026-01-01", "2026-03-01"]


def test_symbol_filename_is_sanitized(tmp_path: Path):
    """symbol 中的路径分隔符被替换（不产生子目录/目录穿越）。"""
    snapshot = build_financial_inputs({**_fin_data(), "stock_code": "600519/../../evil"}, as_of_date="2026-02-01")
    assert append_financial_inputs(snapshot, base_dir=tmp_path) is True

    written = list(tmp_path.glob("*.jsonl"))
    assert len(written) == 1
    assert written[0].parent == tmp_path
    assert "/" not in written[0].name
    assert load_financial_inputs("600519/../../evil", base_dir=tmp_path) == [snapshot]


def test_default_dir_constant_matches_design():
    assert DEFAULT_DATA_DIR == Path("data") / "midterm" / "financial_inputs"
    assert fi.default_data_dir() == DEFAULT_DATA_DIR


def test_append_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "a" / "b"
    snapshot = build_financial_inputs(_fin_data(), as_of_date="2026-02-01")
    assert append_financial_inputs(snapshot, base_dir=nested) is True
    assert (nested / "600519.SH.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# resolve_persist_financial_inputs：默认 false / 缺字段动态默认 / 异常与非法 false
# ─────────────────────────────────────────────────────────────────────────────


def _pin_config(monkeypatch: pytest.MonkeyPatch, *, value: object = None, drop_field: bool = False) -> None:
    midterm = SimpleNamespace(classifier_order="etf")
    if not drop_field:
        midterm.persist_financial_inputs = value
    monkeypatch.setattr("alphabee.config.get_settings", lambda: SimpleNamespace(midterm=midterm))


def test_resolve_default_config_is_false(monkeypatch):
    _pin_config(monkeypatch, value=False)
    assert resolve_persist_financial_inputs() is False


def test_resolve_missing_field_uses_model_default(monkeypatch):
    """缺段/缺字段 ⇒ 动态读模型默认（当前 false），不硬编码。"""
    _pin_config(monkeypatch, drop_field=True)
    assert resolve_persist_financial_inputs() is False

    monkeypatch.setattr("alphabee.config.get_settings", lambda: SimpleNamespace(midterm=None))
    assert resolve_persist_financial_inputs() is False


def test_resolve_falls_back_to_model_default_drift(monkeypatch):
    """模型默认漂移时 resolve 跟随（证明未硬编码 false）。"""
    from alphabee.config import MidtermClassifierSettings

    field = MidtermClassifierSettings.model_fields["persist_financial_inputs"]
    _pin_config(monkeypatch, drop_field=True)
    monkeypatch.setattr(field, "default", True)
    assert resolve_persist_financial_inputs() is True


def test_resolve_config_exception_and_invalid_falls_back_false(monkeypatch):
    def _boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr("alphabee.config.get_settings", _boom)
    assert resolve_persist_financial_inputs() is False

    _pin_config(monkeypatch, value="yes")  # 非 bool ⇒ False（不误开启）
    assert resolve_persist_financial_inputs() is False


def test_resolve_explicit_value_wins():
    assert resolve_persist_financial_inputs(True) is True
    assert resolve_persist_financial_inputs(False) is False
    assert resolve_persist_financial_inputs("true") is False  # 非法显式值 ⇒ False


# ─────────────────────────────────────────────────────────────────────────────
# 接线：get_factor_snapshot 仅在 true 时落盘；失败 fail-open（不触网）
# ─────────────────────────────────────────────────────────────────────────────


def _patch_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """打桩全部数据源（不触网、不触发 tushare 初始化）。"""
    monkeypatch.setattr(factors_mod, "_get_financial_fact", lambda _s: _fin_data())
    monkeypatch.setattr(factors_mod, "_fetch_segments", lambda _s: None)
    monkeypatch.setattr(factors_mod, "_get_expectation_fact", lambda _s: {"forecast": [], "express": []})
    monkeypatch.setattr(factors_mod, "_build_consensus", lambda _c: None)
    monkeypatch.setattr(factors_mod, "_get_market_fact", lambda _s: {})
    monkeypatch.setattr(factors_mod, "_get_industry_fact", lambda _s: {})
    monkeypatch.setattr(factors_mod, "_build_crowding", lambda _c: None)
    monkeypatch.setattr(
        factors_mod,
        "_get_risk_fact",
        lambda _s: {"pledge": [], "repurchase": [], "news": [], "audit": [], "risk_missing": []},
    )


def test_snapshot_flag_false_writes_nothing(monkeypatch):
    """flag=false ⇒ 不 build/append（零文件）；快照照常构建。"""
    _patch_sources(monkeypatch)
    calls: list[FinancialInputSnapshot] = []
    monkeypatch.setattr(fi, "resolve_persist_financial_inputs", lambda enabled=None: False)
    monkeypatch.setattr(fi, "append_financial_inputs", lambda snapshot, base_dir=None: calls.append(snapshot))

    snap = get_factor_snapshot("600519.SH", include_market=False)
    assert snap.fundamental.report_period == "20250930"
    assert calls == []


def test_snapshot_flag_true_writes_to_tmp(monkeypatch, tmp_path: Path):
    """flag=true ⇒ build + append 真实写盘（base_dir 指向 tmp，不碰真实 data/）。"""
    _patch_sources(monkeypatch)
    monkeypatch.setattr(fi, "resolve_persist_financial_inputs", lambda enabled=None: True)
    calls: list[FinancialInputSnapshot] = []

    def _append_to_tmp(snapshot, base_dir=None):
        calls.append(snapshot)
        return append_financial_inputs(snapshot, base_dir=tmp_path)

    monkeypatch.setattr(fi, "append_financial_inputs", _append_to_tmp)

    snap = get_factor_snapshot("600519.SH", include_market=False)
    assert len(calls) == 1
    written = calls[0]
    assert written.symbol == "600519.SH"
    assert written.as_of_date == snap.as_of_date
    assert set(written.series) == set(_SERIES_FIELDS)

    loaded = load_financial_inputs("600519.SH", base_dir=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].content_hash == written.content_hash


def test_snapshot_persist_failure_is_fail_open(monkeypatch):
    """写盘异常 ⇒ warning 后继续返回快照（不打断决策、不置 degraded）。"""
    _patch_sources(monkeypatch)
    monkeypatch.setattr(fi, "resolve_persist_financial_inputs", lambda enabled=None: True)

    def _boom(snapshot, base_dir=None):
        raise OSError("disk full")

    monkeypatch.setattr(fi, "append_financial_inputs", _boom)
    snap = get_factor_snapshot("600519.SH", include_market=False)

    assert snap.degraded is False
    assert snap.fundamental.report_period == "20250930"
