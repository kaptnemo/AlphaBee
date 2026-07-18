"""Unit & integration tests for Eastmoney report detail fetching."""

import json
from unittest.mock import MagicMock

import pytest

from alphabee.collectors.eastmoney.helper import (
    EastmoneyHelper,
    EastmoneyReportDetail,
    jsonp_to_json,
)

# ──────────────────────────────────────────────────────────────────────
# Sample data
# ──────────────────────────────────────────────────────────────────────

_ENCODED_URL = "F6IVvWrQaTYYJ+d9R8ZkdjHRxDceAw0egnKFijFOgRE="

_ZWINFO_JSON = {
    "attach_pages": "3",
    "attach_size": "484",
    "attach_type": "0",
    "attach_url": "https://pdf.dfcfw.com/pdf/H3_AP202607101826864211_1.pdf",
    "company_code": "80000031",
    "eitime": "2026-07-10 10:52:20",
    "extend": {},
    "info_code": "AP202607101826864211",
    "notice_content": "　　蔚蓝锂芯(002245)\\n　　投资要点\\n　　事件：测试报告内容。\\n　　风险提示：下游需求不及预期。",
    "notice_date": "2026-07-10 00:00:00",
    "notice_title": "印尼拟扩建5GWh小圆柱",
    "page_size": 1,
    "rating": "A",
    "researcher": "曾朵红,阮巧燕,朱家佟",
    "security": [
        {
            "market_uni": "0",
            "publish_relation": [{"originalCode": "1033", "publishName": "电池"}],
            "short_name": "蔚蓝锂芯",
            "stock": "002245",
        }
    ],
    "short_name": "蔚蓝锂芯",
    "source_sample_name": "东吴证券",
    "star": "3",
}

_HTML_WITH_ZWINFO = f"""
<html><body>
<script>var zwinfo={json.dumps(_ZWINFO_JSON, ensure_ascii=False)};</script>
</body></html>
"""

_HTML_WITHOUT_ZWINFO = """
<html><body>
<div class="c-infos"><span>www.eastmoney.com</span><span>东吴证券</span><span>曾朵红</span></div>
<h1 id="zw-title">测试研报标题</h1>
<div id="ctx-content"><p>　　测试段落一</p><p>　　测试段落二</p></div>
<a class="pdf-link" href="http://pdf.dfcfw.com/test.pdf">查看PDF原文</a>
</body></html>
"""

_CONTENT_API_RESPONSE = (
    'callback({"hits":1,"size":1,"data":[{"infoCode":"AP202607101826864211",'
    '"title":"测试研报","stockName":"蔚蓝锂芯","stockCode":"002245",'
    '"orgName":"东吴证券股份有限公司","orgSName":"东吴证券",'
    '"publishDate":"2026-07-10 00:00:00.000","researcher":"曾朵红,阮巧燕",'
    '"emRatingName":"买入","attachUrl":"http://pdf.dfcfw.com/pdf/test.pdf",'
    '"indvInduCode":"1033","indvInduName":"电池","encodeUrl":"F6IVvWrQaTYY",'
    '"contentInfoBodyContent":"","contentInfoBodyForm":"001"}],"currentYear":2026})'
)

_CONTENT_API_EMPTY = 'callback({"hits":0,"size":0,"data":[],"currentYear":2026})'


def _make_mock_session(text: str):
    """Create a MagicMock that behaves like requests.Session.get()."""
    mock_resp = MagicMock()
    mock_resp.text = text
    mock_resp.raise_for_status = lambda: None
    session = MagicMock()
    session.get.return_value = mock_resp
    return session


# ══════════════════════════════════════════════════════════════════════
# 1. jsonp_to_json helper
# ══════════════════════════════════════════════════════════════════════


class TestJsonpToJson:
    def test_normal_jsonp(self):
        result = jsonp_to_json(_CONTENT_API_RESPONSE)
        assert result["hits"] == 1
        assert result["data"][0]["title"] == "测试研报"

    def test_jsonp_with_semicolon(self):
        result = jsonp_to_json('fn({"k":"v"});')
        assert result["k"] == "v"

    def test_jsonp_no_semicolon(self):
        result = jsonp_to_json('fn({"k":"v"})')
        assert result["k"] == "v"

    def test_invalid_jsonp_raises(self):
        with pytest.raises(ValueError, match="JSONP parse failed"):
            jsonp_to_json("no parentheses here")


