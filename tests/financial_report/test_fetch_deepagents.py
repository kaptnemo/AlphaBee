import asyncio

import pytest
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage

from alphabee.financial_report import fetch_deepagents, report_cache
from alphabee.financial_report.fetch_deepagents import (
    ReportFilesystemBackend,
    _render_tool_call,
    create_report_fetch_agent,
)
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


class TestBackendPathGuardWiring:
    """P1：agent 工厂默认挂 ReportFilesystemBackend，未命中时复用 index 给提示。"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        clear_report_caches()
        yield
        clear_report_caches()

    @pytest.fixture
    def captured_backends(self, monkeypatch: pytest.MonkeyPatch) -> list[FilesystemBackend]:
        backends: list[FilesystemBackend] = []

        class _FakeAgent:
            pass

        def fake_create_deep_agent(**kwargs):
            backends.append(kwargs["backend"])
            return _FakeAgent()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", fake_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())
        return backends

    def test_uses_report_filesystem_backend_by_default(
        self, tmp_path, captured_backends: list[FilesystemBackend]
    ) -> None:
        (tmp_path / "第一节_公司简介.md").write_text("公司简介\n", encoding="utf-8")

        create_report_fetch_agent(tmp_path)

        [backend] = captured_backends
        assert isinstance(backend, ReportFilesystemBackend)
        # backend 与 index 绑定：未命中路径直接拿到真实候选，无需重新扫描目录
        result = backend.read("/缺章节/第一节_公司简介情况.md")
        assert result.error is not None
        assert "可能的真实路径（请直接使用其一）" in result.error
        assert "- /第一节_公司简介.md" in result.error

    def test_fallback_backend_has_no_path_hints(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, captured_backends: list[FilesystemBackend]
    ) -> None:
        (tmp_path / "a.md").write_text("x", encoding="utf-8")

        def boom(root):
            raise RuntimeError("index build failed")

        monkeypatch.setattr(report_cache, "build_report_index", boom)

        create_report_fetch_agent(tmp_path)

        [backend] = captured_backends
        assert isinstance(backend, FilesystemBackend)
        assert not isinstance(backend, ReportFilesystemBackend)


class TestBackendInstanceWiring:
    """守卫覆盖 subagent 的机制：工厂只建一个 backend 实例并把它交给 create_deep_agent。"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        clear_report_caches()
        yield
        clear_report_caches()

    def test_single_backend_instance_is_passed_to_create_deep_agent(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[ReportFilesystemBackend] = []

        class RecordingBackend(ReportFilesystemBackend):
            def __init__(self, root_dir, index) -> None:
                super().__init__(root_dir, index)
                created.append(self)

        passed_backends: list[FilesystemBackend] = []

        class _FakeAgent:
            pass

        def fake_create_deep_agent(**kwargs):
            passed_backends.append(kwargs["backend"])
            return _FakeAgent()

        monkeypatch.setattr(fetch_deepagents, "ReportFilesystemBackend", RecordingBackend)
        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", fake_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())
        (tmp_path / "第一节_公司简介.md").write_text("公司简介\n", encoding="utf-8")

        create_report_fetch_agent(tmp_path)

        # 只创建一个守卫 backend，并且原样交给 create_deep_agent（GP subagent 与主 agent 共用）
        assert len(created) == 1
        assert len(passed_backends) == 1
        assert passed_backends[0] is created[0]

    def test_passed_backend_guards_async_reads(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """子 agent 的工具走 BackendProtocol.aread（asyncio.to_thread(read)）。"""
        captured: list[FilesystemBackend] = []

        class _FakeAgent:
            pass

        def fake_create_deep_agent(**kwargs):
            captured.append(kwargs["backend"])
            return _FakeAgent()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", fake_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())
        (tmp_path / "第一节_公司简介.md").write_text("公司简介\n", encoding="utf-8")
        create_report_fetch_agent(tmp_path)

        [backend] = captured
        assert isinstance(backend, ReportFilesystemBackend)
        result = asyncio.run(backend.aread("/缺章节/第一节_公司简介情况.md"))
        assert result.error is not None
        assert "- /第一节_公司简介.md" in result.error


class TestPathGuardSwitch:
    """§3.4/§5.4：path_guard_enabled 单独控制 backend 守卫，index/prompt 不受影响。"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        clear_report_caches()
        yield
        clear_report_caches()

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        calls: list[dict] = []

        class _FakeAgent:
            pass

        def fake_create_deep_agent(**kwargs):
            calls.append(kwargs)
            return _FakeAgent()

        monkeypatch.setattr(fetch_deepagents, "create_deep_agent", fake_create_deep_agent)
        monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: object())
        return calls

    def test_guard_enabled_uses_report_backend(self, tmp_path, captured: list[dict]) -> None:
        (tmp_path / "a.md").write_text("x", encoding="utf-8")

        create_report_fetch_agent(tmp_path)

        [call] = captured
        assert isinstance(call["backend"], ReportFilesystemBackend)
        assert "<report_paths>" in call["system_prompt"]  # index 仍用于 prompt

    def test_guard_disabled_falls_back_to_plain_backend(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, captured: list[dict]
    ) -> None:
        monkeypatch.setattr(
            report_cache, "REPORT_CACHE_CONFIG", report_cache.ReportCacheConfig(path_guard_enabled=False)
        )
        (tmp_path / "a.md").write_text("x", encoding="utf-8")

        create_report_fetch_agent(tmp_path)

        [call] = captured
        backend = call["backend"]
        assert isinstance(backend, FilesystemBackend)
        assert not isinstance(backend, ReportFilesystemBackend)
        # 关掉的只是路径提示：index 仍用于压缩目录树 prompt
        assert "<report_paths>" in call["system_prompt"]
        assert "- a.md" in call["system_prompt"]
        # 普通 backend 未命中时不附加候选
        result = backend.read("/缺章节/完全不存在.md")
        assert result.error is not None
        assert "可能的真实路径" not in result.error
