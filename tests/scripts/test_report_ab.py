"""``scripts/report_ab.py`` 离线 A/B 测试。

覆盖：同一 payload 双模式端到端（假模型）、``<mode>.json/.md`` + ``metrics.json``、
legacy/memo 布局差异、模型参数一致、路径守卫（工作区外拒绝）、降级标注、非法输入与
``--help``。全部离线：不联网、不写 ``data/``、模型调用全部 monkeypatch。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alphabee.orchestrator import reporter
from alphabee.orchestrator.contracts import ReportGenerationPayload
from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT, REPORT_GENERATOR_PROMPT_MEMO

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "report_ab.py"

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


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("report_ab_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report_ab = _load_module()


def _report_json() -> dict[str, Any]:
    return {
        "title": "A/B 测试报告",
        "sections": {key: f"{key}正文 10.07%" for key in _SECTION_KEYS},
        "summary": "摘要",
        "risk_count": {},
        "overall_confidence": "medium",
        "disclosed_issue_ids": [],
    }


def _write_payload(tmp_path: Path) -> Path:
    path = tmp_path / "payload.json"
    path.write_text(
        json.dumps(ReportGenerationPayload().model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class _ModeAwareFakeModel:
    """按 system prompt 返回 legacy / memo 版报告 JSON（不联网、不调真实模型）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> SimpleNamespace:
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("fake model failure")
        report = _report_json()
        if messages[0].content == REPORT_GENERATOR_PROMPT_MEMO:
            report["layout"] = "memo"
        return SimpleNamespace(content=json.dumps(report, ensure_ascii=False))


def _patch_model(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    factory_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _factory(*args: Any, **kwargs: Any) -> _ModeAwareFakeModel:
        factory_calls.append((args, kwargs))
        return _ModeAwareFakeModel(fail=fail)

    monkeypatch.setattr(reporter, "create_structured_model", _factory)
    return factory_calls


def test_report_ab_end_to_end_two_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _write_payload(tmp_path)
    out = tmp_path / "ab"
    monkeypatch.setattr(report_ab, "WORKSPACE_ROOT", tmp_path)
    factory_calls = _patch_model(monkeypatch)

    code = report_ab.main(["--payload", str(payload), "--out", str(out)])

    assert code == 0
    assert sorted(path.name for path in out.iterdir()) == [
        "legacy.json",
        "legacy.md",
        "memo_lite.json",
        "memo_lite.md",
        "metrics.json",
    ]
    legacy = json.loads((out / "legacy.json").read_text(encoding="utf-8"))
    memo = json.loads((out / "memo_lite.json").read_text(encoding="utf-8"))
    assert "layout" not in legacy
    assert memo["layout"] == "memo"

    legacy_md = (out / "legacy.md").read_text(encoding="utf-8")
    memo_md = (out / "memo_lite.md").read_text(encoding="utf-8")
    assert "## 核心观点摘要" in legacy_md
    assert "## 一页结论" in memo_md
    assert "## 核心观点摘要" not in memo_md

    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    for mode in ("legacy", "memo_lite"):
        assert {"main_chars", "appendix_chars", "numeric_tokens", "section_count", "degraded"} <= set(metrics[mode])
        assert metrics[mode]["degraded"] is False
        assert metrics[mode]["section_count"] == 13
    assert metrics["legacy"]["appendix_chars"] == 0
    assert metrics["memo_lite"]["appendix_chars"] > 0
    assert metrics["legacy"]["numeric_tokens"] > 0
    # 同一 payload、同一模型参数
    assert factory_calls == [
        (("agent.report",), {"max_tokens": 8192}),
        (("agent.report",), {"max_tokens": 8192}),
    ]


def test_report_ab_single_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _write_payload(tmp_path)
    out = tmp_path / "ab-single"
    monkeypatch.setattr(report_ab, "WORKSPACE_ROOT", tmp_path)
    _patch_model(monkeypatch)

    assert report_ab.main(["--payload", str(payload), "--modes", "legacy", "--out", str(out)]) == 0
    assert sorted(path.name for path in out.iterdir()) == ["legacy.json", "legacy.md", "metrics.json"]
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert "memo_lite" not in metrics


def test_report_ab_failure_degrades_and_marks_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _write_payload(tmp_path)
    out = tmp_path / "ab-degraded"
    monkeypatch.setattr(report_ab, "WORKSPACE_ROOT", tmp_path)
    _patch_model(monkeypatch, fail=True)

    assert report_ab.main(["--payload", str(payload), "--modes", "legacy", "--out", str(out)]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["legacy"]["degraded"] is True
    assert "RuntimeError: fake model failure" in metrics["legacy"]["error"]
    markdown = (out / "legacy.md").read_text(encoding="utf-8")
    assert "确定性降级版" in markdown


def test_report_ab_rejects_outside_workspace(tmp_path: Path) -> None:
    payload = _write_payload(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    report_ab.WORKSPACE_ROOT = workspace
    try:
        code = report_ab.main(["--payload", str(payload), "--modes", "legacy", "--out", str(outside)])
    finally:
        report_ab.WORKSPACE_ROOT = _REPO_ROOT
    assert code == 2
    assert not outside.exists()


def test_report_ab_rejects_invalid_modes_and_payload(tmp_path: Path) -> None:
    payload = _write_payload(tmp_path)
    assert report_ab.main(["--payload", str(payload), "--modes", "bogus", "--out", str(tmp_path)]) == 2
    assert report_ab.main(["--payload", str(tmp_path / "missing.json"), "--out", str(tmp_path)]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert report_ab.main(["--payload", str(bad), "--out", str(tmp_path)]) == 2


def test_report_ab_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        report_ab.main(["--help"])
    assert excinfo.value.code == 0


def test_report_ab_same_prompt_selection_per_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """legacy 用旧 prompt、memo_lite 用 memo prompt（模型工厂被调用两次）。"""
    payload = _write_payload(tmp_path)
    out = tmp_path / "ab-prompts"
    monkeypatch.setattr(report_ab, "WORKSPACE_ROOT", tmp_path)
    models: list[_ModeAwareFakeModel] = []

    def _factory(*args: Any, **kwargs: Any) -> _ModeAwareFakeModel:
        model = _ModeAwareFakeModel()
        models.append(model)
        return model

    monkeypatch.setattr(reporter, "create_structured_model", _factory)
    assert report_ab.main(["--payload", str(payload), "--out", str(out)]) == 0
    assert [model.calls[0][0].content for model in models] == [
        REPORT_GENERATOR_PROMPT,
        REPORT_GENERATOR_PROMPT_MEMO,
    ]
