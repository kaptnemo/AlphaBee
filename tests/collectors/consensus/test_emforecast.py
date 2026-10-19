"""东财 F10 盈利预测数据源 + consensus provider 链的单测（E 生态设计 §7 C 矩阵）。

覆盖面（全部离线，HTTP 层用合成响应替身，与冒烟报告
``docs/implementation/midterm/E_ECOSYSTEM_SMOKE_REPORT.md`` 的实测样本同形）：

1. **字段映射逐字段**：``yctj_list[E1/E2/E3].EPS`` / ``EPS_COUNT`` / ``jgyc[0].PUBLISH_DATE``
   → ``eps_fy1/fy2/fy3`` / ``coverage_count`` / ``as_of_date``（冒烟 §8.1 口径）；
2. **入参前缀**：``600519`` / ``600519.SH`` / ``sh600519`` → ``SH600519``；错配/北交所旧代码
   不发请求直接 fail-open；
3. **空值形态**：无覆盖 / 次新（有评级无预测）/ 非法代码（HTTP 200 + status=-1）/ 非 JSON /
   HTTP 5xx / 网络异常 → ``{}`` 不抛（冒烟 §6）；
4. **快照落盘**：append-only、同日幂等、损坏行跳过、不可用值不落盘；
5. **revision 自算**：1m/3m 由快照序列纯规则计算，不足窗口 → ``None`` 不编造；
   ``build_f10_consensus`` 的 revision 事件日 = ``ConsensusOutput.as_of_date``（快照日）；
6. **provider 链**：默认 ``report_list`` 逐字现状、``f10_forecast`` 走新源、失败回落 +
   warnings 留痕、``auto`` 优先新源、非法值 warn + 回落不抛、配置面三层同源。
"""

from __future__ import annotations

import json
import logging
import os
import types

import pytest
import requests
import yaml
from pydantic import ValidationError

from alphabee import PROJECT_ROOT
from alphabee.collectors.consensus import eastmoney, emforecast
from alphabee.collectors.consensus.emforecast import (
    SOURCE_NAME,
    append_snapshot,
    build_f10_consensus,
    compute_revisions,
    fetch_profit_forecast,
    load_snapshots,
    market_prefixed_code,
)
from alphabee.collectors.consensus.engine import aggregate_consensus

ASOF = "2026-10-19"


# ─────────────────────────────────────────────────────────────────────────────
# 合成东财 F10 响应（键名/字段逐字照抄冒烟报告 §5.1 的贵州茅台样本）
# ─────────────────────────────────────────────────────────────────────────────
def _f10_payload(
    *,
    publish_date: str = f"{ASOF} 00:00:00",
    eps1: float | None = 67.249318181818,
    eps2: float | None = 71.125909090909,
    eps3: float | None = 75.18,
    count: int | None = 44,
    lastmonths: float | None = 67.623260869565,
    lastmonths_count: int | None = 46,
    years: tuple[int, int, int] = (2026, 2027, 2028),
) -> dict:
    """带机构覆盖的 F10 响应（5 个区块齐全）。"""
    y1, y2, y3 = years
    rows = []
    if eps1 is not None:
        rows.append(
            {
                "YEAR": y1,
                "YEAR_MARK": "E",
                "EPS": eps1,
                "EPS_LASTMONTHS": lastmonths,
                "EPS_COUNT": count,
                "EPS_LASTMONTHS_COUNT": lastmonths_count,
                "PARENT_NETPROFIT": 84099207500,
                "RANK": 4,
            }
        )
    if eps2 is not None:
        rows.append({"YEAR": y2, "YEAR_MARK": "E", "EPS": eps2, "EPS_COUNT": count, "RANK": 5})
    if eps3 is not None:
        rows.append({"YEAR": y3, "YEAR_MARK": "E", "EPS": eps3, "EPS_COUNT": 43, "RANK": 6})
    return {
        "pjtj": [
            {
                "SECUCODE": "600519.SH",
                "DATE_TYPE_CODE": 5,
                "DATE_TYPE": "1年内",
                "COMPRE_RATING_NUM": 4.816326530612,
                "COMPRE_RATING": "买入",
                "RATING_ORG_NUM": 49,
                "RATING_BUY_NUM": 40,
                "RATING_ADD_NUM": 9,
            }
        ],
        "jgyc": [
            {
                "SECUCODE": "600519.SH",
                "SECURITY_CODE": "600519",
                "PUBLISH_DATE": publish_date,
                "ORG_CODE": "00000000",
                "ORG_NAME_ABBR": "近六月平均",
                "YEAR1": 2025,
                "YEAR_MARK1": "A",
                "EPS1": 65.851754826108,
                "YEAR2": y1,
                "YEAR_MARK2": "E",
                "EPS2": eps1,
                "YEAR3": y2,
                "YEAR_MARK3": "E",
                "EPS3": eps2,
                "YEAR4": y3,
                "YEAR_MARK4": "E",
                "EPS4": eps3,
            }
        ],
        "yctj_chart": [{"YEAR": y1, "YEAR_MARK": "E", "EPS": eps1}],
        # 第 0 行是实际年度（无 *_COUNT），E 行行序 = 当前财年 → 次年 → 次次年
        "yctj_list": [
            {"YEAR": 2025, "YEAR_MARK": "A", "EPS": 65.851754826108, "EPS_COUNT": None},
            *rows,
        ],
        "ycmx": [
            {
                "PUBLISH_DATE": "2026-09-21 00:00:00",
                "ORG_NAME_ABBR": "诚通证券",
                "RESEARCHER": "陈文倩",
                "YEAR2": y1,
                "YEAR_MARK2": "E",
                "EPS2": 65.14,
                "RATING": "买入",
            }
        ],
    }


