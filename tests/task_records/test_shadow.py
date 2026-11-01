"""TaskRecord → 影子观测帧适配的单元测试（F-Marginal design §5.2 / §7.1 G-8）。

覆盖：``midterm`` 存在/缺失、帧形状与 artifact dump 一致、同日多帧交由
``dedupe_frames`` 处理、以及适配结果可直接被 ``shadow_report`` 消费。
全部合成对象，不读真实 ``data/`` 语料、不联网。
"""

from __future__ import annotations

from alphabee.midterm.marginal_shadow import DEDUPE_SYMBOL_DAY, dedupe_frames, shadow_report
from alphabee.midterm.models import (
    CompanyStateArtifact,
    FactorSnapshot,
    FundamentalFactor,
    StateBelief,
    VariableScores,
)
from alphabee.task_records.models import TaskRecord
from alphabee.task_records.shadow import load_task_record_frames


def _artifact(*, as_of_date: str = "2026-10-30") -> CompanyStateArtifact:
    return CompanyStateArtifact(
        symbol="600760.SH",
        as_of_date=as_of_date,
        thesis="H",
        f_marginal_mode="shadow",
        state=StateBelief(distribution={"S4": 0.6}, argmax_state="S4", entropy=0.3),
        variable_scores=VariableScores(f_fundamental_trend=-0.4933, e_revision=0.1, t_relative_strength=0.2),
        factor_snapshot=FactorSnapshot(
            symbol="600760.SH",
            as_of_date=as_of_date,
            fundamental=FundamentalFactor(
                score_breakdown={
                    "f": -0.4933,
                    "marginal": 0.096,
                    "f_shadow": -0.45,
                    "f_shadow_delta": 0.0433,
                    "f_marginal_source": 7,
                }
            ),
        ),
    )


def _record(*, midterm: CompanyStateArtifact | None, timestamp: str = "2026-10-30T10:00:00") -> TaskRecord:
    return TaskRecord(task_id=f"task-{timestamp}", query="q", timestamp=timestamp, midterm=midterm)


def test_load_task_record_frames_adapts_midterm_and_counts_missing():
    artifact = _artifact()
    frames, skipped = load_task_record_frames([_record(midterm=artifact), _record(midterm=None)])

    assert skipped == 1  # midterm=None（旧记录 / 未启用 midterm）只计数不抛
    assert frames == [artifact.model_dump(mode="json")]
    assert frames[0]["f_marginal_mode"] == "shadow"
    assert frames[0]["factor_snapshot"]["fundamental"]["score_breakdown"]["f_shadow"] == -0.45


def test_load_task_record_frames_feeds_shadow_report_directly():
    frames, _ = load_task_record_frames([_record(midterm=_artifact())])
    report = shadow_report(frames)

    assert report["n_frames"] == 1
    assert report["n_with_marginal"] == 1
    assert report["n_delta_frames"] == 1  # f 与 f_shadow 都在 ⇒ ΔF 可算


def test_load_task_record_frames_keeps_same_day_runs_until_dedupe():
    """适配层不去重（保持"每 run 一帧"）；样本单位由调用方按 symbol-day 归一。"""
    records = [
        _record(midterm=_artifact(), timestamp="2026-10-30T10:00:00"),
        _record(midterm=_artifact(), timestamp="2026-10-30T15:00:00"),
    ]
    frames, skipped = load_task_record_frames(records)
    assert skipped == 0
    assert len(frames) == 2

    kept, dropped = dedupe_frames(frames, scope=DEDUPE_SYMBOL_DAY)
    assert len(kept) == 1
    assert dropped == 1
