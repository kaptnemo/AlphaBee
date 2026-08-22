"""Primitive/Playbook 加载与目录闭合校验测试（DOMAIN_CONTEXT_ROADMAP P0 第 1 步）。"""

import pytest

from alphabee.domain_context import (
    load_catalog,
    load_playbooks,
    load_primitives,
    validate_closure,
)
from alphabee.domain_context.schemas import PlaybookSchema, PrimitiveSchema


def test_catalog_loads_6_primitives_3_playbooks():
    cat = load_catalog()
    assert len(cat.primitives) == 6
    assert len(cat.playbooks) == 3


def test_expected_primitive_ids():
    assert set(load_primitives()) == {
        "commodity_cycle",
        "capacity_cycle",
        "cost_curve",
        "working_capital_stress",
        "biological_inventory",
        "project_delivery",
    }


def test_expected_playbook_ids():
    assert set(load_playbooks()) == {"hog_cycle", "mining_services", "generic_fundamental"}


def test_closure_passes_on_loaded_catalog():
    assert validate_closure() == []


def test_hog_cycle_primitive_set():
    playbooks = load_playbooks()
    assert playbooks["hog_cycle"].primitives == [
        "commodity_cycle",
        "biological_inventory",
        "cost_curve",
        "capacity_cycle",
    ]


def test_generic_fundamental_is_fallback():
    playbooks = load_playbooks()
    assert playbooks["generic_fundamental"].primitives == [
        "cost_curve",
        "capacity_cycle",
        "working_capital_stress",
    ]


def test_closure_detects_missing_primitive():
    primitives = {"a": PrimitiveSchema(id="a")}
    playbooks = {"pb": PlaybookSchema(id="pb", primitives=["a", "ghost"])}
    errors = validate_closure(primitives, playbooks)
    assert len(errors) == 1
    assert "ghost" in errors[0]
    assert "pb" in errors[0]


def test_duplicate_id_raises(tmp_path):
    from alphabee.domain_context import loader

    (tmp_path / "a.yaml").write_text("id: dup\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("id: dup\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        loader._load_yaml_files(tmp_path, PrimitiveSchema, "primitive")
