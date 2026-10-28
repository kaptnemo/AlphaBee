"""Eastmoney consensus fetcher — provider 链（研报列表聚合 / F10 盈利预测）。

直接消费 :func:`alphabee.tools.eastmoney.get_eastmoney_report_list`（全量 58 字段），
**不要**依赖 :func:`alphabee.financial_report.links.get_research_report_links`
（它只服务 PDF 下载链接，会裁剪 EPS/目标价字段）。

provider 链（E 生态设计 §5.3）：

- ``report_list``（**缺省**）⇒ 东财研报列表聚合，逐字保持现状
  （``source="eastmoney:report/list"``）；
- ``f10_forecast`` ⇒ 东财 F10 盈利预测快照 + 快照序列自算 revision
  （``source="eastmoney:f10/profit_forecast"``），不可用 ⇒ 回落 ``report_list`` 并在
  ``ConsensusOutput.warnings`` 留痕；
- ``auto`` ⇒ 优先新源，不可用回落 ``report_list``（同样留痕）；
- 非法值 / 配置不可用 ⇒ logger warn + ``report_list``（**绝不抛**）。

纪律与 :func:`alphabee.midterm.classifier_dispatch.resolve_classifier_order` 同构：
配置在**调用时**读取（不 import 期读），非法值 warn + 回落，绝不打断调用方。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from alphabee.collectors.consensus.engine import ConsensusOutput, aggregate_consensus
from alphabee.tools.eastmoney import get_eastmoney_report_list

logger = logging.getLogger(__name__)

CONSENSUS_SOURCE_REPORT_LIST = "report_list"
CONSENSUS_SOURCE_F10 = "f10_forecast"
CONSENSUS_SOURCE_AUTO = "auto"
KNOWN_CONSENSUS_SOURCES = (
    CONSENSUS_SOURCE_REPORT_LIST,
    CONSENSUS_SOURCE_F10,
    CONSENSUS_SOURCE_AUTO,
)
#: 新源不可用时统一留痕文本（warnings 审计面）
_F10_FALLBACK_WARNING = (
    "consensus provider fallback: f10_forecast → report_list（f10 快照不可用：网络失败 / 非法代码 / 无一致预期覆盖）"
)


def _normalize_source(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def configured_consensus_source() -> str | None:
    """读 ``settings.midterm.consensus_source``（调用时读取；缺段/异常 → ``None``，绝不抛）。"""
    try:
        from alphabee.config import get_settings

        configured = getattr(getattr(get_settings(), "midterm", None), "consensus_source", None)
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断共识采集
        logger.warning("midterm.consensus_source 配置不可用（%s），按 report_list 处理", exc)
        return None
    return _normalize_source(configured)


def resolve_consensus_source(consensus_source: str | None = None) -> tuple[str, str]:
    """解析 consensus provider，返回 ``(source, warning)``（fail-open，绝不抛）。

    - 显式合法值优先；``None`` → 读 ``settings.midterm.consensus_source``；
    - 显式非法值 → warn + ``report_list``，并返回 warning（写入 ``ConsensusOutput.warnings``）；
    - 配置值非法 → 同上；配置段缺失 / 读配置异常 → ``report_list``（无额外 warning）。
    """
    raw = _normalize_source(consensus_source) if consensus_source is not None else configured_consensus_source()
    if raw in KNOWN_CONSENSUS_SOURCES:
        return str(raw), ""
    if raw is None:
        return CONSENSUS_SOURCE_REPORT_LIST, ""
    warning = f"midterm.consensus_source 非法值 {raw!r}，回落 report_list"
    logger.warning(warning)
    return CONSENSUS_SOURCE_REPORT_LIST, warning


def _normalize_code(code: str) -> str:
    """``300750.SZ`` / ``sz300750`` → ``300750``（6 位，去交易所前后缀）。"""
    code = (code or "").strip().lower()
    if code.startswith(("sz", "sh", "bj")):
        code = code[2:]
    if "." in code:
        code = code.split(".")[0]
    return code


def fetch_report_records(
    code: str,
    *,
    months: int = 12,
    page_size: int = 100,
    timeout: int = 20,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """拉取个股近 ``months`` 个月全量研报列表（自动翻页），返回原始记录列表。"""
    sec_code = _normalize_code(code)
    if not sec_code:
        raise ValueError("code 不能为空。")

    end = date.today()
    start = end - timedelta(days=round(months * 30.44))

    records: list[dict[str, Any]] = []
    page_num = 1
    while page_num <= max_pages:
        result = get_eastmoney_report_list(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            page_num=page_num,
            page_size=page_size,
            code=sec_code,
            timeout=timeout,
        )
        reports = result.get("reports") or []
        records.extend(reports)
        if not reports or not result.get("has_next"):
            break
        page_num += 1
    return records


def _report_list_consensus(
    code: str,
    *,
    months: int,
    page_size: int,
    timeout: int,
    max_pages: int,
    as_of_date: str | None,
    target_price_coverage_threshold: float,
) -> ConsensusOutput:
    """现状研报聚合路径（逐字不变，供 provider 链与回落共用）。"""
    records = fetch_report_records(
        code,
        months=months,
        page_size=page_size,
        timeout=timeout,
        max_pages=max_pages,
    )
    return aggregate_consensus(
        records,
        as_of_date=as_of_date,
        target_price_coverage_threshold=target_price_coverage_threshold,
    )


def _f10_consensus(
    code: str,
    *,
    timeout: int,
    as_of_date: str | None,
    snapshot_dir: str | None = None,
) -> ConsensusOutput | None:
    """F10 快照路径；不可用（拉取失败 / 无覆盖）→ ``None``（由调用方回落并留痕）。"""
    from alphabee.collectors.consensus import emforecast

    return emforecast.build_f10_consensus(
        code,
        timeout=timeout,
        data_dir=snapshot_dir,
        as_of_date=as_of_date,
    )


def build_consensus(
    code: str,
    *,
    months: int = 12,
    page_size: int = 100,
    timeout: int = 20,
    max_pages: int = 20,
    as_of_date: str | None = None,
    target_price_coverage_threshold: float = 0.3,
    consensus_source: str | None = None,
    snapshot_dir: str | None = None,
) -> ConsensusOutput:
    """按 provider 链聚合个股 consensus canonical 字段（:class:`ConsensusOutput`）。

    Args:
        code: 股票代码（``600519.SH`` / ``sz300750`` / ``600519`` 均可）。
        months / page_size / max_pages / timeout: 研报列表路径的取数参数（同现状）。
        as_of_date: 聚合基准日（``YYYY-MM-DD``）；缺省按各 provider 自己的口径
            （report_list ⇒ 今天；f10 ⇒ 快照自带的数据刷新日）。
        target_price_coverage_threshold: 目标价覆盖率阈值（report_list 路径）。
        consensus_source: provider 显式指定；``None`` ⇒ 读
            ``settings.midterm.consensus_source``（缺省 ``report_list``，与现状逐字一致）。
        snapshot_dir: 快照落盘目录（运维/测试逃生口）；``None`` ⇒
            ``<data_root>/consensus_snapshots``。

    默认 ``report_list`` 下行为与改造前**逐字一致**（``source="eastmoney:report/list"``、
    无附加 warnings）。
    """
    resolved, source_warning = resolve_consensus_source(consensus_source)

    if resolved in (CONSENSUS_SOURCE_F10, CONSENSUS_SOURCE_AUTO):
        fresh = _f10_consensus(code, timeout=timeout, as_of_date=as_of_date, snapshot_dir=snapshot_dir)
        if fresh is not None:
            return fresh
        out = _report_list_consensus(
            code,
            months=months,
            page_size=page_size,
            timeout=timeout,
            max_pages=max_pages,
            as_of_date=as_of_date,
            target_price_coverage_threshold=target_price_coverage_threshold,
        )
        out.warnings.append(_F10_FALLBACK_WARNING)
        return out

    out = _report_list_consensus(
        code,
        months=months,
        page_size=page_size,
        timeout=timeout,
        max_pages=max_pages,
        as_of_date=as_of_date,
        target_price_coverage_threshold=target_price_coverage_threshold,
    )
    if source_warning:
        out.warnings.append(source_warning)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("code", help="股票代码（600519.SH / sz300750 / 600519）")
    parser.add_argument("--months", type=int, default=12, help="拉取研报的月份跨度")
    parser.add_argument("--page-size", type=int, default=100, help="研报列表每页条数")
    parser.add_argument("--max-pages", type=int, default=20, help="研报列表最大翻页数")
    parser.add_argument("--timeout", type=int, default=20, help="网络请求超时秒数")
    parser.add_argument("--as-of-date", type=str, default=None, help="聚合基准日（YYYY-MM-DD）")
    parser.add_argument(
        "--target-price-coverage-threshold",
        type=float,
        default=0.3,
        help="目标价覆盖率阈值（report_list 路径）",
    )
    parser.add_argument(
        "--consensus-source",
        type=str,
        default=None,
        help="provider 显式指定（report_list / f10_forecast / auto），None 读配置",
    )
    args = parser.parse_args()

    consensus = build_consensus(
        args.code,
        months=args.months,
        page_size=args.page_size,
        max_pages=args.max_pages,
        timeout=args.timeout,
        as_of_date=args.as_of_date,
        target_price_coverage_threshold=args.target_price_coverage_threshold,
        consensus_source=args.consensus_source,
    )
    print(consensus)
