"""Anomaly detection module — 勾稽关系异常检测（《手财》框架）。

一阶：10 条勾稽关系 z-score 检查
二阶：8 个异常模式匹配
输出：AnomalyReport → fact_values → 信号规则

业务定位：
- 不直接给买卖结论，而是识别“财务报表内部逻辑是否失真”
- 先把异常识别成结构化事实，再交给 signal / thesis / report 层消费
"""

from alphabee.agents.anomaly.engine import AnomalyEngine, run_anomaly_detection
from alphabee.agents.anomaly.models import AnomalyReport, CrossRule, MetricAnomaly, PatternMatch
from alphabee.agents.anomaly.registry import load_patterns, load_rules

__all__ = [
    "AnomalyEngine",
    "AnomalyReport",
    "CrossRule",
    "MetricAnomaly",
    "PatternMatch",
    "load_rules",
    "load_patterns",
    "run_anomaly_detection",
]
