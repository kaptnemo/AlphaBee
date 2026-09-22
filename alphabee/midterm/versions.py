"""``ThesisVersion`` 读写（研究连续体 P5 / W5；``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §14.3 D3 + §15.5）。

**定位（反漂移）**：``ThesisVersion`` 的既有意图是"**原始买入理由与证伪条件不可被行情或叙事重写**"
（ROADMAP 7.6 契约注释）。本模块把该意图落成可执行的读写：

* **首个版本**（无历史）⇒ ``version=1``、``buy_rationale=[thesis]``、``invalidation=list(invalidation)``；
* **thesis 文本变了** ⇒ ``version=prev.version+1``、``thesis=新文本``，而 **``buy_rationale`` /
  ``invalidation`` 继承首版**（后续行情/叙事**不得**重写"当初为什么买"）；
* **thesis 未变** ⇒ **不追加**（同日重复调用幂等）。

**与 D3 的关系（§15.5-C，避免重复建设）**：本模块承载的正是 §14.3 D3 所排除的"thesis 语义是否变了"
—— 它与研究状态轴**正交**：状态轴回答"现在该做什么"，版本轴回答"当初为什么这么想、后来改了没有"。
本阶段只做**登记与比对**，不做自动回滚、不改写任何结论。

**存储**：``data/midterm/thesis_versions/<symbol>.jsonl``，append-only JSONL（每行一条
``model_dump(mode="json")``），与 ``midterm.persistence`` **同构**（同一套容错口径：损坏行跳过、
按 id 幂等）。

**不改契约面**：``ThesisVersion`` **没有 ``symbol`` 字段**（标的由**文件名**承载），故本模块
**不 import、也不修改** ``midterm/models.py`` 的模型定义。

**fail-open**：读失败 → 空列表/``None``；写失败 → 不抛（调用方 ``register_thesis_if_changed``
带异常边界）—— 版本登记绝不打断跟踪帧（§15.0 C-3）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alphabee.midterm.models import ThesisVersion

logger = logging.getLogger(__name__)

__all__ = [
    "APPEND_REASON_CHANGED",
    "APPEND_REASON_FIRST",
    "APPEND_REASON_UNCHANGED",
    "DEFAULT_VERSION_DIR",
    "append_version",
    "latest_version",
    "load_versions",
    "register_thesis_if_changed",
]

#: 版本文件根目录（与 ``midterm.persistence`` 的 ``data/midterm/state`` 同级）。
DEFAULT_VERSION_DIR = Path("data") / "midterm" / "thesis_versions"

#: ``register_thesis_if_changed`` 的三种 reason（**稳定字符串**：会被写进 ``TrackingReport``）。
APPEND_REASON_FIRST = "first_registered"
APPEND_REASON_CHANGED = "changed"
APPEND_REASON_UNCHANGED = "unchanged"


def default_version_dir() -> Path:
    """默认版本目录（按需创建；与 :data:`DEFAULT_VERSION_DIR` 同值）。"""
    return DEFAULT_VERSION_DIR


def _version_path(symbol: str, data_dir: str | Path | None = None) -> Path:
    """``<data_dir>/<symbol>.jsonl``（缺省 :data:`DEFAULT_VERSION_DIR`）。"""
    base = Path(data_dir) if data_dir else default_version_dir()
    return base / f"{symbol}.jsonl"


def _version_id(version: ThesisVersion) -> str:
    """确定性持久化 id：``f"{as_of_date}#{version}"``（同日同版号 ⇒ 幂等；不带 symbol，标的在文件名里）。"""
    return f"{version.as_of_date}#{version.version}"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """读回 JSONL 全部行（文件缺失 → ``[]``；**损坏行跳过**并保留可读行）。

    与 ``midterm.persistence._read_rows`` **同一容错口径**（本模块不 import 它：那是同层的私有
    helper，复制 3 行比跨模块依赖私有名更稳；两处口径由用例分别钉住）。
    """
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


def _sort_key(version: ThesisVersion) -> tuple[str, int]:
    """升序口径：``(as_of_date, version)``（同日多版按版本号）。"""
    return (str(version.as_of_date or ""), int(version.version or 0))


def append_version(symbol: str, version: ThesisVersion, *, data_dir: str | Path | None = None) -> str:
    """按 id **幂等**追加一条版本（append-only JSONL），返回持久化 id。

    已存在同 id（同日同版号）⇒ 直接返回 id、不重复追加（与 ``persistence.append_artifact`` 同口径）。
    写盘失败**不抛**：只 ``logger.warning`` 并仍返回 id（fail-open，调用方据此判定"已登记过"）。
    """
    id_ = _version_id(version)
    path = _version_path(symbol, data_dir)
    try:
        rows = _read_rows(path)
        if any(str(row.get("id")) == id_ for row in rows):
            return id_
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"id": id_, **version.model_dump(mode="json")}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断帧（fail-open，§15.0 C-3）
        logger.warning("thesis version append failed (fail-open) symbol=%s id=%s: %s", symbol, id_, exc)
    return id_


def load_versions(symbol: str, *, data_dir: str | Path | None = None) -> list[ThesisVersion]:
    """读回某标的全部版本（按 ``(as_of_date, version)`` **升序**；无记录/损坏行 → 不抛）。

    单行反序列化失败（字段缺失/类型不符）同样**跳过该行**并保留其余（与损坏 JSON 行同一口径）。
    """
    versions: list[ThesisVersion] = []
    for row in _read_rows(_version_path(symbol, data_dir)):
        payload = {key: value for key, value in row.items() if key != "id"}
        try:
            versions.append(ThesisVersion.model_validate(payload))
        except Exception as exc:  # noqa: BLE001 - 坏行跳过，不拖垮其余版本（fail-open）
            logger.warning("thesis version row skipped (invalid payload) symbol=%s: %s", symbol, exc)
            continue
    versions.sort(key=_sort_key)
    return versions


def latest_version(symbol: str, *, data_dir: str | Path | None = None) -> ThesisVersion | None:
    """最新版本（无记录 → ``None``）。"""
    versions = load_versions(symbol, data_dir=data_dir)
    return versions[-1] if versions else None


def register_thesis_if_changed(
    symbol: str,
    *,
    as_of: str,
    thesis: str,
    invalidation: Sequence[str] = (),
    data_dir: str | Path | None = None,
) -> tuple[ThesisVersion | None, str]:
    """thesis 文本与最新版本不一致 → 追加新版本；一致 → 不动。

    反漂移语义（§15.5-A）：

    * **首个版本**（无历史）⇒ ``version=1``、``buy_rationale=[thesis]``、``invalidation=list(invalidation)``，
      reason = :data:`APPEND_REASON_FIRST`；
    * **thesis 变了** ⇒ ``version=prev.version+1``、``thesis=新文本``，**``buy_rationale`` /
      ``invalidation`` 继承首版**（`prev.buy_rationale or [prev.thesis]` / `prev.invalidation`），
      reason = :data:`APPEND_REASON_CHANGED`；
    * **thesis 未变** ⇒ 返回 ``(None, APPEND_REASON_UNCHANGED)``，**不追加**。

    fail-open：任何异常（读/写/校验）⇒ ``(None, APPEND_REASON_UNCHANGED)`` + warning，绝不抛给调用方
    （跟踪帧的产出优先于版本登记）。

    Returns:
        ``(新版本 | None, reason)``；reason ∈ {:data:`APPEND_REASON_FIRST`, :data:`APPEND_REASON_CHANGED`,
        :data:`APPEND_REASON_UNCHANGED`}。
    """
    try:
        text = str(thesis or "")
        previous = latest_version(symbol, data_dir=data_dir)
        if previous is not None and str(previous.thesis or "") == text:
            return None, APPEND_REASON_UNCHANGED

        if previous is None:
            version = ThesisVersion(
                version=1,
                as_of_date=str(as_of),
                thesis=text,
                buy_rationale=[text],
                invalidation=[str(item) for item in invalidation],
            )
            reason = APPEND_REASON_FIRST
        else:
            # ★ 反漂移：**继承首版**的买入理由与证伪条件（后续行情/叙事不得重写"当初为什么买"）。
            # `buy_rationale` 为空（历史数据/手工造的行）时回落首版 thesis —— 不让"继承"变成"清空"。
            inherited_rationale = [str(item) for item in previous.buy_rationale] or [str(previous.thesis or "")]
            version = ThesisVersion(
                version=int(previous.version or 0) + 1,
                as_of_date=str(as_of),
                thesis=text,
                buy_rationale=inherited_rationale,
                invalidation=[str(item) for item in previous.invalidation],
            )
            reason = APPEND_REASON_CHANGED

        append_version(symbol, version, data_dir=data_dir)
        return version, reason
    except Exception as exc:  # noqa: BLE001 - 版本登记绝不打断跟踪帧（fail-open，§15.0 C-3 / §15.5-B）
        logger.warning("thesis version registration failed (fail-open) symbol=%s as_of=%s: %s", symbol, as_of, exc)
        return None, APPEND_REASON_UNCHANGED
