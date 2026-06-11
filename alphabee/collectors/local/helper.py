from pathlib import Path
import pandas as pd

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"



def get_all_stocks():
    file_path = STATIC_DIR / "all_stocks.csv"
    return pd.read_csv(file_path)

ALL_STOCKS = get_all_stocks()

def get_stock_basic(stock_code: str) -> dict[str, str] | None:
    """从本地 CSV 文件中获取单只股票的基本信息，返回包含公司名称和所属行业的字典。"""
    stock_info = ALL_STOCKS[ALL_STOCKS["stock_code"] == stock_code]
    if stock_info.empty:
        return None
    row = stock_info.iloc[0]
    return row.to_dict()


def get_industry_peers(industry: str, exclude_stock_code: str | None = None, max_peers: int = 10) -> list[dict[str, str]]:
    """从本地 CSV 文件中获取同一行业的股票列表，返回包含公司名称和股票代码的字典列表。"""
    peers = ALL_STOCKS[ALL_STOCKS["industry"] == industry]
    if exclude_stock_code:
        peers = peers[peers["stock_code"] != exclude_stock_code]
    return peers.head(max_peers).to_dict(orient="records")


if __name__ == "__main__":
    stocks = get_all_stocks()
    print(stocks.head())