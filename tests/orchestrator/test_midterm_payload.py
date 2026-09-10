"""决策点 6(a)/7 展示边界 + recorder 摘要测试（Phase 3）。

覆盖：finalize_message 含 MIDTERM_DECISION 摘要；recorder 记 midterm 摘要且缺失不报错；
generate_report payload 不含 MIDTERM_DECISION/仓位（研究归研究、决策归决策）。

注意：``alphabee.orchestrator.agent`` import 时传递性触发 tushare ``set_token``
（无 token 写 ``$HOME/tk.csv``），workspace-write 沙箱下需在 import 前把 HOME 重定向到
workspace 内临时目录（与 test_midterm_routing.py 同一模式）。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

_TMP_HOME = Path(tempfile.mkdtemp(prefix="midterm_payload_home_"))
os.environ["HOME"] = str(_TMP_HOME)


def _cleanup_tmp_home() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


atexit.register(_cleanup_tmp_home)

from alphabee.core import Artifact, ArtifactType, Run, RunStatus  # noqa: E402
from alphabee.midterm.models import (  # noqa: E402
    CognitiveState,
    CompanyStateArtifact,
    EvidenceEvent,
    PositionDecision,
    StateBelief,
)
from alphabee.orchestrator.agent import finalize_message  # noqa: E402
from alphabee.orchestrator.services.payload_builders import (  # noqa: E402
    build_report_generation_payload,
)
from alphabee.task_records.recorder import TaskRecorder  # noqa: E402


def _company_state(symbol="600519.SH"):
    return CompanyStateArtifact(
        symbol=symbol,
        state=StateBelief(
            distribution={"S1": 0.2, "S2": 0.8},
            argmax_state=CognitiveState.S2_CONFIRM.value,
            entropy=0.3,
        ),
        thesis="看多核心观点",
        thesis_confidence=0.6,
        prior_confidence=0.5,
        evidence_log=[
            EvidenceEvent(
                id="ev-1",
                date="2025-01-01",
                kind="expectation",
                description="一致预期上修",
                effect_on_thesis="confirming",
                confidence_delta=0.1,
            ),
            EvidenceEvent(
                id="ev-2",
                date="2025-01-02",
                kind="fundamental",
                description="净利超预期",
                effect_on_thesis="confirming",
                confidence_delta=0.1,
            ),
        ],
        position=PositionDecision(position_band="加仓"),
    )


def _midterm_artifact(symbol="600519.SH"):
    return Artifact(
        id="a-midterm",
        type=ArtifactType.MIDTERM_DECISION,
        producer_step="resolve_midterm_decision",
        value=_company_state(symbol).model_dump(mode="json"),
    )


def _report_artifact():
    return Artifact(
        id="a-report",
        type=ArtifactType.REPORT,
        producer_step="generate_report",
        value={"title": "投资分析报告", "summary": "研究摘要", "sections": {}},
    )


def _run(symbol="600519.SH"):
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.SUCCEEDED,
        context={"symbol": symbol, "query": "分析贵州茅台"},
    )


# ── finalize_message ────────────────────────────────────────────────────────


def test_finalize_payload_includes_midterm_decision_summary():
    state = {
        "run": _run(),
        "artifacts": [_midterm_artifact(), _report_artifact()],
        "decisions": [],
        "issues": [],
    }
    result = finalize_message(state)
    message = result["messages"][0]
    payload = json.loads(message.content)

    assert payload["midterm_decision"] == {
        "state": "S2",
        "confidence": 0.6,
        "evidence_count": 2,
        "position_band": "加仓",
    }
    # 完整决策 payload 仍在 artifacts 列表里，供审计端读取
    assert any(a["type"] == ArtifactType.MIDTERM_DECISION.value for a in payload["artifacts"])


def test_finalize_payload_without_midterm_artifact_is_null():
    state = {"run": _run(), "artifacts": [_report_artifact()], "decisions": [], "issues": []}
    payload = json.loads(finalize_message(state)["messages"][0].content)
    assert payload["midterm_decision"] is None


# ── recorder.capture ────────────────────────────────────────────────────────


def test_recorder_captures_midterm_summary():
    recorder = TaskRecorder()
    record = recorder.capture(
        query="分析贵州茅台",
        symbol="600519.SH",
        flags={"enhance": False, "llm_review": False, "midterm": True},
        payload={"final_report": {"title": "报告"}, "issues": []},
        artifacts=[{"type": "midterm_decision", "value": _company_state().model_dump(mode="json")}],
        start_ts=0.0,
    )

    assert record.midterm_state == "S2"
    assert record.midterm_confidence == 0.6
    assert record.midterm_evidence_count == 2


def test_recorder_missing_midterm_artifact_does_not_error():
    recorder = TaskRecorder()
    record = recorder.capture(
        query="分析贵州茅台",
        symbol="600519.SH",
        flags={"enhance": False, "llm_review": False, "midterm": False},
        payload={"final_report": {"title": "报告"}, "issues": []},
        artifacts=[],
        start_ts=0.0,
    )

    assert record.midterm_state == ""
    assert record.midterm_confidence == 0.0
    assert record.midterm_evidence_count == 0


# ── generate_report 不含决策/仓位（决策点 6(a)）──────────────────────────────


def test_report_generation_payload_excludes_midterm_and_position():
    state = {
        "run": _run(),
        "artifacts": [_midterm_artifact(), _report_artifact()],
        "issues": [],
        "decisions": [],
    }
    payload = build_report_generation_payload(state)

    dumped = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
    assert "midterm" not in dumped
    assert "position_band" not in dumped
    assert not hasattr(payload, "midterm_decision")
