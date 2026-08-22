"""Orchestrator —— 主流水线编排层。

``alphabee_agent``（编译后的 LangGraph）采用**惰性加载**：只在访问
``alphabee.orchestrator.alphabee_agent`` 时才导入 ``orchestrator.agent``，避免「只想消费
typed contract（如 ``contracts.coerce_driver_profile``）的调用方」被迫付完整 graph 编译 +
tushare 初始化（``ts.set_token`` 写 ``~/tk.csv``）的代价。

直接运行主流水线的入口（``main.py``）请用
``from alphabee.orchestrator.agent import alphabee_agent``。
"""

from __future__ import annotations

__all__ = ["alphabee_agent"]


def __getattr__(name: str):
    if name == "alphabee_agent":
        from alphabee.orchestrator.agent import alphabee_agent

        return alphabee_agent
    raise AttributeError(f"module 'alphabee.orchestrator' has no attribute {name!r}")