def _no_coverage_payload() -> dict:
    """无机构覆盖：``pjtj``/``jgyc``/``ycmx`` 空，``yctj_list`` 只有实际年度（冒烟 §6.1）。"""
    return {
        "pjtj": [],
        "jgyc": [],
        "yctj_chart": [{"YEAR": 2025, "YEAR_MARK": "A", "EPS": -0.238}],
        "yctj_list": [
            {"YEAR": 2023, "YEAR_MARK": "A", "EPS": -0.418323368725, "EPS_COUNT": None},
            {"YEAR": 2024, "YEAR_MARK": "A", "EPS": -0.28390506427, "EPS_COUNT": None},
            {"YEAR": 2025, "YEAR_MARK": "A", "EPS": -0.23828398218, "EPS_COUNT": None},
        ],
        "ycmx": [],
    }


def _secondary_new_payload() -> dict:
    """次新股：``pjtj`` 有评级但 ``jgyc`` 空、无 E 行（冒烟 §6.2，评级≠一致预期）。"""
    payload = _no_coverage_payload()
    payload["pjtj"] = [
        {"DATE_TYPE_CODE": 1, "DATE_TYPE": "1月内", "COMPRE_RATING_NUM": None, "RATING_ORG_NUM": None},
        {
            "DATE_TYPE_CODE": 5,
            "DATE_TYPE": "1年内",
            "COMPRE_RATING_NUM": 5,
            "COMPRE_RATING": "买入",
            "RATING_ORG_NUM": 1,
            "RATING_BUY_NUM": 1,
        },
    ]
    return payload


class _FakeResponse:
    def __init__(self, payload=None, *, status_code: int = 200, json_error: bool = False):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("No JSON object could be decoded")
        return self._payload


@pytest.fixture
def http(monkeypatch):
    """替换 ``emforecast.requests.get``，返回可控响应并记录调用参数。"""

    class _Http:
        def __init__(self):
            self.calls: list[dict] = []
            self.response = _FakeResponse(_f10_payload())
            self.error: Exception | None = None

        def __call__(self, url, *, params=None, timeout=None):
            self.calls.append({"url": url, "params": params, "timeout": timeout})
            if self.error is not None:
                raise self.error
            return self.response

    stub = _Http()
    monkeypatch.setattr(emforecast.requests, "get", stub)
    return stub


def _report(publish_date: str = "2026-09-06", *, eps1=1.0, eps2=2.0, org="机构A") -> dict:
    return {
        "publishDate": publish_date,
        "predictThisYearEps": eps1,
        "predictNextYearEps": eps2,
        "indvAimPriceT": 30.0,
        "emRatingValue": 3,
        "lastEmRatingValue": 2,
        "orgSName": org,
    }


@pytest.fixture
def report_records(monkeypatch):
    """替换研报列表路径（``fetch_report_records``），返回固定记录。"""
    records = [_report("2026-09-01", eps1=1.0, eps2=2.0, org="机构A"), _report("2026-09-05", eps1=1.2, org="机构B")]
    monkeypatch.setattr(eastmoney, "fetch_report_records", lambda *a, **k: list(records))
    return records


def _patch_settings(monkeypatch, *, consensus_source="report_list", raise_exc=False, has_midterm=True):
    """按 classifier_dispatch 测试同构的方式替换 ``alphabee.config.get_settings``。"""

    def _get_settings():
        if raise_exc:
            raise RuntimeError("config boom")
        if not has_midterm:
            return types.SimpleNamespace()
        return types.SimpleNamespace(midterm=types.SimpleNamespace(consensus_source=consensus_source))

    monkeypatch.setattr("alphabee.config.get_settings", _get_settings)


# ─────────────────────────────────────────────────────────────────────────────
# C-冒烟 fixture：字段映射逐字段（冒烟 §8.1）
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_maps_fields_verbatim_from_smoke_sample(http):
    """EPS 均值 / 机构数 / 快照日逐字段映射（茅台样本原值，不做任何换算）。"""
    snapshot = fetch_profit_forecast("600519.SH")

    assert http.calls[0]["params"] == {"code": "SH600519"}
    assert snapshot["as_of_date"] == ASOF
    assert snapshot["eps_fy1"] == pytest.approx(67.249318181818)
    assert snapshot["eps_fy2"] == pytest.approx(71.125909090909)
    assert snapshot["eps_fy3"] == pytest.approx(75.18)
    assert snapshot["eps_fy1_year"] == 2026
    assert snapshot["eps_fy2_year"] == 2027
    assert snapshot["eps_fy3_year"] == 2028
    assert snapshot["coverage_count"] == 44  # 机构家数（EPS_COUNT），不是研报数
    assert snapshot["eps_fy1_lastmonths"] == pytest.approx(67.623260869565)
    assert snapshot["eps_fy1_lastmonths_count"] == 46
    assert snapshot["source"] == SOURCE_NAME


