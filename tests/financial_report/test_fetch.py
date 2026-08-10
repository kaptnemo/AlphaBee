from alphabee.financial_report.fetch import (
    _extract_report_year,
    _is_message,
    _iter_text_blocks,
    _render_tool_call,
    build_fetch_prompt,
    build_file_tree,
)


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