# ══════════════════════════════════════════════════════════════════════
# 2. EastmoneyReportDetail — value object
# ══════════════════════════════════════════════════════════════════════


class TestEastmoneyReportDetail:
    def test_construction_with_all_fields(self):
        detail = EastmoneyReportDetail(
            info_code="AP001",
            title="测试标题",
            publish_date="2026-07-10",
            org_name="测试证券",
            researcher="张三,李四",
            stock_name="测试股票",
            stock_code="000001",
            content="完整报告正文内容。",
            rating="买入",
            attach_url="http://pdf.example.com/test.pdf",
            industry_code="1001",
            industry_name="白酒",
            raw_json='{"key":"value"}',
        )
        assert detail.info_code == "AP001"
        assert detail.title == "测试标题"
        assert detail.content == "完整报告正文内容。"

    def test_construction_minimal(self):
        detail = EastmoneyReportDetail(
            info_code="AP002",
            title="标题",
            publish_date="",
            org_name="",
            researcher="",
            stock_name="",
            stock_code="",
            content="",
        )
        assert detail.info_code == "AP002"
        assert detail.rating is None
        assert detail.attach_url is None

    def test_to_dict(self):
        detail = EastmoneyReportDetail(
            info_code="AP003",
            title="T",
            publish_date="2026-07-10",
            org_name="O",
            researcher="R",
            stock_name="S",
            stock_code="000001",
            content="C",
            rating="买入",
            attach_url="http://a.b",
            industry_code="1001",
            industry_name="白酒",
        )
        d = detail.to_dict()
        assert d["info_code"] == "AP003"
        assert d["title"] == "T"
        assert d["content"] == "C"
        assert d["rating"] == "买入"
        assert d["industry_name"] == "白酒"


# ══════════════════════════════════════════════════════════════════════
# 3. _extract_zwinfo — HTML parsing
# ══════════════════════════════════════════════════════════════════════


class TestExtractZwinfo:
    def test_extract_from_valid_html(self):
        result = EastmoneyHelper._extract_zwinfo(_HTML_WITH_ZWINFO)
        assert result["info_code"] == "AP202607101826864211"
        assert result["notice_title"] == "印尼拟扩建5GWh小圆柱"
        assert result["source_sample_name"] == "东吴证券"
        assert result["researcher"] == "曾朵红,阮巧燕,朱家佟"
        assert result["short_name"] == "蔚蓝锂芯"
        assert "测试报告内容" in result["notice_content"]

    def test_extract_security_info(self):
        result = EastmoneyHelper._extract_zwinfo(_HTML_WITH_ZWINFO)
        assert len(result["security"]) == 1
        assert result["security"][0]["stock"] == "002245"
        assert result["security"][0]["short_name"] == "蔚蓝锂芯"
        pr = result["security"][0]["publish_relation"][0]
        assert pr["originalCode"] == "1033"
        assert pr["publishName"] == "电池"

    def test_extract_attach_url(self):
        result = EastmoneyHelper._extract_zwinfo(_HTML_WITH_ZWINFO)
        assert result["attach_url"] == "https://pdf.dfcfw.com/pdf/H3_AP202607101826864211_1.pdf"

    def test_extract_star_rating(self):
        result = EastmoneyHelper._extract_zwinfo(_HTML_WITH_ZWINFO)
        assert result["star"] == "3"

    def test_fallback_without_zwinfo(self):
        result = EastmoneyHelper._extract_zwinfo(_HTML_WITHOUT_ZWINFO)
        assert result["notice_title"] == "测试研报标题"
        assert "测试段落一" in result["notice_content"]
        assert "测试段落二" in result["notice_content"]
        assert result["source_sample_name"] == "东吴证券"
        assert result["researcher"] == "曾朵红"

    def test_fallback_pdf_link(self):
        result = EastmoneyHelper._extract_zwinfo(_HTML_WITHOUT_ZWINFO)
        assert result["attach_url"] == "http://pdf.dfcfw.com/test.pdf"

    def test_empty_html(self):
        result = EastmoneyHelper._extract_zwinfo("")
        assert result["info_code"] == ""
        assert result["notice_content"] == ""

    def test_notice_content_newline_normalization(self):
        result = EastmoneyHelper._extract_zwinfo(_HTML_WITH_ZWINFO)
        assert "蔚蓝锂芯" in result["notice_content"]
        assert "\\n" in result["notice_content"]


