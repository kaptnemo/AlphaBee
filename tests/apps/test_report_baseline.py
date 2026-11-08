"""报告基线统计脚本（``scripts/report_baseline.py``）测试。

覆盖：章节切分（含缺失章节/空文档/备忘录标题）、比例分母为 0、数字 token 与
千字密度、issue 行与中期块切分、CSV 列名与顺序、legacy vs memo 口径差异、
坏 JSON 跳过、以及 ``--reports`` / ``--task-records`` 两条 CLI 主流程。

全部用 ``tmp_path``：不读真实 ``data/``、不写数据目录、不联网、不调用任何模型。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from alphabee.apps.cli.task_report import build_report_markdown
from alphabee.task_records.models import IssueRecord, TaskRecord

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "report_baseline.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("report_baseline_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline = _load_module()

_SEPARATOR = "=" * 64

_SAMPLE_REPORT = f"""# 600519.SH 贵州茅台 投资分析报告 — 财报口径 20260630

**整体置信度**: high
**任务**: `task-demo`

> 摘要一句话。

## 核心观点摘要

核心观点正文 10.07%。

## 投资观点展开

投资观点正文。

## 情景分析

情景正文。

## 核心指标

| 指标 | 数值 |

## 风险信号

信号正文。

## 勾稽关系异常

异常正文。

## 数据矛盾

矛盾正文。

## 维度分析

维度正文。

## 公司赛道与对标组

赛道正文。

## 审查发现

审查正文。

## 可证伪条件

证伪正文。

## 主要风险

风险正文。

## 免责声明

免责声明正文。

## 系统问题

- [critical] verified_conflict: 第一条消息 10.07%
- [high] thesis_gap: 第二条消息

共 2 个问题

{_SEPARATOR}
中期决策总结  symbol=600519.SH
  状态 S3

