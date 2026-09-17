"""偏离度量层（deviation telemetry，F5 / 文档 §11.1 / §14.5-B / §15 验收 1）。

把 §3 的八维从定性框架变成**每 run 可计算**的指标（§11 目标），数据源 = §5 账本
（``deviation_events``）+ ``OrchestratorState``。本模块只做**确定性算术**，不做判读、不做 LLM。

八项指标的精确定义（与 §11.1 / §14.5-B 逐项对应，口径已按现有数据模型核实）
-----------------------------------------------------------------------

======================  ================================================================
指标                     定义（本模块的可执行口径）
======================  ================================================================
``detection_rate``      §11.1 检测率：**节点级发现** ÷（节点级发现 + 兜底发现的上游遗留）。
                        判据用账本两列 ``detected_at_step``（检测端）与 ``step_id``（产生端）
                        的节点序差（§11.1 数据源列）：差 ≤ 0 = 检测端不晚于产生端 = 节点级；
                        差 > 0 = 由下游/报告 gate 兜底发现的**上游遗留**。任一端节点序未知的
                        事件**不计入分子分母**（不猜测），分母为 0 → ``None``。
``mean_detection_latency``  平均检测时延（节点数）= 有已知节点序差的事件的算术平均；
                        无已知时延 → ``None``。时延复用 F0 已认证实现
                        （``deviation_store._detection_latency`` + ``services.deviation.step_index``），
                        负数**不夹取**（暴露 mis-wired 检测器，与 F0 约定同源）。
``recovery_rate``       §11.1 恢复率：``resolved=True`` 且 ``recovery_action`` 非空 ÷ 全部账本行；
                        全部行 0 → ``None``。
``recovery_half_life``  从检测到 resolved 的**节点数中位数**（只统计 ``resolved=True`` 的行）。
                        账本没有"resolved 所在节点"列（§14.1-C 的 17 列已冻结），而 ``resolved``
                        在账本里是**写入时对 ``Issue.status`` 的快照**
                        （``deviation_store._is_resolved``）⇒ 它的可见点恒为**本 run 已执行节点的
                        末位**（正常路径即 sink 节点 ``record_deviations``）。故每条 resolved 行的
                        跨度为 ``末位节点序 − 检测端节点序``，取中位数；无 resolved 行 / 无已执行
                        节点 / 检测端节点序未知 → ``None``。负数不夹取（同 F0 口径）。
``amplification_overturn_rate``  §8.2 加权边推翻率：``maker="amplification_audit"`` 的 Decision 中
                        "方向不一致"占比；无此类 Decision → ``None``。
``on_track_curve``      P(节点产出无 D1–D5 偏离) 随节点序：按 ``NODE_ORDER`` 取本 run **已执行**的
                        流水线节点，逐节点累计算"到该节点为止未产出过偏离的节点占比"。产出端取账本
                        ``step_id``（产生地）。无已执行节点 → ``[]``（空曲线，不伪造）。
``budget_consumption``  §10.2 ``D_cum / D_max``：``D_cum`` = Σ ``recovery.cost_exposure``(未 resolved
                        的 issue)（**复用 F2 已认证公式，不重写**）；``D_max`` 从
                        ``run.context["d_max"]``（run 级显式预算）或
                        ``settings.deviation.budget.d_max``（标量，或按 ``run.context["task_kind"]``
                        取键的映射，缺省 ``"analysis"``）解析。``D_max`` 缺失或非正 → ``None``。
``silent_degradation_rate``  §11.1 静默劣化率：（兜底发现的、且分类 ∈ {D1, D2} 的账本行）÷ 全部行
                        （越接近 0 越好）；全部行 0 → ``None``。
======================  ================================================================

比率一律"分母为 0/缺失 → ``None``"（§14.5-B：**绝不静默回退 0**）；浮点统一保留 4 位小数
（``_METRIC_PRECISION``），保证落库与断言逐字节可复现。

工程约束（§14.0 / §14.8 PR8）
-----------------------------
1. **fail-open**：DB / 配置 / 账本异常只 ``logger.warning``，**绝不向上抛**（§14.0 约定 3）；
2. **零 LLM**：本模块不 import 任何 LLM 工厂（§16 反模式："不做 LLM 检测器/LLM 度量"）；
3. **只读视图**：:func:`render_deviation_timeline` 只读账本，不改编排、不触发 run ——
   §14.8 PR8 的回滚方式就是"不调用即可"；
4. **import 期不读配置**（§14.6：``config/__init__.py`` 有模块级 ``settings = get_settings()``），
   配置只在函数内惰性读取，且缺段/缺字段一律回落 §14.6 默认值；
5. **不重写已认证公式**：检测时延复用 ``deviation_store._detection_latency``、代价敞口复用
   ``recovery.cost_exposure``、节点序复用 ``services.deviation.step_index``、建表缓存复用
   ``deviation_store._ensure_init``（同一个 URL 级缓存，测试切换 DB 时不会两套缓存互相错位）。
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from alphabee.core.schemas import DeviationClass, IssueStatus
from alphabee.data_fetch import deviation_store as ledger_store
from alphabee.data_fetch.database import get_session
from alphabee.data_fetch.models import DeviationMetric
from alphabee.orchestrator.recovery import cost_exposure
from alphabee.orchestrator.services.deviation import NODE_INDEX, NODE_ORDER, step_index

logger = logging.getLogger(__name__)

__all__ = [
    "AMPLIFICATION_AUDIT_MAKER",
    "AMPLIFICATION_OVERTURN_MARKER",
    "D_MAX_CONTEXT_KEY",
    "OVERTURN_CONFIDENCE_CEILING",
    "TASK_KIND_CONTEXT_KEY",
    "DeviationMetrics",
    "compute_deviation_metrics",
    "latest_run_id",
    "ledger_enabled",
    "load_metrics",
    "render_deviation_timeline",
    "store_metrics",
]

#: §8.2 规则 2：放大审计 Decision 的 ``maker``（生产点 = ``review_thesis`` 节点，F3）。
AMPLIFICATION_AUDIT_MAKER = "amplification_audit"

#: 方向不一致的 rationale 标记。F3 的四条边审计分支在 ``direction_consistent=False`` 时一律写入
#: 该粗体标记（``reviewer._audit_*``），故它是**稳定机读标记**。
AMPLIFICATION_OVERTURN_MARKER = "**不一致**"

#: 方向不一致时的 Decision confidence 上界。F3 发射点固定 ``0.85``（一致）/ ``0.3``（不一致）
#: （``orchestrator/agent.py``）。与上面的标记构成**两条独立线索**，取"或"以降低单一文案改动
#: 导致推翻率**静默归零**的风险。
OVERTURN_CONFIDENCE_CEILING = 0.3

#: ``run.context`` 里 run 级预算（D_max）与任务类型的显式注入键（§10.2 "D_max 按任务类型配置"）。
D_MAX_CONTEXT_KEY = "d_max"
TASK_KIND_CONTEXT_KEY = "task_kind"

#: ``budget.d_max`` 为映射时的缺省任务类型（分析场景，§10.2）。
DEFAULT_TASK_KIND = "analysis"

#: §11.1 静默劣化率的分类面：D1 数据偏离 + D2 结构偏离（"劣化"）。
_SILENT_DEGRADATION_CLASSES = frozenset({DeviationClass.D1_DATA.value, DeviationClass.D2_STRUCTURE.value})

#: 浮点保留位数（落库与断言可复现）。
_METRIC_PRECISION = 4

#: 账本写入开关的缺省值（§14.6 ``deviation.ledger.enabled``；与 ``record_deviations.ledger_switches`` 同口径）。
_DEFAULT_LEDGER_ENABLED = True

#: "无已知节点序"时的排序权重：排在所有已登记节点之后（渲染用，保证确定性）。
_UNKNOWN_NODE_ORDER = len(NODE_ORDER)

#: 时间线里 message 列的截断宽度（确定性，不依赖终端宽度）。
_TIMELINE_MESSAGE_WIDTH = 60


class DeviationMetrics(BaseModel):
    """每 run 偏离指标（§14.5-B 八字段，逐字一致）。

    **缺失分母 → ``None``**：任何以"条数/节点数"为分母的比率在分母为 0（或数据不足以判定）时
    取 ``None``，绝不静默回退 0 —— 否则"没有偏离"与"没有数据"会被混为一谈（§11 度量层的前提）。
    """

    detection_rate: float | None = Field(
        default=None,
        description="§11.1 检测率 = 节点级发现 ÷（节点级发现 + gate 兜底发现的上游遗留）；无已知节点序差 → None",
    )
    mean_detection_latency: float | None = Field(
        default=None,
        description="平均检测时延（节点数）；无已知时延 → None",
    )
    recovery_rate: float | None = Field(
        default=None,
        description="有 recovery_action 且 resolved 的账本行 ÷ 全部行；无行 → None",
    )
    recovery_half_life: float | None = Field(
        default=None,
        description="从检测到 resolved 的节点数中位数；无 resolved 行/无已执行节点 → None",
    )
    amplification_overturn_rate: float | None = Field(
        default=None,
        description="§8.2 amplification_audit Decision 中方向不一致占比；无审计 Decision → None",
    )
    on_track_curve: list[float] = Field(
        default_factory=list,
        description="P(节点产出无 D1–D5 偏离) 随节点序；无已执行节点 → []",
    )
    budget_consumption: float | None = Field(
        default=None,
        description="§10.2 D_cum / D_max；D_max 缺失或非正 → None",
    )
    silent_degradation_rate: float | None = Field(
        default=None,
        description="gate 兜底发现的 D2/D1 ÷ 全部行（越接近 0 越好）；无行 → None",
    )


# ── 取值适配（state / 账本行 / Decision 三类载荷的防御性只读取值） ─────────────


def _round(value: float | None) -> float | None:
    """统一浮点精度；``None`` 透传。"""
    return None if value is None else round(float(value), _METRIC_PRECISION)


def _ratio(numerator: int, denominator: int) -> float | None:
    """比率；分母 ≤ 0 → ``None``（§14.5-B：缺失分母不静默回退 0）。"""
    if denominator <= 0:
        return None
    return _round(numerator / denominator)


def _state_list(state: Any, key: str) -> list[Any]:
    """``state[key]`` 的列表视图；缺失/``None`` → ``[]``（state 是 TypedDict，运行期即 dict）。"""
    if isinstance(state, Mapping):
        items = state.get(key)
    else:  # pragma: no cover - 防御性分支：state 理论上恒为 Mapping
        items = getattr(state, key, None)
    return list(items or [])


def _run_context(state: Any, key: str) -> Any:
    """取 ``run.context[key]``（``run``/``context``/键任一缺失 → ``None``）。"""
    run = state.get("run") if isinstance(state, Mapping) else getattr(state, "run", None)
    context = getattr(run, "context", None)
    if isinstance(context, Mapping):
        return context.get(key)
    return None


def _as_float(value: Any) -> float | None:
    """宽松数值解析：``None`` / 非数值 → ``None``（不猜测）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_value(issue: Any) -> str:
    status = getattr(issue, "status", None)
    return str(getattr(status, "value", None) or status or "")