# ══════════════════════════════════════════════════════════════════════
# 4. fetch_report_detail — full pipeline (unit, mocked)
# ══════════════════════════════════════════════════════════════════════


class TestFetchReportDetailUnit:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    @pytest.fixture
    def mock_session(self):
        return _make_mock_session(_HTML_WITH_ZWINFO)

    def test_fetch_report_detail_returns_correct_fields(self, helper, mock_session):
        detail = helper.fetch_report_detail(mock_session, _ENCODED_URL)
        assert detail.info_code == "AP202607101826864211"
        assert detail.title == "印尼拟扩建5GWh小圆柱"
        assert detail.org_name == "东吴证券"
        assert detail.researcher == "曾朵红,阮巧燕,朱家佟"
        assert detail.stock_name == "蔚蓝锂芯"
        assert detail.stock_code == "002245"
        assert detail.rating == "3"
        assert detail.attach_url == "https://pdf.dfcfw.com/pdf/H3_AP202607101826864211_1.pdf"
        assert detail.industry_code == "1033"
        assert detail.industry_name == "电池"

    def test_fetch_report_detail_content_unwrap(self, helper, mock_session):
        detail = helper.fetch_report_detail(mock_session, _ENCODED_URL)
        assert "蔚蓝锂芯" in detail.content
        assert "投资要点" in detail.content
        assert "测试报告内容" in detail.content
        assert "风险提示" in detail.content
        assert "\\n" not in detail.content

    def test_fetch_report_detail_calls_correct_url(self, helper, mock_session):
        helper.fetch_report_detail(mock_session, _ENCODED_URL)
        call_args = mock_session.get.call_args
        params = call_args[1]["params"]
        assert params["encodeUrl"] == _ENCODED_URL

    def test_fetch_report_detail_raw_json_stored(self, helper, mock_session):
        detail = helper.fetch_report_detail(mock_session, _ENCODED_URL)
        assert detail.raw_json is not None
        parsed = json.loads(detail.raw_json)
        assert parsed["info_code"] == "AP202607101826864211"

    def test_fetch_report_detail_no_security_graceful(self, helper):
        html_no_security = (
            '<html><script>var zwinfo={"info_code":"AP001",'
            '"notice_title":"T","notice_content":"C","notice_date":"",'
            '"source_sample_name":"","researcher":"","short_name":"",'
            '"star":"","attach_url":""};</script></html>'
        )
        session = _make_mock_session(html_no_security)
        detail = helper.fetch_report_detail(session, _ENCODED_URL)
        assert detail.stock_code == ""
        assert detail.industry_code is None
        assert detail.industry_name is None

    def test_fetch_report_detail_empty_publish_relation_graceful(self, helper):
        html_empty_publish_relation = (
            '<html><script>var zwinfo={"info_code":"AP001",'
            '"notice_title":"T","notice_content":"C","notice_date":"",'
            '"source_sample_name":"","researcher":"","short_name":"",'
            '"star":"","attach_url":"","security":[{"stock":"000001",'
            '"publish_relation":[]}]};</script></html>'
        )
        session = _make_mock_session(html_empty_publish_relation)
        detail = helper.fetch_report_detail(session, _ENCODED_URL)
        assert detail.stock_code == "000001"
        assert detail.industry_code is None
        assert detail.industry_name is None

    def test_fetch_report_detail_empty_content(self, helper):
        html_empty = (
            '<html><script>var zwinfo={"info_code":"AP001",'
            '"notice_title":"T","notice_content":"","notice_date":"",'
            '"source_sample_name":"","researcher":"","short_name":"",'
            '"star":"","attach_url":"","security":[]};</script></html>'
        )
        session = _make_mock_session(html_empty)
        detail = helper.fetch_report_detail(session, _ENCODED_URL)
        assert detail.content == ""


