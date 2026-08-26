"""行业成分股财务取数（industry-context Phase 0 垂直切片 + Phase 1 归一化分层）。

best-effort 层：任何一步失败都返回 ([] , 错误信息)，由调用方（resolve_industry_context
节点 / 行业研究工作流）走降级路径，绝不让行业数据获取阻塞分析。

数据源：Tushare（申万行业成分 index_member + 财务指标 fina_indicator）。
**本模块只做取数与列名透传**（TuShareHelper 返回的行已经过 adapter 重命名为 canonical
列名，但数值仍是源单位：百分比）；单位/口径转换统一在 ``alphabee.industry.normalize``
（单一转换点，防重复 ÷100）。外部原始列名只存在于 adapter mapping YAML。

Phase 1 变更：
- 新增 ``fetch_industry_peers``（返回源单位行 + 实际参与推导的成分股代码，
  写入 artifact.peer_universe 保证可复现）；
- ``fetch_peer_financials`` 保持 Phase 0 兼容签名 (records, error)，内部委托前者。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from alphabee.industry.normalize import _TUSHARE_RAW_KEYS, _TUSHARE_VALUATION_KEYS

if TYPE_CHECKING:
    import pandas as pd

    from alphabee.collectors.tushare.helper import TuShareHelper

_PEER_LIMIT = 20  # 成分股抽样上限，控制单次分析 API 调用量
_VALUATION_LOOKBACK_DAYS = 7  # daily_basic 回看窗口（取最新交易日）


def _normalize_ts_code(symbol: str) -> str:
    """把各种股票代码格式转为 Tushare 标准格式（与 agents.facts.tools._utils 同逻辑）。

    内联实现而非 import：避免顶层拉起 ``alphabee.agents.facts`` 包（其 import 链会触发
    tushare token 初始化副作用）；行业子系统保持自包含。
    """
    s = symbol.strip().lower()
    if s.startswith("sh"):
        return s[2:].upper() + ".SH"
    if s.startswith("sz"):
        return s[2:].upper() + ".SZ"
    if s.startswith("bj"):
        return s[2:].upper() + ".BJ"
    upper = symbol.strip().upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return upper
    if upper.startswith(("6", "9")):
        return upper + ".SH"
    if upper.startswith(("0", "3")):
        return upper + ".SZ"
    if upper.startswith(("4", "8")):
        return upper + ".BJ"
    raise ValueError(f"Cannot determine exchange for symbol: {symbol}")


# 与 normalize 的输入键保持一致（adapter 重命名后的列名）
_NUMERIC_INPUT_KEYS = tuple(_TUSHARE_RAW_KEYS.values())


def _has_numeric_field(row: dict[str, object]) -> bool:
    """轻量存在性检查：至少一个数值输入键非 None（完整转换交给 normalize）。"""
    return any(row.get(key) is not None for key in _NUMERIC_INPUT_KEYS)


def _latest_daily_basic(helper: TuShareHelper, ts_code: str) -> dict[str, object]:
    """取个股最新交易日的估值行（pe_ttm / pb_ratio，adapter 重命名后）。"""
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=_VALUATION_LOOKBACK_DAYS)).strftime("%Y%m%d")
    df = helper.daily_basic(
        ts_code=ts_code,
        start_date=start,
        end_date=end,
        fields="ts_code,trade_date,pe_ttm,pb",
    ).data
    if df is None or df.empty:
        return {}
    latest = df.sort_values("trade_date").iloc[-1].to_dict()
    return {key: latest.get(key) for key in _TUSHARE_VALUATION_KEYS.values() if key in latest}


def _con_codes(member_df: pd.DataFrame) -> list[str]:
    """从 index_member 结果中提取成分股代码列表（按入指顺序）。"""
    con_col = next((c for c in ("con_code", "ts_code") if c in member_df.columns), None)
    if con_col is None:
        raise ValueError("index_member 无成分股代码列")
    codes = [str(code) for code in member_df[con_col].dropna().unique() if str(code).strip()]
    if not codes:
        raise ValueError("index_member 成分列表为空")
    return codes


def _fetch_rows_for_codes(helper: TuShareHelper, codes: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """对显式代码列表取最新一期财务（源单位行）+ 估值补全（daily_basic）。

    单只成分取数失败静默跳过（best-effort）；返回 (rows, peer_codes) 一一对应。
    """
    rows: list[dict[str, Any]] = []
    peer_codes: list[str] = []
    for con_code in codes:
        try:
            ts_code = _normalize_ts_code(con_code)
            fina_df = helper.fina_indicator(ts_code=ts_code).data
            if fina_df is None or fina_df.empty:
                continue
            row = fina_df.iloc[0].to_dict()
            if not _has_numeric_field(row):
                continue
            # 估值补全：合并最新交易日 daily_basic 的 pe_ttm / pb_ratio
            # （best-effort，单只失败不影响整体；中位数估值见 benchmarks.derive）
            try:
                row.update(_latest_daily_basic(helper, ts_code))
            except Exception:
                pass
            rows.append(row)
            peer_codes.append(ts_code)
        except Exception:
            continue  # 单只成分取数失败不影响整体
    return rows, peer_codes


def fetch_industry_peers(
    sw_code: str,
    limit: int = _PEER_LIMIT,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """获取行业成分股最新一期财务指标（源单位行）+ 参与推导的成分股代码。

    Args:
        sw_code: 申万行业指数代码，用于 index_member 取成分股。
        limit: 最多取多少只成分股（按成分股列表顺序抽样）。

    Returns:
        (rows, peer_codes, error)：rows 为 fina_indicator 最新一行（adapter 重命名后
        的列名，数值为源单位百分比）；peer_codes 为对应成分股代码（与 rows 一一对应）。
        失败时 rows/peer_codes 为空并返回错误信息。
    """
    if not sw_code:
        return [], [], "sw_code 缺失，无法取行业成分股"

    try:
        from alphabee.collectors.tushare.helper import TuShareHelper
    except Exception as exc:  # tushare 不可用（token/网络）
        return [], [], f"tushare 不可用: {exc}"

    try:
        with TuShareHelper() as helper:
            member_df = helper.index_member(index_code=sw_code).data
            if member_df is None or member_df.empty:
                return [], [], "index_member 返回空成分列表"
            con_codes = _con_codes(member_df)
            rows, peer_codes = _fetch_rows_for_codes(helper, con_codes[:limit])
            if not rows:
                return [], [], "成分股财务指标均取数失败"
            return rows, peer_codes, None
    except Exception as exc:
        return [], [], f"行业成分股取数失败: {exc}"


def fetch_peer_financials_for_codes(
    peer_codes: list[str],
    limit: int = _PEER_LIMIT,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """对显式对标组代码列表取最新一期财务（源单位行）+ 估值补全（COMPANY_TRACK Phase D1）。

    与 ``fetch_industry_peers`` 的取数/归一化链路完全一致，只是代码列表由调用方给定
    （对标组而非申万指数成分）；后续 normalize + derive_benchmarks 复用同一套纯函数。

    Args:
        peer_codes: 对标组代码列表（Tushare 格式，如 ["002415.SZ", "688396.SH", ...]）。
        limit: 最多取多少只（按列表顺序抽样）。

    Returns:
        (rows, fetched_codes, error)：rows 为源单位行；fetched_codes 为实际取数成功的
        代码（与 rows 一一对应）；失败时为空并返回错误信息。
    """
    normalized = [code for code in (peer_codes or []) if str(code).strip()]
    if not normalized:
        return [], [], "对标组代码列表为空"

    try:
        from alphabee.collectors.tushare.helper import TuShareHelper
    except Exception as exc:  # tushare 不可用（token/网络）
        return [], [], f"tushare 不可用: {exc}"

    try:
        with TuShareHelper() as helper:
            rows, fetched = _fetch_rows_for_codes(helper, normalized[:limit])
            if not rows:
                return [], [], "对标组财务指标均取数失败"
            return rows, fetched, None
    except Exception as exc:
        return [], [], f"对标组取数失败: {exc}"


def fetch_peer_financials(
    symbol: str,
    industry: str,
    sw_code: str | None,
    limit: int = _PEER_LIMIT,
) -> tuple[list[dict[str, Any]], str | None]:
    """获取行业成分股最新一期财务指标（源单位行，Phase 0 兼容签名）。

    内部委托 ``fetch_industry_peers``；调用方（resolve_industry_context 节点）需经
    ``alphabee.industry.normalize.normalize_industry_records`` 转换后再推导基准。

    Args:
        symbol: 目标股票代码（仅用于日志/血缘）。
        industry: 行业名（仅用于日志/血缘）。
        sw_code: 申万行业指数代码。
        limit: 最多取多少只成分股。

    Returns:
        (rows, error)：rows 为源单位行；失败时为空列表并返回错误信息。
    """
    del symbol, industry  # 血缘信息，暂不参与取数逻辑
    rows, _, error = fetch_industry_peers(sw_code or "", limit)
    return rows, error
