"""--task-report 渲染测试：从 TaskRecord 直接重建最终报告（markdown）。

覆盖：全章节渲染 / 占位符跳过 / 空记录 fail-open / 文件落盘字节一致 /
未知章节透传 / CLI 接线。渲染为纯函数（禁 LLM、无网络），全部离线可跑。
"""

from __future__ import annotations

import argparse

from alphabee.apps.cli.task_report import (
    _SECTION_TITLES,
    build_report_markdown,
    default_report_output,
    render_task_record_report,
)
from alphabee.task_records.models import IssueRecord, TaskRecord

ALL_SECTIONS = {
    "executive_summary": "财报口径：20260630。",
    "investment_viewpoint": "投资观点正文。",
    "scenario_analysis": "情景分析正文。",
    "key_metrics": "| 指标 | 数值 |",
    "signal_analysis": "信号正文。",
    "anomaly_detection": "异常正文。",
    "conflict_analysis": "矛盾正文。",
    "dimension_analysis": "维度正文。",
    "company_track": "对标组正文。",
    "review_findings": "审查正文。",
    "falsification_conditions": "证伪条件正文。",
    "risks": "风险正文。",
    "disclaimer": "免责声明。",
}


def _record(**overrides) -> TaskRecord:
    defaults = dict(
        task_id="task-test123456",
        query="分析贵州茅台",
        symbol="600519.SH",
        timestamp="2026-10-20T10:00:00",
        overall_confidence="high",
        risk_count={"high": 1, "medium": 2, "low": 0, "blocked": 0},
        midterm_state="S3",
        midterm_confidence=0.85,
        midterm_evidence_count=12,
        report_raw={
            "title": "贵州茅台投资分析报告",
            "summary": "一句话摘要。",
            "overall_confidence": "high",
            "risk_count": {"high": 1, "medium": 2},
            "sections": dict(ALL_SECTIONS),
        },
        issues=[
            IssueRecord(severity="medium", category="missing_data", message="缺少北向数据"),
            IssueRecord(severity="low", category="parse_error", message="JSON 解析降级"),
        ],
    )
    defaults.update(overrides)
    return TaskRecord(**defaults)


def test_build_report_markdown_full() -> None:
    md = build_report_markdown(_record())
    assert md.startswith("# 贵州茅台投资分析报告")
    assert "**整体置信度**: high" in md
    assert "**风险分布**: 高风险 1 中风险 2" in md
    assert "**任务**: `task-test123456`" in md
    assert "**标的**: 600519.SH" in md
    assert "**中期决策**: 状态 S3 置信度 0.85 证据 12 条" in md
    assert "> 一句话摘要。" in md
    # 13 个章节标题全部出现（钉住标题映射完整，含 company_track）
    for display in _SECTION_TITLES.values():
        assert f"## {display}" in md
    assert "## 系统问题" in md
    assert "- [medium] missing_data: 缺少北向数据" in md
    assert "共 2 个问题" in md


def test_placeholder_sections_skipped() -> None:
    sections = dict(ALL_SECTIONS)
    sections["investment_viewpoint"] = "未生成独立投资观点，以下为结构化数据分析。"
    sections["review_findings"] = "未执行审查"
    sections["scenario_analysis"] = "未生成情景分析。"
    md = build_report_markdown(_record(report_raw={"title": "T", "sections": sections}))
    assert "## 投资观点展开" not in md
    assert "## 审查发现" not in md
    assert "## 情景分析" not in md
    assert "## 核心指标" in md  # 正常章节不受影响


def test_empty_report_raw_graceful() -> None:
    md = build_report_markdown(TaskRecord(task_id="task-empty1", query="分析比亚迪"))
    assert md.startswith("# 分析比亚迪")
    assert "report_raw 为空" in md
    assert "## 系统问题" not in md


def test_unknown_section_key_falls_through() -> None:
    md = build_report_markdown(_record(report_raw={"title": "T", "sections": {"new_section": "新章节正文"}}))
    assert "## new_section" in md
    assert "新章节正文" in md


def test_render_to_file_byte_identical(tmp_path) -> None:
    record = _record()
    out = tmp_path / "nested" / "report.md"
    md = render_task_record_report(record, output=out)
    assert out.is_file()
    assert out.read_text(encoding="utf-8") == md
    assert md.endswith("\n")


def test_args_task_report_output_optional_value(monkeypatch) -> None:
    """省略 → None；只给开关 → auto；给路径 → 路径。"""
    import sys

    from alphabee.apps.cli import args as args_mod

    monkeypatch.setattr(sys, "argv", ["main.py", "--task-report", "tid"])
    assert args_mod.parse_args().task_report_output is None

    monkeypatch.setattr(sys, "argv", ["main.py", "--task-report", "tid", "--task-report-output"])
    assert args_mod.parse_args().task_report_output == "auto"

    monkeypatch.setattr(sys, "argv", ["main.py", "--task-report", "tid", "--task-report-output", "x.md"])
    assert args_mod.parse_args().task_report_output == "x.md"


def test_default_report_output_path(tmp_path, monkeypatch) -> None:
    import alphabee.utils.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_data_root", lambda: tmp_path)
    path = default_report_output(_record())
    label = storage_mod.normalize_symbol("600519.SH")
    assert path == tmp_path / "reports" / f"{label}-2026-10-20.md"