# ══════════════════════════════════════════════════════════════════════
# 5. fetch_report_content_by_encoded_url — unit (mocked)
# ══════════════════════════════════════════════════════════════════════


class TestFetchReportContentUnit:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    @pytest.fixture
    def mock_session(self):
        return _make_mock_session(_CONTENT_API_RESPONSE)

    def test_returns_correct_fields(self, helper, mock_session):
        result = helper.fetch_report_content_by_encoded_url(mock_session, _ENCODED_URL)
        assert result["infoCode"] == "AP202607101826864211"
        assert result["title"] == "测试研报"
        assert result["stockName"] == "蔚蓝锂芯"
        assert result["stockCode"] == "002245"
        assert result["orgName"] == "东吴证券股份有限公司"
        assert result["attachUrl"] == "http://pdf.dfcfw.com/pdf/test.pdf"
        assert result["emRatingName"] == "买入"

    def test_raw_json_included(self, helper, mock_session):
        result = helper.fetch_report_content_by_encoded_url(mock_session, _ENCODED_URL)
        assert "raw_json" in result

    def test_empty_data_raises(self, helper):
        session = _make_mock_session(_CONTENT_API_EMPTY)
        with pytest.raises(ValueError, match="No report content found"):
            helper.fetch_report_content_by_encoded_url(session, _ENCODED_URL)

    def test_calls_correct_url_params(self, helper, mock_session):
        helper.fetch_report_content_by_encoded_url(mock_session, _ENCODED_URL)
        call_args = mock_session.get.call_args
        params = call_args[1]["params"]
        assert params["encodeUrl"] == _ENCODED_URL
        assert params["cb"] == "callback"


# ══════════════════════════════════════════════════════════════════════
# 6. fetch_report_detail_by_info_code — unit (mocked)
# ══════════════════════════════════════════════════════════════════════


class TestFetchReportDetailByInfoCodeUnit:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    def test_content_api_success_with_encode_url_fallback(self, helper):
        first_detail_resp = MagicMock()
        first_detail_resp.text = "<html><body>no zwinfo here</body></html>"
        first_detail_resp.raise_for_status = lambda: None

        content_resp = MagicMock()
        content_resp.text = _CONTENT_API_RESPONSE
        content_resp.raise_for_status = lambda: None

        second_detail_resp = MagicMock()
        second_detail_resp.text = _HTML_WITH_ZWINFO
        second_detail_resp.raise_for_status = lambda: None

        session = MagicMock()
        session.get.side_effect = [first_detail_resp, content_resp, second_detail_resp]

        detail = helper.fetch_report_detail_by_info_code(session, "AP202607101826864211")
        assert detail is not None
        assert detail.title == "印尼拟扩建5GWh小圆柱"
        assert detail.content

    def test_content_api_returns_none(self, helper):
        session = MagicMock()
        session.get.side_effect = Exception("network error")

        detail = helper.fetch_report_detail_by_info_code(session, "AP001")
        assert detail is None

    def test_content_api_success_full_content(self, helper):
        full_content_json = {
            "hits": 1,
            "data": [
                {
                    "infoCode": "AP001",
                    "title": "T",
                    "publishDate": "2026-07-10",
                    "orgName": "O",
                    "stockName": "S",
                    "stockCode": "000001",
                    "researcher": "R",
                    "emRatingName": "买入",
                    "attachUrl": "http://a",
                    "indvInduCode": "1033",
                    "indvInduName": "电池",
                    "encodeUrl": "",
                    "contentInfoBodyContent": "报告全文内容",
                }
            ],
        }
        resp = MagicMock()
        resp.text = f"callback({json.dumps(full_content_json, ensure_ascii=False)})"
        resp.raise_for_status = lambda: None

        session = MagicMock()
        session.get.return_value = resp

        detail = helper.fetch_report_detail_by_info_code(session, "AP001")
        assert detail is not None
        assert detail.content == "报告全文内容"
        assert detail.title == "T"


