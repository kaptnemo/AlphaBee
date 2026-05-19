# -*- coding: utf-8 -*-
"""
Eastmoney Research Reports Ingest (MongoDB)
- Fetch from: https://reportapi.eastmoney.com/report/list (JSONP)
- Save rich fields + raw_json
- Industry fallback: indvIndu* > industry*
"""

import re
import json
import time
import requests
import argparse
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
from mongoclient import mongo_client, MONGO_DATABASE
from pymongo import MongoClient
EASTMONEY_REPORT_URL = "https://reportapi.eastmoney.com/report/list"


# ----------------------------
# Helpers
# ----------------------------
def jsonp_to_json(text: str) -> Dict[str, Any]:
    """Extract JSON object from JSONP."""
    m = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
    if not m:
        raise ValueError("JSONP parse failed: cannot find '(...)' wrapper")
    return json.loads(m.group(1))

def _none_if_blank(v: Any) -> Any:
    return None if v in ("", None) else v

def safe_float(v: Any) -> Optional[float]:
    v = _none_if_blank(v)
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def safe_int(v: Any) -> Optional[int]:
    v = _none_if_blank(v)
    try:
        return int(v) if v is not None else None
    except Exception:
        return None

def to_json_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)

def split_researcher_list(researcher_raw: Any) -> Optional[List[str]]:
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

def choose_industry(x: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Industry fallback: indvIndu > industry."""
    code = _none_if_blank(x.get("indvInduCode")) or _none_if_blank(x.get("industryCode"))
    name = _none_if_blank(x.get("indvInduName")) or _none_if_blank(x.get("industryName"))
    return (str(code) if code is not None else None, str(name) if name is not None else None)


class EastmoneyReportResult:
    """A class to encapsulate the result of an Eastmoney Research Report query.
    This class holds the data and provides a method to save the result to MongoDB.
    """
    def __init__(self, data: List[Dict[str, Any]], page_num: int, page_size: int, has_next: bool):
        self.data = self.process_data(data)
        self.page_num = page_num
        self.page_size = page_size
        self.has_next = has_next

    def process_data(self, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
        else:
            with mongo_client() as client:
                self._save_to_mongo(collection_name, replace, client)

    def _save_to_mongo(self, collection_name: str, replace: bool, client: MongoClient):
        """Internal method to save data to MongoDB."""
        if replace:
            client[MONGO_DATABASE].drop_collection(collection_name)
        mongo_collection = client[MONGO_DATABASE][collection_name]
        mongo_collection.insert_many(self.data)
        print(f"Saved {len(self.data)} documents to MongoDB collection '{collection_name}'.")


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
        print(f"Fetched page {page_num}/{total_pages} with {len(data.get('data', []))} records (total hits: {total_hits}).")
        has_next = page_num < total_pages if total_pages is not None else False
        return EastmoneyReportResult(data=jsonp_to_json(r.text).get("data", []), page_num=page_num, page_size=page_size, has_next=has_next)
    
if __name__ == "__main__":
    # Example usage
    end_date = date.today().strftime("%Y-%m-%d")
    start_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    page_num = 1
    page_size = 100
    replace = True
    while True:
        with requests.Session() as session:
            helper = EastmoneyHelper()
            result = helper.fetch_report_list(
                session=session,
                page_num=page_num,
                page_size=page_size,
                start_date=start_date,
                end_date=end_date,
                code="*",
                industry_code="*",
                qtype=0,
            )
            result.save_to_mongo(collection_name="eastmoney_reports", replace=replace)
            if result.has_next:
                page_num += 1
                replace = False  # Only replace on the first page
            else:
                break
