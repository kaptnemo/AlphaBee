"""节点出口偏离检测包装器（F1b / §14.2-C）。

**接线决策（§14.2-C 关键决策）**：在 ``agent.py`` 的**注册处**包装节点，而**不改任何节点函数体**——
零侵入 15+ 个节点、可全局开关、测试可直接调用 ``wrapper``（构造 state + 假 update）。

**fail-open 纪律**：

* 开关读取（:func:`detection_switches`）**运行时**逐层 ``getattr``，配置段/字段缺失或读取异常 →
  默认 ``True``（与 §14.6 默认值一致）。**不得**直读 ``settings.deviation.detection.enabled``——
  ``DeviationSettings`` 由 F2 落地，直读会在 F1 上线时让每个被包装节点抛 ``AttributeError``；
  也**不得**在 import 期读配置。
* 检测器自身异常由 :func:`~alphabee.orchestrator.detectors.run_detectors` 吞掉（warning + 跳过），
  节点返回的 ``update`` 除新增 ``issues`` 外**逐字段不变**；无偏离时返回原 ``update`` 对象本身。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from typing import Any, cast

from langchain_core.runnables import RunnableConfig

from alphabee.orchestrator.detectors import NodeContext, run_detectors
from alphabee.orchestrator.node_contracts import get_contract
from alphabee.orchestrator.state import OrchestratorState, _append_items, _merge_by_id

logger = logging.getLogger(__name__)

__all__ = [
    "detection_switches",
    "with_deviation_detection",
]

NodeFn = Callable[..., Any]

#: §14.6 默认值：检测默认开启（fail-open）。
_DEFAULT_DETECTION_ENABLED = True


def detection_switches() -> bool:
    """读取检测开关（§14.6 ``deviation.detection.enabled``），容忍配置缺失。

    **R2-6 口径定稿（择"改文档口径"侧，保持裸 ``bool``）**：与
    :func:`alphabee.orchestrator.nodes.record_deviations.ledger_switches` 返回
    ``tuple[bool, bool]`` 的差异是**刻意**的——账本侧 §14.6 定义了**两个**开关
    （``enabled`` + ``fingerprint_normalize``），检测侧只有**一个**；为对称而包成元组会迫使
    所有调用点解包，属无收益复杂度。两者真正共有的契约是三条：运行时逐层 ``getattr``、
    缺失/异常 → 默认 ``True``、**模块级不读配置**。

    运行时读取 ``get_settings().deviation.detection``；配置段/字段不存在或读取异常 →
    ``True``（fail-open，与 §14.6 默认一致）。``DeviationSettings`` 由 F2 落地，
    因此这里必须容忍 ``AttributeError`` 而不是假定字段存在。
    """
    try:
        from alphabee.config import get_settings

        detection = getattr(getattr(get_settings(), "deviation", None), "detection", None)
        if detection is None:
            return _DEFAULT_DETECTION_ENABLED
        return bool(getattr(detection, "enabled", _DEFAULT_DETECTION_ENABLED))
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断 run
        logger.warning("deviation detection settings unavailable (fail-open, default on): %s", exc)
        return _DEFAULT_DETECTION_ENABLED


def _merge_state_view(state: Mapping[str, Any] | None, update: dict[str, Any] | None) -> dict[str, Any]:
    """构造 ``state ⊕ update`` 的只读合并视图。

    复用 ``state.py`` 的 reducer（``_merge_by_id`` / ``_append_items``），避免检测器看到的
    ``artifacts`` / ``issues`` / ``decisions`` 与 LangGraph 实际归并结果不一致。
    """
    view: dict[str, Any] = dict(state or {})
    for key, value in (update or {}).items():
        if key == "artifacts" or key == "decisions" or key == "observations" or key == "issues":
            view[key] = _merge_by_id(view.get(key), value)
        elif key in {"steps", "messages"}:
            view[key] = _append_items(view.get(key), value)
        else:
            view[key] = value
    return view


def _last_step(update: dict[str, Any] | None) -> Any:
    """取本节点 update 里的最后一步（节点自检的检测时延锚点）。"""
    steps = (update or {}).get("steps") or []
    return steps[-1] if steps else None


def with_deviation_detection(node_id: str, fn: NodeFn) -> NodeFn:
    """把节点函数包成"先执行、再按契约跑出口检测器"的包装器（§14.2-C）。

    * 契约缺失、无检测器、或开关关闭 → **原样返回节点的 update**（零改动，纯增量）；
    * 有偏离 → 返回 ``{**update, "issues": [*原有, *新增]}``（只追加 issues，不改其他字段）。
    """
    if inspect.iscoroutinefunction(fn):

        async def _async_wrapper(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
            update = await fn(state, config)
            return cast("dict[str, Any]", _detect(node_id, state, update))

        return _async_wrapper

    def _sync_wrapper(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
        update = fn(state, config)
        return cast("dict[str, Any]", _detect(node_id, state, update))

    return _sync_wrapper


def _detect(node_id: str, state: Mapping[str, Any] | None, update: Any) -> Any:
    """在节点出口执行契约检测器；任何异常都 fail-open 返回原 ``update``。"""
    try:
        if not isinstance(update, dict):
            return update  # 非 dict 返回视为原样透传（本流水线节点均返回 dict）
        contract = get_contract(node_id)
        if contract is None or not contract.detectors:
            return update
        if not detection_switches():
            return update
        ctx = NodeContext(
            node_id=node_id,
            step=_last_step(update),
            new_artifacts=list(update.get("artifacts") or []),
            new_decisions=list(update.get("decisions") or []),
            view=_merge_state_view(state, update),
        )
        issues = run_detectors(contract, ctx)
        if not issues:
            return update
        return {**update, "issues": [*(update.get("issues") or []), *issues]}
    except Exception as exc:  # noqa: BLE001 - 检测层绝不允许打断节点
        logger.warning("deviation detection failed for node %r (fail-open): %s", node_id, exc)
        return update
