"""Q1-lite 报告模式测试：``report.mode`` → prompt / layout 选择与终端双布局。

覆盖：
- legacy 用现有 ``REPORT_GENERATOR_PROMPT``，生成的报告 JSON **不含** ``layout`` 键；
- ``memo_lite`` 用 ``REPORT_GENERATOR_PROMPT_MEMO``，报告 JSON 写 ``layout="memo"``；
- LLM 解析失败 / 调用异常的确定性降级与 LLM 路径同口径；
- 配置读取失败 fail-open 回 legacy；``ReportSettings`` 默认值；
- 旧 ``ReportOutput``（无 layout）可反序列化；
- 终端 memo 布局（主文六节 + 附录 A–F 前 800 字裁剪，常量可配置）与 legacy 旧标题。

全部用假模型（monkeypatch ``create_structured_model``），禁真实 LLM / 网络。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import alphabee.config as config_mod
from alphabee.agents.schemas import ReportOutput
from alphabee.apps.cli.colors import set_color_enabled
from alphabee.apps.cli.renderer import render_final_report
from alphabee.apps.cli.report_layout import APPENDIX_TITLES, MAIN_TITLES
from alphabee.config import ReportSettings
from alphabee.orchestrator import reporter
from alphabee.orchestrator.contracts import ReportGenerationPayload
from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT, REPORT_GENERATOR_PROMPT_MEMO

_SECTION_KEYS = (
    "executive_summary",
    "investment_viewpoint",
    "scenario_analysis",
    "key_metrics",
    "signal_analysis",
    "anomaly_detection",
    "conflict_analysis",
    "dimension_analysis",
    "company_track",
    "review_findings",
    "falsification_conditions",
    "risks",
    "disclaimer",
)


def _sections() -> dict[str, str]:
    return {key: f"{key}正文" for key in _SECTION_KEYS}


def _report_json(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "title": "测试报告",
        "sections": _sections(),
        "summary": "摘要",
        "risk_count": {"high": 1, "medium": 0, "low": 0, "blocked": 0},
        "overall_confidence": "medium",
        "disclosed_issue_ids": [],
    }
    report.update(overrides)
    return report


class _FakeModel:
    """最小假模型：记录收到的消息，返回固定文本（不做任何网络/LLM 调用）。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> SimpleNamespace:
        self.calls.append(messages)
        return SimpleNamespace(content=self.content)


class _RaisingModel:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def invoke(self, messages: list[Any]) -> SimpleNamespace:
        raise self.exc


def _patch_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """把 ``report.mode`` 固定为给定值（report 段其余字段取默认）。"""
    monkeypatch.setattr(config_mod, "get_settings", lambda: SimpleNamespace(report=ReportSettings(mode=mode)))


async def _generate(monkeypatch: pytest.MonkeyPatch, mode: str, content: str) -> tuple[dict[str, Any], _FakeModel]:
    fake = _FakeModel(content)
    monkeypatch.setattr(reporter, "create_structured_model", lambda *args, **kwargs: fake)
    _patch_mode(monkeypatch, mode)
    result = await reporter.generate_report({"artifacts": [], "issues": []}, {})
    return result, fake


def _artifact_value(result: dict[str, Any]) -> dict[str, Any]:
    value = result["artifacts"][0].value
    assert isinstance(value, dict)
    return value


# ═══════════════════════════════════════════════════════════════════
# 模式 → prompt / layout
# ═══════════════════════════════════════════════════════════════════


async def test_legacy_uses_legacy_prompt_and_omits_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    result, fake = await _generate(monkeypatch, "legacy", json.dumps(_report_json()))
    assert fake.calls, "假模型必须被调用"
    system_prompt = fake.calls[0][0].content
    assert system_prompt == REPORT_GENERATOR_PROMPT
    assert system_prompt != REPORT_GENERATOR_PROMPT_MEMO
    value = _artifact_value(result)
    assert "layout" not in value  # legacy 新生成报告 JSON 不写 layout 键
    assert value["sections"]["executive_summary"] == "executive_summary正文"
    assert result["issues"] == []


async def test_memo_lite_uses_memo_prompt_and_sets_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    result, fake = await _generate(monkeypatch, "memo_lite", json.dumps(_report_json()))
    system_prompt = fake.calls[0][0].content
    assert system_prompt == REPORT_GENERATOR_PROMPT_MEMO
    assert system_prompt != REPORT_GENERATOR_PROMPT
    value = _artifact_value(result)
    assert value["layout"] == "memo"
    assert value["sections"]["scenario_analysis"] == "scenario_analysis正文"