def _is_open_issue(issue: Any) -> bool:
    """未 resolved 的 issue（``D_cum`` 的统计面，§10.2）。"""
    return _status_value(issue) != IssueStatus.RESOLVED.value


def _is_resolved_event(event: Any) -> bool:
    return bool(getattr(event, "resolved", False))


def _has_recovery_action(event: Any) -> bool:
    """§11.1 字面口径："有 ``recovery_action``" = 非空字符串。

    **不**额外剔除 ``keep`` / ``detected``：§11.1 的判据只有"有 action 且已 resolved"，多剔除一项
    就是从字段里发明第三态。注意这与 §10.1 的敞口口径**不同**（那里 ``{"", "keep"}`` 视为未恢复），
    故 :func:`_budget_consumption` 直接复用 ``recovery.cost_exposure``，不在这里做二次加工。
    """
    return bool(str(getattr(event, "recovery_action", None) or "").strip())


def _event_latency(event: Any) -> int | None:
    """检测时延（节点数）：复用 F0 已认证实现（未登记节点 / 任一端未知 → ``None``）。"""
    # 私有名复用是**有意**的：F0 的 ``_detection_latency`` 是检测时延的唯一已认证实现（模块约束 5），
    # 复制一份公式会让文档一个口径、代码两个口径。
    return ledger_store._detection_latency(event, step_index)


