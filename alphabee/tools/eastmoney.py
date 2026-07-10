from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import requests
from pydantic import BaseModel, Field

from alphabee.collectors.eastmoney.helper import EastmoneyHelper

DEFAULT_EASTMONEY_OUTPUT_DIR = Path("outputs/eastmoney_reports")
SKILLS_PATH = Path(__file__).resolve().parents[2] / "skills" /"eastmoney"

class EastmoneyReportListOutput(BaseModel):
    page_num: int = Field(description="页码")
    page_size: int = Field(description="每页条数")
    has_next: bool = Field(description="是否还有下一页")
    report_count: int = Field(description="当前页报告数量")
    reports: list[dict[str, Any]] = Field(description="研报列表")


class EastmoneyReportDetailOutput(BaseModel):
    found: bool = Field(description="是否找到结果")
    detail: dict[str, Any] | None = Field(default=None, description="研报详情")


class EastmoneyIndustryInfoOutput(BaseModel):
    found: bool = Field(description="是否找到结果")
    industry_code: str = Field(description="行业代码")
    industry_name: str = Field(description="行业名称")
    stock_code: str = Field(default="", description="股票代码")
    stock_name: str = Field(default="", description="股票名称")
    source: str = Field(default="", description="来源（detail_page/content_api）")
    raw_json: str | None = Field(default=None, description="原始 JSON")


class EastmoneyIndustryReportsOutput(BaseModel):
    industry_code: str = Field(description="行业代码")
    industry_name: str = Field(description="行业名称")
    page_num: int = Field(description="页码")
    page_size: int = Field(description="每页条数")
    has_next: bool = Field(description="是否还有下一页")
    report_count: int = Field(description="当前页报告数量")
    reports: list[dict[str, Any]] = Field(description="行业研报列表")


class EastmoneyDownloadOutput(BaseModel):
    downloaded: bool = Field(description="是否下载成功")
    path: str | None = Field(default=None, description="保存路径")
    size_bytes: int = Field(default=0, description="文件大小")


def _normalize_save_dir(save_dir: str | Path | None) -> Path:
    path = Path(save_dir) if save_dir is not None else DEFAULT_EASTMONEY_OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _to_detail_payload(detail) -> dict[str, Any]:
    return detail.to_dict() if detail is not None else {}


def _to_report_list_payload(result) -> EastmoneyReportListOutput:
    return EastmoneyReportListOutput(
        page_num=result.page_num,
        page_size=result.page_size,
        has_next=result.has_next,
        report_count=len(result.data),
        reports=result.data,
    )


def _helper() -> EastmoneyHelper:
    return EastmoneyHelper()


def get_eastmoney_report_list(
    start_date: str,
    end_date: str,
    page_num: int = 1,
    page_size: int = 100,
    code: str = "*",
    industry_code: str = "*",
    qtype: int = 0,
    timeout: int = 20,
) -> dict[str, Any]:
    """获取研报列表。

    适合先拿到一页报告，再从结果里取 `infoCode` / `encodeUrl` 继续查详情、
    行业信息或 PDF。
    """
    helper = _helper()
    with requests.Session() as session:
        result = helper.fetch_report_list(
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
    return _to_report_list_payload(result).model_dump()


def get_eastmoney_report_detail_by_encoded_url(
    encoded_url: str,
    timeout: int = 20,
) -> dict[str, Any]:
    """通过 encodeUrl 获取研报详情。"""
    helper = _helper()
    with requests.Session() as session:
        detail = helper.fetch_report_detail(session, encoded_url, timeout=timeout)
    return {"found": detail is not None, "detail": _to_detail_payload(detail)}


def get_eastmoney_report_detail_by_info_code(
    info_code: str,
    timeout: int = 20,
) -> dict[str, Any]:
    """通过 infoCode 获取研报详情。"""
    helper = _helper()
    with requests.Session() as session:
        detail = helper.fetch_report_detail_by_info_code(session, info_code, timeout=timeout)
    return {"found": detail is not None, "detail": _to_detail_payload(detail)}


def get_eastmoney_report_industry_info_by_info_code(
    info_code: str,
    timeout: int = 20,
) -> dict[str, Any]:
    """通过 infoCode 获取研报里的行业信息。"""
    helper = _helper()
    with requests.Session() as session:
        detail = helper.fetch_report_industry_info_by_info_code(session, info_code, timeout=timeout)
    if detail is None:
        return {"found": False}
    return EastmoneyIndustryInfoOutput(found=True, **detail).model_dump()


def get_eastmoney_industry_reports(
    industry_code: str,
    start_date: str,
    end_date: str,
    page_num: int = 1,
    page_size: int = 100,
    code: str = "*",
    qtype: int = 0,
    timeout: int = 20,
) -> dict[str, Any]:
    """通过行业代码获取该行业的研报列表。"""
    helper = _helper()
    with requests.Session() as session:
        result = helper.fetch_industry_info_by_code(
            session=session,
            industry_code=industry_code,
            start_date=start_date,
            end_date=end_date,
            page_num=page_num,
            page_size=page_size,
            code=code,
            qtype=qtype,
            timeout=timeout,
        )
    return EastmoneyIndustryReportsOutput(**result).model_dump()


def download_eastmoney_report_pdf(
    encoded_url: str,
    save_dir: str | Path | None = None,
    filename: Optional[str] = None,
    timeout: int = 30,
    chunk_size: int = 8192,
) -> dict[str, Any]:
    """通过 encodeUrl 下载研报 PDF。"""
    helper = _helper()
    target_dir = _normalize_save_dir(save_dir)
    with requests.Session() as session:
        path = helper.download_report_pdf(
            session=session,
            encoded_url=encoded_url,
            save_dir=target_dir,
            filename=filename,
            timeout=timeout,
            chunk_size=chunk_size,
        )
    return EastmoneyDownloadOutput(
        downloaded=path is not None,
        path=str(path) if path is not None else None,
        size_bytes=path.stat().st_size if path is not None and path.exists() else 0,
    ).model_dump()


def download_eastmoney_report_pdf_by_info_code(
    info_code: str,
    save_dir: str | Path | None = None,
    filename: Optional[str] = None,
    timeout: int = 30,
    chunk_size: int = 8192,
) -> dict[str, Any]:
    """通过 infoCode 下载研报 PDF。"""
    helper = _helper()
    target_dir = _normalize_save_dir(save_dir)
    with requests.Session() as session:
        path = helper.download_report_pdf_by_info_code(
            session=session,
            info_code=info_code,
            save_dir=target_dir,
            filename=filename,
            timeout=timeout,
            chunk_size=chunk_size,
        )
    return EastmoneyDownloadOutput(
        downloaded=path is not None,
        path=str(path) if path is not None else None,
        size_bytes=path.stat().st_size if path is not None and path.exists() else 0,
    ).model_dump()
