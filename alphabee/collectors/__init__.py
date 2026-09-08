"""AlphaBee collectors package.

本包承载各数据域的 source-specific collector（采集 + 归一化），输出 canonical 字段；
外部数据源字段名只允许出现在本包（collector/fetcher 层）。

顶层仅显式 re-export 无 import 副作用的采集器：
- ``consensus`` —— 东财研报聚合 → E 因子（canonical: consensus.yaml）
- ``crowding``  —— 股东户数/人气榜/换手率/覆盖热度 → C 因子（canonical: crowding.yaml）

其余采集器（tushare / akshare / market_regime / eastmoney）保持按需 import，避免顶层
import 触发 ``alphabee.collectors.tushare`` 的 ``ts.set_token`` HOME 写副作用。
"""

from alphabee.collectors import consensus, crowding

__all__ = ["consensus", "crowding"]
