"""Task records — 任务执行记录与规则自蒸馏模块。

用法::

    # 采集（在 main.py 中集成）
    from alphabee.task_records import TaskRecorder, TaskStore
    recorder = TaskRecorder()
    record = recorder.capture(query=..., symbol=..., flags=..., payload=..., artifacts=...)
    store = TaskStore()
    store.save(record)

    # 分析
    from alphabee.task_records import TaskAnalyzer, TaskStore
    analyzer = TaskAnalyzer(TaskStore())
    print(analyzer.signal_trigger_rates())
    print(analyzer.summary())

    # 蒸馏
    from alphabee.task_records import RuleDistiller, distill
    report = distill()
    print(report)
"""

from alphabee.task_records.analyzer import TaskAnalyzer
from alphabee.task_records.distiller import RuleDistiller, distill
from alphabee.task_records.models import TaskRecord
from alphabee.task_records.recorder import TaskRecorder
from alphabee.task_records.store import TaskStore

__all__ = [
    "TaskAnalyzer",
    "TaskRecord",
    "TaskRecorder",
    "TaskStore",
    "RuleDistiller",
    "distill",
]
