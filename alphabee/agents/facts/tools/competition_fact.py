"""CompetitionFact tool — 同行竞争对手关键指标对比。"""

import datetime
from typing import Any

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache
from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str

_CACHE = SyncTTLCache(ttl_seconds=1800.0)


def get_competition_fact(symbol: str, max_peers: int = 10) -> dict[str, Any]:
    """获取A股公司的竞争格局数据，包括同行业上市公司列表及关键财务和市值指标对比。

    适用场景：
    - 了解公司在行业内的市值规模排名
    - 比较同行业公司的PE、PB估值水平
    - 对比主要竞争对手的盈利能力（ROE、毛利率）
    - 评估公司的行业地位与相对竞争优势
    - 筛选行业内的龙头股和价值洼地

    Args:
        symbol:    股票代码，支持多种格式，如 "600519"、"600519.SH"
        max_peers: 返回竞争对手数量（默认10家，含目标公司自身）

    Returns:
        包含同行对比数据的字典，所有字段使用 AlphaBee 标准命名。
    """
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        today = datetime.date.today().strftime("%Y%m%d")
        lookback = (datetime.date.today() - datetime.timedelta(days=10)).strftime("%Y%m%d")
        one_year_ago = (datetime.date.today() - datetime.timedelta(days=400)).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            basic_df = helper.stock_basic(
                ts_code=ts_code,
                fields="ts_code,name,industry",
            ).data

        if basic_df.empty:
            return {
                "stock_code": ts_code,
                "company_name": "",
                "industry": "",
                "total_peers": 0,
                "peers": [],
            }

        target = basic_df.iloc[0]
        industry = safe_str(target.get("industry"))
        company_name = safe_str(target.get("company_name"))

        with TuShareHelper() as helper:
            peers_df = helper.stock_basic(
                industry=industry,
                fields="ts_code,name,industry,market",
                list_status="L",
            ).data

        if peers_df.empty:
            return {
                "stock_code": ts_code,
                "company_name": company_name,
                "industry": industry,
                "total_peers": 0,
                "peers": [],
            }

        peer_codes = peers_df["stock_code"].tolist()
        peer_codes_str = ",".join(peer_codes[:50])

        with TuShareHelper() as helper:
            daily_basic_df = helper.daily_basic(
                ts_code=peer_codes_str,
                trade_date=today,
                fields="ts_code,pe_ttm,pb,total_mv,circ_mv,turnover_rate",
            ).data

        if daily_basic_df.empty:
            with TuShareHelper() as helper:
                daily_basic_df = helper.daily_basic(
                    ts_code=peer_codes_str,
                    start_date=lookback,
                    end_date=today,
                    fields="ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv,turnover_rate",
                ).data
            if not daily_basic_df.empty:
                daily_basic_df = daily_basic_df.sort_values("trade_date", ascending=False)
                daily_basic_df = daily_basic_df.drop_duplicates(subset=["stock_code"])

        # Fetch ROE & gross margin per peer
        fina_map: dict[str, dict] = {}
        for code in peer_codes[:30]:
            try:
                with TuShareHelper() as helper:
                    f = helper.fina_indicator(
                        ts_code=code,
                        start_date=one_year_ago,
                        fields="ts_code,end_date,roe,grossprofit_margin",
                    ).data
                if not f.empty:
                    row = f.iloc[0]
                    fina_map[safe_str(row.get("stock_code"))] = {
                        "roe": safe_float(row.get("roe")),
                        "gross_margin": safe_float(row.get("gross_margin")),
                    }
            except Exception:
                continue

        name_map = dict(zip(peers_df["stock_code"], peers_df["company_name"]))

        # Build peer records using canonical field names
        peers: list[dict] = []
        if not daily_basic_df.empty:
            daily_basic_df["_mv_sort"] = daily_basic_df["market_cap"].apply(safe_float)
            sorted_df = daily_basic_df.sort_values("_mv_sort", ascending=False)
            for _, row in sorted_df.head(max_peers).iterrows():
                code = safe_str(row.get("stock_code"))
                fina = fina_map.get(code, {})
                peers.append({
                    "stock_code": code,
                    "company_name": name_map.get(code, ""),
                    "market_cap": safe_float(row.get("market_cap")),
                    "pe_ttm": safe_float(row.get("pe_ttm")),
                    "pb_ratio": safe_float(row.get("pb_ratio")),
                    "roe": fina.get("roe", 0.0),
                    "gross_margin": fina.get("gross_margin", 0.0),
                    "has_metrics": True,
                })
        else:
            for _, row in peers_df.head(max_peers).iterrows():
                code = safe_str(row.get("stock_code"))
                peers.append({
                    "stock_code": code,
                    "company_name": safe_str(row.get("company_name")),
                    "market_cap": 0.0,
                    "pe_ttm": 0.0,
                    "pb_ratio": 0.0,
                    "roe": 0.0,
                    "gross_margin": 0.0,
                    "has_metrics": False,
                })

        return {
            "stock_code": ts_code,
            "company_name": company_name,
            "industry": industry,
            "total_peers": len(peer_codes),
            "peers": peers,
        }

    return _CACHE.get_or_compute(("competition_fact", ts_code, max_peers), _compute)


def render(data: dict[str, Any]) -> str:
    """将竞争格局数据渲染为Markdown格式的文本。"""
    stock_code = data.get("stock_code", "")
    company_name = data.get("company_name", stock_code)
    industry = data.get("industry", "")
    total_peers = data.get("total_peers", 0)
    peers = data.get("peers", [])

    if not peers and not industry:
        return f"## {stock_code} 竞争格局\n\n_无法获取公司基本信息_\n"

    lines = [f"## {stock_code}（{company_name}）竞争格局\n", f"**所属行业**: {industry}\n"]

    if not peers:
        lines.append("_暂无同行业可比公司数据_\n")
        return "\n".join(lines)

    has_metrics = peers[0].get("has_metrics", False)

    if has_metrics:
        lines += [
            f"### 同行竞争对手对比（按总市值排序，共{total_peers}家同行）",
            "| 股票代码 | 公司名称 | 总市值(亿元) | PE(TTM) | PB | ROE(%) | 毛利率(%) |",
            "|---------|---------|------------|--------|---|------|---------|",
        ]
        for peer in peers:
            code = peer["stock_code"]
            marker = " ◀ 目标" if code == stock_code else ""
            lines.append(
                f"| {code} | {peer['company_name']}{marker} "
                f"| {peer['market_cap']/10000:,.1f} "
                f"| {peer['pe_ttm']:.1f} "
                f"| {peer['pb_ratio']:.2f} "
                f"| {peer['roe']:.2f} "
                f"| {peer['gross_margin']:.2f} |"
            )
    else:
        lines += [
            f"### 同行公司列表（{total_peers}家，估值数据暂不可用）",
            "| 股票代码 | 公司名称 |",
            "|---------|---------|",
        ]
        for peer in peers:
            code = peer["stock_code"]
            marker = " ◀ 目标" if code == stock_code else ""
            lines.append(f"| {code} | {peer['company_name']}{marker} |")

    lines.append("")
    return "\n".join(lines)

