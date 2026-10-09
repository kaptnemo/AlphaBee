"""本地财报「管理层讨论与分析」片段测试（对标组在线兜底来源）。"""

from __future__ import annotations

import json

import alphabee.company_track.peer_report as report_module
from alphabee.company_track.peer_report import fetch_local_report_fragments

_SAMPLE_MD = """## 重要提示
## 第一节 重要提示
不得作任何保证。

## 第三节 管理层讨论与分析
### 一、 报告期内公司从事的主要业务
公司主营印制电路板（PCB）与封装基板，为内资最大封装基板供应商。
### 二、 核心竞争力分析
3-In-One 商业模式。

## 第四节 公司治理
治理内容不应被截入。
"""


def _write_report(root, code, period, full_text):
    report_dir = root / f"测试公司({code})" / "财报" / period
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / ".report_manifest.json").write_text(
        json.dumps(
            {
                "report_period": period,
                "company_name": "测试公司",
                "company_code": code,
                "full_text_path": str(full_text),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _setup_reports(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    full_text = tmp_path / "full.md"
    full_text.write_text(_SAMPLE_MD, encoding="utf-8")
    _write_report(root, "002916", "2026年半年度报告", full_text)
    _write_report(root, "002916", "2025年年度报告", full_text)
    monkeypatch.setattr(report_module, "REPORT_DIR", root)
    return root


def test_fetch_fragments_picks_latest_and_extracts_section(tmp_path, monkeypatch):
    _setup_reports(tmp_path, monkeypatch)

    fragments, meta = fetch_local_report_fragments("002916.SZ")

    assert len(fragments) == 1
    assert "印制电路板" in fragments[0]
    assert "封装基板" in fragments[0]
    assert "第四节" not in fragments[0]  # 截到第四节前
    assert "第一节" not in fragments[0]
    assert meta["report_period"] == "2026年半年度报告"


def test_period_rank_prefers_annual_over_semiannual(tmp_path, monkeypatch):
    """同一年内年报 > 半年报（``半年度报告`` 不得被 ``年度报告`` 子串误判）。"""
    root = tmp_path / "reports"
    full_text = tmp_path / "full.md"
    full_text.write_text(_SAMPLE_MD, encoding="utf-8")
    _write_report(root, "002916", "2025年半年度报告", full_text)
    _write_report(root, "002916", "2025年年度报告", full_text)
    monkeypatch.setattr(report_module, "REPORT_DIR", root)

    _, meta = fetch_local_report_fragments("002916.SZ")
    assert meta["report_period"] == "2025年年度报告"


def test_fetch_fragments_no_local_report(tmp_path, monkeypatch):
    monkeypatch.setattr(report_module, "REPORT_DIR", tmp_path / "empty")

    fragments, meta = fetch_local_report_fragments("002916.SZ")

    assert fragments == []
    assert "无本地财报" in meta["note"]


def test_fetch_fragments_missing_full_text(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    report_dir = root / "测试公司(002916)" / "财报" / "2026年半年度报告"
    report_dir.mkdir(parents=True)
    (report_dir / ".report_manifest.json").write_text(
        json.dumps({"report_period": "2026年半年度报告", "full_text_path": ""}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(report_module, "REPORT_DIR", root)

    fragments, meta = fetch_local_report_fragments("002916.SZ")

    assert fragments == []
    assert "full_text_path" in meta["note"]


def test_fetch_fragments_unreadable_full_text(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    _write_report(root, "002916", "2026年半年度报告", tmp_path / "missing.md")
    monkeypatch.setattr(report_module, "REPORT_DIR", root)

    fragments, meta = fetch_local_report_fragments("002916.SZ")

    assert fragments == []
    assert meta["error"] is not None