def _executed_nodes(state: Any) -> list[str]:
    """本 run 已执行的流水线节点（按 ``NODE_ORDER`` 序）。"""
    nodes: set[str] = set()
    for step in _state_list(state, "steps"):
        node_id = getattr(step, "id", None)
        if node_id is None and isinstance(step, Mapping):  # pragma: no cover - 防御性分支
            node_id = step.get("id")
        if isinstance(node_id, str) and node_id in NODE_INDEX:
            nodes.add(node_id)
    return sorted(nodes, key=NODE_INDEX.__getitem__)


def _deviation_settings() -> Any | None:
    """惰性读取 ``settings.deviation``；配置不可用 → ``None``（fail-open，且**不在 import 期**读）。"""
    try:
        from alphabee.config import get_settings

        return getattr(get_settings(), "deviation", None)
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断度量
        logger.warning("deviation settings unavailable (fail-open): %s", exc)
        return None


def ledger_enabled() -> bool:
    """账本写入开关（§14.6 ``deviation.ledger.enabled``）。

    与 ``orchestrator.nodes.record_deviations.ledger_switches`` **同口径**（同一配置项、同一缺省值、
    同一 fail-open 方向），但在本模块内独立实现，避免 ``nodes → services`` 的 import 环
    （sink 节点 import 本模块）。
    """
    deviation = _deviation_settings()
    if deviation is None:
        return _DEFAULT_LEDGER_ENABLED
    ledger = getattr(deviation, "ledger", None)
    if ledger is None:
        return _DEFAULT_LEDGER_ENABLED
    return bool(getattr(ledger, "enabled", _DEFAULT_LEDGER_ENABLED))


