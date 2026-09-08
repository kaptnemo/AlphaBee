"""Consensus (E 因子) collectors。

``engine`` 为纯聚合逻辑（无网络，可单测）；``eastmoney`` 负责按股票拉取东财研报
列表（翻页）并调用 ``engine.aggregate_consensus`` 输出 canonical 字段。
外部字段名只允许出现在本包（collector/fetcher 层），下游一律使用 canonical 名。
"""

from alphabee.collectors.consensus.eastmoney import build_consensus, fetch_report_records
from alphabee.collectors.consensus.engine import (
    CONSENSUS_FIELDS,
    ConsensusOutput,
    aggregate_consensus,
)

__all__ = [
    "CONSENSUS_FIELDS",
    "ConsensusOutput",
    "aggregate_consensus",
    "build_consensus",
    "fetch_report_records",
]