def test_coverage_count_comes_from_fy1_row(http):
    """机构数取与 ``eps_fy1`` 同一行（FY2 行家数不同也不串用）。"""
    payload = _f10_payload()
    payload["yctj_list"][2]["EPS_COUNT"] = 29  # FY2 行故意不同（实测各指标家数不等）
    http.response = _FakeResponse(payload)

    assert fetch_profit_forecast("SH600519")["coverage_count"] == 44


@pytest.mark.parametrize(
    "code",
    ["600519", "600519.SH", "sh600519", "SH600519", "  Sh600519  "],
)
def test_code_inputs_all_normalize_to_prefixed_form(code):
    """入参必须是「前缀 + 6 位」：各种 canonical 形态都要反向补前缀（冒烟 §2/§9.3）。"""
    assert market_prefixed_code(code) == "SH600519"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("000333.SZ", "SZ000333"), ("300750", "SZ300750"), ("920002", "BJ920002"), ("BJ920002", "BJ920002")],
)
def test_code_prefix_inference_covers_all_boards(code, expected):
    assert market_prefixed_code(code) == expected


@pytest.mark.parametrize("code", ["SZ600519", "600519.SZ", "430047", "BJ430047", "SH60599", "", "   "])
def test_unsupported_codes_are_not_requested(http, code):
    """错配前缀 / 北交所旧代码段 / 非 6 位 → 不发请求，直接 fail-open 返回 {}。"""
    assert fetch_profit_forecast(code) == {}
    assert http.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# C-空值形态：一律 fail-open 返回 {}，绝不抛（冒烟 §6）
# ─────────────────────────────────────────────────────────────────────────────
def test_no_coverage_returns_empty_snapshot(http):
    """无覆盖：``yctj_list`` 非空（有实际年度）但无 E 行 ⇒ 不可用（错误判据反面钉）。"""
    http.response = _FakeResponse(_no_coverage_payload())

    assert fetch_profit_forecast("600519.SH") == {}
    assert build_f10_consensus("600519.SH") is None


def test_secondary_new_stock_rating_without_consensus_is_empty(http):
    """次新股 ``pjtj`` 有评级但无一致预期 ⇒ 仍是不可用（评级≠预测，冒烟 §6.2）。"""
    http.response = _FakeResponse(_secondary_new_payload())

    assert fetch_profit_forecast("603202.SH") == {}


def test_invalid_code_payload_is_fail_open(http):
    """非法代码：HTTP 200 + ``{"status": -1}`` ⇒ {}（不能只看 status_code，冒烟 §6.4/§9.4）。"""
    http.response = _FakeResponse({"status": -1, "message": "股票代码不合法"})

    assert fetch_profit_forecast("600519.SH") == {}


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(status_code=500),
        _FakeResponse(json_error=True),
        _FakeResponse({"pjtj": [], "jgyc": []}),  # 缺 yctj_list 区块
        _FakeResponse(["not", "a", "dict"]),
    ],
)
def test_transport_and_payload_failures_are_fail_open(http, response):
    http.response = response

    assert fetch_profit_forecast("600519.SH") == {}


def test_network_error_is_fail_open(http):
    http.error = requests.exceptions.ConnectTimeout("timeout")

    assert fetch_profit_forecast("600519.SH") == {}
    assert build_f10_consensus("600519.SH") is None


def test_rating_block_without_forecast_years_is_not_mistaken_for_coverage(http):
    """``yctj_list`` 只有 A 行（无 ``YEAR_MARK="E"``）⇒ eps_fy1 缺失 ⇒ 不可用。"""
    payload = _no_coverage_payload()
    payload["yctj_list"].append({"YEAR": 2026, "YEAR_MARK": "A", "EPS": 1.0, "EPS_COUNT": None})
    http.response = _FakeResponse(payload)

    assert fetch_profit_forecast("600519.SH") == {}


# ─────────────────────────────────────────────────────────────────────────────
# C-快照时序：append-only / 同日幂等 / 坏行跳过
# ─────────────────────────────────────────────────────────────────────────────
def test_append_snapshot_writes_canonical_row_and_is_idempotent_same_day(tmp_path):
    values = {"as_of_date": ASOF, "eps_fy1": 10.0, "eps_fy2": 12.0, "coverage_count": 7, "source": SOURCE_NAME}

    assert append_snapshot("600519.SH", values, data_dir=tmp_path) == ASOF
    assert append_snapshot("600519.SH", values, data_dir=tmp_path) == ASOF  # 同日幂等

    path = tmp_path / "600519.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["as_of_date"] == ASOF
    assert row["eps_fy1"] == 10.0
    assert row["eps_fy2"] == 12.0
    assert row["coverage_count"] == 7