# ── 八项指标 ────────────────────────────────────────────────────────────────


def _detection_rate(events: Sequence[Any]) -> float | None:
    """§11.1 检测率：节点级发现 ÷（节点级发现 + 兜底发现的上游遗留）。"""
    latencies = [latency for event in events if (latency := _event_latency(event)) is not None]
    node_level = sum(1 for latency in latencies if latency <= 0)
    gate_level = sum(1 for latency in latencies if latency > 0)
    return _ratio(node_level, node_level + gate_level)


def _mean_detection_latency(events: Sequence[Any]) -> float | None:
    latencies = [latency for event in events if (latency := _event_latency(event)) is not None]
    if not latencies:
        return None
    return _round(statistics.fmean(latencies))


def _recovery_rate(events: Sequence[Any]) -> float | None:
    """§11.1 恢复率：``resolved`` 且 ``recovery_action`` 非空 ÷ 全部账本行。"""
    recovered = sum(1 for event in events if _is_resolved_event(event) and _has_recovery_action(event))
    return _ratio(recovered, len(events))


def _recovery_half_life(state: Any, resolved_events: Sequence[Any]) -> float | None:
    """从检测到 resolved 的节点数中位数（resolved 端 = 本 run 末位已执行节点，见模块 docstring）。"""
    executed = _executed_nodes(state)
    if not resolved_events or not executed:
        return None
    tail_index = NODE_INDEX[executed[-1]]
    spans = [
        tail_index - detected
        for event in resolved_events
        if (detected := step_index(getattr(event, "detected_at_step", None))) is not None
    ]
    if not spans:
        return None
    return _round(statistics.median(spans))


def _is_overturned(decision: Any) -> bool:
    """方向不一致判定：rationale 标记 **或** confidence 不超过不一致上界（两条独立线索取或）。"""
    if AMPLIFICATION_OVERTURN_MARKER in str(getattr(decision, "rationale", "") or ""):
        return True
    confidence = _as_float(getattr(decision, "confidence", None))
    return confidence is not None and confidence <= OVERTURN_CONFIDENCE_CEILING


def _amplification_overturn_rate(state: Any) -> float | None:
    """§8.2 加权边推翻率：``amplification_audit`` Decision 中方向不一致占比。"""
    audits = [
        decision
        for decision in _state_list(state, "decisions")
        if str(getattr(decision, "maker", "") or "") == AMPLIFICATION_AUDIT_MAKER
    ]
    if not audits:
        return None
    return _ratio(sum(1 for decision in audits if _is_overturned(decision)), len(audits))


