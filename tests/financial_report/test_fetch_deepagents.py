from langchain_core.messages import AIMessage

from alphabee.financial_report.fetch_deepagents import _render_tool_call, create_report_fetch_agent


class TestRenderToolCall:
    def test_renders_read_file(self) -> None:
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"file_path": "/21、_固定资产/(2)_._折旧方法.md"}, "id": "1"}],
        )
        assert _render_tool_call(msg) == ["[read_file] /21、_固定资产/(2)_._折旧方法.md"]

    def test_renders_grep_and_glob(self) -> None:
        msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "grep", "args": {"pattern": "折旧方法"}, "id": "1"},
                {"name": "glob", "args": {"pattern": "**/*.md"}, "id": "2"},
                {"name": "ls", "args": {"path": "/"}, "id": "3"},
            ],
        )
        assert _render_tool_call(msg) == ["[grep] 折旧方法", "[glob] **/*.md", "[ls] /"]

    def test_renders_fallback_for_unknown_tool(self) -> None:
        msg = AIMessage(content="", tool_calls=[{"name": "weird", "args": {"a": 1}, "id": "1"}])
        assert _render_tool_call(msg) == ["[weird] {'a': 1}"]

    def test_empty_for_no_tool_calls(self) -> None:
        assert _render_tool_call(AIMessage(content="回答")) == []


class TestCreateReportFetchAgent:
    def test_raises_for_missing_dir(self) -> None:
        import pytest

        with pytest.raises(FileNotFoundError):
            create_report_fetch_agent("/tmp/opencode/does_not_exist")