def test_append_snapshot_keeps_history_append_only(tmp_path):
    append_snapshot("600519.SH", {"as_of_date": "2026-09-19", "eps_fy1": 9.0}, data_dir=tmp_path)
    append_snapshot("600519.SH", {"as_of_date": ASOF, "eps_fy1": 10.0}, data_dir=tmp_path)

    rows = load_snapshots("600519.SH", data_dir=tmp_path)
    assert [r["as_of_date"] for r in rows] == ["2026-09-19", ASOF]
    assert [r["eps_fy1"] for r in rows] == [9.0, 10.0]


def test_append_snapshot_skips_unusable_values(tmp_path):
    """空值/缺 eps_fy1/缺 as_of_date → 不落盘（无覆盖日不污染 revision 序列）。"""
    assert append_snapshot("600519.SH", {}, data_dir=tmp_path) == ""
    assert append_snapshot("600519.SH", {"as_of_date": ASOF, "eps_fy1": None}, data_dir=tmp_path) == ""
    assert append_snapshot("600519.SH", {"eps_fy1": 1.0}, data_dir=tmp_path) == ""
    assert not (tmp_path / "600519.jsonl").exists()


def test_load_snapshots_skips_corrupt_lines(tmp_path):
    path = tmp_path / "600519.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"as_of_date": "2026-09-19", "eps_fy1": 9.0}),
                "{not json at all",
                "",
                "[1, 2, 3]",  # 非 dict
                json.dumps({"eps_fy1": 1.0}),  # 缺 as_of_date
                json.dumps({"as_of_date": "not-a-date", "eps_fy1": 2.0}),  # 日期不可解析
                json.dumps({"as_of_date": ASOF, "eps_fy1": 10.0}),
                json.dumps({"as_of_date": ASOF, "eps_fy1": 999.0}),  # 同日重复行 → 保留首条
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = load_snapshots("600519.SH", data_dir=tmp_path)
    assert [r["as_of_date"] for r in rows] == ["2026-09-19", ASOF]
    assert [r["eps_fy1"] for r in rows] == [9.0, 10.0]


def test_load_snapshots_missing_file_is_empty(tmp_path):
    assert load_snapshots("000001.SZ", data_dir=tmp_path) == []


# ─────────────────────────────────────────────────────────────────────────────
# C-revision：快照序列纯规则自算（不足窗口 → None，不编造）
# ─────────────────────────────────────────────────────────────────────────────
def _seed_series(tmp_path) -> None:
    seed = [
        ("2026-07-01", 100.0, None),
        ("2026-08-01", 90.0, None),
        ("2026-08-20", 95.0, None),
        ("2026-09-20", 105.0, 200.0),
        ("2026-10-20", 110.0, 210.0),
    ]
    for as_of, eps1, eps2 in seed:
        append_snapshot("600519.SH", {"as_of_date": as_of, "eps_fy1": eps1, "eps_fy2": eps2}, data_dir=tmp_path)


def test_compute_revisions_from_snapshot_series(tmp_path):
    """1m/3m 由快照序列算（PERCENT）：110 vs 105（1m）、110 vs 100（3m）、FY2 210 vs 200。"""
    _seed_series(tmp_path)

    revisions = compute_revisions("600519.SH", data_dir=tmp_path, as_of="2026-10-20")

    assert revisions["eps_fy1_revision_1m"] == pytest.approx(4.7619)
    assert revisions["eps_fy1_revision_3m"] == pytest.approx(10.0)
    assert revisions["eps_fy2_revision_1m"] == pytest.approx(5.0)
    # revision_1m 的二阶差分（engine 同一算法）：4.7619 - 10.5263
    assert revisions["revision_acceleration"] == pytest.approx(-5.7644)


def test_compute_revisions_insufficient_history_is_none(tmp_path):
    """快照不足 1m/3m ⇒ None（绝不拿当期值充基准，也不回退 0）。"""
    append_snapshot("600519.SH", {"as_of_date": ASOF, "eps_fy1": 10.0}, data_dir=tmp_path)

    revisions = compute_revisions("600519.SH", data_dir=tmp_path, as_of=ASOF)

    assert revisions["eps_fy1_revision_1m"] is None
    assert revisions["eps_fy1_revision_3m"] is None
    assert revisions["eps_fy2_revision_1m"] is None
    assert revisions["revision_acceleration"] is None


def test_build_f10_consensus_maps_values_and_appends_snapshot(tmp_path, http):
    """快照 → ConsensusOutput：13 字段全初始化、新源不可得字段显式 missing、快照落盘。"""
    out = build_f10_consensus("600519.SH", data_dir=tmp_path)

    assert out is not None
    assert out.source == SOURCE_NAME
    assert out.as_of_date == ASOF  # 快照日（jgyc[0].PUBLISH_DATE），非本地日
    assert out.values["eps_fy1"] == pytest.approx(67.249318181818)
    assert out.values["eps_fy2"] == pytest.approx(71.125909090909)
    assert out.values["eps_fy3"] == pytest.approx(75.18)
    assert out.values["coverage_count"] == 44
    # 接口不存在目标价 / 量表不可比 / 无上期评级 ⇒ None + 显式 missing，绝不回退 0
    assert out.values["target_price"] is None
    assert out.values["rating_mean"] is None
    assert out.values["rating_upgrade_1m"] is None
    assert out.values["rating_downgrade_1m"] is None
    assert "consensus_missing: target_price" in out.missing
    assert "consensus_missing: rating_mean" in out.missing
    assert "consensus_missing: eps_fy1_revision_1m" in out.missing
    assert out.report_count == 0  # 新源不统计研报条数（机构数见 coverage_count）
    row = load_snapshots("600519.SH", data_dir=tmp_path)[0]
    assert row["as_of_date"] == ASOF
    # 落盘行的契约字段（设计 §5.2）
    assert {"as_of_date", "eps_fy1", "eps_fy2", "coverage_count"} <= row.keys()
    assert row["eps_fy1"] == pytest.approx(67.249318181818)
    assert row["coverage_count"] == 44


