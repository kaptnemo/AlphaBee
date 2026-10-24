"""本地财报目录定位（decide_root_path 等）测试。

覆盖新嵌套结构（``<公司>(<代码>)/财报/<报告期>/``）与旧平铺结构
（``<报告名>/``）的兼容定位；REPORT_DIR 通过 monkeypatch 指向临时目录，
不触碰真实 reports/。
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from alphabee.financial_report import fetch_deepagents, report_cache
from alphabee.tools import financial_report as fr
from alphabee.tools.cache import SyncTTLCache
from alphabee.tools.financial_report import FinancialReportRequest


@pytest.fixture
def reports_root(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "REPORT_DIR", tmp_path)
    # 新嵌套结构
    (tmp_path / "宁德时代(300750)" / "财报" / "2026年半年度报告").mkdir(parents=True)
    (tmp_path / "宁德时代(300750)" / "财报" / "2025年年度报告").mkdir(parents=True)
    # 旧平铺结构
    (tmp_path / "贵州茅台：2025年年度报告").mkdir(parents=True)
    return tmp_path


def test_decide_nested_by_name_year_type(reports_root):
    path = fr.decide_root_path(
        FinancialReportRequest(company_name="宁德时代", year=2026, report_type="semiannual", query="x")
    )
    assert path == str(reports_root / "宁德时代(300750)" / "财报" / "2026年半年度报告")


def test_decide_nested_by_code(reports_root, monkeypatch):
    # 代码无法反查公司名时，仍能靠 ``<公司>(<代码>)`` 括号内代码直接命中
    monkeypatch.setattr(fr, "resolve_company_name_by_code", lambda code: None)
    path = fr.decide_root_path(
        FinancialReportRequest(company_code="300750", year=2025, report_type="annual", query="x")
    )
    assert path == str(reports_root / "宁德时代(300750)" / "财报" / "2025年年度报告")


def test_decide_flat_legacy(reports_root):
    path = fr.decide_root_path(
        FinancialReportRequest(company_name="贵州茅台", year=2025, report_type="annual", query="x")
    )
    assert path == str(reports_root / "贵州茅台：2025年年度报告")


def test_decide_ambiguous_year_only_returns_none(reports_root):
    # 仅年份命中多家公司，存在歧义 → None
    assert fr.decide_root_path(FinancialReportRequest(year=2025, query="x")) is None


def test_decide_missing_company_returns_none(reports_root):
    assert fr.decide_root_path(FinancialReportRequest(query="x")) is None


def test_list_available_reports_nested_and_flat(reports_root):
    names = fr._list_available_reports("宁德时代")
    assert names == [
        "宁德时代(300750)/财报/2025年年度报告",
        "宁德时代(300750)/财报/2026年半年度报告",
    ]
    flat = fr._list_available_reports("贵州茅台")
    assert flat == ["贵州茅台：2025年年度报告"]


def test_extract_code():
    assert fr._extract_code("宁德时代(300750)") == "300750"
    assert fr._extract_code("贵州茅台：2025年年度报告") == ""
    assert fr._extract_code("某公司(abc123)") == ""


def test_normalize_locator_code():
    assert fr._normalize_locator_code("300750.SZ") == "300750"
    assert fr._normalize_locator_code("600519") == "600519"


# ── P2：query_financial_report 走 agent 缓存 ────────────────────────────────

SAMPLE_REPORT = "宁德时代(300750)/财报/2025年年度报告"


class _EchoModel(BaseChatModel):
    """假模型：回显最后一条 HumanMessage，不发真实请求。"""

    @property
    def _llm_type(self) -> str:
        return "echo-chat-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last_human = next((msg for msg in reversed(messages) if isinstance(msg, HumanMessage)), None)
        text = last_human.content if last_human is not None else ""
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"echo: {text}"))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.fixture
def cached_report_root(tmp_path, monkeypatch):
    """临时 reports/：一份可被定位的报告 + 干净的定位/agent 缓存。"""
    monkeypatch.setattr(fr, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(fr, "_REPORT_LOCATE_CACHE", SyncTTLCache())
    report_dir = tmp_path / SAMPLE_REPORT
    (report_dir / "第一节").mkdir(parents=True)
    (report_dir / "第一节" / "营业收入.md").write_text("营业收入 1 亿元", encoding="utf-8")
    report_cache.clear_report_caches()
    yield report_dir
    report_cache.clear_report_caches()


async def test_query_financial_report_uses_agent_cache(cached_report_root, monkeypatch):
    report_dir = cached_report_root
    monkeypatch.setattr(fetch_deepagents, "create_chat_model", lambda component: _EchoModel())
    real_create = fetch_deepagents.create_deep_agent
    build_calls: list[int] = []

    def flaky_create_deep_agent(**kwargs):
        build_calls.append(1)
        if len(build_calls) == 1:
            raise RuntimeError("compile boom")  # 首次缓存构建失败 → fail-open 回退
        return real_create(**kwargs)

    monkeypatch.setattr(fetch_deepagents, "create_deep_agent", flaky_create_deep_agent)

    request = FinancialReportRequest(company_name="宁德时代", year=2025, report_type="annual", query="营业收入？")

    answer = await fr.query_financial_report(request)
    assert answer == "echo: 营业收入？"
    assert len(build_calls) == 2  # 1 次缓存构建失败 + 1 次回退构建成功
    assert report_cache.stats()["agent"]["entries"] == 0  # 回退产物不进缓存

    # 第二次查询重新尝试缓存构建并成功
    assert await fr.query_financial_report(request) == "echo: 营业收入？"
    assert len(build_calls) == 3
    assert report_cache.stats()["agent"]["entries"] == 1

    # 第三次查询命中 agent 缓存：不再构建 graph
    assert await fr.query_financial_report(request) == "echo: 营业收入？"
    assert len(build_calls) == 3

    # 失效后重建
    report_cache.invalidate_report_caches(report_dir)
    assert await fr.query_financial_report(request) == "echo: 营业收入？"
    assert len(build_calls) == 4


async def test_query_financial_report_not_found_keeps_reason_code(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(fr, "_REPORT_LOCATE_CACHE", SyncTTLCache())

    answer = await fr.query_financial_report(
        FinancialReportRequest(company_name="不存在公司", year=2025, query="营业收入？")
    )

    assert answer.startswith(fr.REASON_REPORT_NOT_FOUND)


def test_query_financial_report_is_wired_to_cache_entry() -> None:
    # 工具层直接复用 report_cache 的异步缓存入口（而不是同步工厂）
    assert fr.aget_report_agent is report_cache.aget_report_agent


async def test_query_financial_report_no_answer_keeps_reason_code(cached_report_root, monkeypatch):
    class _SilentAgent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [HumanMessage(content="q"), AIMessage(content="")]}

    async def fake_aget_report_agent(root, **kwargs):
        return _SilentAgent()

    monkeypatch.setattr(fr, "aget_report_agent", fake_aget_report_agent)

    answer = await fr.query_financial_report(
        FinancialReportRequest(company_name="宁德时代", year=2025, report_type="annual", query="q")
    )

    assert answer.startswith(fr.REASON_AGENT_NO_ANSWER)
