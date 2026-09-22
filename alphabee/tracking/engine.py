"""L2 研究引擎协议（研究连续体 P6 / §8；``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.6-A）。

**定位**：给"用哪个引擎做研究"这件事一个**接缝** —— 上层只依赖本模块的 :class:`ResearchEngine`
协议与两个数据模型（:class:`ResearchContext` / :class:`ResearchOutput`），换引擎不动调用方。
v1 **只交付协议 + 现有主图的适配器 + 一个 stub 引擎测试**，**不接入任何外部引擎**
（MiroThinker / MiroFlow / Tongyi 等）。

**v1 明确不做**（§15.6-B 的非目标，防范围膨胀）：

* 不把主图重构成插件系统；
* 不改 ``orchestrator/agent.py`` 的图结构、不改 ``services/deviation.NODE_ORDER``；
* 不接入任何外部引擎（本模块**无任何 orchestrator 依赖**，纯数据契约 + Protocol）。

**依赖方向（§15.0 C-6 / §15.0 B-2）**：本模块只依赖 ``pydantic`` —— **不 import orchestrator**，
也不 import 具体引擎实现（适配器在 ``alphabee.tracking.engines``）。因此
``import alphabee.tracking`` 不会拉起 ``orchestrator.collectors`` → tushare ``set_token`` 副作用。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "ResearchContext",
    "ResearchEngine",
    "ResearchOutput",
]


class ResearchContext(BaseModel):
    """L2 引擎输入（研究对象的上下文，**不绑定主图内部类型**）。

    ``evidence_ids`` / ``open_questions`` 是"跨 run 复用"与"待研究问题"的载体：协议层先固定形状，
    主图要真正消费它们属**后续独立一期**（并需按 steward 流程登记 ``OrchestratorState`` / 节点契约变更）

    —— v1 **不**把它们注入 ``Run.context``（无消费者 ⇒ 注入即 dead-end，§15.6-C）。
    """

    symbol: str
    question: str = ""
    as_of: str = ""
    thesis: str = ""
    prior_confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)  # 已有证据 id（跨 run 复用）
    open_questions: list[str] = Field(default_factory=list)  # 待研究问题（§6.1 Q5 / 未探索区域）


class ResearchOutput(BaseModel):
    """L2 引擎输出（**序列化产物**，不要求与主图 ``Artifact`` 同构）。"""

    engine: str = ""
    summary: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False
    degradation_reason: str = ""


@runtime_checkable
class ResearchEngine(Protocol):
    """研究引擎协议（结构化子类型；**不要求**继承本类）。

    ``@runtime_checkable`` 只让 ``isinstance`` 能查**成员存在性**（``name`` / ``run``），
    **不校验签名**，也不校验 ``run`` 是协程函数 —— 消费方若需要强校验请自行 ``inspect``（见
    ``tests/tracking/test_engine.py`` 的结构化断言）。
    """

    name: str

    async def run(self, context: ResearchContext) -> ResearchOutput:  # pragma: no cover - 协议方法
        ...
