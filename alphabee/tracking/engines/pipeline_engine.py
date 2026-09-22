"""现有主图的 L2 引擎适配器（研究连续体 P6 / §8；``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.6-B/C）。

:class:`PipelineEngine` 把 ``orchestrator.agent.alphabee_agent`` 适配成 :class:`ResearchEngine`：

* ``name = "pipeline"``；
* :meth:`PipelineEngine.run` 构造主图 initial state（``messages`` + 三个控制标志 + **注入的 ``run``**），
  调 ``alphabee_agent.ainvoke(...)``，再把最终 payload 映射为 :class:`ResearchOutput`
  （抽 ``artifacts`` / ``issues`` / ``summary`` / ``degraded``）。

**两条硬约束**

1. **延迟 import**：``alphabee.orchestrator.agent`` **只在方法体内** import —— 模块顶层 import 会拉起
   ``orchestrator.collectors`` → ``tushare.set_token``（写 ``$HOME/tk.csv``），而 ``import
   alphabee.tracking`` 必须零副作用（§15.0 B-2 / C-6；``tests/tracking/test_engine.py`` 有子进程实测 +
   AST 双重钉子）。
2. **fail-open**（§15.0 C-3）：宿主图抛异常 ⇒ 返回 ``degraded=True`` 的 :class:`ResearchOutput`
   （带 ``degradation_reason``），**不把异常抛给上层**（上层据此决定是否降级/回退，而不是崩掉）。

**``ResearchContext → Run.context`` 的注入边界（§15.6-C）**：v1 只注入 **4 个键**
（``symbol`` / ``as_of`` / ``thesis_prior``（= ``context.thesis``）/ ``prior_confidence``）；
**不注入** ``evidence_ids`` / ``open_questions``（主图当前无消费者，注入即 dead-end）。
主图要真正消费这些键属**后续独立一期**（需按 steward 流程登记 ``OrchestratorState`` / 节点契约变更）。

本适配器注入的 ``run`` 之所以能生效，是因为 ``collectors.collect_raw_facts`` 已改为
"**已有 run 则复用/合并 context**"（§15.6-C 的落地细节，同属 P6；见 ``collectors.py`` 与
``tests/tracking/test_engine.py::test_collect_raw_facts_merges_incoming_run_context``）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from alphabee.core import Run, RunStatus
from alphabee.tracking.engine import ResearchContext, ResearchOutput
from alphabee.utils.pipeline import extract_text, make_id

logger = logging.getLogger(__name__)

__all__ = ["PipelineEngine", "to_output"]

#: 主图 ``Run.context`` 里被本适配器注入的键（§15.6-C 的 4 键；值见 :meth:`PipelineEngine._initial_run`）。
INJECTED_CONTEXT_KEYS: tuple[str, ...] = ("symbol", "as_of", "thesis_prior", "prior_confidence")

#: 视为"降级"的主图 ``Run.status``（``partial`` = 部分完成；``failed`` = 失败）。
_DEGRADED_RUN_STATUSES = frozenset({RunStatus.PARTIAL.value, RunStatus.FAILED.value})


class PipelineEngine:
    """把现有主图适配为 L2 引擎（见模块 docstring）。"""

    name = "pipeline"

    def __init__(self, *, enhance: bool = False, llm_review: bool = False, midterm: bool = False) -> None:
        """控制标志（与 CLI 同名，§15.6-B）：缺省全 ``False`` = 与 CLI 裸调用同档。"""
        self.enhance = bool(enhance)
        self.llm_review = bool(llm_review)
        self.midterm = bool(midterm)

    # ── 主图 initial state ────────────────────────────────────────────────────

    def _initial_run(self, context: ResearchContext) -> Run:
        """构造注入用 ``Run``：**只含 §15.6-C 的 4 键**（不注入 ``evidence_ids`` / ``open_questions``）。"""
        return Run(
            id=make_id("engine-run"),
            goal=context.question or context.symbol,
            status=RunStatus.RUNNING,
            context={
                "symbol": context.symbol,
                "as_of": context.as_of,
                "thesis_prior": context.thesis,
                "prior_confidence": context.prior_confidence,
            },
            started_at=datetime.now(),
        )

    def initial_state(self, context: ResearchContext) -> dict[str, Any]:
        """主图 initial state（**纯函数**，便于用例直接断言映射，不必真跑图）。

        ``messages`` 用 ``context.question or context.symbol``（与 CLI 的单次查询同形）；
        三个控制标志来自构造参数；``run`` 为 :meth:`_initial_run` 的注入 Run。
        """
        return {
            "messages": [HumanMessage(content=context.question or context.symbol)],
            "run": self._initial_run(context),
            "enhance": self.enhance,
            "llm_review": self.llm_review,
            "midterm": self.midterm,
        }

    # ── 协议方法 ────────────────────────────────────────────────────────────

    async def run(self, context: ResearchContext) -> ResearchOutput:
        """跑一次主图并映射为 :class:`ResearchOutput`（fail-open：异常 ⇒ ``degraded=True``）。"""
        try:
            from alphabee.orchestrator.agent import alphabee_agent  # ← 延迟 import（§15.0 B-2）

            final = await alphabee_agent.ainvoke(self.initial_state(context))  # type: ignore[call-overload]
            return to_output(final, engine=self.name)
        except Exception as exc:  # noqa: BLE001 - fail-open：引擎失败返回降级产物，不抛给上层（§15.0 C-3）
            logger.warning("pipeline engine run failed (fail-open): symbol=%s err=%s", context.symbol, exc)
            return ResearchOutput(
                engine=self.name,
                degraded=True,
                degradation_reason=f"pipeline 引擎执行失败：{exc}",
            )


# ── 映射：主图最终 payload → ResearchOutput ─────────────────────────────────


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """只保留真正的 ``list[dict]`` 元素（其余类型跳过：不猜测、不抛）。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _state_items(final: Any, key: str) -> list[dict[str, Any]]:
    """兜底路径：直接读最终 state 的 ``artifacts`` / ``issues`` 对象并序列化（payload 不可用时）。"""
    items = getattr(final, "get", None)
    raw = items(key) if callable(items) else None
    if not isinstance(raw, list):
        return []
    dumped: list[dict[str, Any]] = []
    for item in raw:
        dump = getattr(item, "model_dump", None)
        if not callable(dump):
            continue
        try:
            value = dump(mode="json")
        except TypeError:  # pragma: no cover - 非 pydantic 对象
            continue
        if isinstance(value, dict):
            dumped.append(value)
    return dumped


