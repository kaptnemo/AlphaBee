"""CompanyStateArtifact 追加式持久化（design MIDTERM_STATE_DIFF_DESIGN.md §10 / §1.5）。

把 :class:`~alphabee.midterm.models.CompanyStateArtifact` 以 JSON Lines（每行一条
``model_dump(mode="json")``）追加落盘，供 diff 引擎的 :class:`ArtifactRef` 引用
（引用不内嵌，反漂移）。

API 对齐 ``alphabee.market_regime.persistence`` 的 append/load/latest/drop 模式，
但存储语义为 **append-only**（不 upsert、不覆盖历史）而非 CSV 按日覆盖：

- ``append_artifact(artifact) -> id``：追加一条，返回确定性持久化 id
  （``f"{symbol}:{as_of_date}"``，与 diff 的 ``ArtifactRef.id`` 同口径）；同 id
  已存在时幂等返回（不产生重复行）；
- ``load_artifacts(symbol) -> list[CompanyStateArtifact]``：读回某标的全部历史
  （按 ``as_of_date`` 排序）；
- ``latest_artifact(symbol)``：最新一帧；
- ``drop_artifact(id) -> bool``：按 id 删除一条（测试 / 数据修复的逃生口，非正常
  追加流的一部分）。

布局：``data/midterm/state/<symbol>.jsonl``（每标的独立 append-only 日志）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alphabee.midterm.models import CompanyStateArtifact

DEFAULT_DATA_DIR = Path("data") / "midterm" / "state"


def default_data_dir() -> Path:
    """默认落盘目录（按需创建）。"""
    return DEFAULT_DATA_DIR


def _artifact_path(symbol: str, data_dir: str | Path | None = None) -> Path:
    base = Path(data_dir) if data_dir else default_data_dir()
    return base / f"{symbol}.jsonl"


def _artifact_id(artifact: CompanyStateArtifact) -> str:
    """确定性持久化 id：``symbol:as_of_date``（与 diff 的 ``ArtifactRef.id`` 同口径）。"""
    return f"{artifact.symbol}:{artifact.as_of_date}"


def _symbol_from_id(id_: str) -> str:
    """从 ``symbol:as_of_date`` 形态的 id 还原 symbol（A 股代码不含 ``:``）。"""
    return id_.split(":", 1)[0]


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """读回 JSONL 全部行（文件缺失 → ``[]``）；损坏行跳过并保留可读行。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def append_artifact(artifact: CompanyStateArtifact, data_dir: str | Path | None = None) -> str:
    """追加一条 CompanyStateArtifact，返回持久化 id（幂等，append-only）。

    Args:
        artifact: 待落盘的 CompanyStateArtifact（含 StateBelief / FactorSnapshot /
            ExpectedValue / PositionDecision 等嵌套结构）。
        data_dir: 落盘目录（默认 ``data/midterm/state``）。

    Returns:
        持久化 id ``f"{symbol}:{as_of_date}"``，供 diff 的 ``ArtifactRef.id`` 引用。
    """
    id_ = _artifact_id(artifact)
    path = _artifact_path(artifact.symbol, data_dir)
    rows = _read_rows(path)
    if any(r.get("id") == id_ for r in rows):
        return id_  # 已存在 → 幂等返回，不重复追加（append-only 反漂移）

    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": id_, **artifact.model_dump(mode="json")}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return id_


def load_artifacts(symbol: str, data_dir: str | Path | None = None) -> list[CompanyStateArtifact]:
    """读回某标的全部历史帧（按 ``as_of_date`` 升序；无记录 → ``[]``）。"""
    path = _artifact_path(symbol, data_dir)
    artifacts: list[CompanyStateArtifact] = []
    for row in _read_rows(path):
        payload = {k: v for k, v in row.items() if k != "id"}
        artifacts.append(CompanyStateArtifact.model_validate(payload))
    artifacts.sort(key=lambda a: a.as_of_date)
    return artifacts


def latest_artifact(symbol: str, data_dir: str | Path | None = None) -> CompanyStateArtifact | None:
    """最新一帧（无记录 → ``None``）。"""
    artifacts = load_artifacts(symbol, data_dir)
    return artifacts[-1] if artifacts else None


def drop_artifact(id_: str, data_dir: str | Path | None = None) -> bool:
    """按 id 删除一条（测试 / 数据修复逃生口）。删除成功 → ``True``，未命中 → ``False``。"""
    symbol = _symbol_from_id(id_)
    path = _artifact_path(symbol, data_dir)
    rows = _read_rows(path)
    kept = [r for r in rows if r.get("id") != id_]
    if len(kept) == len(rows):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in kept:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return True