def test_default_snapshot_dir_is_data_consensus_snapshots(monkeypatch, tmp_path):
    """默认落盘目录 = ``<data_root>/consensus_snapshots``；显式 data_dir 优先（测试/运维逃生口）。"""
    monkeypatch.setattr("alphabee.utils.storage.get_data_root", lambda: tmp_path)

    assert emforecast.snapshots_dir() == tmp_path / "consensus_snapshots"
    assert emforecast.snapshots_dir().name == "consensus_snapshots"
    assert emforecast.snapshots_dir(tmp_path / "override") == tmp_path / "override"


def test_build_f10_consensus_revision_uses_previous_snapshot(tmp_path, http):
    """两次快照（相隔 30 天）→ 第二次的 1m revision 由序列算出，事件日=快照日。"""
    http.response = _FakeResponse(_f10_payload(publish_date="2026-09-19 00:00:00", eps1=100.0, eps2=200.0))
    first = build_f10_consensus("600519.SH", data_dir=tmp_path)
    http.response = _FakeResponse(_f10_payload(publish_date=f"{ASOF} 00:00:00", eps1=110.0, eps2=210.0))
    second = build_f10_consensus("600519.SH", data_dir=tmp_path)

    assert first is not None and first.values["eps_fy1_revision_1m"] is None
    assert second is not None
    assert second.values["eps_fy1_revision_1m"] == pytest.approx(10.0)
    assert second.values["eps_fy2_revision_1m"] == pytest.approx(5.0)
    assert second.values["eps_fy1_revision_3m"] is None  # 90 天内只有两个点 → 不编造
    assert second.as_of_date == ASOF
    assert [r["as_of_date"] for r in load_snapshots("600519.SH", data_dir=tmp_path)] == ["2026-09-19", ASOF]


# ─────────────────────────────────────────────────────────────────────────────
# C-provider 链（§5.3）：默认逐字现状 / 新源 / 回落留痕 / 非法值 fail-open
# ─────────────────────────────────────────────────────────────────────────────
def test_default_source_is_report_list_verbatim(monkeypatch, report_records):
    """缺省（不传 consensus_source）⇒ 读配置 report_list ⇒ 与改造前逐字一致。"""
    _patch_settings(monkeypatch)
    expected = aggregate_consensus(list(report_records))

    out = eastmoney.build_consensus("600519.SH")  # 与 decision_model 调用点同形（单参）

    assert out.source == "eastmoney:report/list"
    assert out.values == expected.values
    assert out.missing == expected.missing
    assert out.warnings == []
    assert out.as_of_date == expected.as_of_date
    assert out.report_count == expected.report_count


def test_explicit_report_list_matches_aggregate(monkeypatch, report_records):
    _patch_settings(monkeypatch, consensus_source="f10_forecast")  # 显式值必须压过配置
    expected = aggregate_consensus(list(report_records))

    out = eastmoney.build_consensus("600519.SH", consensus_source="report_list")

    assert out.source == "eastmoney:report/list"
    assert out.values == expected.values
    assert out.warnings == []


def test_f10_forecast_provider_uses_new_source(monkeypatch, http, tmp_path):
    """``f10_forecast`` ⇒ 新源（source 自证），研报路径一次都不走。"""

    def _boom(*a, **k):
        raise AssertionError("f10 成功时不得回落 report_list")

    monkeypatch.setattr(eastmoney, "fetch_report_records", _boom)

    out = eastmoney.build_consensus("600519.SH", consensus_source="f10_forecast", snapshot_dir=str(tmp_path))

    assert out.source == SOURCE_NAME
    assert out.values["coverage_count"] == 44
    assert out.values["eps_fy1"] == pytest.approx(67.249318181818)
    assert out.warnings == []
    assert (tmp_path / "600519.jsonl").exists()


def test_f10_forecast_failure_falls_back_to_report_list_with_warning(monkeypatch, report_records, http):
    """新源不可用 ⇒ 回落 report_list（source 诚实指向实际来源）+ warnings 留痕。"""
    _patch_settings(monkeypatch)
    http.error = requests.exceptions.ConnectionError("down")
    expected = aggregate_consensus(list(report_records))

    out = eastmoney.build_consensus("600519.SH", consensus_source="f10_forecast")

    assert out.source == "eastmoney:report/list"
    assert out.values == expected.values
    assert len(out.warnings) == 1
    assert "f10_forecast" in out.warnings[0] and "fallback" in out.warnings[0]


