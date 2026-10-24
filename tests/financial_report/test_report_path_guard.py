"""P1：backend 路径守卫与相似路径提示（设计 §7.2 / §7.4 / §11）。

覆盖：

- ``suggest_paths`` 的归一化、匹配顺序（basename → 相似度 → 同目录优先）、
  上限与去重、带前导 ``/``；
- ``ReportFilesystemBackend.read``：精确路径行为不变、拼错/漏前缀/目录对文件名错
  都能拿到真实候选、仅提示不拦截、``read_file_miss`` 观测日志。

全部用例只使用 ``tmp_path`` 上的真实 markdown 文件与真实 index，不依赖 LLM/网络；
直接构造 backend 也等价于「GP subagent 与主 agent 共用同一 backend」的场景（§7.1）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend
from structlog.testing import capture_logs

from alphabee.financial_report import report_cache
from alphabee.financial_report.fetch_deepagents import (
    MAX_PATH_HINTS,
    PATH_HINTS_HEADER,
    ReportFilesystemBackend,
)
from alphabee.financial_report.report_cache import (
    ReportIndex,
    build_report_index,
    clear_report_caches,
    suggest_paths,
)

REAL_NESTED = "企业精神/五、_合并财务报表主要项目注释－续/营业收入和营业成本/(1)_营业收入和营业成本情况.md"
TYPO_NESTED = "企业精神/五、_合并财务报表主要项目注释－续/营业收入和营业成本/营业收入和营业成本情况.md"


def _make_report(root: Path, rel_files: list[str]) -> ReportIndex:
    for rel_file in rel_files:
        path = root / rel_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {Path(rel_file).stem}\n正文内容。\n", encoding="utf-8")
    return build_report_index(root)


def _hints_from_error(error: str) -> list[str]:
    lines = error.splitlines()
    try:
        start = lines.index(PATH_HINTS_HEADER) + 1
    except ValueError:  # 没有提示段落
        return []
    return [line[2:] for line in lines[start:] if line.startswith("- ")]


@pytest.fixture
def report_root(tmp_path: Path) -> Path:
    """一份带真实目录层级与 ``(1)_`` 前缀的小报告。"""
    clear_report_caches()
    _make_report(
        tmp_path,
        [
            REAL_NESTED,
            "企业精神/五、_合并财务报表主要项目注释－续/营业收入和营业成本/(2)_营业成本构成.md",
            "第一节_公司简介.md",
            "章节A/(1)_折旧方法.md",
            "章节B/(2)_折旧方法-续.md",
        ],
    )
    yield tmp_path
    clear_report_caches()


@pytest.fixture
def report_index(report_root: Path) -> ReportIndex:
    return build_report_index(report_root)


@pytest.fixture
def backend(report_root: Path, report_index: ReportIndex) -> ReportFilesystemBackend:
    return ReportFilesystemBackend(report_root, report_index)


class TestSuggestPaths:
    def test_returns_leading_slash_and_normalizes_request(self, report_index: ReportIndex) -> None:
        # 反斜杠 + 重复斜杠 + 前导 / + ./ 都要归一化到同一条真实路径
        for requested in (
            REAL_NESTED,
            f"/{REAL_NESTED}",
            REAL_NESTED.replace("/", "\\\\"),
            f".//{REAL_NESTED}",
        ):
            hints = suggest_paths(report_index, requested)
            assert hints[0] == f"/{REAL_NESTED}"
            assert all(hint.startswith("/") for hint in hints)

    def test_basename_exact_match_ignores_directory(self, report_index: ReportIndex) -> None:
        hints = suggest_paths(report_index, "完全不存在的目录/第一节_公司简介.md")
        assert hints[:1] == ["/第一节_公司简介.md"]

    def test_missing_number_prefix_is_resolved_by_basename(self, report_index: ReportIndex) -> None:
        hints = suggest_paths(report_index, TYPO_NESTED)
        assert f"/{REAL_NESTED}" in hints
        assert all(hint.endswith(".md") for hint in hints)

    def test_same_directory_candidates_come_first(self, report_index: ReportIndex) -> None:
        # 目录对（章节A）、文件名多了一个 "-续" → 同目录的 (1)_折旧方法.md 应排第一
        hints = suggest_paths(report_index, "章节A/(2)_折旧方法-续.md")
        assert hints[0] == "/章节A/(1)_折旧方法.md"
        assert "/章节B/(2)_折旧方法-续.md" in hints

    def test_directory_request_lists_files_under_it(self, report_index: ReportIndex) -> None:
        hints = suggest_paths(report_index, "企业精神/五、_合并财务报表主要项目注释－续")
        assert hints
        assert all(hint.startswith("/企业精神/五、_合并财务报表主要项目注释－续/") for hint in hints)

    def test_respects_n_and_dedupes(self, report_index: ReportIndex) -> None:
        hints = suggest_paths(report_index, "折旧方法.md", n=2)
        assert 0 < len(hints) <= 2
        assert len(hints) == len(set(hints))
        assert suggest_paths(report_index, REAL_NESTED, n=0) == []

    def test_unrelated_request_returns_empty(self, report_index: ReportIndex) -> None:
        assert suggest_paths(report_index, "完全不相关/zzz.md") == []

    def test_empty_index_returns_empty(self, tmp_path: Path) -> None:
        clear_report_caches()
        index = build_report_index(tmp_path)
        assert suggest_paths(index, "a.md") == []


class TestReportFilesystemBackend:
    def test_is_a_filesystem_backend(self, backend: ReportFilesystemBackend) -> None:
        assert isinstance(backend, FilesystemBackend)

    def test_exact_path_read_is_unchanged(self, backend: ReportFilesystemBackend, report_root: Path) -> None:
        result = backend.read("/第一节_公司简介.md")
        plain = FilesystemBackend(root_dir=str(report_root), virtual_mode=True).read("/第一节_公司简介.md")

        assert result.error is None
        assert result.file_data is not None
        assert result.file_data["content"] == plain.file_data["content"]  # type: ignore[index]
        assert "正文内容" in result.file_data["content"]

    def test_miss_lists_real_candidates(self, backend: ReportFilesystemBackend) -> None:
        result = backend.read(f"/{TYPO_NESTED}")

        assert result.error is not None
        # 仍然是失败结果：deepagents 会把非空 error 转成 ToolMessage(status="error")
        assert result.file_data is None
        assert "not found" in result.error
        assert PATH_HINTS_HEADER in result.error
        hints = _hints_from_error(result.error)
        assert hints, result.error
        assert f"/{REAL_NESTED}" in hints
        assert len(hints) <= MAX_PATH_HINTS
        assert len(hints) == len(set(hints))

    def test_missing_number_prefix_reaches_real_file(self, backend: ReportFilesystemBackend) -> None:
        result = backend.read("/章节A/折旧方法.md")
        assert result.error is not None
        assert "/章节A/(1)_折旧方法.md" in _hints_from_error(result.error)

    def test_directory_mistake_prefers_same_directory(self, backend: ReportFilesystemBackend) -> None:
        result = backend.read("/章节A/(2)_折旧方法-续.md")
        assert result.error is not None
        hints = _hints_from_error(result.error)
        assert hints[0] == "/章节A/(1)_折旧方法.md"

    def test_read_directory_path_gets_file_hints(self, backend: ReportFilesystemBackend) -> None:
        result = backend.read("/企业精神/五、_合并财务报表主要项目注释－续")
        assert result.error is not None
        hints = _hints_from_error(result.error)
        assert hints
        assert any("(1)_营业收入和营业成本情况.md" in hint for hint in hints)

    def test_hints_capped_at_five(self, report_root: Path) -> None:
        # 造 8 个高度相似的文件，验证提示条数上限
        for i in range(8):
            path = report_root / "相似章节" / f"({i})_测试项目明细.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        index = build_report_index(report_root)
        backend = ReportFilesystemBackend(report_root, index)

        result = backend.read("/相似章节/测试项目明细.md")

        assert result.error is not None
        hints = _hints_from_error(result.error)
        assert len(hints) == MAX_PATH_HINTS

    def test_success_does_not_emit_miss_log(self, backend: ReportFilesystemBackend) -> None:
        with capture_logs() as logs:
            backend.read("/第一节_公司简介.md")
        assert [entry for entry in logs if entry["event"] == "read_file_miss"] == []

    def test_miss_emits_observability_log(self, backend: ReportFilesystemBackend, report_index: ReportIndex) -> None:
        requested = "/不存在/营业收入和营业成本情况.md"
        with capture_logs() as logs:
            backend.read(requested)

        [event] = [entry for entry in logs if entry["event"] == "read_file_miss"]
        assert event["log_level"] == "info"
        assert event["root"] == str(report_index.root)
        assert event["version"] == report_index.version
        assert event["requested"] == requested
        assert event["hints_count"] >= 1
        # 不记录文件内容
        assert "正文内容" not in str(event.values())

    def test_miss_without_candidates_still_logged(self, backend: ReportFilesystemBackend) -> None:
        with capture_logs() as logs:
            result = backend.read("/zzz/qqq.md")

        [event] = [entry for entry in logs if entry["event"] == "read_file_miss"]
        assert event["hints_count"] == 0
        assert result.error is not None
        assert PATH_HINTS_HEADER not in result.error

    def test_non_not_found_errors_are_not_annotated(self, backend: ReportFilesystemBackend) -> None:
        result = backend.read("/第一节_公司简介.md", offset=999)

        assert result.error is not None
        assert PATH_HINTS_HEADER not in result.error
        with capture_logs() as logs:
            backend.read("/第一节_公司简介.md", offset=999)
        assert [entry for entry in logs if entry["event"] == "read_file_miss"] == []

    def test_no_strict_allowlist_for_new_files(self, report_root: Path, report_index: ReportIndex) -> None:
        # index 构建之后新增的文件不在 index 里：仍然可读（只提示、不拦截）
        new_file = report_root / "新增章节.md"
        new_file.write_text("解析过程中新增的内容", encoding="utf-8")
        assert "新增章节.md" not in report_index.rel_files

        backend = ReportFilesystemBackend(report_root, report_index)
        result = backend.read("/新增章节.md")

        assert result.error is None
        assert result.file_data is not None
        assert result.file_data["content"] == "解析过程中新增的内容"

    def test_hints_do_not_rescan_directory(
        self, backend: ReportFilesystemBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 候选必须来自已构建的 index：屏蔽目录遍历后仍能给出提示
        def boom(*args, **kwargs):
            raise AssertionError("suggest_paths 不应重新扫描目录")

        monkeypatch.setattr("os.walk", boom)
        monkeypatch.setattr(report_cache, "build_report_index", boom)

        result = backend.read("/不存在/营业收入和营业成本情况.md")

        assert result.error is not None
        assert f"/{REAL_NESTED}" in _hints_from_error(result.error)

    def test_subagent_style_shared_backend_gets_hints(self, backend: ReportFilesystemBackend) -> None:
        # GP subagent 与主 agent 共享同一个 backend（§7.1）：直接调用即等价覆盖
        primary_result = backend.read(f"/{TYPO_NESTED}")
        subagent_result = backend.read(f"/{TYPO_NESTED}")

        assert primary_result.error is not None
        assert subagent_result.error == primary_result.error
        assert f"/{REAL_NESTED}" in _hints_from_error(subagent_result.error)
