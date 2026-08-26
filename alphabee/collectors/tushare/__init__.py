import os

import tushare as ts

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")
ts.set_token(TUSHARE_TOKEN)

__all__ = ["ts"]
