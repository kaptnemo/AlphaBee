"""``compute_params_hash`` 测试（P0；设计 §4.7）。

性质（验收 P-6）：

- 同值两次同哈希；改值变哈希；改 registry 的 status/notes 不影响哈希；
- 16 位小写十六进制；算法与设计 §4.7 逐字一致（固定向量 + 手算 payload 双重钉住）。

全部离线、纯函数；registry 用临时 YAML，配置读取 monkeypatch。
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import alphabee.config as config_mod
from alphabee.params import (
    DEFAULT_EVIDENCE_PARAMS,
    DEFAULT_REGISTRY_PATH,
    DEFAULT_SCORE_PARAMS,
    compute_params_hash,
    load_registry,
    resolve_parameters,
)

#: 默认参数（15 值全等于模块常量）的固定指纹。算法/默认值任一改动都会让它变化。
EXPECTED_DEFAULT_HASH = "4b4521f2cddfe676"

_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _registry_dict() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def _write_registry(tmp_path: Path, mutate) -> Path:
    data = copy.deepcopy(_registry_dict())
    mutate(data)
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _set_entry(data: dict[str, Any], param_id: str, **fields: Any) -> None:
    for entry in data["entries"]:
        if entry["id"] == param_id:
            entry.update(fields)
            return
    raise AssertionError(f"registry 中不存在 {param_id}")


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(parameters=SimpleNamespace(profile="default", overrides={}))
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake)


def test_default_hash_is_16_lowercase_hex() -> None:
    digest = compute_params_hash(DEFAULT_SCORE_PARAMS, DEFAULT_EVIDENCE_PARAMS)
    assert _HEX16.match(digest)
    assert digest == EXPECTED_DEFAULT_HASH


def test_hash_stable_for_same_values() -> None:
    first = compute_params_hash(DEFAULT_SCORE_PARAMS, DEFAULT_EVIDENCE_PARAMS)
    second = compute_params_hash(DEFAULT_SCORE_PARAMS, DEFAULT_EVIDENCE_PARAMS)
    assert first == second


def test_changing_a_value_changes_hash() -> None:
    changed_score = replace(DEFAULT_SCORE_PARAMS, beat_bonus=0.1)
    assert compute_params_hash(changed_score, DEFAULT_EVIDENCE_PARAMS) != EXPECTED_DEFAULT_HASH
    changed_evidence = replace(DEFAULT_EVIDENCE_PARAMS, temperature=0.3)
    assert compute_params_hash(DEFAULT_SCORE_PARAMS, changed_evidence) != EXPECTED_DEFAULT_HASH


def test_status_and_notes_do_not_affect_hash(tmp_path: Path) -> None:
    # 只改注册表元数据（status / notes / review_condition），生效值不变 ⇒ 指纹不变。
    path = _write_registry(
        tmp_path,
        lambda data: (
            _set_entry(data, "midterm.evidence.temperature", status="placeholder"),
            _set_entry(data, "midterm.score.beat_bonus", notes="改注释不改值"),
            _set_entry(data, "midterm.score.beat_bonus", review_condition="任意复核条件"),
        ),
    )
    assert "改注释不改值" in load_registry(path)[2].notes  # 证明临时注册表确实被改写
    assert load_registry(path)[11].status == "placeholder"

    result = resolve_parameters(registry_path=path)
    assert result.params_hash == EXPECTED_DEFAULT_HASH
    assert compute_params_hash(result.score, result.evidence) == EXPECTED_DEFAULT_HASH


def test_override_changes_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(
        parameters=SimpleNamespace(profile="default", overrides={"midterm.score.beat_bonus": 0.1}),
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake)
    result = resolve_parameters()
    assert result.params_hash != EXPECTED_DEFAULT_HASH
    assert result.params_hash == compute_params_hash(result.score, result.evidence)


def test_hash_matches_documented_algorithm() -> None:
    """手算设计 §4.7 的 payload，逐字对照实现（不只对照自己算的哈希）。"""
    score_values = {
        "fundamental_yoy_scale": 30.0,
        "ocf_negative_penalty": 0.5,
        "beat_bonus": 0.2,
        "miss_penalty": 0.3,
        "miss_gap_threshold": 20.0,
        "segment_divergence_threshold": 15.0,
        "segment_divergence_bonus": 0.2,
        "f_marginal_weight": 0.30,
        "anchor_weight": 0.6,
        "research_weight": 0.4,
        "min_consensus_coverage": 3,
    }
    evidence_values = {
        "s0_baseline": 0.30,
        "s0_floor": 0.10,
        "w_e": 0.60,
        "w_t": 0.80,
        "w_f": 1.00,
        "severe_lo": 0.25,
        "severe_hi": 0.60,
        "break_lambda": 2.0,
        "temperature": 0.22,
        "uncertain_p_top": 0.55,
    }
    values = {
        **{f"midterm.score.{key}": value for key, value in score_values.items()},
        **{f"midterm.evidence.{key}": value for key, value in evidence_values.items()},
    }
    payload = "\n".join(f"{key}={float(value):.12g}" for key, value in sorted(values.items()))
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    assert compute_params_hash(DEFAULT_SCORE_PARAMS, DEFAULT_EVIDENCE_PARAMS) == expected


def test_hash_is_insensitive_to_report_quality_params() -> None:
    # 报告阈值不进帧指纹（设计 §4.7）：函数签名只接受 score + evidence，本用例显式钉住这一点。
    import inspect

    signature = inspect.signature(compute_params_hash)
    assert list(signature.parameters) == ["score", "evidence"]
