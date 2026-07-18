"""Thesis registry — 从 YAML 文件加载 Thesis 维度定义。

每个维度（ThesisDimensionDef）定义：
- signal_dimension_key: 对应信号规则 thesis_impact 中的 key
- interpretation_templates: 各判断档位的解释文字
- critic_questions: 该维度触发特定档位时应追问的问题
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DIMENSIONS_DIR = Path(__file__).resolve().parent / "dimensions"


@dataclass
class DimensionCriticQuestion:
    """维度级别的 Critic 追问定义（来自 YAML）。"""

    question: str
    category: str
    severity: str
    trigger_on: list[str]  # 在哪些判断档位下触发


@dataclass
class ThesisDimensionDef:
    """Thesis 维度定义（从 YAML 加载）。"""

    id: str
    name: str
    description: str
    signal_dimension_key: str  # 对应信号 thesis_impact 的 key
    interpretation_templates: dict[str, str] = field(default_factory=dict)
    critic_questions: list[DimensionCriticQuestion] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThesisDimensionDef:
        dim_id = data.get("id", "")
        critic_qs = []
        for q in data.get("critic_questions", []):
            critic_qs.append(
                DimensionCriticQuestion(
                    question=q.get("question", ""),
                    category=q.get("category", "general"),
                    severity=q.get("severity", "minor"),
                    trigger_on=q.get("trigger_on", []),
                )
            )
        return cls(
            id=dim_id,
            name=data.get("name", dim_id),
            description=data.get("description", ""),
            signal_dimension_key=data.get("signal_dimension_key", dim_id),
            interpretation_templates=data.get("interpretation_templates", {}),
            critic_questions=critic_qs,
        )

    def get_interpretation(self, judgment: str) -> str:
        raw = self.interpretation_templates.get(judgment, "")
        # YAML 多行字符串可能包含换行和多余空白，统一清理
        return " ".join(raw.split())


# ── 全局维度注册表 ─────────────────────────────────────────────────────

DIMENSION_DEFS: dict[str, ThesisDimensionDef] = {}

_loaded = False


def load_dimension_defs() -> None:
    """扫描 dimensions/ 目录，加载所有 .yaml 维度定义到 DIMENSION_DEFS。"""
    global _loaded
    import yaml

    DIMENSION_DEFS.clear()
    for yaml_file in sorted(DIMENSIONS_DIR.glob("*.yaml")):
        with open(yaml_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            continue
        dim = ThesisDimensionDef.from_dict(data)
        DIMENSION_DEFS[dim.id] = dim

    _loaded = True


def ensure_loaded() -> None:
    if not _loaded:
        load_dimension_defs()
