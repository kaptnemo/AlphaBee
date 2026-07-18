"""
Eastmoney Research Reports Ingest (MongoDB)
- Fetch from: https://reportapi.eastmoney.com/report/list (JSONP)
- Save rich fields + raw_json
- Industry fallback: indvIndu* > industry*
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

try:
    from mongoclient import MONGO_DATABASE, mongo_client  # type: ignore[import-untyped]
except ImportError:
    mongo_client = None  # type: ignore[assignment]
    MONGO_DATABASE = None  # type: ignore[assignment]

try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None  # type: ignore[assignment,misc]
from bs4 import BeautifulSoup

EASTMONEY_REPORT_URL = "https://reportapi.eastmoney.com/report/list"
EASTMONEY_REPORT_CONTENT_URL = "https://reportapi.eastmoney.com/report/content"
EASTMONEY_REPORT_DETAIL_URL = "https://data.eastmoney.com/report/zw_stock.jshtml"
EASTMONEY_REPORT_INFO_DETAIL_URL = "https://data.eastmoney.com/report/zw_stock.jshtml"


# ----------------------------
# Helpers
# ----------------------------
def jsonp_to_json(text: str) -> dict[str, Any]:
    """Extract JSON object from JSONP."""
    m = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
    if not m:
        raise ValueError("JSONP parse failed: cannot find '(...)' wrapper")
    return json.loads(m.group(1))


def _none_if_blank(v: Any) -> Any:
    return None if v in ("", None) else v


def safe_float(v: Any) -> float | None:
    v = _none_if_blank(v)
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def safe_int(v: Any) -> int | None:
    v = _none_if_blank(v)
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def to_json_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def split_researcher_list(researcher_raw: Any) -> list[str] | None:
    """
    Split:
      '林子健,任春阳' / '林子健，任春阳' / '林子健 任春阳' / '林子健、任春阳'
    """
    s = _none_if_blank(researcher_raw)
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    parts = re.split(r"[,\，\s、]+", s)
    parts = [p.strip() for p in parts if p.strip()]
    return parts or None


def choose_industry(x: dict[str, Any]) -> tuple[str | None, str | None]:
    """Industry fallback: indvIndu > industry."""
    code = _none_if_blank(x.get("indvInduCode")) or _none_if_blank(x.get("industryCode"))
    name = _none_if_blank(x.get("indvInduName")) or _none_if_blank(x.get("industryName"))
    return (str(code) if code is not None else None, str(name) if name is not None else None)


class EastmoneyReportResult:
    """A class to encapsulate the result of an Eastmoney Research Report query.
    This class holds the data and provides a method to save the result to MongoDB.
    """

    def __init__(self, data: list[dict[str, Any]], page_num: int, page_size: int, has_next: bool):
        self.data = self.process_data(data)
        self.page_num = page_num
        self.page_size = page_size
        self.has_next = has_next

    def process_data(self, data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        保留所有字段，仅对需要转换的字段通过配置统一转换，字段名不变。
        参考upsert_reports的字段和转换逻辑。
        """
        # 配置：字段名 -> 转换函数
        field_transform = {
            # int类型
            "reportType": safe_int,
            "ratingChange": safe_int,
            "attachPages": safe_int,
            "attachSize": safe_int,
            "count": safe_int,
            # float类型
            "indvAimPriceL": safe_float,
            "indvAimPriceT": safe_float,
            "predictThisYearEps": safe_float,
            "predictThisYearPe": safe_float,
            "predictNextYearEps": safe_float,
            "predictNextYearPe": safe_float,
            "predictNextTwoYearEps": safe_float,
            "predictNextTwoYearPe": safe_float,
            "predictLastYearEps": safe_float,
            "predictLastYearPe": safe_float,
            "actualLastYearEps": safe_float,
            "actualLastTwoYearEps": safe_float,
            "newIssuePrice": safe_float,
            "newPeIssueA": safe_float,
            # 研究员列表
            "researcher_list_json": lambda v: to_json_str(split_researcher_list(v)),
            # 作者
            "author_json": to_json_str,
            "author_id_json": to_json_str,
        }
        processed = []
        for x in data:
            try:
                # 先复制所有原始字段
                item = dict(x)
                # 行业兜底
                final_code, final_name = choose_industry(x)
                item["industry_final_code"] = final_code
                item["industry_final_name"] = final_name
                # 研究员原始
                researcher_raw = x.get("researcher")
                item["researcher_raw"] = researcher_raw
                # 统一转换配置字段
                for k, func in field_transform.items():
                    if k == "researcher_list_json":
                        item[k] = func(researcher_raw)
                    elif k == "author_json":
                        item[k] = func(x.get("author"))
                    elif k == "author_id_json":
                        item[k] = func(x.get("authorID"))
                    else:
                        if k in x:
                            item[k] = func(x.get(k))
                # 保留原始json
                item["raw_json"] = json.dumps(x, ensure_ascii=False)
                processed.append(item)
            except Exception as e:
                print(f"Error processing record {x.get('infoCode')}: {e}")
        return processed

    def save_to_mongo(self, collection_name: str, replace: bool = False, client: MongoClient = None):
        """Save the result to MongoDB.
        Args:
            collection_name (str): The name of the MongoDB collection to save the data.
            replace (bool): Whether to replace the existing collection.
            client (MongoClient): The MongoDB client instance. If not provided, a new client will be created.
        """
        if not self.data:
            print("No data to save to MongoDB.")
            return
        if client:
            self._save_to_mongo(collection_name, replace, client)
        elif mongo_client is not None:
            with mongo_client() as client:
                self._save_to_mongo(collection_name, replace, client)
        else:
            raise RuntimeError(
                "mongoclient is not available. Provide a pymongo.MongoClient instance via the 'client' parameter."
            )

    def _save_to_mongo(self, collection_name: str, replace: bool, client: MongoClient):
        """Internal method to save data to MongoDB."""
        if replace:
            client[MONGO_DATABASE].drop_collection(collection_name)
        mongo_collection = client[MONGO_DATABASE][collection_name]
        mongo_collection.insert_many(self.data)
        print(f"Saved {len(self.data)} documents to MongoDB collection '{collection_name}'.")