def _on_track_curve(state: Any, events: Sequence[Any]) -> list[float]:
    """P(节点产出无 D1–D5 偏离) 随节点序（产出端 = 账本 ``step_id``）。"""
    executed = _executed_nodes(state)
    if not executed:
        return []
    # 只有能归因到已登记节点的产出地才计入"该节点产出过偏离"；未知产出地不猜测（避免冤枉任何节点）。
    deviating: set[str] = set()
    for event in events:
        step_id = getattr(event, "step_id", None)
        if isinstance(step_id, str) and step_id in NODE_INDEX:
            deviating.add(step_id)

    curve: list[float] = []
    clean = 0
    for index, node_id in enumerate(executed, start=1):
        if node_id not in deviating:
            clean += 1
        ratio = _ratio(clean, index)
        curve.append(0.0 if ratio is None else ratio)  # index ≥ 1 ⇒ 分母恒为正
    return curve


def _budget_limit(state: Any) -> float | None:
    """``D_max``：``run.context["d_max"]`` 优先，其次 ``settings.deviation.budget.d_max``；缺失 → ``None``。"""
    explicit = _as_float(_run_context(state, D_MAX_CONTEXT_KEY))
    if explicit is not None:
        return explicit

    deviation = _deviation_settings()
    budget = getattr(deviation, "budget", None)
    configured = getattr(budget, "d_max", None)
    if isinstance(configured, Mapping):
        task_kind = _run_context(state, TASK_KIND_CONTEXT_KEY) or DEFAULT_TASK_KIND
        return _as_float(configured.get(str(task_kind)))
    return _as_float(configured)


def _budget_consumption(state: Any) -> float | None:
    """§10.2 ``D_cum / D_max``（``D_cum`` 复用 F2 ``recovery.cost_exposure``）。"""
    limit = _budget_limit(state)
    if limit is None or limit <= 0:
        return None
    exposure = sum(cost_exposure(issue) for issue in _state_list(state, "issues") if _is_open_issue(issue))
    return _round(exposure / limit)


def _silent_degradation_rate(events: Sequence[Any]) -> float | None:
    """§11.1 静默劣化率：兜底发现的 D1/D2 ÷ 全部账本行。"""
    silent = sum(
        1
        for event in events
        if str(getattr(event, "deviation_class", "") or "").strip().lower() in _SILENT_DEGRADATION_CLASSES
        and (latency := _event_latency(event)) is not None
        and latency > 0
    )
    return _ratio(silent, len(events))


def compute_deviation_metrics(state: Any, ledger: Sequence[Any]) -> DeviationMetrics:
    """由 ``state`` + 账本计算 §11.1 的八项指标（§14.5-B 签名）。

    :param state: ``OrchestratorState``（``steps`` / ``decisions`` / ``issues`` / ``run`` 被读取）；
        允许是等价 dict（测试与离线回放用）。
    :param ledger: 本 run 的账本行（``DeviationEvent`` 列表；通常 = ``list_events(run_id=...)``）。
    :returns: :class:`DeviationMetrics`；缺失分母一律 ``None``（见类 docstring）。

    纯函数：不写库、不改 state、不读全局可变状态（配置只读且 fail-open）。
    """
    events = list(ledger or [])
    resolved_events = [event for event in events if _is_resolved_event(event)]
    return DeviationMetrics(
        detection_rate=_detection_rate(events),
        mean_detection_latency=_mean_detection_latency(events),
        recovery_rate=_recovery_rate(events),
        recovery_half_life=_recovery_half_life(state, resolved_events),
        amplification_overturn_rate=_amplification_overturn_rate(state),
        on_track_curve=_on_track_curve(state, events),
        budget_consumption=_budget_consumption(state),
        silent_degradation_rate=_silent_degradation_rate(events),
    )


# ── 落库 / 读取（同一 fetch_events.db，新表 deviation_metrics） ───────────────


def _metric_columns(metrics: DeviationMetrics) -> dict[str, Any]:
    return {
        "detection_rate": metrics.detection_rate,
        "mean_detection_latency": metrics.mean_detection_latency,
        "recovery_rate": metrics.recovery_rate,
        "recovery_half_life": metrics.recovery_half_life,
        "amplification_overturn_rate": metrics.amplification_overturn_rate,
        "on_track_curve": list(metrics.on_track_curve),
        "budget_consumption": metrics.budget_consumption,
        "silent_degradation_rate": metrics.silent_degradation_rate,
    }