def _final_payload(final: Any) -> dict[str, Any] | None:
    """解析主图 finalize payload（最后一条消息里的 JSON，含 ``final_report`` 键）；不可用 → ``None``。

    与 ``apps/cli/parsing.parse_report_payload`` 同口径（这里不复用 CLI 模块：引擎层不该依赖 CLI）。
    """
    messages = getattr(final, "get", None)
    raw_messages = messages("messages") if callable(messages) else None
    if not isinstance(raw_messages, list):
        return None
    for message in reversed(raw_messages):
        text = extract_text(getattr(message, "content", ""))
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and "final_report" in payload:
            return payload
    return None


def to_output(final: Any, *, engine: str = "pipeline") -> ResearchOutput:
    """把主图最终 state / finalize payload 映射为 :class:`ResearchOutput`（**纯函数**，fail-open）。

    * ``artifacts`` / ``issues``：优先取 finalize payload 里的**序列化**形式；payload 不可用则从
      state 的 ``Artifact`` / ``Issue`` 对象 ``model_dump(mode="json")`` 兜底；
    * ``summary``：``payload["final_report"]["summary"]``（报告契约里的既有字段）；
    * ``degraded`` / ``degradation_reason``：``payload["run"]["status"]`` ∈ {``partial``, ``failed``}
      ⇒ 降级，并给出可读原因（含 issues 条数）；payload 无 ``run``（或状态未知）⇒ **不**判降级
      （保守：不把"未知"当"降级"）。
    """
    payload = _final_payload(final) or {}

    artifacts = _as_dict_list(payload.get("artifacts")) or _state_items(final, "artifacts")
    issues = _as_dict_list(payload.get("issues")) or _state_items(final, "issues")

    report = payload.get("final_report")
    summary = str(report.get("summary") or "") if isinstance(report, dict) else ""

    run_payload = payload.get("run")
    status = str(run_payload.get("status") or "") if isinstance(run_payload, dict) else ""
    degraded = status in _DEGRADED_RUN_STATUSES
    reason = f"主图 run.status={status}（issues={len(issues)}）" if degraded else ""

    return ResearchOutput(
        engine=engine,
        summary=summary,
        artifacts=artifacts,
        issues=issues,
        degraded=degraded,
        degradation_reason=reason,
    )