class EastmoneyReportDetail:
    """Encapsulates the detailed content of a single Eastmoney research report."""

    def __init__(
        self,
        info_code: str,
        title: str,
        publish_date: str,
        org_name: str,
        researcher: str,
        stock_name: str,
        stock_code: str,
        content: str,
        rating: str | None = None,
        attach_url: str | None = None,
        industry_code: str | None = None,
        industry_name: str | None = None,
        raw_json: str | None = None,
    ):
        self.info_code = info_code
        self.title = title
        self.publish_date = publish_date
        self.org_name = org_name
        self.researcher = researcher
        self.stock_name = stock_name
        self.stock_code = stock_code
        self.content = content
        self.rating = rating
        self.attach_url = attach_url
        self.industry_code = industry_code
        self.industry_name = industry_name
        self.raw_json = raw_json

    def to_dict(self) -> dict[str, Any]:
        return {
            "info_code": self.info_code,
            "title": self.title,
            "publish_date": self.publish_date,
            "org_name": self.org_name,
            "researcher": self.researcher,
            "stock_name": self.stock_name,
            "stock_code": self.stock_code,
            "content": self.content,
            "rating": self.rating,
            "attach_url": self.attach_url,
            "industry_code": self.industry_code,
            "industry_name": self.industry_name,
            "raw_json": self.raw_json,
        }

    def save_to_mongo(self, collection_name: str, client: MongoClient | None = None):
        if not self.info_code:
            raise ValueError("Cannot save report detail without info_code")
        doc = self.to_dict()
        if client:
            client[MONGO_DATABASE][collection_name].update_one(
                {"info_code": self.info_code}, {"$set": doc}, upsert=True
            )
            print(f"Saved report detail '{self.info_code}' to MongoDB collection '{collection_name}'.")
        elif mongo_client is not None:
            with mongo_client() as client:
                client[MONGO_DATABASE][collection_name].update_one(
                    {"info_code": self.info_code}, {"$set": doc}, upsert=True
                )
                print(f"Saved report detail '{self.info_code}' to MongoDB collection '{collection_name}'.")
        else:
            raise RuntimeError(
                "mongoclient is not available. Provide a pymongo.MongoClient instance via the 'client' parameter."
            )


