import pytest

from alphabee.financial_report.fetch import (
    _extract_report_year,
    _is_message,
    _iter_text_blocks,
    _render_tool_call,
    build_compact_tree,
    build_fetch_prompt,
    build_file_tree,
)
from alphabee.financial_report.report_cache import build_report_index, clear_report_caches


class TestExtractReportYear:
    def test_extracts_year_from_annual_report_name(self) -> None:
        assert _extract_report_year("江西沃格光电集团股份有限公司_2025_年年度报告") == "2025"

    def test_none_when_no_year(self) -> None:
        assert _extract_report_year("审计报告") is None
        assert _extract_report_year("") is None


class TestBuildFetchPrompt:
    def test_contains_report_name_and_query(self) -> None:
        prompt = build_fetch_prompt("某公司_2025_年年度报告", "营业收入是多少")
        assert "某公司_2025_年年度报告" in prompt
        assert "营业收入是多少" in prompt

    def test_instructs_search_strategy(self) -> None:
        prompt = build_fetch_prompt("r", "q")
        assert "Grep" in prompt
        assert "Read" in prompt
        assert "Glob" in prompt
        assert "file_tree" in prompt
        assert "不要" in prompt

    def test_announces_report_year(self) -> None:
        prompt = build_fetch_prompt("某公司_2025_年年度报告", "q")
        assert "2025 年" in prompt
        assert "按期间回答" in prompt

    def test_handles_report_without_year(self) -> None:
        prompt = build_fetch_prompt("审计报告", "q")
        assert "本报告年度" in prompt
        assert "None" not in prompt

    def test_declares_path_convention(self) -> None:
        prompt = build_fetch_prompt("某公司_2025_年年度报告", "q")
        assert "## 路径约定" in prompt
        assert "只能使用 Grep/Glob 返回的路径" in prompt
        assert "不要" in prompt and "自行拼接文件名" in prompt
        assert "可能的真实路径（请直接使用其一）" in prompt
        assert "必须直接使用其中一条" in prompt
        assert "禁止继续猜测路径" in prompt

    def test_requires_full_relative_path_citation(self) -> None:
        prompt = build_fetch_prompt("某公司_2025_年年度报告", "q")
        assert "使用**完整相对路径**" in prompt
        assert "不要只写文件名" in prompt


class TestBuildFileTree:
    def test_renders_indented_tree(self, tmp_path) -> None:
        (tmp_path / "1、_资产.md").write_text("资产")
        (tmp_path / "2、_负债").mkdir()
        (tmp_path / "2、_负债" / "(1)_长期借款.md").write_text("借款")
        (tmp_path / "notes.txt").write_text("忽略非 md 文件")

        tree = build_file_tree(tmp_path)
        assert "1、_资产.md" in tree
        assert "(1)_长期借款.md" in tree
        assert "notes.txt" not in tree
        assert "└──" in tree

    def test_truncates_oversized_tree(self, tmp_path) -> None:
        for i in range(3):
            (tmp_path / f"f{i}.md").write_text("x")
        tree = build_file_tree(tmp_path, max_entries=2)
        assert "已截断" in tree


class TestBuildCompactTree:
    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        clear_report_caches()
        yield
        clear_report_caches()

    def test_small_report_lists_full_relative_paths(self, tmp_path) -> None:
        (tmp_path / "第一节_公司简介.md").write_text("x")
        (tmp_path / "第二节_财务数据").mkdir()
        (tmp_path / "第二节_财务数据" / "(1)_营业收入.md").write_text("x")
        index = build_report_index(tmp_path)

        compact = build_compact_tree(index)

        assert compact.startswith("<report_paths>")
        assert compact.rstrip().endswith("</report_paths>")
        assert "- 第一节_公司简介.md" in compact
        assert "- 第二节_财务数据/(1)_营业收入.md" in compact
        assert "<chapter_map>" not in compact

    def test_large_report_uses_stats_and_chapter_map(self, tmp_path) -> None:
        for i in range(85):
            (tmp_path / f"f{i:03d}.md").write_text("x")
        chapter_dir = tmp_path / "第五章_合并财务报表"
        chapter_dir.mkdir()
        for i in range(5):
            (chapter_dir / f"s{i}.md").write_text("x")
        index = build_report_index(tmp_path)
        assert index.total_files == 90

        compact = build_compact_tree(index)

        assert "<report_stats>" in compact
        assert "文件总数: 90" in compact
        assert "目录数: 1" in compact
        assert "<chapter_map>" in compact
        assert "/第五章_合并财务报表 (5 files)" in compact
        assert "/ (85 files)" in compact
        assert "<report_paths>" not in compact
        # 大报告不再依赖 max_entries=500 截断
        assert "已截断" not in compact

    def test_full_path_limit_override(self, tmp_path) -> None:
        for i in range(3):
            (tmp_path / f"f{i}.md").write_text("x")
        index = build_report_index(tmp_path)

        assert "<report_paths>" in build_compact_tree(index, full_path_limit=3)
        assert "<chapter_map>" in build_compact_tree(index, full_path_limit=2)