# ══════════════════════════════════════════════════════════════════════
# 7. fetch_report_industry_info_by_info_code — unit (mocked)
# ══════════════════════════════════════════════════════════════════════


class TestFetchReportIndustryInfoByInfoCodeUnit:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    def test_detail_page_returns_industry_info(self, helper):
        detail_resp = MagicMock()
        detail_resp.text = (
            '<html><script>var zwinfo={"info_code":"AP001",'
            '"notice_title":"T","notice_content":"C","notice_date":"",'
            '"source_sample_name":"","researcher":"","short_name":"测试股票",'
            '"star":"","attach_url":"http://pdf.dfcfw.com/pdf/H3_AP001_1.pdf",'
            '"security":[{"stock":"000001","publish_relation":['
            '{"originalCode":"1033","publishName":"电池"}]}]};</script></html>'
        )
        detail_resp.raise_for_status = lambda: None

        session = MagicMock()
        session.get.return_value = detail_resp

        result = helper.fetch_report_industry_info_by_info_code(session, "AP001")
        assert result is not None
        assert result["info_code"] == "AP001"
        assert result["stock_code"] == "000001"
        assert result["industry_code"] == "1033"
        assert result["industry_name"] == "电池"
        assert result["source"] == "detail_page"

    def test_falls_back_to_detail_page(self, helper):
        detail_resp = MagicMock()
        detail_resp.text = (
            '<html><script>var zwinfo={"info_code":"AP001",'
            '"notice_title":"T","notice_content":"C","notice_date":"",'
            '"source_sample_name":"","researcher":"","short_name":"测试股票",'
            '"star":"","attach_url":"http://pdf.dfcfw.com/pdf/H3_AP001_1.pdf",'
            '"security":[{"stock":"000001","publish_relation":['
            '{"originalCode":"1033","publishName":"电池"}]}]};</script></html>'
        )
        detail_resp.raise_for_status = lambda: None

        session = MagicMock()
        session.get.return_value = detail_resp

        result = helper.fetch_report_industry_info_by_info_code(session, "AP001")
        assert result is not None
        assert result["source"] == "detail_page"
        assert result["stock_code"] == "000001"
        assert result["industry_code"] == "1033"
        assert result["industry_name"] == "电池"

    def test_no_industry_info_returns_none(self, helper):
        session = MagicMock()
        session.get.side_effect = Exception("network error")

        result = helper.fetch_report_industry_info_by_info_code(session, "AP001")
        assert result is None


# ══════════════════════════════════════════════════════════════════════
# 8. fetch_industry_info_by_code — unit (mocked)
# ══════════════════════════════════════════════════════════════════════


class TestFetchIndustryInfoByCodeUnit:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    def test_returns_industry_summary_and_reports(self, helper):
        report_result = type("R", (), {})()
        report_result.data = [
            {
                "infoCode": "AP001",
                "title": "测试研报",
                "industry_final_name": "电池",
                "industry_final_code": "1033",
            }
        ]
        report_result.page_num = 1
        report_result.page_size = 50
        report_result.has_next = False

        helper.fetch_report_list = MagicMock(return_value=report_result)

        result = helper.fetch_industry_info_by_code(
            session=MagicMock(),
            industry_code="1033",
            start_date="2026-07-01",
            end_date="2026-07-10",
        )

        assert result["industry_code"] == "1033"
        assert result["industry_name"] == "电池"
        assert result["report_count"] == 1
        assert result["reports"][0]["infoCode"] == "AP001"


# ══════════════════════════════════════════════════════════════════════
# 9. _normalize_pdf_url — static helper
# ══════════════════════════════════════════════════════════════════════