def store_metrics(metrics: DeviationMetrics, *, run_id: str) -> None:
    """把本 run 的指标写入 ``deviation_metrics``（§14.5-B）。

    * 同一 ``run_id`` 幂等 upsert（run 尾部节点重放不会堆行）；
    * ``deviation.ledger.enabled=false`` → **零写入**（直接返回，不建表、不查询）——
      与账本写入同一开关，保证 §14.8 PR8 的"关掉即无副作用"；
    * DB 不可用 / 表缺失 / 任何异常 → 只 ``logger.warning``，**绝不向上抛**（§14.0 约定 3）。

    返回 ``None``（§14.5-B 签名）；落库是否成功请用 :func:`load_metrics` 复核（fail-open 内部吞异常，
    不把成功与否混进返回值语义）。
    """
    if not ledger_enabled():
        logger.info("deviation ledger disabled by config; metrics not stored (run_id=%s)", run_id)
        return
    try:
        ledger_store._ensure_init()
        session = get_session()
        try:
            row = session.query(DeviationMetric).filter(DeviationMetric.run_id == run_id).first()
            now = datetime.now()
            columns = _metric_columns(metrics)
            if row is None:
                session.add(DeviationMetric(run_id=run_id, created_at=now, updated_at=now, **columns))
            else:
                for key, value in columns.items():
                    setattr(row, key, value)
                row.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - 度量是旁路观测，绝不打断 run（§14.0 约定 3）
        logger.warning("deviation metrics store failed (fail-open): %s", exc, exc_info=True)


def load_metrics(run_id: str) -> DeviationMetrics | None:
    """读取某 run 已落库的指标；无记录 / DB 异常 → ``None``（fail-open，只记 warning）。"""
    try:
        ledger_store._ensure_init()
        session = get_session()
        try:
            row = session.query(DeviationMetric).filter(DeviationMetric.run_id == run_id).first()
            if row is None:
                return None
            return DeviationMetrics(
                detection_rate=row.detection_rate,
                mean_detection_latency=row.mean_detection_latency,
                recovery_rate=row.recovery_rate,
                recovery_half_life=row.recovery_half_life,
                amplification_overturn_rate=row.amplification_overturn_rate,
                on_track_curve=list(row.on_track_curve or []),
                budget_consumption=row.budget_consumption,
                silent_degradation_rate=row.silent_degradation_rate,
            )
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - 读失败同样 fail-open
        logger.warning("deviation metrics load failed (fail-open): %s", exc)
        return None


def _safe_events(*, run_id: str | None = None) -> list[Any]:
    """账本读取的**最外层** fail-open 边界。

    ``deviation_store.list_events`` 自身已经 fail-open（内部吞异常返回 ``[]``），但只读视图是
    CLI 的直接入口：这里再兜一层，保证"账本读取抛异常（打桩/未来实现变化）"也不会把异常
    抛给用户（§14.8 PR8：视图只读、可独立回滚）。
    """
    try:
        if run_id is None:
            return list(ledger_store.list_events())
        return list(ledger_store.list_events(run_id=run_id))
    except Exception as exc:  # noqa: BLE001 - 视图层不把账本异常抛给 CLI
        logger.warning("deviation ledger read failed (fail-open, empty view): %s", exc)
        return []


def latest_run_id() -> str | None:
    """账本中最近出现的非空 ``run_id``（``list_events`` 已按 ``last_seen_at`` 倒序）；无 → ``None``。"""
    for event in _safe_events():
        run_id = str(getattr(event, "run_id", "") or "").strip()
        if run_id:
            return run_id
    return None


# ── 只读时间线视图（§15 验收 1 / CLI --deviations） ─────────────────────────