async def test_memo_lite_overrides_llm_supplied_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """布局由配置决定，不信任 LLM 在 JSON 里自带的值。"""
    result, _fake = await _generate(
        monkeypatch,
        "memo_lite",
        json.dumps(_report_json(layout="legacy")),
    )
    assert _artifact_value(result)["layout"] == "memo"


async def test_legacy_strips_llm_supplied_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _fake = await _generate(monkeypatch, "legacy", json.dumps(_report_json(layout="memo")))
    assert "layout" not in _artifact_value(result)


# ═══════════════════════════════════════════════════════════════════
# 降级 / fail-open
# ═══════════════════════════════════════════════════════════════════


async def test_legacy_parse_failure_falls_back_without_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _fake = await _generate(monkeypatch, "legacy", "not a json")
    value = _artifact_value(result)
    assert "layout" not in value
    assert "parse_error" in [issue.category for issue in result["issues"]]


async def test_memo_parse_failure_falls_back_with_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _fake = await _generate(monkeypatch, "memo_lite", "not a json")
    value = _artifact_value(result)
    assert value["layout"] == "memo"
    assert "parse_error" in [issue.category for issue in result["issues"]]


async def test_model_exception_falls_back_with_mode_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reporter,
        "create_structured_model",
        lambda *args, **kwargs: _RaisingModel(RuntimeError("boom")),
    )
    _patch_mode(monkeypatch, "memo_lite")
    result = await reporter.generate_report({"artifacts": [], "issues": []}, {})
    value = _artifact_value(result)
    assert value["layout"] == "memo"
    assert "subagent_failure" in [issue.category for issue in result["issues"]]


async def test_config_failure_fail_open_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken_settings() -> Any:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(config_mod, "get_settings", _broken_settings)
    fake = _FakeModel(json.dumps(_report_json()))
    monkeypatch.setattr(reporter, "create_structured_model", lambda *args, **kwargs: fake)
    result = await reporter.generate_report({"artifacts": [], "issues": []}, {})
    assert fake.calls[0][0].content == REPORT_GENERATOR_PROMPT
    assert "layout" not in _artifact_value(result)


def test_build_deterministic_report_layout_by_mode() -> None:
    payload = ReportGenerationPayload()
    legacy = reporter.build_deterministic_report(payload)
    assert "layout" not in legacy
    assert reporter.build_deterministic_report(payload, mode="legacy") == legacy

    memo = reporter.build_deterministic_report(payload, mode="memo_lite")
    assert memo["layout"] == "memo"
    assert memo["sections"] == legacy["sections"]  # 布局只改字段，不改内容

    unknown = reporter.build_deterministic_report(payload, mode="memo_claims")
    assert "layout" not in unknown  # 未知 mode fail-open 回 legacy


# ═══════════════════════════════════════════════════════════════════
# schema / 配置默认值
# ═══════════════════════════════════════════════════════════════════


def test_report_output_layout_additive_and_backward_compatible() -> None:
    old_payload = _report_json()  # 无 layout 字段的旧 JSON
    assert "layout" not in old_payload
    assert ReportOutput.model_validate(old_payload).layout == "legacy"
    assert ReportOutput.model_validate(_report_json(layout="memo")).layout == "memo"


def test_report_settings_defaults() -> None:
    settings = ReportSettings()
    assert settings.mode == "legacy"
    assert settings.capture_payload is False
    assert ReportSettings(mode="memo_lite").mode == "memo_lite"
    with pytest.raises(ValueError):
        ReportSettings(mode="memo")


def test_memo_prompt_contains_appendix_a_requirements() -> None:
    prompt = REPORT_GENERATOR_PROMPT_MEMO
    # 全局约束 + 主文六节（标题/预算/固定骨架）
    for marker in (
        "投资备忘录",
        "≤10000 字",
        "一页结论",
        "核心主张",
        "情景与触发",
        "我们可能错在哪里",
        "观察清单",
        "关键指标",
        "**主张**",
        "**依据**",
        "**推理**",
        "**反证**",
        "**限定**",
        "**失效条件**",
        "见附录",
        '不再写"与 core_view 的关系"',
    ):
        assert marker in prompt
    # 附录六节仍在 prompt 中（保留明细格式）
    for key in (
        "signal_analysis",
        "anomaly_detection",
        "conflict_analysis",
        "dimension_analysis",
        "company_track",
        "review_findings",
    ):
        assert key in prompt
    # 旧 prompt 逐字保留（未混入 memo 标题）
    assert "一页结论" not in REPORT_GENERATOR_PROMPT
    assert "【核心观点摘要】" in REPORT_GENERATOR_PROMPT