def test_cli_task_report_auto_output(tmp_path, capsys, monkeypatch) -> None:
    """--task-report-output 不带值时自动生成默认路径并落盘。"""
    import alphabee.task_records as task_records_mod
    import alphabee.utils.storage as storage_mod
    from alphabee.apps.cli import tasks as tasks_mod
    from alphabee.task_records.store import TaskStore

    store = TaskStore(base_dir=tmp_path / "records")
    record = _record()
    store.save(record)

    monkeypatch.setattr(task_records_mod, "TaskStore", lambda: store)
    monkeypatch.setattr(storage_mod, "get_data_root", lambda: tmp_path)
    args = argparse.Namespace(
        task_stats=False,
        distill=False,
        task_history=None,
        task_record=None,
        task_report=record.task_id,
        task_report_output="auto",
    )
    tasks_mod.handle_task_cli(args)
    captured = capsys.readouterr().out
    label = storage_mod.normalize_symbol("600519.SH")
    expected = tmp_path / "reports" / f"{label}-2026-10-20.md"
    assert expected.is_file()
    assert expected.read_text(encoding="utf-8").startswith("# 贵州茅台投资分析报告")
    assert "报告已保存" in captured


def test_cli_task_report_wiring(tmp_path, capsys, monkeypatch) -> None:
    """CLI 接线：--task-report 经 handle_task_cli 渲染到终端（TaskStore 用临时目录）。"""
    import alphabee.task_records as task_records_mod
    from alphabee.apps.cli import tasks as tasks_mod
    from alphabee.task_records.store import TaskStore

    store = TaskStore(base_dir=tmp_path)
    record = _record()
    store.save(record)

    # handle_task_cli 在函数体内 ``from alphabee.task_records import TaskStore``，
    # 因此补丁打在 alphabee.task_records 命名空间上。
    monkeypatch.setattr(task_records_mod, "TaskStore", lambda: store)
    args = argparse.Namespace(
        task_stats=False,
        distill=False,
        task_history=None,
        task_record=None,
        task_report=record.task_id,
        task_report_output=None,
    )
    tasks_mod.handle_task_cli(args)
    captured = capsys.readouterr().out
    assert "# 贵州茅台投资分析报告" in captured
    assert "## 核心观点摘要" in captured


# ── 状态证据 V2（design §6.9）：formula / 不确定 / 顶背离 渲染 ─────────────────


def test_meta_line_renders_formula_uncertain_and_adverse() -> None:
    md = build_report_markdown(_record(midterm_formula="v2", midterm_uncertain=False, midterm_adverse_severity=0.76))
    assert "**中期决策**: 状态 S3 置信度 0.85 证据 12 条 · formula v2 · 不确定 否 · 顶背离 0.76" in md


def test_meta_line_renders_uncertain_yes() -> None:
    md = build_report_markdown(_record(midterm_formula="v1", midterm_uncertain=True))
    assert "· formula v1 · 不确定 是" in md
    assert "顶背离" not in md  # severity 0.0 = 无顶背离，不打印噪声值


def test_meta_line_old_record_keeps_current_rendering() -> None:
    """旧记录（无 formula 字段）保持现状：不出现空标签。"""
    md = build_report_markdown(_record())  # _record 未设 formula / uncertain / severity
    assert "**中期决策**: 状态 S3 置信度 0.85 证据 12 条" in md
    assert "formula" not in md
    assert "不确定" not in md
    assert "顶背离" not in md


# ── CLI 渲染面（renderer 的 resolve_midterm_decision 分支，design §6.7）────────
# 仓库此前没有 renderer 专属测试文件（§8.4「若有」）；本文件已属 apps 渲染面，
# 故把该分支的只读标签断言放在这里（不新增测试文件）。


def _midterm_payload(**overrides) -> dict:
    payload = {
        "state": {"argmax_state": "S5", "entropy": 0.7993},
        "thesis_confidence": 0.6,
        "evidence_log": [{"id": "e1"}, {"id": "e2"}],
        "position": {"position_band": "减仓"},
        "degraded": False,
        "classifier_formula": "v2",
        "uncertain": True,
        "adverse_divergence": {"severity": 0.7626},
    }
    payload.update(overrides)
    return payload


def _render_midterm_cli_branch(capsys, md_value: dict) -> str:
    from alphabee.apps.cli import renderer
    from alphabee.apps.cli.colors import set_color_enabled

    set_color_enabled(False)  # 关闭 ANSI，断言纯文本
    try:
        renderer.print_node_update_summary(
            "resolve_midterm_decision",
            {"artifacts": [{"type": "midterm_decision", "value": md_value}]},
            0.1,
        )
    finally:
        set_color_enabled(True)
    return capsys.readouterr().out


def test_cli_midterm_branch_renders_formula_uncertain_and_adverse(capsys) -> None:
    out = _render_midterm_cli_branch(capsys, _midterm_payload())
    assert "认知状态: S5" in out
    assert "公式 v2" in out
    assert "不确定" in out
    assert "顶背离 0.76" in out


def test_cli_midterm_branch_old_payload_keeps_current_rendering(capsys) -> None:
    payload = _midterm_payload()
    for key in ("classifier_formula", "uncertain", "adverse_divergence"):
        payload.pop(key)
    out = _render_midterm_cli_branch(capsys, payload)
    assert "认知状态: S5" in out
    assert "公式" not in out
    assert "不确定" not in out
    assert "顶背离" not in out
