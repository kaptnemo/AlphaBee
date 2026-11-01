"""TaskRecord → 影子观测帧适配（只读、纯函数、无 I/O）。

F 边际影子期（F-Marginal design §5.2 / §7.1 G-8）的语料有两个来源：

1. ``data/midterm/state/<symbol>.jsonl`` —— ``tracking/scheduler.py`` 的跟踪帧
   （由 :func:`alphabee.midterm.marginal_shadow.load_frames` 读取）；
2. ``data/task_records/**/*.json`` —— 一次性查询 run 的 ``TaskRecord.midterm``
   （本模块适配）。

两者**写入语义不同**（跟踪帧带 ``stale_after`` / ``exit_conditions``、同日幂等；
查询帧每次 run 一条），因此**不合并写侧**：查询帧留在 task_records，统计侧只读汇总，
避免污染 tracking 状态机与 preflight 陈旧阻断。

本模块只做「对象 → 帧 dict」的形状转换，不做去重、不做统计：去重由
:func:`alphabee.midterm.marginal_shadow.dedupe_frames` 统一处理（样本单位 = symbol-day）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def load_task_record_frames(records: Iterable[Any]) -> tuple[list[dict[str, Any]], int]:
    """把 ``TaskRecord`` 的 ``midterm`` artifact 转成影子观测帧。

    Args:
        records: 可迭代的 ``TaskRecord``（鸭子类型：只读 ``.midterm`` 属性；
            这里不 import ``TaskRecord``，避免 ``midterm`` 包反向依赖 ``task_records``）。

    Returns:
        ``(frames, skipped)``：``frames`` 是 ``CompanyStateArtifact.model_dump(mode="json")``
        的扁平 dict 列表（与 state 帧同形）；``skipped`` 是 ``midterm is None`` 的记录数
        （旧记录 / 未启用 midterm），与坏文件/坏行一样**只计数不抛**。

    去重不在本函数内做：同一标的同日多次查询会产生多条同 ``(symbol, as_of_date)`` 的帧，
    由调用方用 ``dedupe_frames`` 按口径处理（默认 symbol-day）。
    """
    frames: list[dict[str, Any]] = []
    skipped = 0
    for record in records:
        artifact = getattr(record, "midterm", None)
        if artifact is None:
            skipped += 1
            continue
        if isinstance(artifact, dict):
            frames.append(dict(artifact))
            continue
        frames.append(artifact.model_dump(mode="json"))
    return frames, skipped
