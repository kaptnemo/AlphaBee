from alphabee.collectors.akshare.helper import AkShareHelper


def get_stock_news_summary(symbol: str) -> str:
    """获取指定A股股票的最新新闻资讯摘要（最近100条标题）。

    当用户询问某只股票最近的新闻动态、市场舆情、利好利空消息、
    公司公告或行业事件等信息时，调用此工具。
    不适用于查询价格行情或财务数据。

    Args:
        symbol: 股票代码（纯数字），例如 "600519"、"000001"、"300750"

    Returns:
        最近新闻的标题与发布时间列表，每行格式为"[发布时间] 新闻标题"，
        若无数据则返回提示字符串。
    """
    with AkShareHelper() as helper:
        result = helper.stock_news_em(symbol=symbol)
        df = result.data

    if df.empty:
        return f"未找到股票 {symbol} 的相关新闻。"

    lines = [
        f"[{row['发布时间']}] {row['新闻标题']}"
        for _, row in df.iterrows()
    ]
    return "\n".join(lines)
