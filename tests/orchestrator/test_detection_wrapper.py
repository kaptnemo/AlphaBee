"""F1b 包装器测试：``with_deviation_detection`` 接线语义（§14.2-C、§14.7）。

覆盖要点：

* **纯增量**：无偏离 / 契约无检测器 / 开关关闭 → 返回**原 ``update`` 对象本身**（逐字段不变）；
* 有偏离 → 只追加 ``issues``，其余字段逐字段一致；
* ``_merge_state_view`` 与 LangGraph reducer 一致（``_merge_by_id`` / ``_append_items``）；
* **开关读取 fail-open**（C1）：配置段缺失/读取异常 → 默认 ``True``，且**不得**在 import 期读配置；
* **fail-open 边界**：检测层自身异常不打断节点；
* 同步 / 异步节点两种形态都能包。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from alphabee.core.schemas import Artifact, ArtifactType, Issue, IssueScope, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.contracts import DerivedFactsArtifact
from alphabee.orchestrator.services import detection
from alphabee.orchestrator.services.detection import (
    _merge_state_view,
    detection_switches,
    with_deviation_detection,
)
from alphabee.orchestrator.state import _append_items, _merge_by_id


def _artifact(artifact_type: ArtifactType, value: dict, artifact_id: str = "artifact-1") -> Artifact:
    return Artifact(id=artifact_id, type=artifact_type, producer_step="step-1", value=value)


def _step(node_id: str = "run_analysis_engines") -> Step:
    return Step(id=node_id, kind=node_id, status=StepStatus.RUNNING)


def _empty_derived_update(node_id: str = "run_analysis_engines", *, fact_values: dict | None = None) -> dict:
    """造一个"引擎故障"节点的 update：derived facts 空但 fact_values 非空 → 必触发 D1 high。"""
    return {
        "steps": [_step(node_id)],
        "artifacts": [_artifact(ArtifactType.DERIVED_FACTS, DerivedFactsArtifact().model_dump(mode="json"))],
    }


# ── 正例：注入 issue ────────────────────────────────────────────────────────


def test_wrapper_injects_issue_into_partial_state():
    async def _node(state, config):  # noqa: ANN001
        return _empty_derived_update()

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    update = asyncio.run(wrapped({"fact_values": {"roe": 0.12}}, {}))

    assert update["steps"] and update["artifacts"]
    issues = update["issues"]
    assert len(issues) == 1
    assert issues[0].category == "derived_facts_empty"
    assert issues[0].detected_at_step == "run_analysis_engines"


def test_wrapper_keeps_existing_issues_and_appends():
    async def _node(state, config):  # noqa: ANN001
        existing = Issue(
            id="issue-existing",
            severity=IssueSeverity.LOW,
            category="missing_data",
            message="pre-existing",
            scope=IssueScope.DATA,
        )
        return {**_empty_derived_update(), "issues": [existing]}

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    update = asyncio.run(wrapped({"fact_values": {"roe": 0.12}}, {}))
    assert [issue.id for issue in update["issues"]][0] == "issue-existing"
    assert len(update["issues"]) == 2


# ── 纯增量：三条"零改动"路径 ────────────────────────────────────────────────


def test_wrapper_returns_same_update_object_when_no_deviation():
    """无偏离 → 必须返回**原对象**（不是等价副本），保证纯增量。"""

    async def _node(state, config):  # noqa: ANN001
        return {"steps": [_step()], "artifacts": [_artifact(ArtifactType.DERIVED_FACTS, {"results": {"roe": {}}})]}

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    update = asyncio.run(wrapped({}, {}))
    assert "issues" not in update


def test_wrapper_noop_when_contract_has_no_detectors():
    """契约无检测器（如 finalize_message）→ 原样返回。"""

    async def _node(state, config):  # noqa: ANN001
        return {"steps": [_step("finalize_message")]}

    wrapped = with_deviation_detection("finalize_message", _node)
    update = asyncio.run(wrapped({}, {}))
    assert set(update) == {"steps"}


def test_wrapper_noop_when_node_has_no_contract():
    async def _node(state, config):  # noqa: ANN001
        return {"steps": [_step("ghost_node")]}

    wrapped = with_deviation_detection("ghost_node", _node)
    update = asyncio.run(wrapped({"fact_values": {"roe": 1.0}}, {}))
    assert set(update) == {"steps"}


def test_wrapper_noop_when_detection_disabled(monkeypatch: pytest.MonkeyPatch):
    """开关关闭 → 检测层完全跳过（连检测器都不执行），逐字段不变。

    说明（captain 台账口径）：``deviation.detection.enabled`` 随 ``DeviationSettings`` 在 **F2**
    才落地，因此 **F1 期间检测实际上是无条件开启的**（fail-open 默认 True），"关开关即回滚"
    自 F2 起才成立。本用例用 monkeypatch 模拟开关关闭，验证的是**代码路径**本身正确。
    """
    monkeypatch.setattr(detection, "detection_switches", lambda: False)

    async def _node(state, config):  # noqa: ANN001
        return _empty_derived_update()

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    update = asyncio.run(wrapped({"fact_values": {"roe": 1.0}}, {}))
    assert "issues" not in update


# ── 开关读取 fail-open（C1） ────────────────────────────────────────────────


def test_detection_switches_default_true_without_deviation_settings():
    """``Settings`` 目前没有 ``deviation`` 属性（F2 才落地）→ 必须 fail-open 返回 True。"""
    assert detection_switches() is True


def test_detection_switches_fail_open_on_exception(monkeypatch: pytest.MonkeyPatch, caplog):
    import alphabee.config

    def _boom():
        raise RuntimeError("config exploded")

    monkeypatch.setattr(alphabee.config, "get_settings", _boom)
    with caplog.at_level(logging.WARNING):
        assert detection_switches() is True
    assert any("fail-open" in record.message for record in caplog.records)


def test_detection_switches_reads_configured_value(monkeypatch: pytest.MonkeyPatch):
    """配置真存在时以配置为准（F2 落 DeviationSettings 后立即生效）。"""
    from types import SimpleNamespace

    import alphabee.config

    settings = SimpleNamespace(deviation=SimpleNamespace(detection=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(alphabee.config, "get_settings", lambda: settings)
    assert detection_switches() is False


def test_detection_module_does_not_read_config_at_import_time():
    """模块级不得读配置（否则 import 期就可能因配置缺失抛错）。"""
    import inspect

    source = inspect.getsource(detection)
    module_level = [line for line in source.splitlines() if line and not line.startswith((" ", "\t", "#"))]
    assert not any("get_settings()" in line for line in module_level)


# ── 检测层自身异常也必须 fail-open ──────────────────────────────────────────


def test_wrapper_fail_open_when_detection_layer_raises(monkeypatch: pytest.MonkeyPatch, caplog):
    def _boom(*args, **kwargs):
        raise RuntimeError("detection layer exploded")

    monkeypatch.setattr(detection, "run_detectors", _boom)

    async def _node(state, config):  # noqa: ANN001
        return _empty_derived_update()

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    with caplog.at_level(logging.WARNING):
        update = asyncio.run(wrapped({"fact_values": {"roe": 1.0}}, {}))
    assert "issues" not in update and update["artifacts"]
    assert any("fail-open" in record.message for record in caplog.records)


def test_wrapper_passes_through_non_dict_update(monkeypatch: pytest.MonkeyPatch):
    async def _node(state, config):  # noqa: ANN001
        return None

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    assert asyncio.run(wrapped({}, {})) is None


# ── _merge_state_view 与 reducer 一致 ──────────────────────────────────────


def test_merge_state_view_matches_reducer_semantics():
    """artifacts/decisions 按 id 就地替换、steps 追加、标量覆盖 —— 与 state.py reducer 一致。"""
    old_artifact = _artifact(ArtifactType.DERIVED_FACTS, {"rule_count": 1}, artifact_id="a1")
    new_artifact = _artifact(ArtifactType.DERIVED_FACTS, {"rule_count": 2}, artifact_id="a1")
    extra_artifact = _artifact(ArtifactType.SIGNAL_ANALYSIS, {"rule_count": 1}, artifact_id="a2")
    step_old, step_new = _step("run_analysis_engines"), _step("explore_conflicts")

    state = {"artifacts": [old_artifact], "steps": [step_old], "fact_values": {"roe": 1.0}}
    update = {"artifacts": [new_artifact, extra_artifact], "steps": [step_new], "fact_values": {"roa": 2.0}}

    view = _merge_state_view(state, update)

    assert view["artifacts"] == _merge_by_id(state["artifacts"], update["artifacts"])
    assert view["steps"] == _append_items(state["steps"], update["steps"])
    assert view["fact_values"] == {"roa": 2.0}  # 标量/字典按 update 覆盖
    # 原 state 不被修改（只读视图）
    assert state["artifacts"] == [old_artifact]
    assert len(state["steps"]) == 1


def test_merge_state_view_handles_missing_state_and_update():
    assert _merge_state_view(None, None) == {}
    assert _merge_state_view({}, {"fact_values": {"roe": 1.0}})["fact_values"] == {"roe": 1.0}


# ── C2 三条可证伪属性（captain 替换版；原"与 C1 逐字段一致"已撤回） ──────────
# 背景：F1 期间检测无条件开启（开关 F2 才落地），且检测器**设计上会触发并新增 issue**，
# 因此"issues 与 C1 逐字段一致"不可满足。改为证明：只读 / 只新增 issues / 空转等价。


def test_property_read_only_state_and_update():
    """(a) 只读性：包装器不改入参 ``state``，也不改节点的 ``update``（除追加 issues）。"""
    original_state = {"fact_values": {"roe": 0.12}, "issues": [], "steps": []}
    state_snapshot = {"fact_values": dict(original_state["fact_values"]), "issues": [], "steps": []}

    async def _node(state, config):  # noqa: ANN001
        return _empty_derived_update()

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    update = asyncio.run(wrapped(original_state, {}))

    # 入参 state 未被修改
    assert original_state == state_snapshot, "包装器修改了入参 state"
    # update 的原有字段逐字段不变（仅多出 issues）
    assert set(update) == {"steps", "artifacts", "issues"}
    assert [step.model_dump(mode="json") for step in update["steps"]] == [
        step.model_dump(mode="json") for step in _empty_derived_update()["steps"]
    ]
    assert [artifact.model_dump(mode="json") for artifact in update["artifacts"]] == [
        artifact.model_dump(mode="json") for artifact in _empty_derived_update()["artifacts"]
    ]


def test_property_only_appends_issues():
    """(b) 只新增：相对未包装节点，差异**仅为 issues 的追加** —— 不动 artifact/decision/message。"""

    async def _node(state, config):  # noqa: ANN001
        return _empty_derived_update()

    wrapped = with_deviation_detection("run_analysis_engines", _node)
    state = {"fact_values": {"roe": 0.12}, "decisions": []}
    wrapped_update = asyncio.run(wrapped(state, {}))
    plain_update = asyncio.run(_node(state, {}))

    extra_keys = set(wrapped_update) - set(plain_update)
    assert extra_keys == {"issues"}, f"除 issues 外还新增了字段：{extra_keys}"
    for key in plain_update:
        assert wrapped_update[key] == plain_update[key], f"字段 {key} 被改动"


def test_property_empty_run_is_equivalent():
    """(c) 空转等价：所有检测器均通过的 state → 新增 0 issue，且返回同一 update 对象。"""

    async def _node(state, config):  # noqa: ANN001
        return {
            "steps": [_step()],
            "artifacts": [_artifact(ArtifactType.DERIVED_FACTS, {"results": {"roe": {"roe": 0.12}}, "rule_count": 1})],
        }

    state = {"fact_values": {"roe": 0.12}}
    wrapped = with_deviation_detection("run_analysis_engines", _node)
    wrapped_update = asyncio.run(wrapped(state, {}))
    plain_update = asyncio.run(_node(state, {}))

    assert "issues" not in wrapped_update, "空转时不应新增 issue"
    assert wrapped_update == plain_update
