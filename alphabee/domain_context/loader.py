"""Domain Context 知识资产加载与目录闭合校验（DOMAIN_CONTEXT_ROADMAP P0 第 1 步）。

职责：
- 从 ``domain_primitives/*.yaml`` / ``domain_playbooks/*.yaml`` 加载并严格校验；
- 检测重复 id；
- 目录闭合校验（playbook 只能引用已声明的 primitive）。

业务逻辑（为什么要「目录闭合校验」）：
playbook 是「命名的 primitive 集合」，如果它引用了不存在的 primitive（如拼写错误
``feed_costt``、或删了某原语却没同步 playbook），下游展开时就会拿到「悬空引用」——
报告层展开 playbook 会发现少了一个框架积木却无从解释。闭合校验在**加载期**就把这类
"组合引用了不存在的积木"炸出来，保证 ``load_catalog()`` 返回的一定是自洽目录。

本模块不依赖任何数据源（无 import 副作用），可被 orchestrator / midterm 安全引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from alphabee.domain_context.schemas import (
    DomainSchemaBase,
    PlaybookSchema,
    PrimitiveSchema,
)

_PRIMITIVES_DIR = Path(__file__).parent / "domain_primitives"
_PLAYBOOKS_DIR = Path(__file__).parent / "domain_playbooks"


@dataclass(frozen=True)
class DomainContextCatalog:
    """加载并校验后的 domain context 目录（primitives + playbooks）。

    由 ``load_catalog()`` 产出；保证目录闭合校验已通过，供下游（ContextRouter、
    DriverProfile 组装、报告注入）安全消费。
    """

    primitives: dict[str, PrimitiveSchema]
    playbooks: dict[str, PlaybookSchema]


def _load_yaml_files[T: DomainSchemaBase](directory: Path, model_cls: type[T], kind: str) -> dict[str, T]:
    """扫描目录下所有 ``*.yaml``，严格校验为 ``model_cls``，按 id 去重。"""
    loaded: dict[str, T] = {}
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        model = model_cls.model_validate(raw)
        if model.id in loaded:
            raise ValueError(f"{kind} id 重复: '{model.id}'（文件 {path.name}）")
        loaded[model.id] = model
    return loaded


@lru_cache(maxsize=1)
def load_primitives() -> dict[str, PrimitiveSchema]:
    """加载全部分析原语（id → PrimitiveSchema）。"""
    return _load_yaml_files(_PRIMITIVES_DIR, PrimitiveSchema, "primitive")


@lru_cache(maxsize=1)
def load_playbooks() -> dict[str, PlaybookSchema]:
    """加载全部组合框架（id → PlaybookSchema）。"""
    return _load_yaml_files(_PLAYBOOKS_DIR, PlaybookSchema, "playbook")


def validate_closure(
    primitives: dict[str, PrimitiveSchema] | None = None,
    playbooks: dict[str, PlaybookSchema] | None = None,
) -> list[str]:
    """目录闭合校验：playbook 只能引用已声明的 primitive。

    Args:
        primitives: 覆盖默认加载的原语（None 时用 ``load_primitives()``）。
        playbooks: 覆盖默认加载的组合框架（None 时用 ``load_playbooks()``）。

    Returns:
        错误列表（空列表 = 校验通过）。每条错误形如
        ``playbook '<id>' 引用了未声明的 primitive '<ref>'``。
    """
    primitives = primitives if primitives is not None else load_primitives()
    playbooks = playbooks if playbooks is not None else load_playbooks()
    declared = set(primitives)
    errors: list[str] = []
    for playbook_id, playbook in playbooks.items():
        for ref in playbook.primitives:
            if ref not in declared:
                errors.append(f"playbook '{playbook_id}' 引用了未声明的 primitive '{ref}'")
    return errors


@lru_cache(maxsize=1)
def load_catalog() -> DomainContextCatalog:
    """加载并校验整个 domain context 目录。

    任一 primitive 引用了未声明的 primitive 时抛 ``ValueError``（保证下游拿到的一定是
    自洽的目录），否则返回 ``DomainContextCatalog``。
    """
    primitives = load_primitives()
    playbooks = load_playbooks()
    errors = validate_closure(primitives, playbooks)
    if errors:
        raise ValueError("domain_context 目录闭合校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
    return DomainContextCatalog(primitives=primitives, playbooks=playbooks)
