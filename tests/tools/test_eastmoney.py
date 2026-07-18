from types import SimpleNamespace

import alphabee.tools.eastmoney as eastmoney_tools


class _DummySession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_get_eastmoney_report_list(monkeypatch):
    result = SimpleNamespace(
        data=[{"infoCode": "AP001", "encodeUrl": "ENC001"}],
        page_num=1,
        page_size=50,
        has_next=False,
    )

    def fake_fetch_report_list(self, **kwargs):
        return result

    monkeypatch.setattr(eastmoney_tools.requests, "Session", lambda: _DummySession())
    monkeypatch.setattr(
        eastmoney_tools.EastmoneyHelper,
        "fetch_report_list",
        fake_fetch_report_list,
    )

    payload = eastmoney_tools.get_eastmoney_report_list(
        start_date="2026-07-01",
        end_date="2026-07-10",
        industry_code="1033",
    )

    assert payload["report_count"] == 1
    assert payload["reports"][0]["infoCode"] == "AP001"


def test_get_eastmoney_report_detail_by_info_code(monkeypatch):
    detail = SimpleNamespace(to_dict=lambda: {"info_code": "AP001", "industry_code": "1033"})

    def fake_fetch_report_detail_by_info_code(self, session, info_code, timeout=20):
        return detail

    monkeypatch.setattr(eastmoney_tools.requests, "Session", lambda: _DummySession())
    monkeypatch.setattr(
        eastmoney_tools.EastmoneyHelper,
        "fetch_report_detail_by_info_code",
        fake_fetch_report_detail_by_info_code,
    )

    payload = eastmoney_tools.get_eastmoney_report_detail_by_info_code("AP001")
    assert payload["found"] is True
    assert payload["detail"]["info_code"] == "AP001"


def test_download_eastmoney_report_pdf_by_info_code(monkeypatch, tmp_path):
    pdf_path = tmp_path / "AP001.pdf"
    pdf_path.write_bytes(b"pdf-bytes")

    def fake_download(self, session, info_code, save_dir=".", filename=None, timeout=30, chunk_size=8192):
        return pdf_path

    monkeypatch.setattr(eastmoney_tools.requests, "Session", lambda: _DummySession())
    monkeypatch.setattr(
        eastmoney_tools.EastmoneyHelper,
        "download_report_pdf_by_info_code",
        fake_download,
    )

    payload = eastmoney_tools.download_eastmoney_report_pdf_by_info_code("AP001", save_dir=tmp_path)
    assert payload["downloaded"] is True
    assert payload["path"] == str(pdf_path)
    assert payload["size_bytes"] == len(b"pdf-bytes")
