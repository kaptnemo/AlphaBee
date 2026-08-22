"""Primitive/Playbook schema 校验测试（DOMAIN_CONTEXT_ROADMAP P0 第 1 步）。"""

import pytest
from pydantic import ValidationError

from alphabee.domain_context.schemas import PlaybookSchema, PrimitiveSchema


def test_minimal_primitive():
    p = PrimitiveSchema(id="foo")
    assert p.id == "foo"
    assert p.version == 1
    assert p.key_variables == []
    assert p.causal_paths == []


def test_primitive_requires_id():
    with pytest.raises(ValidationError):
        PrimitiveSchema()


def test_primitive_rejects_unknown_field():
    # 字段拼写漂移（key_variable 少个 s）必须被 strict schema 拦截
    with pytest.raises(ValidationError):
        PrimitiveSchema(id="foo", key_variable=["x"])


def test_playbook_minimal():
    pb = PlaybookSchema(id="p", primitives=["a", "b"])
    assert pb.primitives == ["a", "b"]


def test_playbook_requires_id():
    with pytest.raises(ValidationError):
        PlaybookSchema(primitives=["a"])


def test_playbook_rejects_unknown_field():
    with pytest.raises(ValidationError):
        PlaybookSchema(id="p", primitive=["a"])  # 单数拼写错误
