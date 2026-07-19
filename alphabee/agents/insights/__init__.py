"""InsightAgent — 投资观点合成模块。

从 signals / anomaly / conflicts / verification 中提炼中心矛盾，
输出可证伪的投资观点，供下游 thesis 和 report 节点消费。
"""

from alphabee.agents.insights.agent import insight_agent_factory
from alphabee.agents.insights.models import (
    EvidenceItem,
    InsightOutput,
    MaterialityRank,
)

__all__ = [
    "insight_agent_factory",
    "InsightOutput",
    "EvidenceItem",
    "MaterialityRank",
]
