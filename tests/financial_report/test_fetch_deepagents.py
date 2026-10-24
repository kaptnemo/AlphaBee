import pytest
from langchain_core.messages import AIMessage

from alphabee.financial_report import fetch_deepagents, report_cache
from alphabee.financial_report.fetch_deepagents import _render_tool_call, create_report_fetch_agent
from alphabee.financial_report.report_cache import ReportIndex, build_report_index, clear_report_caches, stats


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


class TestCreateReportFetchAgentWithIndex:
    """P0：agent 工厂复用 index 缓存（agent 缓存本身属 P2，不在本阶段）。"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        clear_report_caches()
        yield
        clear_report_caches()

    @pytest.fixture
    def captured_prompts(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        prompts: list[str] = []

        class _FakeAgent:
            pass

        def fake_create_deep_agent(**kwargs):
            prompts.append(kwargs["system_prompt"])
            return _FakeAgent()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", fake_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())
        return prompts

    def test_reuses_index_across_builds(self, tmp_path, monkeypatch, captured_prompts: list[str]) -> None:
        (tmp_path / "第一节_公司简介.md").write_text("x")
        (tmp_path / "第二节_财务数据").mkdir()
        (tmp_path / "第二节_财务数据" / "(1)_营业收入.md").write_text("x")

        real_build = report_cache.build_report_index
        build_calls: list[object] = []

        def counting_build(root):
            build_calls.append(root)
            return real_build(root)

        monkeypatch.setattr(report_cache, "build_report_index", counting_build)

        first = create_report_fetch_agent(tmp_path)
        second = create_report_fetch_agent(tmp_path)

        # 每次仍新建 agent（P2 才缓存 agent），但目录只扫描一次
        assert first is not second
        assert len(build_calls) == 1
        assert stats()["builds"] == 1
        assert len(captured_prompts) == 2
        assert captured_prompts[0] == captured_prompts[1]
        assert "<report_paths>" in captured_prompts[0]
        assert "- 第二节_财务数据/(1)_营业收入.md" in captured_prompts[0]

    def test_accepts_explicit_index(self, tmp_path, monkeypatch, captured_prompts: list[str]) -> None:
        (tmp_path / "a.md").write_text("x")
        index = build_report_index(tmp_path)
        assert isinstance(index, ReportIndex)

        def boom(root):
            raise AssertionError("显式传入 index 时不应再构建 index")

        monkeypatch.setattr(report_cache, "build_report_index", boom)

        create_report_fetch_agent(tmp_path, index=index)

        assert len(captured_prompts) == 1
        assert "<report_paths>" in captured_prompts[0]
        assert "- a.md" in captured_prompts[0]

    def test_falls_back_when_index_build_fails(self, tmp_path, monkeypatch, captured_prompts: list[str]) -> None:
        (tmp_path / "a.md").write_text("x")

        def boom(root):
            raise RuntimeError("index build failed")

        monkeypatch.setattr(report_cache, "build_report_index", boom)

        # fail-open：index 构建失败时回退到旧行为（现场渲染完整目录树）
        create_report_fetch_agent(tmp_path)

        assert len(captured_prompts) == 1
        assert "<file_tree>" in captured_prompts[0]
        assert "a.md" in captured_prompts[0]
        assert stats()["entries"] == 0
