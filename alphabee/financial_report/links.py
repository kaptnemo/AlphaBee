"""获取公司财报与研报的下载链接（只产出链接，不做实际下载）。

两类报告、两个数据源：

- **财报**（定期报告公告：年报/半年报/一季报/三季报）——数据源为巨潮资讯网
  （CNINFO）官方披露接口，返回 PDF 下载直链（``https://static.cninfo.com.cn/...``）。
- **研报**（券商研究报告）——数据源为东方财富研报中心，返回 PDF 下载直链
  （``https://pdf.dfcfw.com/...``）。

与 :mod:`alphabee.financial_report.pipeline` 的关系：本模块只**获取下载链接**、
不落地文件；拿到链接后，可交给 ``pipeline.download_report_pdf(pdf_url=...)``
下载，或直接 ``requests.get``。

典型用法::

    from alphabee.financial_report.links import (
        get_financial_report_links,
        get_research_report_links,
    )

    # 财报：宁德时代全部定期报告下载链接
    links = get_financial_report_links(code="300750")
    for r in links["reports"]:
        print(r["date"], r["type"], r["title"], r["download_url"])

    # 研报：宁德时代 2026 年以来券商研报下载链接
    links = get_research_report_links("300750", start_date="2026-01-01")
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import requests

from alphabee.collectors.eastmoney.helper import EastmoneyHelper
from alphabee.tools.eastmoney import get_eastmoney_report_list

# ── CNINFO（巨潮资讯网）——财报/定期报告公告 ───────────────────────────────

CNINFO_SUGGEST_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_ANNOUNCE_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_BASE = "https://static.cninfo.com.cn/"

_CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.cninfo.com.cn/",
}

# 定期报告类别（CNINFO 公告分类）→ (category, 中文标签)
_REPORT_TYPE_CATEGORIES: dict[str, tuple[str, str]] = {
    "annual": ("category_ndbg_szsh", "年报"),
    "semiannual": ("category_bndbg_szsh", "半年报"),
    "q1": ("category_yjdbg_szsh", "一季报"),
    "q3": ("category_sjdbg_szsh", "三季报"),
}

# 报告类型别名 → 规范键（Q2 即半年报、Q4 即年报，A 股披露口径下合并）
_REPORT_TYPE_ALIASES: dict[str, str] = {
    "all": "all",
    "annual": "annual",
    "year": "annual",
    "yearly": "annual",
    "q4": "annual",
    "fourth_quarter": "annual",
    "semiannual": "semiannual",
    "half": "semiannual",
    "half_year": "semiannual",
    "q2": "semiannual",
    "second_quarter": "semiannual",
    "q1": "q1",
    "first_quarter": "q1",
    "q3": "q3",
    "third_quarter": "q3",
}


def _normalize_stock_code(code: str) -> str:
    """去掉交易所后缀（``300750.SZ`` → ``300750``），统一为大写 6 位代码。"""
    code = (code or "").strip().upper()
    if "." in code:
        code = code.split(".")[0]
    return code


def _parse_report_types(report_type: str | list[str] | None) -> list[str]:
    """把 ``report_type`` 归一化为规范键列表；``all`` 或空 → 全部四类。"""
    if not report_type:
        return list(_REPORT_TYPE_CATEGORIES)
    raw = [report_type] if isinstance(report_type, str) else list(report_type)
    canonical: list[str] = []
    for item in raw:
        key = _REPORT_TYPE_ALIASES.get(str(item).strip().lower(), "")
        if key == "all" or not key:
            return list(_REPORT_TYPE_CATEGORIES)
        if key not in canonical:
            canonical.append(key)
    return canonical or list(_REPORT_TYPE_CATEGORIES)


def _resolve_company(code: str | None, name: str | None, timeout: int) -> tuple[str, str, str]:
    """通过巨潮 suggest 接口解析 (股票代码, 公司简称, orgId)。"""
    query = _normalize_stock_code(code) if code else (name or "").strip()
    if not query:
        raise ValueError("必须提供 code 或 name 之一。")

    resp = requests.post(
        CNINFO_SUGGEST_URL,
        data={"keyWord": query, "maxNum": "10"},
        headers=_CNINFO_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        raise ValueError(f"未在巨潮资讯网找到证券：{query}")

    # 给了代码时优先精确命中代码，避免拼音/简称联想命中其它证券
    pick = data[0]
    if code:
        code_norm = _normalize_stock_code(code)
        for item in data:
            if str(item.get("code", "")).upper() == code_norm:
                pick = item
                break
    return str(pick.get("code", "")), str(pick.get("zwjc", "")), str(pick.get("orgId", ""))


def _ms_to_date(ms: int) -> str:
    """毫秒时间戳 → ``YYYY-MM-DD``。"""
    try:
        return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _full_pdf_url(adjunct_url: str) -> str:
    """把巨潮返回的相对路径拼成完整 PDF 直链。"""
    if not adjunct_url:
        return ""
    if adjunct_url.startswith(("http://", "https://")):
        return adjunct_url
    return CNINFO_PDF_BASE + adjunct_url.lstrip("/")


def _fetch_cninfo_page(
    *,
    stock: str,
    category: str,
    page_num: int,
    page_size: int,
    se_date: str,
    timeout: int,
) -> dict[str, Any]:
    """调用巨潮 hisAnnouncement 接口，返回单页 JSON。"""
    params = {
        "pageNum": page_num,
        "pageSize": page_size,
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": stock,
        "searchkey": "",
        "secid": "",
        "category": category,
        "trade": "",
        "seDate": se_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    resp = requests.post(CNINFO_ANNOUNCE_URL, data=params, headers=_CNINFO_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_financial_report_links(
    *,
    code: str | None = None,
    name: str | None = None,
    report_type: str | list[str] | None = "all",
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int = 20,
    timeout: int = 20,
) -> dict[str, Any]:
    """获取公司财报（定期报告公告）的 PDF 下载链接。

    数据源为巨潮资讯网官方披露接口，覆盖年报 / 半年报 / 一季报 / 三季报
    （A 股披露口径下二季报=半年报、四季报=年报）。

    Args:
        code: 股票代码，支持 ``"300750"`` 或 ``"300750.SZ"``；与 ``name`` 二选一，
            优先使用 ``code``（精确命中）。
        name: 公司简称（如 ``"宁德时代"``）；未提供 ``code`` 时按简称联想解析。
        report_type: 报告类型，可选 ``"annual"`` / ``"semiannual"`` / ``"q1"`` /
            ``"q3"``（及 ``"q2"``/``"q4"``/``"first_quarter"`` 等别名），也支持
            列表；缺省或 ``"all"`` 表示全部四类。
        start_date / end_date: 披露日期范围（``YYYY-MM-DD``，可选）；缺省时
            start 取 1990-01-01、end 取今天。
        page_size: 每类报告最多返回条数（单类不超过巨潮单页上限）。
        timeout: 单次请求超时（秒）。

    Returns:
        dict: ``source`` / ``code`` / ``name`` / ``count`` / ``reports``；
        ``reports`` 每条为 ``{title, date, type, category, download_url, size_kb}``。
        无匹配证券或接口失败时抛异常（调用方自行降级）。
    """
    sec_code, sec_name, org_id = _resolve_company(code, name, timeout)
    if not org_id:
        raise ValueError(f"未解析到 {sec_code or name} 的 orgId，无法查询巨潮公告。")

    types = _parse_report_types(report_type)
    start = start_date or "1990-01-01"
    end = end_date or dt.date.today().strftime("%Y-%m-%d")
    se_date = f"{start}~{end}"
    stock = f"{sec_code},{org_id}"

    reports: list[dict[str, Any]] = []
    for key in types:
        category, label = _REPORT_TYPE_CATEGORIES[key]
        data = _fetch_cninfo_page(
            stock=stock,
            category=category,
            page_num=1,
            page_size=page_size,
            se_date=se_date,
            timeout=timeout,
        )
        for item in data.get("announcements") or []:
            adjunct_url = item.get("adjunctUrl") or ""
            reports.append(
                {
                    "title": (item.get("announcementTitle") or "").strip(),
                    "date": _ms_to_date(item.get("announcementTime") or 0),
                    "type": label,
                    "category": key,
                    "download_url": _full_pdf_url(adjunct_url),
                    "size_kb": item.get("adjunctSize"),
                }
            )

    reports.sort(key=lambda r: r["date"], reverse=True)
    return {
        "source": "cninfo",
        "code": sec_code,
        "name": sec_name,
        "count": len(reports),
        "reports": reports,
    }


# ── 东方财富研报中心——券商研报 ─────────────────────────────────────────────


def _normalize_eastmoney_pdf_url(url: str) -> str:
    """http→https，去掉多余 query 参数。"""
    url = (url or "").strip()
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    qpos = url.find("?")
    if qpos > 0:
        url = url[:qpos]
    return url


def get_research_report_links(
    code: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int = 20,
    page_num: int = 1,
    timeout: int = 20,
) -> dict[str, Any]:
    """获取公司研报（券商研究报告）的 PDF 下载链接。

    数据源为东方财富研报中心：先按股票代码拉取研报列表，再逐条解析 PDF 直链。

    Args:
        code: 股票代码，支持 ``"300750"`` 或 ``"300750.SZ"``。
        start_date / end_date: 研报发布日期范围（``YYYY-MM-DD``）；缺省时
            start 取一年前、end 取今天。
        page_size: 每页条数（1~100）。
        page_num: 页码（从 1 起）。
        timeout: 单次请求超时（秒）。

    Returns:
        dict: ``source`` / ``code`` / ``count`` / ``has_next`` / ``reports``；
        ``reports`` 每条为 ``{title, date, org, researcher, rating,
        download_url, info_code, encode_url, pages}``。个别研报若解析不到
        PDF 直链，其 ``download_url`` 为 ``None``，但条目仍保留。
    """
    sec_code = _normalize_stock_code(code)
    if not sec_code:
        raise ValueError("code 不能为空。")

    start = start_date or (dt.date.today() - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    end = end_date or dt.date.today().strftime("%Y-%m-%d")

    list_result = get_eastmoney_report_list(
        start_date=start,
        end_date=end,
        page_num=page_num,
        page_size=page_size,
        code=sec_code,
        timeout=timeout,
    )

    helper = EastmoneyHelper()
    reports: list[dict[str, Any]] = []
    with requests.Session() as session:
        for item in list_result.get("reports", []):
            encode_url = item.get("encodeUrl") or ""
            info_code = item.get("infoCode") or ""
            download_url: str | None = None
            if encode_url:
                try:
                    content = helper.fetch_report_content_by_encoded_url(session, encode_url, timeout=timeout)
                    download_url = _normalize_eastmoney_pdf_url(content.get("attachUrl") or "") or None
                except Exception:
                    download_url = None
            reports.append(
                {
                    "title": (item.get("title") or "").strip(),
                    "date": str(item.get("publishDate") or "")[:10],
                    "org": item.get("orgSName") or item.get("orgName") or "",
                    "researcher": item.get("researcher") or "",
                    "rating": item.get("emRatingName") or "",
                    "download_url": download_url,
                    "info_code": info_code,
                    "encode_url": encode_url,
                    "pages": item.get("attachPages"),
                }
            )

    return {
        "source": "eastmoney",
        "code": sec_code,
        "count": len(reports),
        "has_next": bool(list_result.get("has_next")),
        "reports": reports,
    }


if __name__ == "__main__":
    import sys

    demo_code = sys.argv[1] if len(sys.argv) > 1 else "300750"

    print("==== 财报（定期报告）下载链接 ====")
    for r in get_financial_report_links(code=demo_code, report_type="all")["reports"][:10]:
        print(f"[{r['date']}] ({r['type']}) {r['title']} → {r['download_url']}")

    print("\n==== 研报下载链接 ====")
    for r in get_research_report_links(demo_code, page_size=5)["reports"]:
        print(f"[{r['date']}] ({r['org']}) {r['title']} → {r['download_url']}")