def test_auto_prefers_f10_and_falls_back_when_unavailable(monkeypatch, report_records, http, tmp_path):
    """``auto``：新源可用 → 新源；新源不可用 → report_list + 留痕。"""
    monkeypatch.setattr(eastmoney, "fetch_report_records", lambda *a, **k: list(report_records))

    out_f10 = eastmoney.build_consensus("600519.SH", consensus_source="auto", snapshot_dir=str(tmp_path))
    assert out_f10.source == SOURCE_NAME

    http.error = requests.exceptions.Timeout("slow")
    out_fallback = eastmoney.build_consensus("600519.SH", consensus_source="auto", snapshot_dir=str(tmp_path))
    assert out_fallback.source == "eastmoney:report/list"
    assert any("f10_forecast" in w for w in out_fallback.warnings)


def test_no_coverage_also_falls_back(monkeypatch, report_records, http):
    """无覆盖是合法数据状态但产不出共识 ⇒ 同样回落（不产空帧说谎）。"""
    http.response = _FakeResponse(_no_coverage_payload())

    out = eastmoney.build_consensus("600112.SH", consensus_source="f10_forecast")

    assert out.source == "eastmoney:report/list"
    assert any("f10_forecast" in w for w in out.warnings)


def test_invalid_consensus_source_warns_and_falls_back(monkeypatch, report_records, caplog):
    """非法值 ⇒ logger warn + report_list（绝不抛），warning 同时写进 ConsensusOutput.warnings。"""
    _patch_settings(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="alphabee.collectors.consensus.eastmoney"):
        out = eastmoney.build_consensus("600519.SH", consensus_source="broken_source")

    assert out.source == "eastmoney:report/list"
    assert any("非法值" in w and "report_list" in w for w in out.warnings)
    assert any("非法值" in r.message for r in caplog.records)


def test_invalid_configured_source_warns_and_falls_back(monkeypatch, report_records, caplog):
    """配置值非法（config 段写错）⇒ 同样 warn + report_list。"""
    _patch_settings(monkeypatch, consensus_source="f10")

    with caplog.at_level(logging.WARNING, logger="alphabee.collectors.consensus.eastmoney"):
        out = eastmoney.build_consensus("600519.SH")

    assert out.source == "eastmoney:report/list"
    assert any("非法值" in w for w in out.warnings)


def test_config_unavailable_falls_back_to_report_list(monkeypatch, report_records, caplog):
    """读配置异常 ⇒ report_list，绝不抛（fail-open）。"""
    _patch_settings(monkeypatch, raise_exc=True)

    with caplog.at_level(logging.WARNING, logger="alphabee.collectors.consensus.eastmoney"):
        out = eastmoney.build_consensus("600519.SH")

    assert out.source == "eastmoney:report/list"
    assert out.warnings == []  # 配置不可用不是调用方的错，只在 logger 留痕


def test_default_follows_configured_source(monkeypatch, http, tmp_path):
    """缺省 provider 由 ``settings.midterm.consensus_source`` 决定（不是硬编码）。"""
    _patch_settings(monkeypatch, consensus_source="f10_forecast")

    out = eastmoney.build_consensus("600519.SH", snapshot_dir=str(tmp_path))

    assert out.source == SOURCE_NAME


def test_resolve_consensus_source_tuple_contract(monkeypatch):
    """``(source, warning)`` 契约：合法值无 warning、非法值有 warning。"""
    _patch_settings(monkeypatch)

    assert eastmoney.resolve_consensus_source("AUTO") == ("auto", "")
    assert eastmoney.resolve_consensus_source(None) == ("report_list", "")
    source, warning = eastmoney.resolve_consensus_source("nope")
    assert source == "report_list" and "非法值" in warning


# ─────────────────────────────────────────────────────────────────────────────
# 配置面三层同源（模型默认值 / config.yaml / config.yaml.example）
# ─────────────────────────────────────────────────────────────────────────────
def test_midterm_settings_declares_consensus_source():
    from alphabee.config import MidtermClassifierSettings

    assert MidtermClassifierSettings().consensus_source == "report_list"
    for value in ("report_list", "f10_forecast", "auto"):
        assert MidtermClassifierSettings(consensus_source=value).consensus_source == value
    with pytest.raises(ValidationError):
        MidtermClassifierSettings(consensus_source="bogus")


@pytest.mark.parametrize("file_name", ["config.yaml", "config.yaml.example"])
def test_config_files_declare_consensus_source(file_name):
    path = PROJECT_ROOT / file_name
    if not path.exists():
        pytest.skip(f"{file_name} 不存在（config.yaml 为本地未跟踪文件）")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert raw["midterm"]["consensus_source"] == "report_list"


# ─────────────────────────────────────────────────────────────────────────────
# 硬化 1：快照写盘失败 fail-open（t5 审查建议）
# ─────────────────────────────────────────────────────────────────────────────
def _block_snapshot_writes(monkeypatch, *, exc: Exception | None = None):
    """把快照写入单点替换成必失败（模拟只读目录/磁盘满），不改读路径。"""
    error = exc or PermissionError(13, "Permission denied", "read-only fixture")

    def _boom(path):
        raise error

    monkeypatch.setattr(emforecast, "_open_append", _boom)