class TestNormalizePdfUrl:
    def test_http_to_https(self):
        result = EastmoneyHelper._normalize_pdf_url("http://pdf.dfcfw.com/pdf/H3_AP001_1.pdf")
        assert result == "https://pdf.dfcfw.com/pdf/H3_AP001_1.pdf"

    def test_already_https(self):
        result = EastmoneyHelper._normalize_pdf_url("https://pdf.dfcfw.com/pdf/H3_AP001_1.pdf")
        assert result == "https://pdf.dfcfw.com/pdf/H3_AP001_1.pdf"

    def test_strips_query_params(self):
        result = EastmoneyHelper._normalize_pdf_url("https://pdf.dfcfw.com/pdf/H3_AP001_1.pdf?1783680740000.pdf")
        assert result == "https://pdf.dfcfw.com/pdf/H3_AP001_1.pdf"

    def test_strips_trailing_whitespace(self):
        result = EastmoneyHelper._normalize_pdf_url("  https://pdf.dfcfw.com/pdf/test.pdf  ")
        assert result == "https://pdf.dfcfw.com/pdf/test.pdf"


# ══════════════════════════════════════════════════════════════════════
# 8. _resolve_pdf_url — unit (mocked)
# ══════════════════════════════════════════════════════════════════════


class TestResolvePdfUrl:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    def test_resolves_from_content_api_first(self, helper):
        content_resp = MagicMock()
        content_resp.text = _CONTENT_API_RESPONSE
        content_resp.raise_for_status = lambda: None

        session = MagicMock()
        session.get.return_value = content_resp

        url = helper._resolve_pdf_url(session, _ENCODED_URL)
        assert url == "https://pdf.dfcfw.com/pdf/test.pdf"

    def test_falls_back_to_detail_page(self, helper):
        content_empty = 'callback({"hits":0,"data":[]})'
        content_resp = MagicMock()
        content_resp.text = content_empty
        content_resp.raise_for_status = lambda: None

        detail_resp = MagicMock()
        detail_resp.text = _HTML_WITH_ZWINFO
        detail_resp.raise_for_status = lambda: None

        session = MagicMock()
        session.get.side_effect = [content_resp, detail_resp]

        url = helper._resolve_pdf_url(session, _ENCODED_URL)
        assert url == "https://pdf.dfcfw.com/pdf/H3_AP202607101826864211_1.pdf"

    def test_content_api_error_falls_back(self, helper):
        detail_resp = MagicMock()
        detail_resp.text = _HTML_WITH_ZWINFO
        detail_resp.raise_for_status = lambda: None

        session = MagicMock()
        session.get.side_effect = [Exception("network error"), detail_resp]

        url = helper._resolve_pdf_url(session, _ENCODED_URL)
        assert url is not None

    def test_both_fail_returns_none(self, helper):
        session = MagicMock()
        session.get.side_effect = Exception("network error")

        url = helper._resolve_pdf_url(session, _ENCODED_URL)
        assert url is None


# ══════════════════════════════════════════════════════════════════════
# 9. download_report_pdf — unit (mocked)
# ══════════════════════════════════════════════════════════════════════

_PDF_BYTES = b"%PDF-1.4 test content"


