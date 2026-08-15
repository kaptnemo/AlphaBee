"""行业知识工作流（industry-context Phase 1）。

离线/准离线批处理流水线：确定性顺序执行六节点（nodes.py），产出可版本化、可复用、
可审计的 ``IndustryContextArtifact`` JSON 快照。

形态决策（docs/industry-context-phase1-design.md D3）：不引入 LangGraph——离线批处理
不需要流式/检查点/条件路由；节点统一 ``(state, options) -> state`` 签名，逐个可单测。

用法：:

    from alphabee.industry import IndustryContextWorkflow, IndustryTarget

    result = IndustryContextWorkflow().run(IndustryTarget(symbol="600519.SH"))
    print(result.persist_path, result.artifact.review_status)

CLI：``python -m alphabee.industry.cli --symbol 600519.SH``
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphabee.industry.contracts import (
    IndustryReview,
    IndustryTarget,
    IndustryWorkflowState,
    WorkflowOptions,
)
from alphabee.industry.nodes import (
    collect_industry_facts,
    derive_industry_benchmarks,
    normalize_industry_schema,
    persist_industry_profile,
    review_industry_context,
    synthesize_industry_context,
)
from alphabee.industry.persistence import IndustryProfileStore

_NODE_SEQUENCE = (
    collect_industry_facts,
    normalize_industry_schema,
    derive_industry_benchmarks,
    synthesize_industry_context,
    review_industry_context,
    persist_industry_profile,
)


class IndustryContextWorkflow:
    """行业知识工作流：按主计划 1.1 的节点序列执行并持久化。"""

    def __init__(self, store: IndustryProfileStore | None = None) -> None:
        self.store = store

    def run(
        self,
        target: IndustryTarget | str,
        *,
        qualitative_mode: str = "none",
        as_of_date: str | None = None,
        peer_limit: int = 20,
    ) -> IndustryWorkflowState:
        """运行一次行业研究工作流。

        Args:
            target: ``IndustryTarget``，或直接传股票代码字符串（等价于
                ``IndustryTarget(symbol=...)``）。
            qualitative_mode: ``"none"``（默认，v1 轻量）/ ``"llm"``（可选定性合成）。
            as_of_date: 覆盖数据截止日（YYYY-MM-DD，默认今天）。
            peer_limit: 成分股抽样上限。

        Returns:
            终态 ``IndustryWorkflowState``；``artifact`` 与 ``persist_path`` 在成功时填充。
            行业身份不可得（collect 失败）时提前终止：``review.status == "rejected"``，
            不产 artifact（显式留痕，不静默）。
        """
        if isinstance(target, str):
            target = IndustryTarget(symbol=target)
        options = WorkflowOptions(
            qualitative_mode=qualitative_mode,
            as_of_date=as_of_date,
            peer_limit=peer_limit,
            store=self.store,
        )
        state = IndustryWorkflowState(target=target)

        for node in _NODE_SEQUENCE:
            state = node(state, options)
            # 行业身份不可得 → 无法继续（无行业可研究），显式 rejected 并终止
            if node is collect_industry_facts and state.raw_facts.get("identity") is None:
                state.review = IndustryReview(
                    status="rejected",
                    notes=list(state.errors),
                    reviewed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
                break
        return state
