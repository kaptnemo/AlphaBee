"""偏离账本读写层（deviation ledger store，F0b / 文档 §14.1-C）。

存储选择：复用 ``fetch_events.db``（同一 ``Base.metadata``、同一 ``init_db()``、
同一 ``DATA_FETCH_DB_PATH`` 覆盖口），独立表 ``deviation_events``；新增表只
``create_all``，不改既有表结构。

设计约束
--------
1. **fail-open**：账本是旁路观测，不是主链依赖。所有公开函数在 DB 不可用 / 写入异常时
   ``logger.warning`` 并返回中性值（写 → ``None``/``False``/``0``，读 → 空），
   **绝不向上抛异常**（§14.0 约定 3：任何异常只 warning，不打断 run）。
2. **依赖方向**：只依赖 ``alphabee.core`` 与 ``alphabee.data_fetch`` 内部，不 import
   ``alphabee.orchestrator``（§14.0）。由此产生两处显式约定：
   - 偏离分类由调用方解析后写入 ``issue.deviation_class``（节点侧
     ``resolve_deviation_class``），数据层只做保守兜底（见 :func:`record_event`）；
   - per-symbol 画像里的"平均检测时延"需要节点序（orchestrator 层）→ 由调用方
     注入 ``step_index`` 回调（见 :func:`summarize_symbol`），数据层不反向依赖。
3. **指纹**：复用 ``data_fetch/fingerprint.py::compute_fingerprint``（sha256 前 16 位），
   输入 ``(deviation_class, category, normalized_message, symbol, detected_at_step)``；
   ``normalized_message`` 把数字替换为 ``#`` 并压缩空白，让"同一偏离不同数字"归并为
   同一指纹（否则复发率统计失效）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from alphabee.core.schemas import DeviationClass, Issue, IssueStatus
from alphabee.data_fetch.database import get_engine, get_session, init_db
from alphabee.data_fetch.fingerprint import compute_fingerprint
from alphabee.data_fetch.models import DeviationEvent

logger = logging.getLogger(__name__)

__all__ = [
    "compute_deviation_fingerprint",
    "list_events",
    "mark_resolved",
    "normalize_message",
    "purge_before",
    "record_event",
    "summarize_symbol",
]

#: ``step_index(node_id) -> 节点序 | None``：由 orchestrator 层注入（deviation.step_index）。
StepIndexFn = Callable[[str | None], int | None]

#: ``summarize_symbol`` 里"复发指纹 top-N"的 N。
TOP_RECURRING = 5

#: 数字（含小数/百分数）→ ``#``；先替数字再压空白，避免 "1 000" 之类被拆错。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

#: 已初始化的 DB URL（同一 DB 只 create_all 一次；按 URL 记账以便测试切换 DB 路径时重新建表）。
_initialized_urls: set[str] = set()


def _ensure_init() -> None:
    """幂等建表：按当前 DB URL 判重，避免每个事件都跑一次 ``create_all``。"""
    database_key = str(get_engine().url)
    if database_key in _initialized_urls:
        return
    init_db()
    _initialized_urls.add(database_key)


def reset_init_cache() -> None:
    """清空建表缓存（测试 / ``reset_db()`` 后复用）；不触碰 DB 内容。"""
    _initialized_urls.clear()


def normalize_message(message: str | None) -> str:
    """归一化偏离描述：数字 → ``#``、压缩连续空白（§14.1-C）。

    >>> normalize_message("PE TTM 达 176.73 倍，营收 -11.80%")
    'PE TTM 达 # 倍，营收 -#%'
    """
    if not message:
        return ""
    return " ".join(_NUMBER_RE.sub("#", message).split())


def compute_deviation_fingerprint(
    *,
    deviation_class: str,
    category: str,
    message: str,
    symbol: str | None,
    detected_at_step: str | None,
) -> str:
    """偏离指纹（16 位 hex）：复用 :func:`compute_fingerprint` 的 sha256 规则。

    该 helper 只做"若干成分拼接后 sha256 取前 16 位"，语义由调用方决定，故 5 个成分
    按下列方式映射到它的形参：

    ====================  ====================
    §14.1-C 成分           helper 形参
    ====================  ====================
    ``deviation_class``    ``provider``
    ``category``           ``api_name``
    ``detected_at_step``   ``error_type``
    ``symbol``             ``missing_fields``（helper 内部排序，单元素）
    ``message``            ``error_prefix``（helper 截断前 80 字符）
    ====================  ====================

    传入的 ``message`` 应当已经是 :func:`normalize_message` 的结果；超长消息因
    helper 截断而对尾部不敏感（与失败库同一口径）。
    """
    return compute_fingerprint(
        provider=deviation_class,
        api_name=category or "unknown",
        error_type=detected_at_step or "none",
        missing_fields=[symbol] if symbol else None,
        error_prefix=message or None,
    )


def _deviation_class_value(issue: Issue) -> str:
    """取归一化分类字符串。

    数据层不得 import orchestrator，故这里只做**保守兜底**：``issue.deviation_class``
    为空（旧 issue 未经过节点解析）时按 ``D2_STRUCTURE`` 记 —— 与
    ``services/deviation.resolve_deviation_class`` 的未知回退同值；
    正常路径（``record_deviations`` 节点）已先解析并写入该字段。
    """
    if issue.deviation_class is None:
        return DeviationClass.D2_STRUCTURE.value
    return str(issue.deviation_class)


def _severity_value(issue: Issue) -> str:
    return str(issue.severity)


def _is_resolved(issue: Issue) -> bool:
    """写入时的"是否已恢复"快照：以 ``Issue.status`` 为准（F2 恢复阶梯再用 mark_resolved 修正）。"""
    return issue.status == IssueStatus.RESOLVED


def record_event(
    issue: Issue,
    *,
    run_id: str,
    symbol: str | None = None,
    step_id: str | None = None,
    normalize: bool = True,
) -> DeviationEvent | None:
    """按指纹 upsert 一条偏离账本记录（§14.1-C）。

    - 指纹命中 → ``occurrence_count += 1``、``last_seen_at`` 刷新为本次观测时间，
      行内其余观测字段（severity / message / recovery_* / amplified_by / resolved）
      以**最近一次观测为准**；``first_seen_at`` 保持首见时间；
    - 未命中 → 插入新行（``occurrence_count = 1``）。

    :param issue: 偏离（建议已由节点写入 ``deviation_class``；空则保守记 D2）。
    :param run_id: 所在 run；空串表示未知（表内 NOT NULL）。
    :param symbol: 标的代码（画像维度之一）。
    :param step_id: 产生地节点（缺省取 ``issue.related_step``）。
    :param normalize: 是否对 message 做"数字→#、压缩空白"归一化（§14.6 ``ledger.fingerprint_normalize``）。
    :return: 写入后的 ORM 行；DB 不可用/异常 → ``None``（fail-open，只记 warning）。
    """
    deviation_class = _deviation_class_value(issue)
    category = issue.category or deviation_class
    message = issue.message or ""
    # 指纹用**归一化** message（数字→#），行内 message 存最近一次**原始**文本（保留具体数字），
    # 这样"同一偏离不同数字"归并成一行，同时不丢失最新一次的具体数值。
    fingerprint = compute_deviation_fingerprint(
        deviation_class=deviation_class,
        category=category,
        message=normalize_message(message) if normalize else message,
        symbol=symbol,
        detected_at_step=issue.detected_at_step,
    )
    now = datetime.now()

    try:
        _ensure_init()
        session = get_session()
        try:
            event = session.query(DeviationEvent).filter(DeviationEvent.fingerprint == fingerprint).first()
            if event is None:
                event = DeviationEvent(
                    run_id=run_id or "",
                    symbol=symbol,
                    step_id=step_id or issue.related_step,
                    detected_at_step=issue.detected_at_step,
                    deviation_class=deviation_class,
                    category=category,
                    severity=_severity_value(issue),
                    fingerprint=fingerprint,
                    occurrence_count=1,
                    message=issue.message,
                    recovery_action=issue.recovery_action,
                    recovery_cost=issue.recovery_cost,
                    amplified_by=list(issue.amplified_by or []),
                    resolved=_is_resolved(issue),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(event)
            else:
                event.occurrence_count += 1
                event.last_seen_at = now
                event.run_id = run_id or event.run_id
                event.step_id = step_id or issue.related_step or event.step_id
                event.severity = _severity_value(issue)
                event.message = issue.message
                event.recovery_action = issue.recovery_action
                event.recovery_cost = issue.recovery_cost
                event.amplified_by = list(issue.amplified_by or [])
                # 复发语义：最新一次观测仍未恢复 → resolved 回到 False（对齐失败库 FIXED→NEW 的处理）
                event.resolved = _is_resolved(issue)

            session.commit()
            return event
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as exc:
        logger.warning("deviation ledger write failed (fail-open): %s", exc)
        return None


def mark_resolved(issue_id: str, *, recovery_action: str, recovery_cost: int) -> bool:
    """把一条账本记录标记为已恢复（§14.1-C）。

    ``issue_id`` 指**账本行标识 = ``fingerprint``**（16 位 hex；由
    :func:`record_event` 返回值的 ``.fingerprint`` 或
    :func:`compute_deviation_fingerprint` 得到）。表内不存 ``Issue.id``
    （字段见 §14.1-C），传 ``issue-<hex12>`` 必然匹配不到。

    不刷新 ``last_seen_at``（恢复不是一次观测）。返回是否命中；
    DB 异常 → ``False``（fail-open）。
    """
    try:
        _ensure_init()
        session = get_session()
        try:
            event = session.query(DeviationEvent).filter(DeviationEvent.fingerprint == issue_id).first()
            if event is None:
                return False
            event.resolved = True
            event.recovery_action = recovery_action
            event.recovery_cost = recovery_cost
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as exc:
        logger.warning("deviation ledger mark_resolved failed (fail-open): %s", exc)
        return False


def list_events(
    *,
    symbol: str | None = None,
    run_id: str | None = None,
    deviation_class: DeviationClass | str | None = None,
    since: datetime | None = None,
) -> list[DeviationEvent]:
    """按条件读取账本行，最近出现的排前面。

    :param since: 只取 ``last_seen_at >= since`` 的行。
    :return: 命中行；DB 异常 → ``[]``（fail-open，只记 warning）。
    """
    try:
        _ensure_init()
        session = get_session()
        try:
            query = session.query(DeviationEvent)
            if symbol is not None:
                query = query.filter(DeviationEvent.symbol == symbol)
            if run_id is not None:
                query = query.filter(DeviationEvent.run_id == run_id)
            if deviation_class is not None:
                query = query.filter(DeviationEvent.deviation_class == str(deviation_class))
            if since is not None:
                query = query.filter(DeviationEvent.last_seen_at >= since)
            ordered = query.order_by(DeviationEvent.last_seen_at.desc(), DeviationEvent.event_id.desc())
            return list(ordered.all())
        finally:
            session.close()
    except Exception as exc:
        logger.warning("deviation ledger read failed (fail-open): %s", exc)
        return []


def _detection_latency(event: DeviationEvent, step_index: StepIndexFn | None) -> int | None:
    """``detected_at_step`` 与 ``step_id`` 的节点序差；未注入 step_index 或任一端未知 → None。"""
    if step_index is None:
        return None
    detected = step_index(event.detected_at_step)
    origin = step_index(event.step_id)
    if detected is None or origin is None:
        return None
    return detected - origin


def summarize_symbol(symbol: str, *, step_index: StepIndexFn | None = None) -> dict[str, Any]:
    """per-symbol 偏离画像（§5.3）：各类偏离计数 / 复发指纹 top-N / 平均检测时延。

    :param step_index: orchestrator 层注入的节点序函数（``services.deviation.step_index``）；
        数据层不反向 import orchestrator，未注入时 ``avg_detection_latency`` 为 ``None``。
    :return: 画像 dict（JSON 可序列化）：

        ``symbol`` / ``total``（指纹行数）/ ``total_occurrences``（累计出现次数，含复发）/
        ``unresolved`` / ``class_counts``（D1–D5 全量键，无该类则 0）/
        ``top_recurring``（``occurrence_count > 1`` 的复发指纹 top-N）/
        ``avg_detection_latency`` / ``first_seen_at`` / ``last_seen_at``（ISO 字符串）。

        无记录与 DB 不可用都会返回零值画像（fail-open 的代价；后者伴随 warning 日志）。
    """
    events = list_events(symbol=symbol)

    class_counts: dict[str, int] = dict.fromkeys((member.value for member in DeviationClass), 0)
    for event in events:
        class_counts[event.deviation_class] = class_counts.get(event.deviation_class, 0) + 1

    recurring = sorted(
        (event for event in events if (event.occurrence_count or 0) > 1),
        key=lambda event: (-(event.occurrence_count or 0), event.event_id or 0),
    )[:TOP_RECURRING]

    latencies = [latency for event in events if (latency := _detection_latency(event, step_index)) is not None]

    last_seen = [event.last_seen_at for event in events if event.last_seen_at is not None]
    first_seen = [event.first_seen_at for event in events if event.first_seen_at is not None]

    return {
        "symbol": symbol,
        "total": len(events),
        "total_occurrences": sum(event.occurrence_count or 0 for event in events),
        "unresolved": sum(1 for event in events if not event.resolved),
        "class_counts": class_counts,
        "top_recurring": [
            {
                "fingerprint": event.fingerprint,
                "deviation_class": event.deviation_class,
                "category": event.category,
                "severity": event.severity,
                "occurrence_count": event.occurrence_count,
                "last_seen_at": event.last_seen_at.isoformat() if event.last_seen_at else None,
            }
            for event in recurring
        ],
        "avg_detection_latency": (round(sum(latencies) / len(latencies), 2) if latencies else None),
        "first_seen_at": min(first_seen).isoformat() if first_seen else None,
        "last_seen_at": max(last_seen).isoformat() if last_seen else None,
    }


def purge_before(cutoff: datetime) -> int:
    """删除 ``last_seen_at < cutoff`` 的账本行（保留期清理，§14.6 ``ledger.retention_days``）。

    :return: 删除行数；DB 异常 → ``0``（fail-open，只记 warning）。
    """
    try:
        _ensure_init()
        session = get_session()
        try:
            deleted = (
                session.query(DeviationEvent)
                .filter(DeviationEvent.last_seen_at < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
            return int(deleted or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as exc:
        logger.warning("deviation ledger purge failed (fail-open): %s", exc)
        return 0
