"""`check_message_limit` 消息阈值的可配置性用例（默认 50，默认行为不变）。

覆盖 docs/design/DRIVER_PROFILE_D0_CODING_DESIGN.md §4.3：
- 默认阈值 50：49 条不触发、50 条触发（与既有权值行为逐字一致）；
- 注入 ``message_limit``：按注入值生效（9 条不触发、10 条触发）；
- 缺该配置项 ⇒ 回落 ``DEFAULT_MESSAGE_LIMIT``；
- ``config.yaml`` / ``config.yaml.example`` 与模型默认值同源；
- 6 个共用该 middleware 的 agent 工厂仍将其接在 middleware 列表里。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
import yaml
from langchain_core.messages import HumanMessage

from alphabee import PROJECT_ROOT
from alphabee.config import Settings
from alphabee.middleware.common import DEFAULT_MESSAGE_LIMIT, check_message_limit

_AGENT_FACTORIES = [
    ("alphabee.agents.facts.agent", "fact_collector_agent_factory"),
    ("alphabee.agents.derived_facts.agent", "derived_fact_agent_factory"),
    ("alphabee.agents.signal.agent", "signal_agent_factory"),
    ("alphabee.agents.insights.agent", "insight_agent_factory"),
    ("alphabee.agents.explore_conflicts.agent", "explore_conflicts_agent_factory"),
    ("alphabee.agents.verify_hypotheses.agent", "verify_hypotheses_agent_factory"),
]


def _state(count: int) -> dict:
    return {"messages": [HumanMessage(content=f"m{i}") for i in range(count)]}


def _invoke(count: int) -> dict | None:
    return check_message_limit.before_model(_state(count), None)


def test_default_limit_constant_matches_settings_default() -> None:
    assert DEFAULT_MESSAGE_LIMIT == 50
    assert Settings.model_fields["message_limit"].default == DEFAULT_MESSAGE_LIMIT


@pytest.mark.parametrize("file_name", ["config.yaml", "config.yaml.example"])
def test_config_files_declare_default_message_limit(file_name: str) -> None:
    """两个配置文件都显式登记默认值，避免与代码默认值漂移（config.yaml 为本地配置，不入库）。"""
    path = PROJECT_ROOT / file_name
    if not path.exists():
        pytest.skip(f"{file_name} 不存在（本地配置不入库）")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["message_limit"] == DEFAULT_MESSAGE_LIMIT


def test_default_boundary_49_messages_not_triggered() -> None:
    assert _invoke(49) is None


def test_default_boundary_50_messages_triggers_end_jump() -> None:
    result = _invoke(50)
    assert result is not None
    assert result["jump_to"] == "end"
    assert [message.content for message in result["messages"]] == ["Conversation limit reached."]


@pytest.mark.parametrize(("count", "triggered"), [(9, False), (10, True)])
def test_injected_limit_is_respected(monkeypatch, count: int, triggered: bool) -> None:
    monkeypatch.setattr("alphabee.config.get_settings", lambda: SimpleNamespace(message_limit=10))
    assert (_invoke(count) is not None) is triggered


@pytest.mark.parametrize(("count", "triggered"), [(49, False), (50, True)])
def test_missing_setting_falls_back_to_default(monkeypatch, count: int, triggered: bool) -> None:
    """配置对象缺 ``message_limit`` 属性时回落到默认 50（``getattr`` 兜底）。"""
    monkeypatch.setattr("alphabee.config.get_settings", lambda: SimpleNamespace())
    assert (_invoke(count) is not None) is triggered


def test_check_message_limit_prints_nothing(capsys) -> None:
    assert _invoke(3) is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("module_name", "factory_name"), _AGENT_FACTORIES)
def test_agent_factory_keeps_message_limit_middleware(monkeypatch, tmp_path, module_name, factory_name) -> None:
    """6 个 agent 工厂构造路径不回归：middleware 列表里仍是同一个 ``check_message_limit``。

    LLM 与 deepagents 组装被 stub 掉（真实构造需要 API key 的集成环境）；
    这里钉住的是「工厂接线未变」这一行为面。
    """
    monkeypatch.setenv("HOME", str(tmp_path))  # tushare set_token 会写 ~/tk.csv
    module = importlib.import_module(module_name)
    captured: dict = {}

    def _fake_create_deep_agent(**kwargs: object) -> str:
        captured.update(kwargs)
        return "fake-agent"

    monkeypatch.setattr(module, "create_chat_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "create_deep_agent", _fake_create_deep_agent)

    assert getattr(module, factory_name)() == "fake-agent"
    assert check_message_limit in captured["middleware"]