# ═══════════════════════════════════════════════════════════════════
# 终端双布局
# ═══════════════════════════════════════════════════════════════════


def _render(capsys: pytest.CaptureFixture[str], payload: dict[str, Any], **kwargs: Any) -> str:
    set_color_enabled(False)
    try:
        render_final_report(payload, **kwargs)
    finally:
        set_color_enabled(True)
    return capsys.readouterr().out


def test_terminal_memo_prints_main_sections_and_clips_appendix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sections = _sections()
    sections["signal_analysis"] = "A" * 900
    report = _report_json(layout="memo", sections=sections)
    out = _render(capsys, {"final_report": report, "issues": []})

    for title in (*MAIN_TITLES.values(), *APPENDIX_TITLES.values()):
        assert title in out
    assert "A" * 800 in out
    assert "A" * 900 not in out  # 附录被裁剪到 800 字
    assert "核心观点摘要" not in out  # memo 不打印 legacy 标题
    assert "公司赛道与对标组" in out  # memo 下 company_track 作为附录 E 打印


def test_terminal_appendix_clip_is_configurable(capsys: pytest.CaptureFixture[str]) -> None:
    sections = _sections()
    sections["signal_analysis"] = "A" * 900
    report = _report_json(layout="memo", sections=sections)
    out = _render(capsys, {"final_report": report, "issues": []}, appendix_clip_chars=16)
    assert "A" * 16 in out
    assert "A" * 17 not in out


def test_terminal_legacy_keeps_current_titles_and_skips_company_track(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report_json()  # 无 layout ⇒ legacy
    out = _render(capsys, {"final_report": report, "issues": []})
    assert "核心观点摘要" in out
    assert "一页结论" not in out
    # 旧终端从未打印 company_track：legacy 保持该行为
    assert "公司赛道与对标组" not in out


def test_terminal_memo_keeps_issues_after_sections(capsys: pytest.CaptureFixture[str]) -> None:
    """本步不做折叠：memo 下系统问题仍在章节之后（折叠由下一步负责）。"""
    report = _report_json(layout="memo")
    out = _render(capsys, {"final_report": report, "issues": [{"severity": "high", "category": "x", "message": "m"}]})
    assert out.index("一页结论") < out.index("附录 F 审查发现") < out.index("系统问题")


# ── t5 review F1：legacy 终端按 HEAD 原值语义（不 strip / 不额外跳过）────────


def test_terminal_legacy_head_semantics_for_padded_and_whitespace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sections = _sections()
    sections["executive_summary"] = "\n\n  正文  \n"
    sections["scenario_analysis"] = " 未生成情景分析。 "  # padded 占位符：HEAD 原值比较 ⇒ 打印
    sections["signal_analysis"] = "   "  # 纯空白：HEAD `if value` 为真 ⇒ 打印标题
    sections["disclaimer"] = "\n免责声明\n"
    sections["investment_viewpoint"] = "  " + "A" * 3000  # 原值 >3000 ⇒ 按原值截断
    report = _report_json(sections=sections)  # 无 layout ⇒ legacy
    out = _render(capsys, {"final_report": report, "issues": []})

    # padded known section：按原值打印（旧实现 exec 保留两空格缩进前缀）
    assert "  \n\n  正文  \n" in out
    # padded placeholder：不 strip ⇒ 值 != 占位符 ⇒ 标题 + 原值都打印
    assert "情景分析" in out
    assert " 未生成情景分析。 " in out
    # 纯空白 section：HEAD 视为非空 ⇒ 标题打印
    assert "勾稽关系异常" in out
    # padded disclaimer：按原值打印（含首尾换行）
    assert "\n免责声明\n\n" in out
    # max_chars 按原值截断：raw = 2 空格 + 3000 A → 截到 3000 = 2 空格 + 2998 A
    assert "  " + "A" * 2998 in out
    assert "A" * 3000 not in out