#: 时间线列定义 ``(表头, 列宽)`` —— **表头与数据行共用同一张表**，避免两处宽度漂移。
#: 列宽按字符数计（CJK 宽度按 1 计），只保证"同一账本两次渲染逐字节相同"，不追求终端视觉对齐。
_TIMELINE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("节点序", 6),
    ("检测节点", 24),
    ("产生节点", 24),
    ("分类", 14),
    ("类目", 34),
    ("严重度", 9),
    ("时延", 5),
    ("恢复动作", 16),
    ("状态", 8),
    ("放大", 18),
    ("消息", 0),  # 末列不补白
)

#: 分隔线宽度（列宽之和 + 列间空格）。
_TIMELINE_RULE_WIDTH = sum(width for _, width in _TIMELINE_COLUMNS) + len(_TIMELINE_COLUMNS) - 1


def _timeline_order(event: Any) -> tuple[int, str, str]:
    """确定性排序键：检测端节点序（未知排末） → 类目 → 指纹。"""
    index = step_index(getattr(event, "detected_at_step", None))
    return (
        _UNKNOWN_NODE_ORDER if index is None else index,
        str(getattr(event, "category", "") or ""),
        str(getattr(event, "fingerprint", "") or ""),
    )


def _truncate(text: Any, width: int = _TIMELINE_MESSAGE_WIDTH) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= width else f"{value[: width - 1]}…"


def _pad(text: Any, width: int) -> str:
    value = str(text or "")
    return value if width <= 0 else value.ljust(width)


def _timeline_rule() -> str:
    return "─" * _TIMELINE_RULE_WIDTH


def _timeline_row(event: Any) -> str:
    """单行：节点序 × 检测/产生节点 × 分类/类目/严重度 × 检测时延 × 恢复动作/状态 × 放大 × 消息。"""
    index = step_index(getattr(event, "detected_at_step", None))
    latency = _event_latency(event)
    amplified = list(getattr(event, "amplified_by", None) or [])
    cells: tuple[Any, ...] = (
        f"[{'--' if index is None else f'{index:2d}'}]",
        getattr(event, "detected_at_step", None) or "(未知)",
        getattr(event, "step_id", None) or "(未知)",
        getattr(event, "deviation_class", None) or "",
        getattr(event, "category", None) or "",
        getattr(event, "severity", None) or "",
        "--" if latency is None else f"{latency:2d}",
        getattr(event, "recovery_action", None) or "未恢复",
        "已恢复" if getattr(event, "resolved", False) else "未恢复",
        f"<{','.join(str(edge) for edge in amplified)}>" if amplified else "--",
        _truncate(getattr(event, "message", None)),
    )
    return " ".join(_pad(cell, width) for cell, (_, width) in zip(cells, _TIMELINE_COLUMNS, strict=True)).rstrip()


def render_deviation_timeline(run_id: str) -> str:
    """CLI ``--deviations`` 视图：**节点序 × 偏离 × 检测时延 × 恢复动作**文本时间线（§15 验收 1）。

    * 完全从账本重建（一次 ``list_events(run_id=...)``），不读 state、不触发 run、不改编排；
    * 按节点序（未登记节点排末）→ 类目 → 指纹排序，**不渲染时间戳** ⇒ 同一账本两次渲染逐字节相同；
    * 无记录（含 DB 不可用）→ 确定性空视图（含提示行），**不抛异常**。
    """
    events = sorted(_safe_events(run_id=run_id), key=_timeline_order)
    header = f"偏离时间线 · run_id={run_id or '(未指定)'} · 记录 {len(events)} 条"
    if not events:
        return "\n".join(
            [
                header,
                "无记录：账本为空，或该 run 未产生偏离（可用 --deviations <run_id> 指定其他 run）。",
            ]
        )

    columns = " ".join(_pad(name, width) for name, width in _TIMELINE_COLUMNS).rstrip()
    lines = [header, columns, _timeline_rule()]
    lines.extend(_timeline_row(event) for event in events)

    resolved = sum(1 for event in events if getattr(event, "resolved", False))
    class_counts = {
        member.value: sum(
            1 for event in events if str(getattr(event, "deviation_class", "") or "").strip().lower() == member.value
        )
        for member in DeviationClass
    }
    distribution = " ".join(f"{name}={count}" for name, count in class_counts.items())
    lines.append(f"汇总：已恢复 {resolved} / 未恢复 {len(events) - resolved}；分类 {distribution}")
    return "\n".join(lines)