{_SEPARATOR}
"""


def _write_record(path: Path, record: TaskRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )


def _record(**overrides: Any) -> TaskRecord:
    defaults: dict[str, Any] = dict(
        task_id="task-demo123456",
        query="分析贵州茅台",
        symbol="600519.SH",
        timestamp="2026-10-20T10:00:00",
        overall_confidence="high",
        report_raw={
            "title": "贵州茅台投资分析报告",
            "summary": "一句话摘要。",
            "sections": {
                "executive_summary": "核心观点正文。",
                "investment_viewpoint": "投资观点正文。",
                "scenario_analysis": "情景正文。",
                "key_metrics": "指标正文。",
                "signal_analysis": "信号正文。",
                "anomaly_detection": "异常正文。",
                "conflict_analysis": "矛盾正文。",
                "dimension_analysis": "维度正文。",
                "company_track": "赛道正文。",
                "review_findings": "审查正文。",
                "falsification_conditions": "证伪正文。",
                "risks": "风险正文。",
                "disclaimer": "免责声明正文。",
            },
        },
        issues=[
            IssueRecord(severity="medium", category="missing_data", message="m" * 42),
            IssueRecord(severity="low", category="parse_error", message="降级"),
        ],
    )
    defaults.update(overrides)
    return TaskRecord(**defaults)


# ═══════════════════════════════════════════════════════════════════
# 章节切分
# ═══════════════════════════════════════════════════════════════════


def test_split_sections_legacy_titles() -> None:
    sections = baseline.split_sections(_SAMPLE_REPORT)
    assert set(baseline.SECTION_KEYS) <= set(sections)
    assert sections["executive_summary"] == "核心观点正文 10.07%。"
    assert sections["disclaimer"] == "免责声明正文。"
    # 系统问题区不是 13 节之一，不进入正文统计
    assert "系统问题" not in sections


def test_split_sections_empty_and_missing() -> None:
    assert baseline.split_sections("") == {}
    assert baseline.split_sections("# 只有标题\n\n没有章节正文") == {}
    # 缺失章节直接缺席：body13 只统计出现的章节
    metrics = baseline.compute_metrics("## 核心观点摘要\n\n只有这一节")
    assert metrics["body13_chars"] == len("只有这一节")
    assert metrics["detail_chars"] == 0
    assert metrics["detail_ratio"] == 0.0


def test_title_to_key_memo_titles() -> None:
    memo_text = (
        "## 一页结论\n\n结论正文\n"
        "## 核心主张\n\n主张正文\n"
        "## 情景与触发\n\n情景正文\n"
        "## 我们可能错在哪里\n\n反证正文\n"
        "## 观察清单\n\n观察正文\n"
        "## 关键指标\n\n指标正文\n"
        "## 附录 A 风险信号明细\n\n信号正文\n"
        "## 附录 F 审查发现\n\n审查正文\n"
    )
    sections = baseline.split_sections(memo_text)
    assert sections["executive_summary"] == "结论正文"
    assert sections["investment_viewpoint"] == "主张正文"
    assert sections["scenario_analysis"] == "情景正文"
    assert sections["risks"] == "反证正文"
    assert sections["falsification_conditions"] == "观察正文"
    assert sections["key_metrics"] == "指标正文"
    assert sections["signal_analysis"] == "信号正文"
    assert sections["review_findings"] == "审查正文"


# ═══════════════════════════════════════════════════════════════════
# 指标口径
# ═══════════════════════════════════════════════════════════════════


def test_zero_denominators() -> None:
    metrics = baseline.compute_metrics("")
    assert metrics["total_chars"] == 0
    assert metrics["body13_chars"] == 0
    assert metrics["detail_ratio"] == 0.0
    assert metrics["nonarg_ratio"] == 0.0
    assert metrics["numeric_per_kchar"] == 0.0


def test_numeric_tokens_and_density() -> None:
    text = "10.07% 和 183.5 以及 2026-10-30"
    metrics = baseline.compute_metrics(text)
    assert metrics["numeric_tokens"] == 5  # 10.07% / 183.5 / 2026 / 10 / 30
    assert metrics["numeric_per_kchar"] == round(5 / len(text) * 1000, 2)


def test_legacy_metrics_on_sample() -> None:
    legacy = baseline.compute_metrics(_SAMPLE_REPORT)
    sections = baseline.split_sections(_SAMPLE_REPORT)
    assert legacy["total_chars"] == len(_SAMPLE_REPORT)
    assert legacy["body13_chars"] == sum(len(sections[key]) for key in baseline.SECTION_KEYS)
    assert legacy["narrative_chars"] == sum(len(sections[key]) for key in baseline.NARRATIVE_KEYS)
    assert legacy["detail_chars"] == sum(len(sections[key]) for key in baseline.DETAIL_KEYS)
    assert legacy["narrative_chars"] + legacy["detail_chars"] == legacy["body13_chars"]
    # legacy：主文即 13 节正文，附录为 0
    assert legacy["main_chars"] == legacy["body13_chars"]
    assert legacy["appendix_chars"] == 0


def test_legacy_vs_memo_layout() -> None:
    legacy = baseline.compute_metrics(_SAMPLE_REPORT, layout="legacy")
    memo = baseline.compute_metrics(_SAMPLE_REPORT, layout="memo")
    sections = baseline.split_sections(_SAMPLE_REPORT)
    expected_main = sum(len(sections[key]) for key in baseline.MAIN_SECTION_KEYS)
    expected_appendix = sum(len(sections[key]) for key in baseline.APPENDIX_SECTION_KEYS)
    assert memo["main_chars"] == expected_main
    assert memo["appendix_chars"] == expected_appendix
    # 主文六节 + 附录六节 = 13 节正文 − 免责声明（memo 主文不含免责声明）
    assert memo["main_chars"] + memo["appendix_chars"] == legacy["body13_chars"] - len(sections["disclaimer"])
    assert memo["main_chars"] < legacy["main_chars"]
    # 其余基础列不受布局影响
    for column in baseline.BASE_COLUMNS[2:]:
        if column in ("main_chars", "appendix_chars"):
            continue
        assert memo[column] == legacy[column]


def test_explicit_issue_override_skips_parsing() -> None:
    # task-record 口径：issue 元信息由 record.issues 直接给出，不再解析系统问题区
    metrics = baseline.compute_metrics(_SAMPLE_REPORT, issue_count=7, issue_max_msg_chars=123)
    assert metrics["issue_count"] == 7
    assert metrics["issue_max_msg_chars"] == 123
    assert metrics["issue_parse_available"] is True


# ═══════════════════════════════════════════════════════════════════
# 系统问题区 / 中期块 / issue 行
# ═══════════════════════════════════════════════════════════════════


def test_split_system_region_and_midterm() -> None:
    system = _SAMPLE_REPORT[_SAMPLE_REPORT.index("## 系统问题") :]
    issue_region, midterm_block = baseline.split_system_region(_SAMPLE_REPORT)
    # 系统问题区整体 = issue 区 + 中期块（边界为分隔行前的换行；与设计 §1.2 逐字一致）
    assert issue_region + midterm_block == system
    assert issue_region.endswith("共 2 个问题\n")
    assert midterm_block.startswith("\n" + _SEPARATOR)
    metrics = baseline.compute_metrics(_SAMPLE_REPORT)
    assert metrics["issue_chars"] == len(issue_region)
    assert metrics["midterm_chars"] == len(midterm_block)


def test_split_system_region_unicode_separator() -> None:
    text = "## 系统问题\n\n- [low] demo: 消息\n\n" + "═" * 10 + "\n中期决策\n"
    issue_region, midterm_block = baseline.split_system_region(text)
    assert issue_region.endswith("消息\n")
    assert midterm_block.startswith("\n" + "═" * 10)


def test_no_system_region() -> None:
    metrics = baseline.compute_metrics("## 核心观点摘要\n\n正文")
    assert metrics["issue_chars"] == 0
    assert metrics["midterm_chars"] == 0


def test_parse_issue_lines() -> None:
    region = (
        "## 系统问题\n\n"
        "- [critical] verified_conflict: 第一条消息\n"
        "- [low] parse_error: 第二条消息: 带冒号\n"
        "不是 issue 行\n"
    )
    parsed = baseline.parse_issue_lines(region)
    assert [(severity, category) for severity, category, _ in parsed] == [
        ("critical", "verified_conflict"),
        ("low", "parse_error"),
    ]
    assert parsed[1][2] == "第二条消息: 带冒号"
    metrics = baseline.compute_metrics(_SAMPLE_REPORT)
    assert metrics["issue_count"] == 2
    assert metrics["issue_max_msg_chars"] == len("第一条消息 10.07%")
    assert metrics["issue_parse_available"] is True


def test_parse_issue_lines_unavailable() -> None:
    metrics = baseline.compute_metrics("## 系统问题\n\n没有结构化 issue 行\n")
    assert metrics["issue_count"] == 0
    assert metrics["issue_max_msg_chars"] == 0
    assert metrics["issue_parse_available"] is False


# ═══════════════════════════════════════════════════════════════════
# 文件名推断 / CSV 列
# ═══════════════════════════════════════════════════════════════════


def test_parse_report_identity() -> None:
    assert baseline.parse_report_identity("300504.SZ-2026-10-30.md") == ("300504.SZ", "2026-10-30", True)
    assert baseline.parse_report_identity("兴森科技-2026-10-17.md") == ("兴森科技", "2026-10-17", False)
    assert baseline.parse_report_identity("weird.md") == ("weird", "", False)


def test_csv_columns_order() -> None:
    assert list(baseline.columns_for("legacy")) == [
        "symbol",
        "date",
        "total_chars",
        "body13_chars",
        "narrative_chars",
        "detail_chars",
        "issue_chars",
        "midterm_chars",
        "detail_ratio",
        "nonarg_ratio",
        "numeric_tokens",
        "numeric_per_kchar",
        "issue_count",
        "issue_max_msg_chars",
    ]
    assert list(baseline.columns_for("memo")) == list(baseline.columns_for("legacy")) + [
        "main_chars",
        "appendix_chars",
    ]


# ═══════════════════════════════════════════════════════════════════
# 收集 / CLI
# ═══════════════════════════════════════════════════════════════════


def test_collect_task_records_skips_bad_json(tmp_path: Path) -> None:
    records_dir = tmp_path / "task_records"
    good = records_dir / "600519.SH" / "2026-10-20" / "task-good.json"
    _write_record(good, _record())
    bad = records_dir / "600519.SH" / "2026-10-20" / "task-bad.json"
    bad.write_text("{not valid json", encoding="utf-8")

    rows, skipped = baseline.collect_task_records(records_dir)
    assert len(rows) == 1
    assert len(skipped) == 1
    assert skipped[0]["source"].endswith("task-bad.json")
    row = rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["date"] == "2026-10-20"
    assert row["issue_count"] == 2
    assert row["issue_max_msg_chars"] == 42
    assert row["total_chars"] == len(build_report_markdown(_record()))


def test_task_record_empty_report_raw_still_runs(tmp_path: Path) -> None:
    records_dir = tmp_path / "task_records"
    record = _record(report_raw={}, issues=[IssueRecord(severity="low", category="x", message="m")])
    _write_record(records_dir / "600519.SH" / "2026-10-20" / "task-empty.json", record)

    rows, skipped = baseline.collect_task_records(records_dir)
    assert skipped == []
    assert len(rows) == 1
    assert rows[0]["body13_chars"] == 0
    assert rows[0]["issue_count"] == 1
    # report_raw 为空时仍渲染最小骨架（fail-open），total_chars 非 0
    assert rows[0]["total_chars"] == len(build_report_markdown(record)) > 0


def test_main_reports_flow(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "600519.SH-2026-10-20.md").write_text(_SAMPLE_REPORT, encoding="utf-8")
    (reports / "兴森科技-2026-10-17.md").write_text(_SAMPLE_REPORT, encoding="utf-8")
    out = tmp_path / "out"

    code = baseline.main(["--reports", str(reports), "--format", "all", "--out", str(out)])
    assert code == 0
    assert (out / "baseline.csv").exists()
    assert (out / "baseline.md").exists()
    assert (out / "baseline.json").exists()

    header = (out / "baseline.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(baseline.columns_for("legacy"))

    payload = json.loads((out / "baseline.json").read_text(encoding="utf-8"))
    assert payload["method"] == "reports"
    assert payload["layout"] == "legacy"
    assert payload["count"] == 2
    assert payload["rows"][0]["total_chars"] == len(_SAMPLE_REPORT)
    assert payload["rows"][0]["issue_count"] == 2

    markdown = (out / "baseline.md").read_text(encoding="utf-8")
    assert "## 口径说明" in markdown
    assert "非代码标的" in markdown  # 兴森科技 文件名无代码，需注明
    assert "兴森科技" in markdown


def test_main_task_records_flow_and_memo_columns(tmp_path: Path) -> None:
    records_dir = tmp_path / "task_records"
    _write_record(records_dir / "600519.SH" / "2026-10-20" / "task-good.json", _record())
    bad = records_dir / "600519.SH" / "2026-10-20" / "task-bad.json"
    bad.write_text("[1, 2", encoding="utf-8")
    out = tmp_path / "out"

    code = baseline.main(
        ["--task-records", str(records_dir), "--format", "json", "--layout", "memo", "--out", str(out)]
    )
    assert code == 0
    payload = json.loads((out / "baseline.json").read_text(encoding="utf-8"))
    assert payload["method"] == "task_records"
    assert payload["layout"] == "memo"
    assert payload["count"] == 1
    assert len(payload["skipped"]) == 1
    row = payload["rows"][0]
    assert row["main_chars"] > 0
    assert row["appendix_chars"] > 0
    assert payload["columns"] == list(baseline.columns_for("memo"))


def test_main_default_input_uses_task_records(tmp_path: Path, monkeypatch: Any) -> None:
    _write_record(tmp_path / "data" / "task_records" / "600519.SH" / "2026-10-20" / "t.json", _record())
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    code = baseline.main(["--format", "json", "--out", str(out)])
    assert code == 0
    payload = json.loads((out / "baseline.json").read_text(encoding="utf-8"))
    assert payload["method"] == "task_records"
    assert payload["source"] == "data/task_records"
    assert payload["count"] == 1


def test_main_missing_input_dir_returns_error(tmp_path: Path) -> None:
    assert baseline.main(["--reports", str(tmp_path / "nope"), "--format", "csv", "--out", str(tmp_path)]) == 1
