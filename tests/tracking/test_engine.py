"""P6（§8）L2 引擎协议测试：``tracking/engine.py`` + ``tracking/engines/pipeline_engine.py`` + collectors 复用
（``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.6-D）。

覆盖 §15.8 / §15.6-D 的五条：

1. **Protocol 结构化满足**（``name`` + ``run`` 存在且可调用；stub 引擎同样满足）；
2. **context 映射**（monkeypatch ``alphabee_agent.ainvoke`` → 捕获 initial state，断言 ``run.context`` 四键）；
3. **延迟 import**（**子进程实测** + AST：``import alphabee.tracking[.engines]`` 不拉起 ``orchestrator.collectors``，
   也不写 ``$HOME/tk.csv``）；
4. **stub 引擎可替换**（消费方按协议调用 stub，接缝可用）；
5. **run 复用回归**（``collect_raw_facts`` 在已有 ``run`` 时**合并** context 而非覆盖；无 run 时语义逐字不变）。

外加：``to_output`` 映射表、fail-open（引擎异常 ⇒ ``degraded=True`` 不抛）、v1 非目标钉子
（``NODE_ORDER`` 未变 / 不改 ``agent.py`` / 无外部引擎依赖）。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alphabee.tracking.engine import ResearchContext, ResearchEngine, ResearchOutput
from alphabee.tracking.engines.pipeline_engine import INJECTED_CONTEXT_KEYS, PipelineEngine, to_output

REPO_ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "600519.SH"
THESIS = "锂电需求超预期，毛利率触底回升"


def _context(**overrides) -> ResearchContext:
    base: dict = {
        "symbol": SYMBOL,
        "question": f"分析一下{SYMBOL}",
        "as_of": "2026-09-20",
        "thesis": THESIS,
        "prior_confidence": 0.42,
        "evidence_ids": ["ev-1", "ev-2"],
        "open_questions": ["毛利率是否已触底？"],
    }
    base.update(overrides)
    return ResearchContext(**base)


class _StubEngine:
    """最小 stub 引擎：只实现协议要求的两件事（不改任何既有代码即插即用）。"""

    name = "stub"

    def __init__(self) -> None:
        self.seen: list[ResearchContext] = []

    async def run(self, context: ResearchContext) -> ResearchOutput:
        self.seen.append(context)
        return ResearchOutput(engine=self.name, summary=f"stub 处理 {context.symbol}")


async def _consume(engine: ResearchEngine, context: ResearchContext) -> ResearchOutput:
    """消费方样例：只依赖**协议**，不关心具体引擎。"""
    return await engine.run(context)


# ── ① 协议 / 数据模型（§15.6-A） ────────────────────────────────────────────


def test_context_and_output_fields_match_the_spec():
    assert list(ResearchContext.model_fields) == [
        "symbol",
        "question",
        "as_of",
        "thesis",
        "prior_confidence",
        "evidence_ids",
        "open_questions",
    ]
    assert list(ResearchOutput.model_fields) == [
        "engine",
        "summary",
        "artifacts",
        "issues",
        "degraded",
        "degradation_reason",
    ]
    empty_context = ResearchContext(symbol=SYMBOL)
    assert (empty_context.question, empty_context.as_of, empty_context.thesis) == ("", "", "")
    assert (empty_context.prior_confidence, empty_context.evidence_ids, empty_context.open_questions) == (
        None,
        [],
        [],
    )
    empty_output = ResearchOutput()
    assert (empty_output.engine, empty_output.summary, empty_output.degraded, empty_output.degradation_reason) == (
        "",
        "",
        False,
        "",
    )
    assert empty_output.artifacts == [] and empty_output.issues == []


def test_protocol_is_a_protocol_and_structurally_satisfied():
    assert getattr(ResearchEngine, "_is_protocol", False) is True
    # 非阻塞：结构化满足（不继承、不注册）
    engine = PipelineEngine()
    assert isinstance(engine, ResearchEngine)
    assert engine.name == "pipeline"
    assert callable(engine.run)
    assert isinstance(_StubEngine(), ResearchEngine)


def test_stub_engine_is_a_drop_in_replacement():
    """接缝可用：消费方只依赖协议 ⇒ 换成 stub 也能跑通（未来替换外部引擎的证明）。"""
    stub = _StubEngine()

    output = _run(_consume(stub, _context()))

    assert output.engine == "stub"
    assert output.summary == f"stub 处理 {SYMBOL}"
    assert stub.seen[0].evidence_ids == ["ev-1", "ev-2"]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ── ② context 映射（§15.6-C 的四键注入边界） ────────────────────────────────


def test_initial_state_maps_context_and_flags():
    engine = PipelineEngine(enhance=True, llm_review=False, midterm=True)

    state = engine.initial_state(_context())

    run = state["run"]
    assert list(run.context) == list(INJECTED_CONTEXT_KEYS)
    assert run.context == {
        "symbol": SYMBOL,
        "as_of": "2026-09-20",
        "thesis_prior": THESIS,
        "prior_confidence": 0.42,
    }
    assert run.goal == f"分析一下{SYMBOL}"
    assert state["enhance"] is True and state["llm_review"] is False and state["midterm"] is True
    assert state["messages"][0].content == f"分析一下{SYMBOL}"


def test_initial_state_never_injects_evidence_ids_or_open_questions():
    """§15.6-C：``evidence_ids`` / ``open_questions`` **不注入**（主图无消费者 ⇒ 注入即 dead-end）。"""
    state = PipelineEngine().initial_state(_context())

    assert "evidence_ids" not in state
    assert "open_questions" not in state
    assert "evidence_ids" not in state["run"].context
    assert "open_questions" not in state["run"].context


def test_initial_state_falls_back_to_symbol_as_the_message():
    state = PipelineEngine().initial_state(_context(question=""))

    assert state["messages"][0].content == SYMBOL


def test_run_passes_the_initial_state_to_the_graph(monkeypatch):
    """真跑 ``PipelineEngine.run``：捕获传给 ``alphabee_agent.ainvoke`` 的 initial state。"""
    import alphabee.orchestrator.agent as agent_module

    captured: dict = {}

    class _FakeAgent:
        async def ainvoke(self, initial, *args, **kwargs):
            captured["initial"] = initial
            return {
                "messages": [],
                "artifacts": [],
                "issues": [],
            }

    monkeypatch.setattr(agent_module, "alphabee_agent", _FakeAgent())

    output = _run(PipelineEngine(midterm=True).run(_context()))

    assert captured["initial"]["run"].context["thesis_prior"] == THESIS
    assert captured["initial"]["midterm"] is True
    assert output.engine == "pipeline"
    assert output.degraded is False


def test_run_is_fail_open_when_the_graph_raises(monkeypatch):
    import alphabee.orchestrator.agent as agent_module

    class _BoomAgent:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("graph exploded")

    monkeypatch.setattr(agent_module, "alphabee_agent", _BoomAgent())

    output = _run(PipelineEngine().run(_context()))

    assert output.degraded is True
    assert output.engine == "pipeline"
    assert "graph exploded" in output.degradation_reason


# ── ③ 延迟 import（子进程实测 + AST） ──────────────────────────────────────


@pytest.mark.parametrize("module", ["alphabee.tracking", "alphabee.tracking.engines", "alphabee.tracking.engine"])
def test_importing_tracking_does_not_pull_orchestrator_or_write_tk_csv(module, tmp_path):
    """子进程实测：import 后 ``orchestrator.collectors`` **不在** ``sys.modules``，且 ``$HOME/tk.csv`` 未被创建。

    （``tushare.set_token`` 会写 ``$HOME/tk.csv``：文件不存在即"没拉起 tushare"的最硬证据。）
    """
    home = tmp_path / "home"
    home.mkdir()
    script = (
        "import sys;"
        f"import {module};"
        "assert 'alphabee.orchestrator.collectors' not in sys.modules, 'collectors 被拉起';"
        "assert 'alphabee.orchestrator.agent' not in sys.modules, 'agent 被拉起';"
        "assert 'tushare' not in sys.modules, 'tushare 被拉起';"
        "print('ok')"
    )

    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "ok"
    assert not (home / "tk.csv").exists(), "import 副作用写了 $HOME/tk.csv"


def test_pipeline_engine_module_level_imports_stay_free_of_orchestrator():
    """AST：``pipeline_engine`` 的**模块级** import 不含 ``alphabee.orchestrator``；它在 ``run`` 方法体内。"""
    path = Path("alphabee/tracking/engines/pipeline_engine.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))

    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module)
    assert not any(name.startswith("alphabee.orchestrator") for name in module_level), module_level

    run_fn = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "PipelineEngine"
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    )
    inside = {stmt.module for stmt in ast.walk(run_fn) if isinstance(stmt, ast.ImportFrom) and stmt.module}
    assert "alphabee.orchestrator.agent" in inside, "延迟 import 必须写在方法体内"


def test_engine_protocol_module_imports_only_pydantic_typing():
    """协议模块本身零 orchestrator 依赖（纯数据契约 + Protocol）。"""
    tree = ast.parse(Path("alphabee/tracking/engine.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported == {"__future__", "typing", "pydantic"}


# ── ④ to_output 映射表 ─────────────────────────────────────────────────────


def _final_state(payload: dict | None, *, artifacts=None, issues=None) -> dict:
    messages = []
    if payload is not None:
        from langchain_core.messages import AIMessage

        messages = [AIMessage(content=json.dumps(payload, ensure_ascii=False))]
    return {"messages": messages, "artifacts": list(artifacts or []), "issues": list(issues or [])}


def test_to_output_maps_payload_fields():
    payload = {
        "run": {"status": "succeeded"},
        "final_report": {"summary": "结论摘要"},
        "artifacts": [{"id": "a1", "type": "report"}],
        "issues": [{"id": "i1", "category": "missing_data"}],
    }

    output = to_output(_final_state(payload))

    assert output.engine == "pipeline"
    assert output.summary == "结论摘要"
    assert output.artifacts == [{"id": "a1", "type": "report"}]
    assert output.issues == [{"id": "i1", "category": "missing_data"}]
    assert (output.degraded, output.degradation_reason) == (False, "")


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_to_output_flags_degraded_run_statuses(status):
    payload = {"run": {"status": status}, "final_report": {}, "artifacts": [], "issues": [{"id": "i1"}]}

    output = to_output(_final_state(payload))

    assert output.degraded is True
    assert f"run.status={status}" in output.degradation_reason
    assert "issues=1" in output.degradation_reason


def test_to_output_treats_unknown_status_as_not_degraded():
    """保守：无法判定 ⇒ **不**判降级（不把"未知"当"降级"）。"""
    for payload in ({"final_report": {}}, {"run": {}, "final_report": {}}, {"run": {"status": ""}}):
        assert to_output(_final_state(payload)).degraded is False


def test_to_output_falls_back_to_state_objects_without_a_payload():
    from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity

    artifact = Artifact(id="a1", type=ArtifactType.REPORT, producer_step="generate_report", value={"x": 1})
    issue = Issue(id="i1", severity=IssueSeverity.HIGH, category="missing_data", message="m")

    output = to_output(_final_state(None, artifacts=[artifact], issues=[issue]))

    assert output.artifacts[0]["id"] == "a1"
    assert output.issues[0]["id"] == "i1"
    assert output.summary == ""


def test_to_output_is_total_over_garbage_inputs():
    """fail-open：畸形输入只降级为默认值，不抛。"""

    class _Garbage:
        def get(self, key, default=None):
            return "not-a-list"

    for final in (_Garbage(), {}, {"messages": "not-a-list"}, {"messages": [object()]}):
        output = to_output(final)
        assert isinstance(output, ResearchOutput)
        assert output.artifacts == [] and output.issues == []


# ── ⑤ collectors run 复用回归（§15.6-C） ───────────────────────────────────


class _StubSubAgent:
    async def ainvoke(self, *args, **kwargs):
        from langchain_core.messages import AIMessage

        return {"messages": [AIMessage(content="fact narrative")]}


class _StubFacts:
    def to_fact_values(self) -> dict[str, float]:
        return {"revenue": 1.0}


async def _collect(monkeypatch, state: dict) -> dict:
    import importlib

    collectors = importlib.import_module("alphabee.orchestrator.collectors")
    monkeypatch.setattr(collectors, "fact_collector_agent_factory", lambda: _StubSubAgent())
    monkeypatch.setattr(collectors, "get_financial_facts_model", lambda symbol: _StubFacts())
    monkeypatch.setattr(collectors, "get_market_facts_model", lambda symbol: _StubFacts())

    # query / symbol 抽取与 run 建立已拆到上游 prepare_analysis_context 节点，这里按图顺序先跑它。
    from alphabee.orchestrator.nodes.prepare_analysis_context import prepare_analysis_context

    ctx = await prepare_analysis_context(state, {})
    result = await collectors.collect_raw_facts({**state, "run": ctx["run"]}, {})
    result["run"] = ctx["run"]
    return result


def test_prepare_analysis_context_merges_incoming_run_context(monkeypatch):
    """run 复用回归（§15.6-C 的落地细节）：已有 run ⇒ **保留** id/goal/status/started_at + 合并 context。"""
    from langchain_core.messages import HumanMessage

    from alphabee.core import Run, RunStatus

    injected = Run(
        id="engine-run-1",
        goal="分析一下贵州茅台",
        status=RunStatus.RUNNING,
        context={
            "symbol": SYMBOL,
            "as_of": "2026-09-20",
            "thesis_prior": THESIS,
            "prior_confidence": 0.42,
        },
    )

    result = _run(_collect(monkeypatch, {"messages": [HumanMessage(content="分析一下贵州茅台")], "run": injected}))
    run = result["run"]

    assert run.id == "engine-run-1"  # ★ 不被覆盖
    assert run.goal == injected.goal
    assert run.started_at == injected.started_at
    # 注入的 4 键仍在，且本节点解析出的 query/symbol 已并入
    assert run.context["as_of"] == "2026-09-20"
    assert run.context["thesis_prior"] == THESIS
    assert run.context["prior_confidence"] == 0.42
    assert run.context["query"] == "分析一下贵州茅台"
    assert run.context["symbol"] == SYMBOL


def test_prepare_analysis_context_keeps_injected_symbol_when_query_has_none(monkeypatch):
    """查询串里解析不到标的时**保留**注入的 symbol（不写成 ``None``）。"""
    from langchain_core.messages import HumanMessage

    from alphabee.core import Run, RunStatus

    injected = Run(id="engine-run-2", goal="问题", status=RunStatus.RUNNING, context={"symbol": SYMBOL})

    result = _run(_collect(monkeypatch, {"messages": [HumanMessage(content="护城河如何？")], "run": injected}))

    assert result["run"].context["symbol"] == SYMBOL
    assert result["run"].context["query"] == "护城河如何？"


def test_prepare_analysis_context_without_incoming_run_keeps_legacy_semantics(monkeypatch):
    """零回归：调用方未提供 run ⇒ 与既有实现逐字同形（新建 run，context 恰为 query/symbol）。"""
    from langchain_core.messages import HumanMessage

    result = _run(_collect(monkeypatch, {"messages": [HumanMessage(content="分析一下贵州茅台")]}))
    run = result["run"]

    assert run.id.startswith("orch-run-")
    assert run.context == {"query": "分析一下贵州茅台", "symbol": SYMBOL}
    assert run.goal == "分析一下贵州茅台"


# ── ⑥ v1 非目标钉子 ────────────────────────────────────────────────────────


def test_node_order_and_graph_baseline():
    """基线锚：``NODE_ORDER`` / 图结构 / 契约键集指纹必须与当前实现一致。

    说明：``prepare_analysis_context`` 已从 ``collect_raw_facts`` 拆出并登记为**首个**节点
    （query/symbol 抽取 + run 建立），因此原 v1 的"不改图结构"钉子已随该架构变更更新为新基线。
    """
    import hashlib

    from alphabee.orchestrator.agent import alphabee_agent
    from alphabee.orchestrator.node_contracts import NODE_CONTRACTS, validate_contracts
    from alphabee.orchestrator.services.deviation import NODE_ORDER

    assert len(NODE_CONTRACTS) == 17
    assert hashlib.sha256("|".join(sorted(NODE_CONTRACTS)).encode()).hexdigest()[:16] == "1857784a5cb634e3"
    assert validate_contracts() == []
    assert NODE_ORDER[0] == "prepare_analysis_context" and NODE_ORDER[1] == "collect_raw_facts"
    assert NODE_ORDER[-1] == "finalize_message"
    assert len(NODE_ORDER) == 17
    assert "collect_raw_facts" in alphabee_agent.get_graph().nodes
    assert "prepare_analysis_context" in alphabee_agent.get_graph().nodes


def test_no_external_engine_dependency_is_introduced():
    """非目标：不接入任何外部引擎 —— engines 子包源码不出现 MiroThinker / MiroFlow / Tongyi。"""
    sources = "\n".join(
        Path("alphabee/tracking/engines").joinpath(name).read_text(encoding="utf-8")
        for name in ("__init__.py", "pipeline_engine.py")
    )

    for needle in ("MiroThinker", "MiroFlow", "Tongyi", "miro", "tongyi"):
        assert needle not in sources
