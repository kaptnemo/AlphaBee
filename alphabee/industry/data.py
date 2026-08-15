"""行业成分股财务取数（industry-context-injection Phase 0 垂直切片）。

best-effort 层：任何一步失败都返回 ([] , 错误信息)，由
resolve_industry_context 节点走降级路径，绝不让行业数据获取阻塞个股分析。

数据源：Tushare（申万行业成分 index_member + 财务指标 fina_indicator）。
所有返回字段使用 AlphaBee canonical 命名，外部列名只存在于本模块。
"""

from __future__ import annotations

from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float

_PEER_LIMIT = 20  # 成分股抽样上限，控制单次分析 API 调用量


def _canonical_record(row) -> dict | None:
    """把一行 fina_indicator 记录映射为 canonical 字典（缺字段为 None）。"""
    try:
        return {
            # tushare fina_indicator 列名：tr_yoy 营业收入同比增长率(%)、
            # roe 净资产收益率(%)、debt_to_assets 资产负债率(%)、grossprofit_margin 销售毛利率(%)
            "revenue_yoy": safe_float(row.get("tr_yoy")),
            "roe": safe_float(row.get("roe")),
            "debt_ratio": safe_float(row.get("debt_to_assets")),
            "gross_margin": safe_float(row.get("grossprofit_margin")),
        }
    except Exception:
        return None


def fetch_peer_financials(
    symbol: str,
    industry: str,
    sw_code: str | None,
    limit: int = _PEER_LIMIT,
) -> tuple[list[dict], str | None]:
    """获取行业成分股最新一期财务指标（canonical 键）。

    Args:
        symbol: 目标股票代码（仅用于日志/血缘）。
        industry: 行业名（仅用于日志/血缘）。
        sw_code: 申万行业指数代码，用于 index_member 取成分股。
        limit: 最多取多少只成分股（按成分股列表顺序抽样）。

    Returns:
        (records, error)：records 为 ``[{revenue_yoy, roe, debt_ratio,
        gross_margin}, ...]``；失败时 records 为空列表并返回错误信息。
    """
    del industry  # 血缘信息，暂不参与取数逻辑
    if not sw_code:
        return [], "sw_code 缺失，无法取行业成分股"

    try:
        from alphabee.collectors.tushare.helper import TuShareHelper
    except Exception as exc:  # tushare 不可用（token/网络）
        return [], f"tushare 不可用: {exc}"

    try:
        with TuShareHelper() as helper:
            member_df = helper.index_member(index_code=sw_code).data
            if member_df is None or member_df.empty:
                return [], "index_member 返回空成分列表"

            # 按指数代码筛选（index_member 可能返回多指数），取最近入指的成分
            con_col = next((c for c in ("con_code", "ts_code") if c in member_df.columns), None)
            if con_col is None:
                return [], "index_member 无成分股代码列"
            con_codes = [
                str(code) for code in member_df[con_col].dropna().unique() if str(code).strip()
            ]
            if not con_codes:
                return [], "index_member 成分列表为空"

            records: list[dict] = []
            for con_code in con_codes[:limit]:
                try:
                    fina_df = helper.fina_indicator(ts_code=normalize_ts_code(con_code)).data
                    if fina_df is None or fina_df.empty:
                        continue
                    record = _canonical_record(fina_df.iloc[0])
                    if record is not None:
                        records.append(record)
                except Exception:
                    continue  # 单只成分取数失败不影响整体

            if not records:
                return [], "成分股财务指标均取数失败"
            return records, None
    except Exception as exc:
        return [], f"行业成分股取数失败: {exc}"
