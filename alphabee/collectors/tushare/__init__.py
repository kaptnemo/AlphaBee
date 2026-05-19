import os
from pathlib import Path

import tushare as ts  # type: ignore[import-untyped]

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")
ts.set_token(TUSHARE_TOKEN)