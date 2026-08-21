"""本地财报目录定位（decide_root_path 等）测试。

覆盖新嵌套结构（``<公司>(<代码>)/财报/<报告期>/``）与旧平铺结构
（``<报告名>/``）的兼容定位；REPORT_DIR 通过 monkeypatch 指向临时目录，
不触碰真实 reports/。
"""

from __future__ import annotations

import pytest

from alphabee.tools import financial_report as fr
from alphabee.tools.financial_report import FinancialReportRequest


@pytest.fixture
def reports_root(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "REPORT_DIR", tmp_path)
    # 新嵌套结构
    (tmp_path / "宁德时代(300750)" / "财报" / "2026年半年度报告").mkdir(parents=True)
    (tmp_path / "宁德时代(300750)" / "财报" / "2025年年度报告").mkdir(parents=True)
    # 旧平铺结构
    (tmp_path / "贵州茅台：2025年年度报告").mkdir(parents=True)
    return tmp_path


def test_decide_nested_by_name_year_type(reports_root):
    path = fr.decide_root_path(
        FinancialReportRequest(company_name="宁德时代", year=2026, report_type="semiannual", query="x")
    )
    assert path == str(reports_root / "宁德时代(300750)" / "财报" / "2026年半年度报告")


def test_decide_nested_by_code(reports_root, monkeypatch):
    # 代码无法反查公司名时，仍能靠 ``<公司>(<代码>)`` 括号内代码直接命中
    monkeypatch.setattr(fr, "resolve_company_name_by_code", lambda code: None)
    path = fr.decide_root_path(
        FinancialReportRequest(company_code="300750", year=2025, report_type="annual", query="x")
    )
    assert path == str(reports_root / "宁德时代(300750)" / "财报" / "2025年年度报告")


def test_decide_flat_legacy(reports_root):
    path = fr.decide_root_path(
        FinancialReportRequest(company_name="贵州茅台", year=2025, report_type="annual", query="x")
    )
    assert path == str(reports_root / "贵州茅台：2025年年度报告")


def test_decide_ambiguous_year_only_returns_none(reports_root):
    # 仅年份命中多家公司，存在歧义 → None
    assert fr.decide_root_path(FinancialReportRequest(year=2025, query="x")) is None


def test_decide_missing_company_returns_none(reports_root):
    assert fr.decide_root_path(FinancialReportRequest(query="x")) is None


def test_list_available_reports_nested_and_flat(reports_root):
    names = fr._list_available_reports("宁德时代")
    assert names == [
        "宁德时代(300750)/财报/2025年年度报告",
        "宁德时代(300750)/财报/2026年半年度报告",
    ]
    flat = fr._list_available_reports("贵州茅台")
    assert flat == ["贵州茅台：2025年年度报告"]


def test_extract_code():
    assert fr._extract_code("宁德时代(300750)") == "300750"
    assert fr._extract_code("贵州茅台：2025年年度报告") == ""
    assert fr._extract_code("某公司(abc123)") == ""


def test_normalize_locator_code():
    assert fr._normalize_locator_code("300750.SZ") == "300750"
    assert fr._normalize_locator_code("600519") == "600519"