class EastmoneyHelper:
    """A helper class for interacting with the Eastmoney Research Report API.
    This class provides methods for querying research reports and saving results to MongoDB.
    """

    def __init__(self):
        pass

    def fetch_report_list(
        self,
        session: requests.Session,
        page_num: int,
        page_size: int,
        start_date: str,
        end_date: str,
        code: str = "*",
        industry_code: str = "*",
        qtype: int = 0,
        timeout: int = 20,
    ) -> EastmoneyReportResult:
        """Fetch research report list from Eastmoney API.
        Args:
            page_num (int): The page number to fetch.
            page_size (int): The number of items per page.
            start_date (str): The start date for the data in the format 'YYYY-MM-DD'.
            end_date (str): The end date for the data in the format 'YYYY-MM-DD'.
            code (str): The stock code to query, use '*' for all.
            industry_code (str): The industry code to query, use '*' for all.
            qtype (int): The query type, 0
                - 0: All
                - 1: Only with research report
                - 2: Only without research report
            timeout (int): The timeout for the API request in seconds.
        Returns:
            EastmoneyReportResult: The result object containing the data and save method.
        """
        params = {
            "cb": "datatable",  # JSONP callback name can be any identifier
            "pageNo": page_num,
            "pageSize": page_size,
            "code": code,
            "industryCode": industry_code,
            "beginTime": start_date,
            "endTime": end_date,
            "qType": qtype,
            "_": int(time.time() * 1000),
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
            "Accept": "*/*",
        }
        r = session.get(EASTMONEY_REPORT_URL, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = jsonp_to_json(r.text)
        total_hits = data.get("hits")
        total_pages = data.get("TotalPage") or data.get("totalPages")
        print(
            f"Fetched page {page_num}/{total_pages} with {len(data.get('data', []))} records (total hits: {total_hits})."
        )
        has_next = page_num < total_pages if total_pages is not None else False
        return EastmoneyReportResult(
            data=jsonp_to_json(r.text).get("data", []), page_num=page_num, page_size=page_size, has_next=has_next
        )

    def fetch_report_content_by_encoded_url(
        self,
        session: requests.Session,
        encoded_url: str,
        timeout: int = 20,
    ) -> dict[str, Any]:
        """Fetch report metadata from the content API using encodeUrl.

        Args:
            session: requests.Session instance.
            encoded_url: The encodeUrl value from the report list API.
            timeout: Request timeout in seconds.

        Returns:
            Dict containing metadata fields: infoCode, title, stockName, stockCode,
            orgName, orgSName, researcher, author, publishDate, attachUrl,
            attachPages, attachSize, indvInduCode, indvInduName, emRatingName,
            relateStock, contentInfoBodyContent, and raw_json.
        """
        params = {
            "cb": "callback",
            "encodeUrl": encoded_url,
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
            "Accept": "*/*",
        }
        r = session.get(EASTMONEY_REPORT_CONTENT_URL, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = jsonp_to_json(r.text)
        records = data.get("data", [])
        if not records:
            raise ValueError(f"No report content found for encodeUrl: {encoded_url}")
        record = records[0]
        record["raw_json"] = json.dumps(record, ensure_ascii=False)
        return record

    def fetch_report_detail(
        self,
        session: requests.Session,
        encoded_url: str,
        timeout: int = 20,
    ) -> EastmoneyReportDetail:
        """Fetch full report detail including text content from the HTML detail page.

        Extracts the zwinfo JavaScript object embedded in the report detail page,
        which contains the full report text (notice_content), title, author,
        stock info, rating, and PDF URL.

        Args:
            session: requests.Session instance.
            encoded_url: The encodeUrl value from the report list API.
            timeout: Request timeout in seconds.

        Returns:
            EastmoneyReportDetail with full text content and metadata.
        """
        params = {"encodeUrl": encoded_url}
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/report/",
            "Accept": "text/html,application/xhtml+xml",
        }
        r = session.get(EASTMONEY_REPORT_DETAIL_URL, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()

        zwinfo = self._extract_zwinfo(r.text)
        content_raw = zwinfo.get("notice_content", "") or ""
        content = content_raw.replace("\\n", "\n").strip()
        security = zwinfo.get("security") or []
        first_security = security[0] if security else {}
        publish_relations = first_security.get("publish_relation") or [{}]
        first_publish_relation = publish_relations[0] if publish_relations else {}

        return EastmoneyReportDetail(
            info_code=zwinfo.get("info_code", ""),
            title=zwinfo.get("notice_title", ""),
            publish_date=zwinfo.get("notice_date", ""),
            org_name=zwinfo.get("source_sample_name", ""),
            researcher=zwinfo.get("researcher", ""),
            stock_name=zwinfo.get("short_name", ""),
            stock_code=first_security.get("stock", ""),
            content=content,
            rating=zwinfo.get("star", ""),
            attach_url=zwinfo.get("attach_url"),
            industry_code=first_publish_relation.get("originalCode"),
            industry_name=first_publish_relation.get("publishName"),
            raw_json=json.dumps(zwinfo, ensure_ascii=False),
        )

    def fetch_report_detail_by_info_code(
        self,
        session: requests.Session,
        info_code: str,
        timeout: int = 20,
    ) -> EastmoneyReportDetail | None:
        """Fetch report detail by infoCode without needing encodeUrl.

        First tries the content API with the infoCode to retrieve metadata,
        then attempts to scrape the affiliate HTML page for full text content.

        Note: The content API may return full text via contentInfoBodyContent
        if available, otherwise falls back to HTML scraping.

        Args:
            session: requests.Session instance.
            info_code: The report infoCode (e.g. 'AP202607101826864211').
            timeout: Request timeout in seconds.

        Returns:
            EastmoneyReportDetail if found, None otherwise.
        """
        detail = self._fetch_detail_page_by_info_code(session, info_code, timeout)
        if detail is not None:
            return detail

        try:
            content_data = self._fetch_content_api_by_info_code(session, info_code, timeout)
        except Exception:
            content_data = None

        if content_data:
            content_text = content_data.get("contentInfoBodyContent", "") or ""
            encode_url = content_data.get("encodeUrl", "")
            if not content_text and encode_url:
                try:
                    detail = self.fetch_report_detail(session, encode_url, timeout)
                    return detail
                except Exception:
                    pass

            return EastmoneyReportDetail(
                info_code=content_data.get("infoCode", info_code),
                title=content_data.get("title", ""),
                publish_date=content_data.get("publishDate", ""),
                org_name=content_data.get("orgName", content_data.get("orgSName", "")),
                researcher=content_data.get("researcher", ""),
                stock_name=content_data.get("stockName", ""),
                stock_code=content_data.get("stockCode", ""),
                content=content_text,
                rating=content_data.get("emRatingName"),
                attach_url=content_data.get("attachUrl"),
                industry_code=content_data.get("indvInduCode"),
                industry_name=content_data.get("indvInduName"),
                raw_json=content_data.get("raw_json"),
            )
        return None

    def fetch_industry_info_by_code(
        self,
        session: requests.Session,
        industry_code: str,
        start_date: str,
        end_date: str,
        page_num: int = 1,
        page_size: int = 100,
        code: str = "*",
        qtype: int = 0,
        timeout: int = 20,
    ) -> dict[str, Any]:
        """Fetch all report info for a given industry code.

        Returns the first page of reports filtered by industry_code together with
        a lightweight industry summary derived from the returned records.
        """
        report_result = self.fetch_report_list(
            session=session,
            page_num=page_num,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date,
            code=code,
            industry_code=industry_code,
            qtype=qtype,
            timeout=timeout,
        )
        first_report = report_result.data[0] if report_result.data else {}
        industry_name = (
            first_report.get("industry_final_name")
            or first_report.get("industryName")
            or first_report.get("indvInduName")
            or ""
        )
        return {
            "industry_code": industry_code,
            "industry_name": industry_name,
            "page_num": report_result.page_num,
            "page_size": report_result.page_size,
            "has_next": report_result.has_next,
            "report_count": len(report_result.data),
            "reports": report_result.data,
        }

    def fetch_report_industry_info_by_info_code(
        self,
        session: requests.Session,
        info_code: str,
        timeout: int = 20,
    ) -> dict[str, Any] | None:
        """Fetch industry info for a report by infoCode."""
        detail = self._fetch_detail_page_by_info_code(session, info_code, timeout)
        if detail and (detail.industry_code or detail.industry_name):
            return {
                "info_code": detail.info_code or info_code,
                "stock_code": detail.stock_code,
                "stock_name": detail.stock_name,
                "industry_code": detail.industry_code,
                "industry_name": detail.industry_name,
                "source": "detail_page",
                "raw_json": detail.raw_json,
            }

        return None

    def _fetch_detail_page_by_info_code(
        self,
        session: requests.Session,
        info_code: str,
        timeout: int = 20,
    ) -> EastmoneyReportDetail | None:
        params = {"infocode": info_code}
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/report/",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            r = session.get(EASTMONEY_REPORT_INFO_DETAIL_URL, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            zwinfo = self._extract_zwinfo(r.text)
            if not zwinfo.get("info_code") and not zwinfo.get("security"):
                return None

            content_raw = zwinfo.get("notice_content", "") or ""
            content = content_raw.replace("\\n", "\n").strip()
            security = zwinfo.get("security") or []
            first_security = security[0] if security else {}
            publish_relations = first_security.get("publish_relation") or [{}]
            first_publish_relation = publish_relations[0] if publish_relations else {}

            return EastmoneyReportDetail(
                info_code=zwinfo.get("info_code", info_code),
                title=zwinfo.get("notice_title", ""),
                publish_date=zwinfo.get("notice_date", ""),
                org_name=zwinfo.get("source_sample_name", ""),
                researcher=zwinfo.get("researcher", ""),
                stock_name=zwinfo.get("short_name", ""),
                stock_code=first_security.get("stock", ""),
                content=content,
                rating=zwinfo.get("star", ""),
                attach_url=zwinfo.get("attach_url"),
                industry_code=first_publish_relation.get("originalCode"),
                industry_name=first_publish_relation.get("publishName"),
                raw_json=json.dumps(zwinfo, ensure_ascii=False),
            )
        except Exception:
            return None

    def _resolve_pdf_url(
        self,
        session: requests.Session,
        encoded_url: str,
        timeout: int = 20,
    ) -> str | None:
        """Resolve the PDF download URL for a report.

        Tries content API first, then falls back to HTML detail page.
        """
        try:
            content_data = self.fetch_report_content_by_encoded_url(session, encoded_url, timeout)
            attach_url = content_data.get("attachUrl")
            if attach_url:
                return self._normalize_pdf_url(attach_url)
        except Exception:
            pass

        try:
            detail = self.fetch_report_detail(session, encoded_url, timeout)
            if detail.attach_url:
                return self._normalize_pdf_url(detail.attach_url)
        except Exception:
            pass

        return None

    def download_report_pdf(
        self,
        session: requests.Session,
        encoded_url: str,
        save_dir: str | Path = ".",
        filename: str | None = None,
        timeout: int = 30,
        chunk_size: int = 8192,
    ) -> Path | None:
        """Download the PDF of a research report.

        Args:
            session: requests.Session instance.
            encoded_url: The encodeUrl value from the report list API.
            save_dir: Directory to save the PDF. Defaults to current directory.
            filename: Custom filename (without extension). If None, auto-generates
                      using infoCode: `{infoCode}.pdf`.
            timeout: Download timeout in seconds.
            chunk_size: Stream chunk size in bytes.

        Returns:
            Path to the downloaded file, or None if download failed.
        """
        pdf_url = self._resolve_pdf_url(session, encoded_url, timeout=timeout // 2)
        if not pdf_url:
            raise ValueError(f"Could not resolve PDF URL for encoded_url: {encoded_url}")

        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            info_code_match = re.search(r"H3_(AP\d+)_", pdf_url)
            if info_code_match:
                filename = info_code_match.group(1)
            else:
                filename = "report"

        filepath = save_dir / f"{filename}.pdf"

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/report/",
        }
        r = session.get(pdf_url, headers=headers, stream=True, timeout=timeout)
        r.raise_for_status()

        total_size = 0
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)

        print(f"Downloaded PDF ({total_size} bytes) to: {filepath}")
        return filepath

    def download_report_pdf_by_info_code(
        self,
        session: requests.Session,
        info_code: str,
        save_dir: str | Path = ".",
        filename: str | None = None,
        timeout: int = 30,
        chunk_size: int = 8192,
    ) -> Path | None:
        """Download the PDF of a research report using infoCode.

        Args:
            session: requests.Session instance.
            info_code: The report infoCode (e.g. 'AP202607101826864211').
            save_dir: Directory to save the PDF.
            filename: Custom filename. Defaults to info_code.
            timeout: Download timeout in seconds.
            chunk_size: Stream chunk size in bytes.

        Returns:
            Path to the downloaded file, or None if download failed.
        """
        if filename is None:
            filename = info_code

        pdf_url = self._resolve_pdf_url_by_info_code(session, info_code, timeout=timeout // 2)
        if not pdf_url:
            raise ValueError(f"Could not resolve PDF URL for info_code: {info_code}")

        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        filepath = save_dir / f"{filename}.pdf"

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/report/",
        }
        r = session.get(pdf_url, headers=headers, stream=True, timeout=timeout)
        r.raise_for_status()

        total_size = 0
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)

        print(f"Downloaded PDF ({total_size} bytes) to: {filepath}")
        return filepath

    def _resolve_pdf_url_by_info_code(
        self,
        session: requests.Session,
        info_code: str,
        timeout: int = 20,
    ) -> str | None:
        """Resolve PDF URL using infoCode via content API."""
        content_data = self._fetch_content_api_by_info_code(session, info_code, timeout)
        if content_data:
            attach_url = content_data.get("attachUrl")
            if attach_url:
                return self._normalize_pdf_url(attach_url)
            encode_url = content_data.get("encodeUrl")
            if encode_url:
                try:
                    detail = self.fetch_report_detail(session, encode_url, timeout)
                    if detail.attach_url:
                        return self._normalize_pdf_url(detail.attach_url)
                except Exception:
                    pass
        return None

    @staticmethod
    def _normalize_pdf_url(url: str) -> str:
        """Ensure PDF URL uses HTTPS and strip unnecessary query params."""
        url = url.strip()
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        qpos = url.find("?")
        if qpos > 0:
            url = url[:qpos]
        return url

    @staticmethod
    def _extract_zwinfo(html: str) -> dict[str, Any]:
        m = re.search(r"var zwinfo\s*=\s*(\{.*?\});", html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        soup = BeautifulSoup(html, "html.parser")
        content_div = soup.find("div", id="ctx-content")
        title_el = soup.find("h1", id="zw-title")
        infos = soup.find("div", class_="c-infos")
        content_parts = []
        if content_div:
            for p in content_div.find_all("p"):
                text = p.get_text(strip=True)
                if text:
                    content_parts.append(text)
        result: dict[str, Any] = {
            "info_code": "",
            "notice_title": title_el.get_text(strip=True) if title_el else "",
            "notice_content": "\n".join(content_parts),
            "notice_date": "",
            "source_sample_name": "",
            "researcher": "",
            "short_name": "",
            "security": [],
            "star": "",
            "attach_url": "",
        }
        if infos:
            spans = infos.find_all("span")
            if len(spans) >= 2:
                result["source_sample_name"] = spans[1].get_text(strip=True)
            if len(spans) >= 3:
                result["researcher"] = spans[2].get_text(strip=True)
        pdf_link = soup.find("a", class_="pdf-link")
        if pdf_link:
            result["attach_url"] = pdf_link.get("href", "")
        return result

    def _fetch_content_api_by_info_code(
        self,
        session: requests.Session,
        info_code: str,
        timeout: int = 20,
    ) -> dict[str, Any] | None:
        params = {
            "cb": "callback",
            "infoCode": info_code,
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
            "Accept": "*/*",
        }
        try:
            r = session.get(EASTMONEY_REPORT_CONTENT_URL, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = jsonp_to_json(r.text)
            records = data.get("data", [])
            if records:
                record = records[0]
                record["raw_json"] = json.dumps(record, ensure_ascii=False)
                return record
        except Exception:
            return None
        return None


if __name__ == "__main__":
    # Example usage
    # end_date = date.today().strftime("%Y-%m-%d")
    # start_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    # page_num = 1
    # page_size = 100
    # replace = True
    # while True:
    #     with requests.Session() as session:
    #         helper = EastmoneyHelper()
    #         result = helper.fetch_report_list(
    #             session=session,
    #             page_num=page_num,
    #             page_size=page_size,
    #             start_date=start_date,
    #             end_date=end_date,
    #             code="*",
    #             industry_code="*",
    #             qtype=0,
    #         )
    #         result.save_to_mongo(collection_name="eastmoney_reports", replace=replace)
    #         if result.has_next:
    #             page_num += 1
    #             replace = False  # Only replace on the first page
    #         else:
    #             break

    # test download pdf
    with requests.Session() as session:
        helper = EastmoneyHelper()
        test_info_code = "AP202607101826864211"  # Replace with a valid infoCode
        try:
            pdf_path = helper.download_report_pdf_by_info_code(
                session=session,
                info_code=test_info_code,
                save_dir="pdfs",
                filename=None,
                timeout=30,
            )
            print(f"PDF downloaded to: {pdf_path}")
        except Exception as e:
            print(f"Failed to download PDF for infoCode {test_info_code}: {e}")

        try:
            pdf_path = helper.download_report_pdf(
                session=session,
                encoded_url="F6IVvWrQaTYYJ+d9R8ZkdjHRxDceAw0egnKFijFOgRE=",  # Replace with a valid encodedUrl
                save_dir="pdfs",
                filename=None,
                timeout=30,
            )
            print(f"PDF downloaded to: {pdf_path}")
        except Exception as e:
            print(f"Failed to download PDF for encodedUrl H3_AP202607101826864211_1: {e}")

        # test fetch report detail by infoCode
        try:
            detail = helper.fetch_report_detail_by_info_code(
                session=session,
                info_code=test_info_code,
                timeout=20,
            )
            if detail:
                print(f"Fetched report detail for infoCode {test_info_code}:")
                print(f"Title: {detail.title}")
                print(f"Publish Date: {detail.publish_date}")
                print(f"Organization: {detail.org_name}")
                print(f"Researcher: {detail.researcher}")
                print(f"Stock Name: {detail.stock_name}")
                print(f"Stock Code: {detail.stock_code}")
                print(f"Rating: {detail.rating}")
                print(f"Attach URL: {detail.attach_url}")
                print(f"Industry Code: {detail.industry_code}")
                print(f"Industry Name: {detail.industry_name}")
            else:
                print(f"No report detail found for infoCode {test_info_code}.")
        except Exception as e:
            print(f"Failed to fetch report detail for infoCode {test_info_code}: {e}")

        # test fetch report industry info by infoCode
        try:
            industry_info = helper.fetch_report_industry_info_by_info_code(
                session=session,
                info_code=test_info_code,
                timeout=20,
            )
            if industry_info:
                print(f"Fetched industry info for infoCode {test_info_code}:")
                print(industry_info)
            else:
                print(f"No industry info found for infoCode {test_info_code}.")
        except Exception as e:
            print(f"Failed to fetch industry info for infoCode {test_info_code}: {e}")
