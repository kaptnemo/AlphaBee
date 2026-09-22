"""宏观环一次性调度器（F4 / 设计文档 §9.3、§9.4、§14.5-A）。

**定位**：在 `main.py` 之外给"长时间跟踪"一条**可脚本化**的入口 —— 一次
``reconcile``（增量事实 → 快照 → 五层差分 → 退出检查 → bayes 更新 → 状态检查）
+ 触发判定 + **告警落盘**。**常驻循环是具名非目标**（§14.5-A：v1 只做一次性
reconcile；``--loop`` 会被显式拒绝并给出提示，而不是偷偷起一个后台进程）。

**复用而非重写**（§14.5-A 原文"复用 midterm 引擎"）：

==============================  ==========================================================
既有资产                         本模块用法
==============================  ==========================================================
``midterm/factors.get_factor_snapshot``  增量事实 → :class:`FactorSnapshot`
``midterm/decision_model.evaluate``     快照 + 证据 → :class:`CompanyStateArtifact`（含 bayes 后验）
``midterm/diff.diff``                    两帧 → :class:`CompanyStateDiff`（五层差分 + 归因）
``midterm/diff_consumers.check_exit``    退出检查投影（``ExitSignal``）
``midterm/diff_consumers.monitor_triggers``  监控触发器（``tv_distance``/证据到达率）
``midterm/persistence.*``                append-only 帧持久化（``latest_artifact``/``append_artifact``）
==============================  ==========================================================

**★ 反证强制入账（§9.2）**：每帧 diff 的 ``attribution`` 必须**同时**覆盖支持与反对两类证据；
本模块的 :func:`enforce_attribution_accounting` 做确定性检查——某一侧在本帧有新证据却没有
任何归因条目引用它时，**追加一条显式记账条目**（不静默、不猜测因果），并把结果计入报告。

**★ 安全红线（§9.4）**：跟踪闭环产出的"行动类输出"（状态迁移 / 仓位带变化）只能走
:func:`require_human_confirm` 这一个放行口，而它在 **v1 恒返回 ``False``（永不自动执行）**——
行动类 gate 只允许 Tier 0（完整）或 Tier 5（升级人工），因此 v1 的每一帧都落在 Tier 5：
报告里 ``blocked_actions`` 列出被拦下的动作、``escalation_tier=5``。这是**分析系统的红线**，
不是可配置项（未来接入人工确认 UI 才可放开，届时也只改这一个函数）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ★ 复用而非重写：midterm 的引擎一律**按模块属性调用**（不是 import 期绑定名）——
# 这样 midterm 侧被 monkeypatch（哨兵 / 必抛）时本模块行为会随之变化 / 随之失败，
# 这是"调用真的路由到 midterm"的可证伪判据（import 期绑定会让哨兵失效）。
from alphabee.midterm import decision_model, diff_consumers, factors, persistence
from alphabee.midterm.diff import diff as midterm_diff
from alphabee.midterm.models import (
    ChangeAttribution,
    CompanyStateArtifact,
    CompanyStateDiff,
    EvidenceEvent,
    FactorSnapshot,
)
from alphabee.tracking.triggers import (
    Trigger,
    TriggerKind,
    TriggerThresholds,
    detect_fact_triggers,
    detect_triggers,
    manual_trigger,
    monitor_kwargs,
    thresholds_from_settings,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_CLASS_GATE_TIERS",
    "DEFAULT_STALE_AFTER_DAYS",
    "ContradictionAccounting",
    "TrackingReport",
    "default_alert_dir",
    "enforce_attribution_accounting",
    "main",
    "reconcile",
    "require_human_confirm",
    "run_once",
    "run_watchlist",
]

#: 行动类输出的**唯一允许档位**（§9.4）：Tier 0（完整）/ Tier 5（升级人工）。
ACTION_CLASS_GATE_TIERS: tuple[int, int] = (0, 5)

#: 缺省数据保鲜期（天）：``stale_after`` 缺失时按 ``as_of + N`` 写入，供下一帧判定陈旧。
#: config ``deviation.tracking.stale_after_days`` 可覆盖（缺失 → 本常量，fail-open）。
DEFAULT_STALE_AFTER_DAYS = 7

#: 缺省的帧持久化目录（与 ``midterm/persistence`` 的 ``data/midterm/state`` 同级）。
DEFAULT_STATE_DIR = Path("data") / "midterm" / "state"
#: 缺省的告警落盘目录（本模块新增；append-only JSONL）。
DEFAULT_ALERT_DIR = Path("data") / "tracking" / "alerts"

SnapshotProvider = Callable[[str], FactorSnapshot]
EvidenceProvider = Callable[..., list[EvidenceEvent]]


# ── 报告模型 ────────────────────────────────────────────────────────────────


class ContradictionAccounting(BaseModel):
    """反证强制入账的结果（§9.2；机读，供 F5 度量与报告披露）。"""

    supporting_new: int = 0  # 本帧新增的支持证据条数（effect_on_thesis="confirming"）
    opposing_new: int = 0  # 本帧新增的反对证据条数（"refuting"）
    neutral_new: int = 0
    opposing_in_log: int = 0  # 帧内 evidence_log 的反对证据总条数（跨帧累计）
    forced_sides: list[str] = Field(default_factory=list)  # 因缺失而被强制补记账的侧别
    note: str = ""


class TrackingReport(BaseModel):
    """一次 ``run_once`` 的产物（人读 + 机读；CLI ``--json`` 直接序列化）。

    **它不是流水线契约**（§14.5-A「不新增契约」）：不进 ``OrchestratorState``、不进
    ``artifacts``、不注册 ``ArtifactType`` —— 只是本模块的返回值模型，供 CLI/外部调度器消费。
    """

    symbol: str
    as_of: str
    triggers: list[Trigger] = Field(default_factory=list)

    state: str = ""  # 认知状态 argmax（S0–S5）
    thesis: str = ""
    thesis_confidence: float = 0.0
    tv_distance: float = 0.0

    state_shift_kind: str = ""  # diff 的 L3 迁移类型：upgrade/downgrade/same/reopen/""
    exit_reasons: list[str] = Field(default_factory=list)  # check_exit(diff) 的 reasons
    monitor_reasons: list[str] = Field(default_factory=list)  # monitor_triggers(diff) 的 triggers
    contradiction: ContradictionAccounting = Field(default_factory=ContradictionAccounting)

    # ── §9.4 行动类 gate（v1 恒 Tier 5：人工确认后才可执行）──
    blocked_actions: list[str] = Field(default_factory=list)
    escalation_tier: int = 0  # 0 = 无行动类输出；5 = 升级人工（永不自动执行）

    alerts_path: str = ""
    persisted: bool = False
    degraded: bool = False
    degraded_reason: str = ""
    skipped_reason: str = ""  # 本帧未差分的原因（如 as_of 未推进）

    @property
    def kinds(self) -> list[str]:
        """触发 kind 去重清单（保持出现顺序）；CLI 摘要用。"""
        seen: list[str] = []
        for trigger in self.triggers:
            value = str(trigger.kind)
            if value not in seen:
                seen.append(value)
        return seen


@dataclass(frozen=True)
class _Frame:
    """``reconcile`` 的确定性内核产物（``reconcile`` 与 ``run_once`` 共用同一内核）。"""

    artifact: CompanyStateArtifact
    previous: CompanyStateArtifact | None
    frame_diff: CompanyStateDiff | None
    exit_reasons: tuple[str, ...]
    monitor_reasons: tuple[str, ...]
    accounting: ContradictionAccounting
    skipped_reason: str
    persisted: bool


# ── §9.4 安全红线 ───────────────────────────────────────────────────────────


def require_human_confirm(action: Any) -> bool:
    """**行动类输出的唯一放行口**（§9.4）：v1 **恒返回 ``False``（永不自动执行）**。

    AlphaBee 是**分析系统**：状态迁移、仓位带变化这类行动类输出只有两种合法档位 ——
    Tier 0（完整产出、无偏离）或 Tier 5（升级人工）。本函数就是那道闸：**只有它返回 ``True``
    时行动才可自动执行**，而 v1 永远返回 ``False``。

    放开条件（具名顺延项，不在 F4 范围）：**未来接入人工确认 UI 之后**，改为"人类在 UI 上
    确认过该 action id"才返回 ``True``；即便如此，也不得绕开本函数在别处直接执行动作。
    ``action`` 参数当前不参与判定（恒 ``False``），保留它是为了固定调用契约与将来的确认查询键。

    **注解口径（如实披露）**：§14.5-A 草拟的注解是 ``PositionDecision | StateTransition``；
    本实现取 ``Any``（**宽化 = 超集**）—— 因为 v1 的调用方（:func:`_gate_actions`）手上是 diff 层
    的动作**描述**（L5 ``PositionDiff`` 的投影），真实动作对象要等接入确认 UI 时才有。
    无论传什么，v1 都返回 ``False``。
    """
    return False


# ── 默认依赖（可注入，便于离线测试） ────────────────────────────────────────


def _default_snapshot_provider(symbol: str) -> FactorSnapshot:
    """默认快照来源：复用 ``midterm/factors.get_factor_snapshot``（含市场因子）。"""
    return factors.get_factor_snapshot(symbol, include_market=True)


def _default_evidence_provider(symbol: str, *, thesis: str = "", as_of_date: str = "") -> list[EvidenceEvent]:
    """默认证据来源：复用 ``midterm/decision_model.collect_evidence``（数值规则 + 可选 LLM 通道）。

    ``window_texts`` 留空（原文管线未接，§9.3 的 W 项），因此 v1 只入账**数值类**证据；
    定性通道缺位属具名顺延项，不影响"反证强制入账"的机制（两侧证据只要有就都入账）。
    """
    return decision_model.collect_evidence(symbol, thesis, None, as_of_date=as_of_date)


def _dedupe_triggers(triggers: Iterable[Trigger]) -> list[Trigger]:
    """按 ``(kind, payload)`` 去重（保持出现顺序）。

    两条来源（artifact 侧 :func:`detect_triggers` 与增量事实侧 :func:`detect_fact_triggers`）
    可能对**同一条**行情异动各产一条（``payload`` 逐字段相同）——同一帧内不重复告警，
    故在合并处去重；``payload`` 不同的同类触发一律保留（不丢信息）。
    """
    seen: set[tuple[str, str]] = set()
    unique: list[Trigger] = []
    for trigger in triggers:
        key = (str(trigger.kind), json.dumps(trigger.payload, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        unique.append(trigger)
    return unique


def _resolve_thresholds(thresholds: TriggerThresholds | None) -> TriggerThresholds:
    return thresholds if thresholds is not None else thresholds_from_settings()


def _resolve_as_of(as_of: str | None) -> str:
    """``as_of`` 缺省 = 今天（``YYYY-MM-DD``）；显式给定则原样使用（调用方可回溯复算）。"""
    return as_of or _dt.date.today().isoformat()


def _stale_after(as_of: str, previous: CompanyStateArtifact | None, *, degraded: bool, days: int) -> str:
    """本帧的 ``stale_after``（§9.2「数据陈旧 → 到期自动触发重新采集」的写入口）。

    * **健康帧**（``degraded=False``）：本次 reconcile 已刷新数据 ⇒ 保鲜期推进为
      ``as_of + days``（下一帧只有真正过期时才会触发 ``STALE_EXPIRED``）；
    * **降级帧**（``degraded=True``）：数据没刷新成功 ⇒ **保留上一帧的 ``stale_after``**
      （继续保持"陈旧"状态、持续告警，直到拿到健康帧）；上一帧也没有 → 按 ``as_of + days`` 兜底。
    """
    inherited = str(getattr(previous, "stale_after", None) or "")
    if degraded and inherited:
        return inherited
    try:
        base = _dt.date.fromisoformat(as_of[:10])
    except ValueError:
        base = _dt.date.today()
    return (base + _dt.timedelta(days=max(1, int(days)))).isoformat()


# ── 反证强制入账（§9.2） ────────────────────────────────────────────────────


def _side_of(event: EvidenceEvent) -> str:
    effect = str(getattr(event, "effect_on_thesis", "") or "").strip().lower()
    if effect == "confirming":
        return "supporting"
    if effect == "refuting":
        return "opposing"
    return "neutral"


def _evidence_log(artifact: CompanyStateArtifact) -> list[EvidenceEvent]:
    return list(getattr(artifact, "evidence_log", None) or [])


def enforce_attribution_accounting(
    frame_diff: CompanyStateDiff | None,
    *,
    curr: CompanyStateArtifact,
    pool: Sequence[EvidenceEvent] | None = None,
) -> ContradictionAccounting:
    """★ **反证强制入账**（§9.2）：每帧 diff 必须**同时**入账支持与反对证据。

    确定性规则（无 LLM、无猜测）：

    1. **补全证据面**：``pool``（本帧采集到的证据全集）里若有一条不在
       ``curr.evidence_log``，按 id 幂等追加 —— 杜绝"采集到反证却没进帧"；
    2. **归因覆盖检查（逐条，F4-L1 加固）**：本帧 ``new_evidence`` 按方向分两侧；**每一条**
       未被任何 ``attribution`` 条目引用（``evidence_ids``）的新证据，都**单独**追加一条显式
       :class:`~alphabee.midterm.models.ChangeAttribution`（``note`` 写明"反证强制入账"），
       并把其方向记入 ``forced_sides``（同侧去重）—— 不静默、也不伪造因果（``factor_deltas``
       留空，说明"该条证据尚未定位到具体因子"）。
       **为什么是逐条而非按侧**：按侧守卫（``any(...)`` 即整侧跳过）会在"某一侧只被引用了一部分"
       时把**其余**证据整侧放过（部分覆盖被当成全覆盖）；midterm 当前把 ``new_evidence`` 的全部
       id 写进同一条归因，故该情形**当下不可达**，但那是 midterm 的实现细节、不是本模块可依赖的
       契约 —— 逐条判定把加固成本压到一行，且补了"部分覆盖"用例钉住。

    首帧 / 本帧无新证据 → 规则平凡成立（``forced_sides`` 为空），但仍会跑第 1 步补全。
    """
    log = _evidence_log(curr)
    known_ids = {e.id for e in log}
    for event in pool or []:
        if event.id not in known_ids:
            log.append(event)
            known_ids.add(event.id)
    curr.evidence_log = log

    supporting_in_log = sum(1 for e in log if _side_of(e) == "supporting")
    opposing_in_log = sum(1 for e in log if _side_of(e) == "opposing")

    if frame_diff is None:
        return ContradictionAccounting(
            opposing_in_log=opposing_in_log,
            note=f"无本帧差分（未推进）：证据日志 支持 {supporting_in_log} / 反对 {opposing_in_log} 条",
        )

    new_events = list(getattr(frame_diff, "new_evidence", None) or [])
    sides: dict[str, list[EvidenceEvent]] = {"supporting": [], "opposing": [], "neutral": []}
    for event in new_events:
        sides[_side_of(event)].append(event)

    attributed: set[str] = set()
    for attribution in getattr(frame_diff, "attribution", None) or []:
        attributed.update(str(eid) for eid in (getattr(attribution, "evidence_ids", None) or []))

    forced: list[str] = []
    for side in ("supporting", "opposing"):
        # ★ 逐条判定（F4-L1）：某侧"部分被引用"时，其余**未被引用**的证据仍要各自入账；
        # 按侧守卫（any(...) 即整侧跳过）会把部分覆盖误当全覆盖 —— 那是本次修掉的缺陷。
        unreferenced = [event for event in sides[side] if event.id not in attributed]
        if not unreferenced:
            continue
        for event in unreferenced:
            frame_diff.attribution.append(
                ChangeAttribution(
                    evidence_ids=[event.id],
                    factor_deltas=[],
                    decision_effects=[f"contradiction_accounting:{side}"],
                    note=(
                        f"反证强制入账（§9.2）：本帧该条{'支持' if side == 'supporting' else '反对'}证据"
                        f"（{event.id}）未被任何归因条目引用，此处显式记账（未定位到具体因子，不猜测因果）"
                    ),
                )
            )
        forced.append(side)  # 同侧去重：该侧只要有未引用证据，就登记该侧

    note = (
        f"本帧新增证据：支持 {len(sides['supporting'])} / 反对 {len(sides['opposing'])} / "
        f"中性 {len(sides['neutral'])} 条；日志累计：支持 {supporting_in_log} / 反对 {opposing_in_log} 条"
    )
    if forced:
        note += f"；强制补记账侧别：{'、'.join(forced)}"
    return ContradictionAccounting(
        supporting_new=len(sides["supporting"]),
        opposing_new=len(sides["opposing"]),
        neutral_new=len(sides["neutral"]),
        opposing_in_log=opposing_in_log,
        forced_sides=forced,
        note=note,
    )


# ── 确定性内核：一帧 reconcile ──────────────────────────────────────────────


def _reconcile_frame(
    symbol: str,
    *,
    as_of: str,
    snapshot_provider: SnapshotProvider | None = None,
    evidence_provider: EvidenceProvider | None = None,
    thresholds: TriggerThresholds | None = None,
    state_dir: str | Path | None = None,
    persist: bool = True,
    previous: CompanyStateArtifact | None = None,
) -> _Frame:
    """一帧的确定性内核（``reconcile`` 与 ``run_once`` **共用**，防两套链路漂移）。"""
    limits = _resolve_thresholds(thresholds)
    prev = previous if previous is not None else persistence.latest_artifact(symbol, state_dir)

    provider = snapshot_provider or _default_snapshot_provider
    snapshot = provider(symbol)
    snapshot.symbol = snapshot.symbol or symbol
    snapshot.as_of_date = as_of  # 帧日期以判定时点为准（快照来源可能滞后/缺失日期）

    thesis = str(getattr(prev, "thesis", "") or "")
    evidence_fn = evidence_provider or _default_evidence_provider
    evidence = list(evidence_fn(symbol, thesis=thesis, as_of_date=as_of) or [])

    curr = decision_model.evaluate(
        snapshot,
        evidence,
        prior_confidence=getattr(prev, "thesis_confidence", None) if prev is not None else None,
        thesis=thesis,
    )
    # 字段级接线：evaluate 不产这三项，由本层从上一帧继承（§9.2「字段已有、无调度」的补齐）
    curr.exit_conditions = list(getattr(prev, "exit_conditions", None) or [])
    curr.next_evidence_to_watch = list(getattr(prev, "next_evidence_to_watch", None) or [])
    curr.stale_after = _stale_after(
        as_of, prev, degraded=bool(getattr(curr, "degraded", False)), days=limits.stale_after_days
    )

    # 五层差分（prev=None → 首帧基线登记）；diff 要求 curr.date > prev.date，同日重跑不差分
    skipped = ""
    frame_diff: CompanyStateDiff | None = None
    if prev is None or as_of > str(prev.as_of_date):
        try:
            frame_diff = midterm_diff(prev, curr)
        except ValueError as exc:  # 引擎校验不通过（如 symbol/schema 漂移）→ 降级为不差分
            skipped = f"差分被拒绝（{exc}）"
            logger.warning("tracking diff rejected symbol=%s as_of=%s: %s", symbol, as_of, exc)
    else:
        skipped = f"as_of={as_of} 未推进（上一帧 {prev.as_of_date}）→ 不产差分（diff 要求严格递增）"

    accounting = enforce_attribution_accounting(frame_diff, curr=curr, pool=evidence)

    exit_reasons: tuple[str, ...] = ()
    monitor_reasons: tuple[str, ...] = ()
    if frame_diff is not None:
        exit_signal = diff_consumers.check_exit(frame_diff)
        monitor = diff_consumers.monitor_triggers(frame_diff, **monitor_kwargs(limits))
        exit_reasons = tuple(exit_signal.reasons)
        monitor_reasons = tuple(monitor.triggers)

    persisted = False
    if persist:
        try:
            persistence.append_artifact(curr, state_dir)
            persisted = True
        except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断帧产出（fail-open）
            logger.warning("tracking persist failed symbol=%s as_of=%s: %s", symbol, as_of, exc)

    return _Frame(
        artifact=curr,
        previous=prev,
        frame_diff=frame_diff,
        exit_reasons=exit_reasons,
        monitor_reasons=monitor_reasons,
        accounting=accounting,
        skipped_reason=skipped,
        persisted=persisted,
    )


def reconcile(
    symbol: str,
    *,
    as_of: str,
    snapshot_provider: SnapshotProvider | None = None,
    evidence_provider: EvidenceProvider | None = None,
    thresholds: TriggerThresholds | None = None,
    state_dir: str | Path | None = None,
    persist: bool = True,
) -> CompanyStateArtifact:
    """把标的推进到 ``as_of``：增量事实 → 快照 → 五层差分 + 归因 → 退出检查 → bayes 更新 → 状态检查。

    流程（§9.3 的确定性内核，全部复用 midterm 引擎）：

    1. ``get_factor_snapshot(symbol)`` —— 增量事实 → :class:`FactorSnapshot`；
    2. ``evaluate(snapshot, evidence, prior_confidence=上一帧后验, thesis=上一帧 thesis)``
       —— 含 **bayes 更新**（``update_confidence``）与 StateBelief；
    3. ``diff(prev, curr)`` —— :class:`CompanyStateDiff`（五层差分 + 归因 + L3 状态漂移）；
    4. :func:`enforce_attribution_accounting` —— ★ 反证强制入账；
    5. ``check_exit(diff)`` / ``monitor_triggers(diff)`` —— 退出与监控检查；
    6. ``append_artifact(curr)`` —— append-only 落盘（幂等）。

    Args:
        symbol: 标的代码。
        as_of: 判定时点（``YYYY-MM-DD``，必填；本函数**不读时钟**）。
        snapshot_provider: 快照来源（缺省 ``get_factor_snapshot``；注入以便离线测试）。
        evidence_provider: 证据来源（缺省 ``collect_evidence``）。
        thresholds: 触发阈值（缺省读 config，fail-open 到 midterm 常量）。
        state_dir: 帧持久化目录（缺省 ``data/midterm/state``）。
        persist: 是否落盘（``False`` 用于只读预演/测试）。

    Returns:
        本帧 :class:`CompanyStateArtifact`（已含 ``thesis_confidence`` / ``factor_snapshot`` /
        ``evidence_log`` / ``exit_conditions`` / ``stale_after``）。
    """
    return _reconcile_frame(
        symbol,
        as_of=as_of,
        snapshot_provider=snapshot_provider,
        evidence_provider=evidence_provider,
        thresholds=thresholds,
        state_dir=state_dir,
        persist=persist,
    ).artifact


# ── 告警落盘 ────────────────────────────────────────────────────────────────


def default_alert_dir() -> Path:
    """缺省告警目录（``data/tracking/alerts``，append-only JSONL）。"""
    return DEFAULT_ALERT_DIR


def _alert_path(symbol: str, alert_dir: str | Path | None = None) -> Path:
    base = Path(alert_dir) if alert_dir else default_alert_dir()
    return base / f"{symbol}.jsonl"


def _write_alerts(report: TrackingReport, alert_dir: str | Path | None = None) -> str:
    """把**有触发的**帧追加落盘（无触发 → 不写，避免噪声）；返回路径（未写则空串）。

    fail-open：写盘失败只 warning，不打断跟踪（与账本/检测器的既有纪律一致）。
    """
    if not (report.triggers or report.blocked_actions or report.exit_reasons or report.degraded):
        return ""  # 无触发、无行动类输出、无退出信号、也没降级 → 不写（避免逐帧噪声）
    path = _alert_path(report.symbol, alert_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(report.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return str(path)
    except Exception as exc:  # noqa: BLE001 - 告警落盘失败不阻断
        logger.warning("tracking alert write failed symbol=%s: %s", report.symbol, exc)
        return ""


# ── 行动类 gate（§9.4） ─────────────────────────────────────────────────────


def _action_class_outputs(frame_diff: CompanyStateDiff | None) -> list[str]:
    """从差异里识别**行动类输出**（§9.4）：状态迁移与仓位带变化。

    只看**变化**：``diff=None`` 或 ``diff.is_first``（首帧）→ 无行动类输出（基线登记不是动作）；
    未推进的帧（``diff=None``）同样不产动作。
    """
    if frame_diff is None or getattr(frame_diff, "is_first", False):
        # 首帧 = **基线登记**（不是动作）：midterm 的 diff 首帧会给 argmax_from=None / 空 band，
        # 若按"变化"判定就会把"建仓登记"误当行动类输出 ⇒ 显式排除。
        return []
    actions: list[str] = []
    shift = getattr(frame_diff, "state_shift", None)
    kind = str(getattr(shift, "kind", "") or "")
    if kind in ("upgrade", "downgrade", "reopen"):
        actions.append(
            f"state_transition:{kind}:{getattr(shift, 'argmax_from', None)}→{getattr(shift, 'argmax_to', '')}"
        )
    position = getattr(frame_diff, "position", None)
    band_from = str(getattr(position, "band_from", "") or "")
    band_to = str(getattr(position, "band_to", "") or "")
    if position is not None and band_from != band_to:
        # §9.4 的行动类输出是"**仓位带变化**"（band_from ≠ band_to）；这里刻意**不**读
        # check_exit 用的 ``band_weight_divergence`` 字段——那是退出侧的分支，读它即"第二份实现"。
        actions.append(f"position_band_change:{band_from}→{band_to}")
    return actions


def _gate_actions(actions: Iterable[str]) -> tuple[list[str], int]:
    """行动类输出的放行判定：**只有** :func:`require_human_confirm` 放行才可执行（§9.4）。

    两档语义（= :data:`ACTION_CLASS_GATE_TIERS`）：放行的动作 → **Tier 0**（完整产出、可执行）；
    被拦下的动作 → **Tier 5**（升级人工，本帧只记录不执行）。v1 中 ``require_human_confirm``
    恒 ``False`` ⇒ 只要存在行动类输出，结果必然是 ``(全部动作, 5)`` —— 这正是红线本身。

    Returns:
        ``(blocked, tier)``：被拦下的动作描述；``tier`` ∈ ``{0, 5}``（无行动类输出 → 0）。
    """
    blocked: list[str] = [action for action in actions if not require_human_confirm(action)]
    return blocked, (5 if blocked else 0)


# ── 一次性运行 + 看板 ───────────────────────────────────────────────────────


def run_once(
    symbol: str,
    *,
    as_of: str | None = None,
    snapshot_provider: SnapshotProvider | None = None,
    evidence_provider: EvidenceProvider | None = None,
    thresholds: TriggerThresholds | None = None,
    state_dir: str | Path | None = None,
    alert_dir: str | Path | None = None,
    persist: bool = True,
    write_alerts: bool = True,
    manual: bool = False,
) -> TrackingReport:
    """一次性 reconcile + 触发判定 + 告警落盘（§14.5-A 的 v1 闭环）。

    与 :func:`reconcile` **共用内核** :func:`_reconcile_frame`（同一代码路径，非另起一套）。

    Args:
        symbol: 标的代码。
        as_of: 判定时点（缺省今天）。
        snapshot_provider / evidence_provider / thresholds / state_dir: 见 :func:`reconcile`。
        alert_dir: 告警落盘目录（缺省 ``data/tracking/alerts``）。
        persist: 是否落盘帧。
        write_alerts: 是否落盘告警（``False`` = 只读预演；CLI ``--no-persist`` 会关掉它，
            否则"预演"会往**默认告警目录**里偷偷写东西）。
        manual: ``True`` 时额外注入一条 :attr:`TriggerKind.MANUAL`（CLI ``--trigger manual``）。

    Returns:
        :class:`TrackingReport`。
    """
    limits = _resolve_thresholds(thresholds)
    frame_as_of = _resolve_as_of(as_of)
    report = TrackingReport(symbol=symbol, as_of=frame_as_of)
    try:
        frame = _reconcile_frame(
            symbol,
            as_of=frame_as_of,
            snapshot_provider=snapshot_provider,
            evidence_provider=evidence_provider,
            thresholds=limits,
            state_dir=state_dir,
            persist=persist,
        )
    except Exception as exc:  # noqa: BLE001 - 跟踪入口必须 fail-open，绝不把异常抛给调度/CLI
        logger.warning("tracking reconcile failed symbol=%s as_of=%s: %s", symbol, frame_as_of, exc)
        report.degraded = True
        report.degraded_reason = f"reconcile 失败：{exc}"
        report.alerts_path = _write_alerts(report, alert_dir) if write_alerts else ""
        return report

    artifact = frame.artifact
    report.state = str(getattr(getattr(artifact, "state", None), "argmax_state", "") or "")
    report.thesis = str(getattr(artifact, "thesis", "") or "")
    report.thesis_confidence = float(getattr(artifact, "thesis_confidence", 0.0) or 0.0)
    report.tv_distance = round(
        float(getattr(getattr(frame.frame_diff, "state_shift", None), "tv_distance", 0.0) or 0.0), 6
    )
    report.state_shift_kind = str(getattr(getattr(frame.frame_diff, "state_shift", None), "kind", "") or "")
    report.exit_reasons = list(frame.exit_reasons)
    report.monitor_reasons = list(frame.monitor_reasons)
    report.contradiction = frame.accounting
    report.degraded = bool(getattr(artifact, "degraded", False))
    report.degraded_reason = str(getattr(artifact, "degraded_reason", "") or "")
    report.skipped_reason = frame.skipped_reason
    report.persisted = frame.persisted

    triggers = detect_triggers(
        symbol,
        as_of=frame_as_of,
        artifact=frame.previous,
        frame_diff=frame.frame_diff,  # ← 有 diff 时 exit/tv 判定**委托** midterm（check_exit / monitor_triggers）
        thresholds=limits,
    )
    if artifact.factor_snapshot is not None:
        triggers += detect_fact_triggers(
            symbol,
            snapshot=artifact.factor_snapshot,
            prev_snapshot=getattr(frame.previous, "factor_snapshot", None),
            thresholds=limits,
        )
    if manual:
        triggers.append(manual_trigger(symbol, reason="CLI 显式人工触发（--trigger manual）"))
    report.triggers = _dedupe_triggers(triggers)

    blocked, tier = _gate_actions(_action_class_outputs(frame.frame_diff))
    report.blocked_actions = blocked
    report.escalation_tier = tier
    if tier:
        report.triggers = [
            *report.triggers,
            Trigger(
                kind=TriggerKind.MANUAL,
                symbol=symbol,
                reason=f"行动类输出被 §9.4 人工确认闸拦下（Tier 5）：{'、'.join(blocked)}",
                payload={"source": "human_confirm_gate", "allowed_tiers": list(ACTION_CLASS_GATE_TIERS)},
            ),
        ]

    report.alerts_path = _write_alerts(report, alert_dir) if write_alerts else ""
    return report


def run_watchlist(
    symbols: Sequence[str],
    *,
    as_of: str | None = None,
    **kwargs: Any,
) -> list[TrackingReport]:
    """按序跑一份看板；**单个标的失败不影响其余**（fail-open，逐标的独立报告）。"""
    reports: list[TrackingReport] = []
    for symbol in symbols:
        reports.append(run_once(symbol, as_of=as_of, **kwargs))
    return reports


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alphabee.tracking",
        description=(
            "宏观环一次性跟踪：触发判定 + reconcile（五层差分/退出检查/bayes 更新）+ 告警落盘。"
            "行动类输出永不自动执行（§9.4 红线）。"
        ),
        epilog=(
            "示例：python -m alphabee.tracking --symbol 600519 --once --json\n"
            "常驻循环是具名非目标（v1）：--loop 会被显式拒绝。"
        ),
    )
    parser.add_argument("--symbol", action="append", default=[], help="标的代码（可重复；也支持逗号分隔）")
    parser.add_argument("--watchlist", default="", help="逗号分隔的看板（与 --symbol 合并）")
    parser.add_argument("--once", action="store_true", help="一次性 reconcile（v1 默认且唯一模式）")
    parser.add_argument("--loop", action="store_true", help="常驻循环：**具名非目标**，仅报错退出")
    parser.add_argument("--as-of", dest="as_of", default=None, help="判定时点 YYYY-MM-DD（缺省今天）")
    parser.add_argument("--state-dir", dest="state_dir", default=None, help="帧持久化目录")
    parser.add_argument("--alert-dir", dest="alert_dir", default=None, help="告警落盘目录")
    parser.add_argument("--no-persist", action="store_true", help="只读预演：不落盘帧与告警")
    parser.add_argument("--trigger", choices=["manual"], default=None, help="显式人工触发信号")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    return parser


def _symbols(args: argparse.Namespace) -> list[str]:
    symbols = [s.strip() for s in args.symbol if s and s.strip()]
    symbols += [s.strip() for s in str(args.watchlist).split(",") if s.strip()]
    seen: list[str] = []
    for symbol in symbols:
        if symbol not in seen:
            seen.append(symbol)
    return seen


def _render_text(report: TrackingReport) -> str:
    lines = [
        f"[{report.symbol}] as_of={report.as_of} state={report.state or '-'} "
        f"P(H)={report.thesis_confidence:.3f} tv={report.tv_distance:.3f}"
    ]
    if report.triggers:
        for trigger in report.triggers:
            lines.append(f"  - [{trigger.kind}] {trigger.reason}")
    else:
        lines.append("  - 无触发")
    if report.exit_reasons:
        lines.append(f"  exit: {'；'.join(report.exit_reasons)}")
    if report.monitor_reasons:
        lines.append(f"  monitor: {'；'.join(report.monitor_reasons)}")
    lines.append(f"  反证入账: {report.contradiction.note}")
    if report.escalation_tier:
        lines.append(
            f"  ⚠ 行动类输出已拦下（Tier {report.escalation_tier}，需人工确认）：{'、'.join(report.blocked_actions)}"
        )
    if report.skipped_reason:
        lines.append(f"  跳过差分: {report.skipped_reason}")
    if report.degraded:
        lines.append(f"  降级: {report.degraded_reason}")
    if report.alerts_path:
        lines.append(f"  告警落盘: {report.alerts_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（``python -m alphabee.tracking``）；返回进程退出码。

    退出码：``0`` 成功；``2`` 用法错误（无标的 / ``--loop`` 具名非目标）。
    **常驻循环不在此实现**（§14.5-A v1 具名非目标）——本函数只做一次性 reconcile，
    由外部调度器（cron / 任务队列）决定"多久跑一次"。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.loop:
        print(
            "常驻循环是具名非目标（§14.5-A v1）：本入口只做一次性 reconcile；"
            "请用外部调度器（cron / 任务队列）按需调用。"
        )
        return 2

    symbols = _symbols(args)
    if not symbols:
        parser.print_usage()
        print("error: 至少需要一个 --symbol 或 --watchlist", flush=True)
        return 2

    reports = run_watchlist(
        symbols,
        as_of=args.as_of,
        state_dir=args.state_dir,
        alert_dir=args.alert_dir,
        persist=not args.no_persist,
        write_alerts=not args.no_persist,  # 只读预演不得写任何东西（含默认告警目录）
        manual=args.trigger == "manual",
    )
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in reports], ensure_ascii=False, indent=2))
    else:
        for index, report in enumerate(reports):
            if index:
                print()
            print(_render_text(report))
    return 0
