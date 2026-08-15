"""行业知识资产持久化（industry-context Phase 1）。

存储后端 v1 从简（主计划 1.3）：每行业一个 JSON 快照文件，原子写（先写临时文件再
rename），latest-wins（同一 standard+code 重复运行覆盖旧快照；版本/血缘由 artifact 内
的 schema_version / as_of_date / generated_at / source_refs 承载 + git diff 审计）。

文件布局：``data/industry_profiles/{classification_standard}/{industry_code}.json``
（industry_code 经 normalize_symbol 防路径穿越；数据根目录尊重 config.yaml 的 data.root_dir）。

过期语义（主计划 1.2 / 设计 D6）：
- ``suggest_stale_after``：按基准类别默认天数（估值 30d / 财务 90d / 成长 90d / 定性 30d），
  有效过期日取**最早到期的类别**（最易过期的类别决定整体过期点）；
- ``is_stale``：stale_after 存在 → 过期判断；缺失 → 按估值 30 天兜底（防御性默认）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from alphabee.industry.contracts import IndustryContextArtifact
from alphabee.utils.storage import get_data_root, normalize_symbol

# 基准类别 → 建议过期天数（主计划 1.3："估值基准 7-30 天、财务基准 90 天、定性描述月度"）
STALE_AFTER_DAYS: dict[str, int] = {
    "valuation": 30,
    "financial": 90,
    "growth": 90,
    "qualitative": 30,
}

# stale_after 缺失时的防御性兜底（估值最紧）
_DEFAULT_STALE_AFTER_DAYS = 30


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def suggest_stale_after(as_of_date: str, present_categories: set[str]) -> str | None:
    """按实际存在的基准类别计算建议过期日（取各类别中最早到期）。

    Args:
        as_of_date: 数据截止日（YYYY-MM-DD）。
        present_categories: 实际存在数值的类别集合（见
            ``IndustryContextArtifact.present_benchmark_categories()``）。

    Returns:
        建议过期日（YYYY-MM-DD）；as_of_date 非法或类别集合为空时返回 None。
    """
    base = _parse_date(as_of_date)
    if base is None:
        return None
    if not present_categories:
        return None
    earliest = min(STALE_AFTER_DAYS.get(category, _DEFAULT_STALE_AFTER_DAYS) for category in present_categories)
    from datetime import timedelta

    return (base + timedelta(days=earliest)).isoformat()


def is_stale(artifact: IndustryContextArtifact, *, now: date | None = None) -> bool:
    """判断 artifact 是否过期。

    - ``stale_after`` 存在 → 按它判断；
    - 缺失 → 用 as_of_date + 估值兜底天数（30 天）防御性判断；
    - 两者都不可解析 → 视为未过期（False）。
    """
    today = now or date.today()
    stale_after = _parse_date(artifact.stale_after)
    if stale_after is None:
        base = _parse_date(artifact.as_of_date)
        if base is None:
            return False
        from datetime import timedelta

        stale_after = base + timedelta(days=_DEFAULT_STALE_AFTER_DAYS)
    return today > stale_after


@dataclass
class ProfileInfo:
    """存储里的行业快照一览（CLI --list / 观测用）。"""

    classification_standard: str
    industry_code: str
    industry: str
    as_of_date: str
    generated_at: str
    stale_after: str | None
    degraded: bool
    review_status: str | None
    stale: bool
    peer_count: int | None = None


class IndustryProfileStore:
    """行业知识 JSON 快照存储（原子写、latest-wins）。"""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = get_data_root() / "industry_profiles"
        self.root = Path(root)

    # ── 路径 ───────────────────────────────────────────────────────────

    def path_for(self, classification_standard: str, industry_code: str) -> Path:
        """(standard, code) → 快照文件路径（industry_code 防路径穿越）。"""
        standard = normalize_symbol(classification_standard) or "unknown"
        code = normalize_symbol(industry_code) or "unknown"
        return self.root / standard / f"{code}.json"

    # ── 写 ─────────────────────────────────────────────────────────────

    def save(self, artifact: IndustryContextArtifact) -> Path:
        """原子写入快照（临时文件 + os.replace），返回文件路径。"""
        path = self.path_for(artifact.classification_standard, artifact.industry_code)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = artifact.model_dump(mode="json")

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path

    # ── 读 ─────────────────────────────────────────────────────────────

    def load(
        self,
        classification_standard: str,
        industry_code: str,
        *,
        schema_version: str | None = None,
        as_of_date: str | None = None,
    ) -> IndustryContextArtifact | None:
        """读取快照；``schema_version`` / ``as_of_date`` 过滤不匹配时返回 None。"""
        path = self.path_for(classification_standard, industry_code)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if schema_version is not None and raw.get("schema_version") != schema_version:
            return None
        if as_of_date is not None and raw.get("as_of_date") != as_of_date:
            return None
        try:
            return IndustryContextArtifact.model_validate(raw)
        except Exception:
            return None

    def list_profiles(self) -> list[ProfileInfo]:
        """遍历存储，返回全部快照一览（损坏文件跳过）。"""
        infos: list[ProfileInfo] = []
        if not self.root.exists():
            return infos
        for path in sorted(self.root.rglob("*.json")):
            artifact = self._load_path(path)
            if artifact is None:
                continue
            infos.append(
                ProfileInfo(
                    classification_standard=artifact.classification_standard,
                    industry_code=artifact.industry_code,
                    industry=artifact.industry,
                    as_of_date=artifact.as_of_date,
                    generated_at=artifact.generated_at,
                    stale_after=artifact.stale_after,
                    degraded=artifact.degraded,
                    review_status=artifact.review_status,
                    stale=is_stale(artifact),
                    peer_count=artifact.peer_count,
                )
            )
        return infos

    def _load_path(self, path: Path) -> IndustryContextArtifact | None:
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return IndustryContextArtifact.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