class TestBuildFetchPromptWithIndex:
    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        clear_report_caches()
        yield
        clear_report_caches()

    def test_prompt_renders_index_full_paths(self, tmp_path) -> None:
        (tmp_path / "第一节_公司简介.md").write_text("x")
        (tmp_path / "第二节_财务数据").mkdir()
        (tmp_path / "第二节_财务数据" / "(1)_营业收入.md").write_text("x")
        index = build_report_index(tmp_path)

        prompt = build_fetch_prompt("某公司_2025_年年度报告", "营业收入是多少", report_dir=tmp_path, index=index)

        assert "营业收入是多少" in prompt
        assert "2025 年" in prompt
        assert "<report_paths>" in prompt
        assert "- 第二节_财务数据/(1)_营业收入.md" in prompt
        assert "<chapter_map>" not in prompt
        # 既有约束语义保留
        assert "不要" in prompt and "目录遍历" in prompt
        assert "Grep" in prompt and "Read" in prompt and "Glob" in prompt
        # §7.3 / §8.3：路径约定与完整相对路径引用
        assert "## 路径约定" in prompt
        assert "只能使用 Grep/Glob 返回的路径" in prompt
        assert "必须直接使用其中一条" in prompt
        assert "使用**完整相对路径**" in prompt

    def test_prompt_renders_chapter_map_for_large_report(self, tmp_path) -> None:
        for i in range(85):
            (tmp_path / f"f{i:03d}.md").write_text("x")
        index = build_report_index(tmp_path)

        prompt = build_fetch_prompt("某公司_2025_年年度报告", "q", report_dir=tmp_path, index=index)

        assert "<report_stats>" in prompt
        assert "文件总数: 85" in prompt
        assert "<chapter_map>" in prompt
        assert "/ (85 files)" in prompt
        assert "<report_paths>" not in prompt
        assert "以 Grep 结果为准" in prompt

    def test_prompt_without_index_keeps_legacy_tree(self, tmp_path) -> None:
        (tmp_path / "a.md").write_text("x")
        prompt = build_fetch_prompt("r", "q", report_dir=tmp_path)
        assert "<file_tree>" in prompt
        assert "a.md" in prompt
        assert "<report_stats>" not in prompt


class TestMessageFiltering:
    def test_non_message_object_rejected(self) -> None:
        assert not _is_message(object())
        assert not _is_message({"content": []})

    def test_assistant_message_accepted(self) -> None:
        from claude_agent_sdk.types import AssistantMessage

        msg = AssistantMessage(content=[], model="test")
        assert _is_message(msg)

    def test_render_tool_call_ignores_non_message(self) -> None:
        assert _render_tool_call(object()) == []

    def test_iter_text_blocks_empty_for_no_text(self) -> None:
        from claude_agent_sdk.types import AssistantMessage, ThinkingBlock, ToolUseBlock

        msg = AssistantMessage(
            content=[
                ThinkingBlock(thinking="...", signature="s"),
                ToolUseBlock(name="Bash", input={"command": "ls"}, id="1"),
            ],
            model="test",
        )
        assert list(_iter_text_blocks(msg)) == []

    def test_iter_text_blocks_yields_text(self) -> None:
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        msg = AssistantMessage(content=[TextBlock(text="营业收入 25.5 亿")], model="test")
        assert list(_iter_text_blocks(msg)) == ["营业收入 25.5 亿"]
