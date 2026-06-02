"""RiskFact tool — 风险事件、股权质押、回购动态与负面新闻。"""

import datetime
from typing import Any

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.collectors.akshare.helper import AkShareHelper
from alphabee.tools.cache import SyncTTLCache
from alphabee.agents.facts.tools._utils import normalize_ts_code, to_pure_code, safe_float, safe_str

_CACHE = SyncTTLCache(ttl_seconds=300.0)


def get_risk_fact(symbol: str) -> dict[str, Any]:
    """获取A股公司的风险事实数据，包括最新新闻资讯、股权质押情况、股票回购记录和重大违规/处罚公告。

    适用场景：
    - 了解公司最新负面新闻、舆情风险
    - 查看大股东股权质押比例（高质押率意味着爆仓风险）
    - 跟踪公司股票回购计划执行情况（正面信号）
    - 排查监管处罚、违规记录等合规风险
    - 在投资前进行风险尽职调查

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        包含风险事实数据的字典，所有字段使用 AlphaBee 标准命名。
    """
    ts_code = normalize_ts_code(symbol)
    pure_code = to_pure_code(ts_code)

    def _compute() -> dict[str, Any]:
        lookback_365 = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y%m%d")

        result: dict[str, Any] = {"stock_code": ts_code}

        # 1. 最新新闻（AkShare）→ 规范为 news_title / news_publish_time
        try:
            with AkShareHelper() as helper:
                news_df = helper.stock_news_em(symbol=pure_code).data
            news = []
            if not news_df.empty:
                for _, row in news_df.head(20).iterrows():
                    news.append({
                        "news_publish_time": safe_str(row.get("发布时间", row.get("publish_time", ""))),
                        "news_title": safe_str(row.get("新闻标题", row.get("title", ""))),
                    })
            result["news"] = news
            result["news_error"] = None
        except Exception as e:
            result["news"] = []
            result["news_error"] = str(e)

        # 2. 股权质押
        try:
            with TuShareHelper() as helper:
                pledge_df = helper.pledge_stat(
                    ts_code=ts_code,
                    fields="ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,"
                           "total_share,pledge_ratio",
                ).data
            result["pledge"] = pledge_df.head(5).to_dict(orient="records") if not pledge_df.empty else []
            result["pledge_error"] = None
        except Exception as e:
            result["pledge"] = []
            result["pledge_error"] = str(e)

        # 3. 股票回购
        try:
            with TuShareHelper() as helper:
                repurchase_df = helper.repurchase(
                    ts_code=ts_code,
                    start_date=lookback_365,
                    fields="ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,"
                           "low_limit",
                ).data
            result["repurchase"] = repurchase_df.head(5).to_dict(orient="records") if not repurchase_df.empty else []
            result["repurchase_error"] = None
        except Exception as e:
            result["repurchase"] = []
            result["repurchase_error"] = str(e)

        # 4. 高管薪酬与激励
        try:
            with TuShareHelper() as helper:
                stk_rewards_df = helper.stk_rewards(
                    ts_code=ts_code,
                    fields="ts_code,ann_date,name,title,reward,ex_date",
                ).data
            result["stk_rewards"] = stk_rewards_df.head(5).to_dict(orient="records") if not stk_rewards_df.empty else []
        except Exception:
            result["stk_rewards"] = []

        return result

    return _CACHE.get_or_compute(("risk_fact", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将风险事实数据渲染为Markdown格式的文本。"""
    stock_code = data.get("stock_code", "")
    news = data.get("news", [])
    news_error = data.get("news_error")
    pledge = data.get("pledge", [])
    pledge_error = data.get("pledge_error")
    repurchase = data.get("repurchase", [])
    repurchase_error = data.get("repurchase_error")
    stk_rewards = data.get("stk_rewards", [])

    lines = [f"## {stock_code} 风险事实数据\n"]

    # 1. 新闻
    if news_error:
        lines.append(f"_新闻数据获取失败：{news_error}_\n")
    elif news:
        lines += [
            "### 最新新闻资讯（近100条）",
            "| 发布时间 | 新闻标题 |",
            "|---------|---------|",
        ]
        for row in news:
            lines.append(f"| {safe_str(row.get('news_publish_time'))} | {safe_str(row.get('news_title'))} |")
        lines.append("")
    else:
        lines.append("_暂无新闻数据_\n")

    # 2. 股权质押
    if pledge_error:
        lines.append(f"_股权质押数据获取失败：{pledge_error}_\n")
    elif pledge:
        latest_pledge_ratio = safe_float(pledge[0].get("pledge_ratio"))
        lines += [
            "### 股权质押情况",
            "| 统计日期 | 质押笔数 | 质押比例(%) | 已解押(万股) | 未解押(万股) |",
            "|---------|---------|-----------|-----------|-----------|",
        ]
        for row in pledge:
            lines.append(
                f"| {safe_str(row.get('period'))} "
                f"| {safe_float(row.get('pledge_count')):.0f} "
                f"| {safe_float(row.get('pledge_ratio')):.2f} "
                f"| {safe_float(row.get('released_pledge'))/10000:.2f} "
                f"| {safe_float(row.get('unreleased_pledge'))/10000:.2f} |"
            )
        if latest_pledge_ratio > 30:
            lines.append(
                f"\n> ⚠️ **高质押风险**：当前质押比例 {latest_pledge_ratio:.2f}%，超过30%警戒线，需关注强平风险。"
            )
        lines.append("")
    else:
        lines.append("_无股权质押记录或数据暂不可用_\n")

    # 3. 股票回购
    if repurchase_error:
        lines.append(f"_回购数据获取失败：{repurchase_error}_\n")
    elif repurchase:
        lines += [
            "### 股票回购记录（近一年）",
            "| 公告日 | 截止日期 | 进度 | 回购数量(万股) | 回购金额(万元) |",
            "|--------|---------|-----|-------------|-------------|",
        ]
        for row in repurchase:
            lines.append(
                f"| {safe_str(row.get('ann_date'))} "
                f"| {safe_str(row.get('period'))} "
                f"| {safe_str(row.get('repurchase_progress'))} "
                f"| {safe_float(row.get('repurchase_volume'))/10000:.2f} "
                f"| {safe_float(row.get('repurchase_amount'))/10000:.2f} |"
            )
        lines.append("")
    else:
        lines.append("_近一年无股票回购记录_\n")

    # 4. 高管薪酬
    if stk_rewards:
        lines += [
            "### 高管薪酬与激励（近期公告）",
            "| 公告日 | 姓名 | 职务 | 薪酬(万元) |",
            "|--------|-----|-----|----------|",
        ]
        for row in stk_rewards:
            lines.append(
                f"| {safe_str(row.get('ann_date'))} "
                f"| {safe_str(row.get('executive_name'))} "
                f"| {safe_str(row.get('executive_title'))} "
                f"| {safe_float(row.get('executive_reward')):.2f} |"
            )
        lines.append("")

    return "\n".join(lines)


def get_risk_fact(symbol: str) -> dict[str, Any]:
    """获取A股公司的风险事实数据，包括最新新闻资讯、股权质押情况、股票回购记录和重大违规/处罚公告。

    适用场景：
    - 了解公司最新负面新闻、舆情风险
    - 查看大股东股权质押比例（高质押率意味着爆仓风险）
    - 跟踪公司股票回购计划执行情况（正面信号）
    - 排查监管处罚、违规记录等合规风险
    - 在投资前进行风险尽职调查

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        包含风险事实数据的字典，含近期新闻、股权质押、回购动态等。
    """
    ts_code = normalize_ts_code(symbol)
    pure_code = to_pure_code(ts_code)

    def _compute() -> dict[str, Any]:
        lookback_365 = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y%m%d")

        result: dict[str, Any] = {"ts_code": ts_code}

        # 1. 最新新闻（AkShare）
        try:
            with AkShareHelper() as helper:
                news_df = helper.stock_news_em(symbol=pure_code).data
            result["news"] = news_df.head(20).to_dict(orient="records") if not news_df.empty else []
            result["news_error"] = None
        except Exception as e:
            result["news"] = []
            result["news_error"] = str(e)

        # 2. 股权质押
        try:
            with TuShareHelper() as helper:
                pledge_df = helper.pledge_stat(
                    ts_code=ts_code,
                    fields="ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,"
                           "total_share,pledge_ratio",
                ).data
            result["pledge"] = pledge_df.head(5).to_dict(orient="records") if not pledge_df.empty else []
            result["pledge_error"] = None
        except Exception as e:
            result["pledge"] = []
            result["pledge_error"] = str(e)

        # 3. 股票回购
        try:
            with TuShareHelper() as helper:
                repurchase_df = helper.repurchase(
                    ts_code=ts_code,
                    start_date=lookback_365,
                    fields="ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,"
                           "low_limit",
                ).data
            result["repurchase"] = repurchase_df.head(5).to_dict(orient="records") if not repurchase_df.empty else []
            result["repurchase_error"] = None
        except Exception as e:
            result["repurchase"] = []
            result["repurchase_error"] = str(e)

        # 4. 高管薪酬与激励
        try:
            with TuShareHelper() as helper:
                stk_rewards_df = helper.stk_rewards(
                    ts_code=ts_code,
                    fields="ts_code,ann_date,name,title,reward,ex_date",
                ).data
            result["stk_rewards"] = stk_rewards_df.head(5).to_dict(orient="records") if not stk_rewards_df.empty else []
        except Exception:
            result["stk_rewards"] = []

        return result

    return _CACHE.get_or_compute(("risk_fact", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将风险事实数据渲染为Markdown格式的文本。"""
    ts_code = data.get("ts_code", "")
    news = data.get("news", [])
    news_error = data.get("news_error")
    pledge = data.get("pledge", [])
    pledge_error = data.get("pledge_error")
    repurchase = data.get("repurchase", [])
    repurchase_error = data.get("repurchase_error")
    stk_rewards = data.get("stk_rewards", [])

    lines = [f"## {ts_code} 风险事实数据\n"]

    # 1. 新闻
    if news_error:
        lines.append(f"_新闻数据获取失败：{news_error}_\n")
    elif news:
        lines += [
            "### 最新新闻资讯（近100条）",
            "| 发布时间 | 新闻标题 |",
            "|---------|---------|",
        ]
        for row in news:
            pub_time = safe_str(row.get("发布时间", row.get("publish_time", "")))
            title = safe_str(row.get("新闻标题", row.get("title", "")))
            lines.append(f"| {pub_time} | {title} |")
        lines.append("")
    else:
        lines.append("_暂无新闻数据_\n")

    # 2. 股权质押
    if pledge_error:
        lines.append(f"_股权质押数据获取失败：{pledge_error}_\n")
    elif pledge:
        latest_pledge_ratio = safe_float(pledge[0].get("pledge_ratio"))
        lines += [
            "### 股权质押情况",
            "| 统计日期 | 质押笔数 | 质押比例(%) | 已解押(万股) | 未解押(万股) |",
            "|---------|---------|-----------|-----------|-----------|",
        ]
        for row in pledge:
            lines.append(
                f"| {safe_str(row.get('end_date'))} "
                f"| {safe_float(row.get('pledge_count')):.0f} "
                f"| {safe_float(row.get('pledge_ratio')):.2f} "
                f"| {safe_float(row.get('rest_pledge'))/10000:.2f} "
                f"| {safe_float(row.get('unrest_pledge'))/10000:.2f} |"
            )
        if latest_pledge_ratio > 30:
            lines.append(
                f"\n> ⚠️ **高质押风险**：当前质押比例 {latest_pledge_ratio:.2f}%，超过30%警戒线，需关注强平风险。"
            )
        lines.append("")
    else:
        lines.append("_无股权质押记录或数据暂不可用_\n")

    # 3. 股票回购
    if repurchase_error:
        lines.append(f"_回购数据获取失败：{repurchase_error}_\n")
    elif repurchase:
        lines += [
            "### 股票回购记录（近一年）",
            "| 公告日 | 截止日期 | 进度 | 回购数量(万股) | 回购金额(万元) |",
            "|--------|---------|-----|-------------|-------------|",
        ]
        for row in repurchase:
            lines.append(
                f"| {safe_str(row.get('ann_date'))} "
                f"| {safe_str(row.get('end_date'))} "
                f"| {safe_str(row.get('proc'))} "
                f"| {safe_float(row.get('vol'))/10000:.2f} "
                f"| {safe_float(row.get('amount'))/10000:.2f} |"
            )
        lines.append("")
    else:
        lines.append("_近一年无股票回购记录_\n")

    # 4. 高管薪酬
    if stk_rewards:
        lines += [
            "### 高管薪酬与激励（近期公告）",
            "| 公告日 | 姓名 | 职务 | 薪酬(万元) |",
            "|--------|-----|-----|----------|",
        ]
        for row in stk_rewards:
            lines.append(
                f"| {safe_str(row.get('ann_date'))} "
                f"| {safe_str(row.get('name'))} "
                f"| {safe_str(row.get('title'))} "
                f"| {safe_float(row.get('reward')):.2f} |"
            )
        lines.append("")

    return "\n".join(lines)