def test_append_snapshot_write_failure_is_fail_open(monkeypatch, tmp_path):
    """写盘 OSError ⇒ append_snapshot 不抛、返回 ""（未落盘）；失败说明含 write_failed。"""
    _block_snapshot_writes(monkeypatch)
    values = {"as_of_date": ASOF, "eps_fy1": 10.0, "coverage_count": 7}

    assert append_snapshot("600519.SH", values, data_dir=tmp_path) == ""
    as_of, error = emforecast._append_snapshot_row("600519.SH", values, data_dir=tmp_path)
    assert as_of == ASOF
    assert error.startswith("write_failed")
    assert not (tmp_path / "600519.jsonl").exists()


def test_build_f10_consensus_write_failure_keeps_values_and_warns(monkeypatch, tmp_path, http):
    """写盘失败：当次快照值仍进 ConsensusOutput，warnings 留痕，绝不回退/抛。"""
    _block_snapshot_writes(monkeypatch)

    out = build_f10_consensus("600519.SH", data_dir=tmp_path)

    assert out is not None
    assert out.source == SOURCE_NAME
    assert out.values["eps_fy1"] == pytest.approx(67.249318181818)
    assert out.values["eps_fy2"] == pytest.approx(71.125909090909)
    assert out.values["coverage_count"] == 44
    assert out.as_of_date == ASOF
    assert len(out.warnings) == 1
    assert "落盘失败" in out.warnings[0]
    assert not (tmp_path / "600519.jsonl").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受只读目录权限限制")
def test_build_f10_consensus_readonly_directory_is_fail_open(http, tmp_path):
    """真实只读目录（chmod 500）：全链路不抛，输出仍可用 + 留痕。"""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    os.chmod(readonly, 0o500)
    try:
        out = build_f10_consensus("600519.SH", data_dir=readonly)
    finally:
        os.chmod(readonly, 0o700)

    assert out is not None
    assert out.values["eps_fy1"] == pytest.approx(67.249318181818)
    assert any("落盘失败" in w for w in out.warnings)


