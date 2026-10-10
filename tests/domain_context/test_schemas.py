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


def test_playbook_match_extension_defaults_empty():
    pb = PlaybookSchema(id="p")
    assert pb.match_sw_codes == []
    assert pb.match_segments == []
    assert pb.match_financial_structures == {}


def test_playbook_accepts_match_extension_fields():
    pb = PlaybookSchema(
        id="p",
        match_sw_codes=["801010.SI"],
        match_segments=["生猪养殖"],
        match_financial_structures={"inventory_ratio": {"gt": 0.25}},
    )
    assert pb.match_sw_codes == ["801010.SI"]
    assert pb.match_financial_structures["inventory_ratio"]["gt"] == 0.25


def test_playbook_rejects_unknown_match_extension_field():
    # extra="forbid" 仍然生效：拼错的匹配字段必须被拦，而不是静默变成"永不命中"
    with pytest.raises(ValidationError):
        PlaybookSchema(id="p", match_sw_code=["801010.SI"])