class TestDownloadReportPdf:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    @pytest.fixture
    def tmpdir(self, tmp_path):
        return tmp_path

    def _make_pdf_session(self, pdf_url="http://pdf.dfcfw.com/pdf/H3_AP202607101826864211_1.pdf"):
        content_data = {
            "hits": 1,
            "data": [
                {
                    "infoCode": "AP202607101826864211",
                    "title": "T",
                    "attachUrl": pdf_url,
                }
            ],
        }
        content_resp = MagicMock()
        content_resp.text = f"callback({json.dumps(content_data, ensure_ascii=False)})"
        content_resp.raise_for_status = lambda: None

        pdf_resp = MagicMock()
        pdf_resp.iter_content.return_value = [_PDF_BYTES]

        session = MagicMock()
        session.get.side_effect = [content_resp, pdf_resp]
        return session

    def test_download_success(self, helper, tmpdir):
        session = self._make_pdf_session()
        filepath = helper.download_report_pdf(session, _ENCODED_URL, save_dir=tmpdir)
        assert filepath.exists()
        assert filepath.read_bytes() == _PDF_BYTES
        assert filepath.name == "AP202607101826864211.pdf"

    def test_custom_filename(self, helper, tmpdir):
        session = self._make_pdf_session()
        filepath = helper.download_report_pdf(session, _ENCODED_URL, save_dir=tmpdir, filename="custom_name")
        assert filepath.name == "custom_name.pdf"

    def test_custom_save_dir(self, helper, tmpdir):
        session = self._make_pdf_session()
        subdir = tmpdir / "sub" / "reports"
        filepath = helper.download_report_pdf(session, _ENCODED_URL, save_dir=subdir)
        assert filepath.parent == subdir.resolve()
        assert filepath.exists()

    def test_no_pdf_url_raises(self, helper, tmpdir):
        session = MagicMock()
        session.get.side_effect = Exception("network error")

        with pytest.raises(ValueError, match="Could not resolve PDF URL"):
            helper.download_report_pdf(session, _ENCODED_URL, save_dir=tmpdir)

    def test_info_code_extracted_from_pdf_url(self, helper, tmpdir):
        content_resp = MagicMock()
        content_data = {
            "hits": 1,
            "data": [
                {
                    "infoCode": "AP202607101826864211",
                    "attachUrl": "http://pdf.dfcfw.com/pdf/H3_AP202607101826864211_1.pdf",
                }
            ],
        }
        content_resp.text = f"callback({json.dumps(content_data, ensure_ascii=False)})"
        content_resp.raise_for_status = lambda: None

        pdf_resp = MagicMock()
        pdf_resp.iter_content.return_value = [_PDF_BYTES]

        session = MagicMock()
        session.get.side_effect = [content_resp, pdf_resp]

        filepath = helper.download_report_pdf(session, _ENCODED_URL, save_dir=tmpdir)
        assert filepath.name == "AP202607101826864211.pdf"


# ══════════════════════════════════════════════════════════════════════
# 10. download_report_pdf_by_info_code — unit (mocked)
# ══════════════════════════════════════════════════════════════════════


class TestDownloadReportPdfByInfoCode:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    @pytest.fixture
    def tmpdir(self, tmp_path):
        return tmp_path

    def test_download_success(self, helper, tmpdir):
        content_resp = MagicMock()
        content_data = {
            "hits": 1,
            "data": [
                {
                    "infoCode": "AP001",
                    "attachUrl": "http://pdf.dfcfw.com/pdf/H3_AP001_1.pdf",
                }
            ],
        }
        content_resp.text = f"callback({json.dumps(content_data, ensure_ascii=False)})"
        content_resp.raise_for_status = lambda: None

        pdf_resp = MagicMock()
        pdf_resp.iter_content.return_value = [_PDF_BYTES]

        session = MagicMock()
        session.get.side_effect = [content_resp, pdf_resp]

        filepath = helper.download_report_pdf_by_info_code(session, "AP001", save_dir=tmpdir)
        assert filepath.exists()
        assert filepath.name == "AP001.pdf"
        assert filepath.read_bytes() == _PDF_BYTES

    def test_custom_filename(self, helper, tmpdir):
        content_resp = MagicMock()
        content_data = {
            "hits": 1,
            "data": [{"infoCode": "AP001", "attachUrl": "http://pdf.dfcfw.com/pdf/1.pdf"}],
        }
        content_resp.text = f"callback({json.dumps(content_data, ensure_ascii=False)})"
        content_resp.raise_for_status = lambda: None

        pdf_resp = MagicMock()
        pdf_resp.iter_content.return_value = [_PDF_BYTES]

        session = MagicMock()
        session.get.side_effect = [content_resp, pdf_resp]

        filepath = helper.download_report_pdf_by_info_code(session, "AP001", save_dir=tmpdir, filename="my_report")
        assert filepath.name == "my_report.pdf"

    def test_falls_back_to_detail_page_when_attach_url_missing(self, helper, tmpdir):
        content_resp = MagicMock()
        content_data = {
            "hits": 1,
            "data": [
                {
                    "infoCode": "AP001",
                    "encodeUrl": "ENCODED001",
                }
            ],
        }
        content_resp.text = f"callback({json.dumps(content_data, ensure_ascii=False)})"
        content_resp.raise_for_status = lambda: None

        detail_resp = MagicMock()
        detail_resp.text = (
            '<html><script>var zwinfo={"info_code":"AP001",'
            '"notice_title":"T","notice_content":"C","notice_date":"",'
            '"source_sample_name":"","researcher":"","short_name":"",'
            '"star":"","attach_url":"http://pdf.dfcfw.com/pdf/H3_AP001_1.pdf?abc"};'
            "</script></html>"
        )
        detail_resp.raise_for_status = lambda: None

        pdf_resp = MagicMock()
        pdf_resp.iter_content.return_value = [_PDF_BYTES]

        session = MagicMock()
        session.get.side_effect = [content_resp, detail_resp, pdf_resp]

        filepath = helper.download_report_pdf_by_info_code(session, "AP001", save_dir=tmpdir)
        assert filepath.exists()
        assert filepath.name == "AP001.pdf"
        assert filepath.read_bytes() == _PDF_BYTES
        assert session.get.call_args_list[1].kwargs["params"]["encodeUrl"] == "ENCODED001"
        assert session.get.call_args_list[2].args[0] == "https://pdf.dfcfw.com/pdf/H3_AP001_1.pdf"

    def test_custom_save_dir(self, helper, tmpdir):
        content_resp = MagicMock()
        content_data = {
            "hits": 1,
            "data": [
                {
                    "infoCode": "AP001",
                    "attachUrl": "http://pdf.dfcfw.com/pdf/1.pdf?token=abc",
                }
            ],
        }
        content_resp.text = f"callback({json.dumps(content_data, ensure_ascii=False)})"
        content_resp.raise_for_status = lambda: None

        pdf_resp = MagicMock()
        pdf_resp.iter_content.return_value = [_PDF_BYTES]

        session = MagicMock()
        session.get.side_effect = [content_resp, pdf_resp]

        subdir = tmpdir / "sub" / "reports"
        filepath = helper.download_report_pdf_by_info_code(session, "AP001", save_dir=subdir)
        assert filepath.parent == subdir.resolve()
        assert filepath.exists()
        assert filepath.read_bytes() == _PDF_BYTES
        assert session.get.call_args_list[1].args[0] == "https://pdf.dfcfw.com/pdf/1.pdf"

    def test_no_pdf_url_raises(self, helper, tmpdir):
        session = MagicMock()
        session.get.side_effect = Exception("network error")

        with pytest.raises(ValueError, match="Could not resolve PDF URL"):
            helper.download_report_pdf_by_info_code(session, "AP001", save_dir=tmpdir)


