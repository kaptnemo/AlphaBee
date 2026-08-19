"""对标组持久化（COMPANY_TRACK_ROADMAP Phase D 前置 / Phase C3 存储基础）。

对标组是**可版本化、可人工编辑**的资产：每标的一个 JSON 文件（原子写、latest-wins），
分析师可直接编辑后让在线节点读取。Phase C3 的 LLM 抽取/校验在其上扩展。

文件布局：``data/peer_groups/{symbol}.json``（symbol 经路径消毒；数据根目录尊重
config.yaml 的 data.root_dir）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from alphabee.utils.storage import get_data_root, normalize_symbol


@dataclass
class PeerGroup:
    """一个标的的对标组清单（Phase C4：A 股进基准、境外仅名单）。"""

    symbol: str = ""
    codes: list[str] = field(default_factory=list)  # A 股代码（进基准计算）
    international: list[str] = field(default_factory=list)  # 境外代码（仅名单，不进基准）
    source: str = "manual"  # manual / llm / analyst
    name: str = ""  # 对标组命名（如 "AI 服务器 ODM"）
    updated_at: str = ""
    notes: list[str] = field(default_factory=list)  # 构建/校验告警
    reason_map: dict[str, str] = field(default_factory=dict)  # code → 对标理由

    def is_empty(self) -> bool:
        return not self.codes and not self.international


class PeerGroupStore:
    """对标组 JSON 快照存储（原子写、latest-wins）。"""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = get_data_root() / "peer_groups"
        self.root = Path(root)

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{normalize_symbol(symbol)}.json"

    def save(self, peer_group: PeerGroup) -> Path:
        path = self.path_for(peer_group.symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        peer_group.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "symbol": peer_group.symbol,
                        "codes": peer_group.codes,
                        "source": peer_group.source,
                        "name": peer_group.name,
                        "updated_at": peer_group.updated_at,
                        "notes": peer_group.notes,
                        "international": peer_group.international,
                        "reason_map": peer_group.reason_map,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path

    def load(self, symbol: str) -> PeerGroup | None:
        path = self.path_for(symbol)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return PeerGroup(
                symbol=str(raw.get("symbol") or symbol),
                codes=[str(code) for code in (raw.get("codes") or [])],
                international=[str(code) for code in (raw.get("international") or [])],
                source=str(raw.get("source") or "manual"),
                name=str(raw.get("name") or ""),
                updated_at=str(raw.get("updated_at") or ""),
                notes=[str(note) for note in (raw.get("notes") or [])],
                reason_map={str(key): str(value) for key, value in (raw.get("reason_map") or {}).items()},
            )
        except (OSError, json.JSONDecodeError):
            return None
