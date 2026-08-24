#!/usr/bin/env python3
"""重建 all_stocks.csv（含申万 L1/L2/L3 分类列）。

数据源：tushare ``stock_basic`` + ``index_member_all``（按 L1 逐行业抓取申万成分）。
需有效 Tushare token（两者均需 2000 积分）。

用法::

    poetry run python scripts/build_all_stocks.py [--output path/to/all_stocks.csv]
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "alphabee" / "static" / "all_stocks.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="重建含申万 L1/L2/L3 的 all_stocks.csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"输出路径（默认 {DEFAULT_OUTPUT}）")
    args = parser.parse_args()

    from alphabee.collectors.local.helper import rebuild_all_stocks_csv

    output = rebuild_all_stocks_csv(args.output)
    df = __import__("pandas").read_csv(output)
    sw_cols = [c for c in df.columns if c.startswith("sw_")]
    print(f"已生成 {output}（{len(df)} 行）")
    print("SW 列:", sw_cols)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
