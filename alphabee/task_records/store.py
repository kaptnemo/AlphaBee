"""TaskStore — JSON 文件持久化，按日期分目录。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from alphabee.task_records.models import TaskRecord


class TaskStore:
    """基于 JSON 文件的任务记录存储。

    目录结构::

        {base_dir}/
            2025-06-20/
              task-a1b2c3d4e5f6.json
              task-b2c3d4e5f6a1.json
            2025-06-21/
              ...

    用法::

        store = TaskStore()
        store.save(record)
        records = store.load_all(limit=20)
    """

    def __init__(self, base_dir: str = "outputs/task_records") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ── 写入 ──────────────────────────────────────────────────────────

    def save(self, record: TaskRecord) -> Path:
        """保存记录到 {base_dir}/{YYYY-MM-DD}/{task_id}.json。"""
        date_dir = self.base_dir / datetime.now().strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        filepath = date_dir / f"{record.task_id}.json"
        filepath.write_text(
            record.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return filepath

    # ── 读取 ──────────────────────────────────────────────────────────

    def list_files(self, limit: int = 100) -> list[Path]:
        """列出最近记录的文件路径（最新在前）。"""
        files: list[tuple[float, Path]] = []
        for date_dir in sorted(self.base_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for f in date_dir.glob("*.json"):
                files.append((f.stat().st_mtime, f))
        files.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in files[:limit]]

    def load(self, task_id: str) -> TaskRecord | None:
        """按 task_id 加载单条记录。"""
        for date_dir in self.base_dir.iterdir():
            if not date_dir.is_dir():
                continue
            filepath = date_dir / f"{task_id}.json"
            if filepath.exists():
                return TaskRecord.model_validate_json(
                    filepath.read_text(encoding="utf-8")
                )
        return None

    def load_all(self, limit: int = 100) -> list[TaskRecord]:
        """批量加载最近记录。"""
        records: list[TaskRecord] = []
        for filepath in self.list_files(limit):
            try:
                records.append(TaskRecord.model_validate_json(
                    filepath.read_text(encoding="utf-8")
                ))
            except Exception:
                continue
        return records

    def count(self) -> int:
        """总记录数。"""
        return sum(
            1
            for date_dir in self.base_dir.iterdir()
            if date_dir.is_dir()
            for _ in date_dir.glob("*.json")
        )