class TestEastmoneyReportDetailMongoSafety:
    def test_save_to_mongo_rejects_empty_info_code(self):
        detail = EastmoneyReportDetail(
            info_code="",
            title="T",
            publish_date="",
            org_name="",
            researcher="",
            stock_name="",
            stock_code="",
            content="",
        )

        client = MagicMock()

        with pytest.raises(ValueError, match="without info_code"):
            detail.save_to_mongo("reports", client=client)


# ══════════════════════════════════════════════════════════════════════
# 11. Integration tests (real network calls)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestEastmoneyReportDetailIntegration:
    @pytest.fixture
    def helper(self):
        return EastmoneyHelper()

    def test_fetch_report_detail_real(self, helper):
        import requests

        with requests.Session() as sess:
            detail = helper.fetch_report_detail(sess, _ENCODED_URL)

        assert detail.info_code == "AP202607101826864211"
        assert detail.title != ""
        assert detail.org_name != ""
        assert detail.stock_code != ""
        assert detail.content != ""
        assert len(detail.content) > 100
        assert detail.raw_json is not None

    def test_fetch_report_content_api_real(self, helper):
        import requests

        with requests.Session() as sess:
            result = helper.fetch_report_content_by_encoded_url(sess, _ENCODED_URL)

        assert result["infoCode"] == "AP202607101826864211"
        assert result["title"] != ""
        assert "raw_json" in result
        assert result.get("attachUrl") is not None

    def test_fetch_detail_then_to_dict(self, helper):
        import requests

        with requests.Session() as sess:
            detail = helper.fetch_report_detail(sess, _ENCODED_URL)

        d = detail.to_dict()
        assert d["info_code"] == detail.info_code
        assert d["title"] == detail.title
        assert d["content"] == detail.content
        assert d["org_name"] == detail.org_name
