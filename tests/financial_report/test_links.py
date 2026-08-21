"""财报/研报下载链接获取测试（links.py）。

网络层（巨潮 suggest/hisAnnouncement、东方财富研报列表）全部用 mock 隔离，
只验证参数组装与结果解析，不依赖真实网络。
"""

from __future__ import annotations

import json

import pytest

from alphabee.financial_report import links

# ── 纯函数 ──────────────────────────────────────────────────────────────────


def test_normalize_stock_code():
    assert links._normalize_stock_code("300750") == "300750"
    assert links._normalize_stock_code("300750.SZ") == "300750"
    assert links._normalize_stock_code(" 600519.sh ") == "600519"


def test_parse_report_types():
    assert links._parse_report_types(None) == ["annual", "semiannual", "q1", "q3"]
    assert links._parse_report_types("all") == ["annual", "semiannual", "q1", "q3"]
    assert links._parse_report_types("annual") == ["annual"]
    assert links._parse_report_types("q2") == ["semiannual"]  # 二季报=半年报
    assert links._parse_report_types(["q1", "q3"]) == ["q1", "q3"]
    assert links._parse_report_types(["annual", "annual"]) == ["annual"]


def test_full_pdf_url():
    assert links._full_pdf_url("finalpage/2026-03-10/1225002214.PDF") == (
        "https://static.cninfo.com.cn/finalpage/2026-03-10/1225002214.PDF"
    )
    assert links._full_pdf_url("https://x/y.pdf") == "https://x/y.pdf"
    assert links._full_pdf_url("") == ""


def test_ms_to_date():
    # 1773072000000 ms → 2026-03-08（本地时区，仅校验格式与可解析性）
    assert links._ms_to_date(1773072000000).startswith("2026-03")
    assert links._ms_to_date(None) == ""


def test_normalize_eastmoney_pdf_url():
    assert links._normalize_eastmoney_pdf_url("http://pdf.dfcfw.com/pdf/H3_AP_1.pdf?a=1") == (
        "https://pdf.dfcfw.com/pdf/H3_AP_1.pdf"
    )


# ── get_financial_report_links（mock 巨潮） ────────────────────────────────


def _fake_post(monkeypatch, responses: dict[str, object]):
    def fake_post(url, data=None, headers=None, timeout=None):
        # data 是 dict；用关键字段区分 suggest 与 announce 接口
        key = json.dumps(data, sort_keys=True, ensure_ascii=False)
        for marker, payload in responses.items():
            if marker in key:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected request: {url} {key}")

    monkeypatch.setattr(links.requests, "post", fake_post)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_get_financial_report_links_by_code(monkeypatch):
    suggest_marker = "keyWord"
    announce_marker = "category_ndbg_szsh"
    _fake_post(
        monkeypatch,
        {
            suggest_marker: [{"code": "300750", "zwjc": "宁德时代", "orgId": "GD165627"}],
            announce_marker: {
                "announcements": [
                    {
                        "secCode": "300750",
                        "announcementTitle": "2025年年度报告",
                        "announcementTime": 1773072000000,
                        "adjunctUrl": "finalpage/2026-03-10/1225002214.PDF",
                        "adjunctSize": 1997,
                    }
                ],
                "totalAnnouncement": 1,
            },
        },
    )

    result = links.get_financial_report_links(code="300750", report_type="annual")
    assert result["source"] == "cninfo"
    assert result["code"] == "300750"
    assert result["name"] == "宁德时代"
    assert result["count"] == 1
    report = result["reports"][0]
    assert report["title"] == "2025年年度报告"
    assert report["type"] == "年报"
    assert report["category"] == "annual"
    assert report["download_url"].endswith("/1225002214.PDF")


def test_get_financial_report_links_requires_code_or_name():
    with pytest.raises(ValueError, match="code 或 name"):
        links.get_financial_report_links()


def test_get_financial_report_links_suggest_empty(monkeypatch):
    _fake_post(monkeypatch, {"keyWord": []})
    with pytest.raises(ValueError, match="未在巨潮资讯网找到证券"):
        links.get_financial_report_links(name="不存在的公司")


# ── get_research_report_links（mock 东方财富） ─────────────────────────────


def test_get_research_report_links(monkeypatch):
    fake_list = {
        "page_num": 1,
        "page_size": 2,
        "has_next": True,
        "report_count": 2,
        "reports": [
            {
                "title": "业绩快速增长",
                "publishDate": "2026-07-31 00:00:00.000",
                "orgSName": "国信证券",
                "researcher": "王蔚祺",
                "emRatingName": "增持",
                "infoCode": "AP202607311827537552",
                "encodeUrl": "abc=",
                "attachPages": 5,
            },
            {
                "title": "无链接研报",
                "publishDate": "2026-07-01 00:00:00.000",
                "orgSName": "某证券",
                "infoCode": "APX",
                "encodeUrl": "",
            },
        ],
    }

    class FakeHelper:
        def fetch_report_content_by_encoded_url(self, session, encoded_url, timeout=20):
            return {"attachUrl": "http://pdf.dfcfw.com/pdf/H3_AP202607311827537552_1.pdf"}

    monkeypatch.setattr(links, "get_eastmoney_report_list", lambda **kw: fake_list)
    monkeypatch.setattr(links, "EastmoneyHelper", FakeHelper)

    result = links.get_research_report_links("300750", start_date="2026-01-01", end_date="2026-08-20")
    assert result["source"] == "eastmoney"
    assert result["code"] == "300750"
    assert result["has_next"] is True
    assert len(result["reports"]) == 2

    first = result["reports"][0]
    assert first["download_url"] == "https://pdf.dfcfw.com/pdf/H3_AP202607311827537552_1.pdf"
    assert first["org"] == "国信证券"
    assert first["date"] == "2026-07-31"

    second = result["reports"][1]
    assert second["download_url"] is None  # 解析不到直链但条目保留


def test_get_research_report_links_requires_code():
    with pytest.raises(ValueError, match="code"):
        links.get_research_report_links("")
