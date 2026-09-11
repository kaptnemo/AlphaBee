"""图接线 + midterm flag 门控测试（Phase 2）。

注意：``alphabee.orchestrator.agent`` 在 import 时会传递性触发 tushare 的
``set_token``（无 token 时向 ``$HOME/tk.csv`` 写文件）。workspace-write 沙箱下
``$HOME=/home/chenyiwei`` 不可写，故本模块在 import agent 前把 ``HOME`` 重定向到
workspace 内临时目录，并在退出时清理，避免污染仓库根目录。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP_HOME = Path(tempfile.mkdtemp(prefix="midterm_routing_home_"))
os.environ["HOME"] = str(_TMP_HOME)


def _cleanup_tmp_home() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


atexit.register(_cleanup_tmp_home)

from alphabee.orchestrator.agent import alphabee_agent, route_after_thesis  # noqa: E402


def _edges():
    graph = alphabee_agent.get_graph()
    edges = []
    for edge in graph.edges:
        edges.append((edge.source, edge.target, bool(edge.conditional)))
    return edges


def _outgoing(node: str):
    return [(target, conditional) for source, target, conditional in _edges() if source == node]


def test_route_after_thesis_off_routes_directly_to_report():
    assert route_after_thesis({"midterm": False}) == "generate_report"
    # 未注入 midterm 字段时默认关（向后兼容，与现状路径一致）
    assert route_after_thesis({}) == "generate_report"


def test_route_after_thesis_on_routes_to_midterm():
    assert route_after_thesis({"midterm": True}) == "resolve_midterm_decision"


def test_graph_review_thesis_has_both_branches_conditional():
    outgoing = _outgoing("review_thesis")
    assert ("resolve_midterm_decision", True) in outgoing
    assert ("generate_report", True) in outgoing
    # 原先的单边 review_thesis→generate_report 已改为条件边，不应残留无条件直连
    assert ("generate_report", False) not in outgoing


def test_graph_midterm_node_feeds_reporter_then_generate_report():
    # 决策层产物先经独立 reporter 渲染总结（不进报告），再由 reporter 接回主链出报告。
    assert ("midterm_decision_reporter", False) in _outgoing("resolve_midterm_decision")
    assert ("generate_report", False) in _outgoing("midterm_decision_reporter")


def test_graph_flag_off_path_has_no_midterm_node_between():
    # flag 关时 midterm 节点仍注册在图里（惰性节点），但路由不会经过它；
    # 这里只保证 report 的入边仍来自 review_thesis（条件）或 midterm reporter（可选）。
    report_in = [(source, cond) for source, target, cond in _edges() if target == "generate_report"]
    assert ("review_thesis", True) in report_in
    assert ("midterm_decision_reporter", False) in report_in