def test_mkdir_failure_is_fail_open(monkeypatch, tmp_path, http):
    """目录创建失败（父路径是普通文件 ⇒ FileExistsError/OSError）同样 fail-open。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    real_snapshot_path = emforecast._snapshot_path
    monkeypatch.setattr(
        emforecast,
        "_snapshot_path",
        lambda symbol, data_dir=None: blocker / real_snapshot_path(symbol, data_dir=data_dir).name,
    )

    out = build_f10_consensus("600519.SH", data_dir=tmp_path)

    assert out is not None and out.values["eps_fy1"] == pytest.approx(67.249318181818)
    assert any("落盘失败" in w for w in out.warnings)


def test_load_snapshots_read_error_is_fail_open(tmp_path):
    """读盘 OSError（同名路径是目录 ⇒ IsADirectoryError）⇒ [] 不抛，历史当空。"""
    (tmp_path / "600519.jsonl").mkdir()

    assert load_snapshots("600519.SH", data_dir=tmp_path) == []
    assert compute_revisions("600519.SH", data_dir=tmp_path, as_of=ASOF)["eps_fy1_revision_1m"] is None


def test_provider_chain_does_not_fall_back_on_write_failure(monkeypatch, tmp_path, http):
    """f10/auto 不因写盘失败回落或中断（研报路径一次都不走）；warnings 只留落盘失败。"""
    _block_snapshot_writes(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("写盘失败不得触发 provider 回落")

    monkeypatch.setattr(eastmoney, "fetch_report_records", _boom)

    out_f10 = eastmoney.build_consensus("600519.SH", consensus_source="f10_forecast", snapshot_dir=str(tmp_path))
    assert out_f10.source == SOURCE_NAME
    assert out_f10.values["eps_fy1"] == pytest.approx(67.249318181818)
    assert any("落盘失败" in w for w in out_f10.warnings)

    out_auto = eastmoney.build_consensus("600519.SH", consensus_source="auto", snapshot_dir=str(tmp_path))
    assert out_auto.source == SOURCE_NAME
    assert not any("fallback" in w for w in out_auto.warnings)


# ─────────────────────────────────────────────────────────────────────────────
# 硬化 1b：revision 语义与落盘解耦（落盘失败时当期点不丢）
# ─────────────────────────────────────────────────────────────────────────────
def test_revision_identical_whether_snapshot_persisted_or_not(monkeypatch, tmp_path, http):
    """同一快照序列：落盘成功 / 落盘失败算出的 revision 逐位一致（当次内存点参与计算）。"""
    ok_dir = tmp_path / "ok"
    fail_dir = tmp_path / "fail"

    # 第一天（09-19, eps=100）两边都落盘成功
    http.response = _FakeResponse(_f10_payload(publish_date="2026-09-19 00:00:00", eps1=100.0, eps2=200.0))
    assert build_f10_consensus("600519.SH", data_dir=ok_dir) is not None
    assert build_f10_consensus("600519.SH", data_dir=fail_dir) is not None

    # 第二天（10-19, eps=110）：ok 目录正常落盘；fail 目录写盘失败（历史仍可读）
    http.response = _FakeResponse(_f10_payload(publish_date=f"{ASOF} 00:00:00", eps1=110.0, eps2=210.0))
    baseline = build_f10_consensus("600519.SH", data_dir=ok_dir)
    _block_snapshot_writes(monkeypatch)
    degraded = build_f10_consensus("600519.SH", data_dir=fail_dir)

    assert baseline is not None and degraded is not None
    assert degraded.values["eps_fy1_revision_1m"] == pytest.approx(10.0)
    assert degraded.values["eps_fy1_revision_1m"] == baseline.values["eps_fy1_revision_1m"]
    assert degraded.values["eps_fy2_revision_1m"] == baseline.values["eps_fy2_revision_1m"] == pytest.approx(5.0)
    # 当期值不丢（来自内存快照），且失败帧留痕
    assert degraded.values["eps_fy1"] == pytest.approx(110.0)
    assert any("落盘失败" in w for w in degraded.warnings)
    # fail 目录只留第一天一行（写失败不产生半行/脏行）
    assert [row["as_of_date"] for row in load_snapshots("600519.SH", data_dir=fail_dir)] == ["2026-09-19"]


# ─────────────────────────────────────────────────────────────────────────────
# 硬化 2：区块类型守卫（非 list 不进入迭代，绝不抛 TypeError）
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "payload",
    [
        {"pjtj": [], "jgyc": 5, "yctj_list": 5},
        {"pjtj": None, "jgyc": [], "yctj_list": []},
        {"pjtj": [], "jgyc": [], "yctj_list": "not-a-list"},
        {"pjtj": {}, "jgyc": [], "yctj_list": []},
    ],
)
def test_non_list_required_block_is_fail_open(http, payload):
    """必需区块非 list（含 {"jgyc":5,"yctj_list":5}）⇒ {} + 明确原因，绝不抛 TypeError。"""
    http.response = _FakeResponse(payload)

    assert fetch_profit_forecast("600519.SH") == {}
    assert build_f10_consensus("600519.SH") is None
    _raw, reason = emforecast._fetch_raw("600519.SH", timeout=5)
    assert reason.startswith("invalid_block:")


@pytest.mark.parametrize("payload_patch", [{"ycmx": 5}, {"yctj_chart": "x"}, {"ycmx": None}])
def test_non_list_unused_block_is_tolerated(http, payload_patch):
    """本模块不消费的旁支区块非 list ⇒ 只记 warning，不判死可用数据（不因旁支丢主数据）。"""
    payload = _f10_payload()
    payload.update(payload_patch)
    http.response = _FakeResponse(payload)

    snapshot = fetch_profit_forecast("600519.SH")

    assert snapshot["eps_fy1"] == pytest.approx(67.249318181818)
    assert snapshot["coverage_count"] == 44


# ─────────────────────────────────────────────────────────────────────────────
# 硬化 3：快照文件名单一形态（纯 6 位代码；evidence 300750.SZ ≡ factors 300750）
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("symbol", "stem"),
    [
        ("300750.SZ", "300750"),
        ("300750", "300750"),
        ("sz300750", "300750"),
        ("SZ300750", "300750"),
        ("600519.SH", "600519"),
        ("920002.BJ", "920002"),
    ],
)
def test_snapshot_filename_stem_is_pure_code(symbol, stem):
    assert emforecast._snapshot_path(symbol).name == f"{stem}.jsonl"


def test_write_and_read_share_one_file_across_symbol_forms(tmp_path):
    """``300750.SZ``（evidence 路径）写入，``300750``/``sz300750``（factors 路径）读出同一序列。"""
    append_snapshot("300750.SZ", {"as_of_date": "2026-09-19", "eps_fy1": 20.0}, data_dir=tmp_path)
    append_snapshot("300750", {"as_of_date": ASOF, "eps_fy1": 21.0}, data_dir=tmp_path)

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["300750.jsonl"]
    for symbol in ("300750.SZ", "300750", "sz300750", "SZ300750"):
        assert [row["eps_fy1"] for row in load_snapshots(symbol, data_dir=tmp_path)] == [20.0, 21.0]


def test_revision_history_shared_across_symbol_forms(tmp_path, http):
    """跨路径取数复用同一 revision 历史：.SZ 写第一天、纯代码写第二天 ⇒ 1m revision 可算。"""
    http.response = _FakeResponse(_f10_payload(publish_date="2026-09-19 00:00:00", eps1=100.0))
    assert build_f10_consensus("300750.SZ", data_dir=tmp_path) is not None
    http.response = _FakeResponse(_f10_payload(publish_date=f"{ASOF} 00:00:00", eps1=110.0))
    out = build_f10_consensus("300750", data_dir=tmp_path)

    assert out is not None
    assert out.values["eps_fy1_revision_1m"] == pytest.approx(10.0)
    assert [p.name for p in tmp_path.iterdir()] == ["300750.jsonl"]
