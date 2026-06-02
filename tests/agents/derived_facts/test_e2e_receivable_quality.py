"""
全链路集成测试：从用户提问到应收账款质量结论。

链路：用户提问
  → fact_collector_agent（调用 get_financial_fact，获取 accounts_receivable 数据）
  → derived_fact_agent（调用 evaluate_derived_facts，计算 accounts_receivable_yoy
                        与 receivable_growth_gap）
  → 最终给出应收账款质量结论

运行方式：
    poetry run pytest tests/agents/derived_facts/test_e2e_receivable_quality.py -v -m integration
"""

import json
import math
import re

import pytest
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, ToolMessage

load_dotenv()

# 测试标的：贵州茅台（数据完整、稳定）
TS_CODE = "600519.SH"
QUESTION = "茅台集团的应收账款质量怎么样？"


# ── 辅助函数 ────────────────────────────────────────────────────────────────

def _tool_calls(messages: list) -> list[dict]:
    """提取所有 tool_call 记录（name + args）。"""
    result = []
    for m in messages:
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                result.append({"name": tc["name"], "args": tc.get("args", {})})
    return result


def _tool_responses(messages: list, tool_name: str) -> list[str]:
    """提取指定工具的所有 ToolMessage 内容。"""
    return [
        m.content for m in messages
        if isinstance(m, ToolMessage) and tool_name in str(m.content)
    ]


def _extract_ar_from_financial_fact(messages: list) -> tuple[float, float] | None:
    """
    从 fact_collector_agent 消息历史中提取 get_financial_fact 工具返回的
    最近两期年报应收账款值，返回 (ar_current, ar_prev)。
    """
    for m in messages:
        if not isinstance(m, ToolMessage):
            continue
        try:
            data = json.loads(m.content)
        except (json.JSONDecodeError, TypeError):
            continue
        balance = data.get("balance", [])
        annual = [
            r for r in balance
            if str(r.get("period", "")).endswith("1231")
            and r.get("accounts_receivable") is not None
        ]
        if len(annual) >= 2:
            return float(annual[0]["accounts_receivable"]), float(annual[1]["accounts_receivable"])
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Stage 1：fact_collector_agent 获取应收账款事实
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestFactCollectorStage:
    """验证 fact_collector_agent 能根据应收账款质量问题获取正确财务数据。"""

    @pytest.fixture(scope="class")
    async def fact_result(self):
        from alphabee.agents.facts.agent import fact_collector_agent_factory
        agent = fact_collector_agent_factory()
        result = await agent.ainvoke({
            "messages": [HumanMessage(content=(
                f"请获取{TS_CODE}贵州茅台最近几期（至少两期年报）的财务数据，"
                "重点需要包含应收账款（accounts_receivable）数据。"
            ))]
        })
        return result

    def test_fact_collector_called_get_financial_fact(self, fact_result):
        """fact_collector_agent 应调用 get_financial_fact 工具。"""
        calls = _tool_calls(fact_result["messages"])
        names = [c["name"] for c in calls]
        assert "get_financial_fact" in names, (
            f"预期调用 get_financial_fact，实际调用了：{names}"
        )

    def test_get_financial_fact_queried_correct_symbol(self, fact_result):
        """get_financial_fact 的 symbol 参数应包含 600519。"""
        calls = _tool_calls(fact_result["messages"])
        for c in calls:
            if c["name"] == "get_financial_fact":
                symbol = c["args"].get("symbol", "")
                assert "600519" in symbol, f"symbol 应包含 600519，实际：{symbol}"
                return
        pytest.fail("未找到 get_financial_fact 调用")

    def test_tool_response_contains_accounts_receivable(self, fact_result):
        """get_financial_fact 的工具响应应包含 accounts_receivable 字段。"""
        for m in fact_result["messages"]:
            if not isinstance(m, ToolMessage):
                continue
            try:
                data = json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                continue
            balance = data.get("balance", [])
            if any("accounts_receivable" in r for r in balance):
                return
        pytest.fail("工具响应中未找到 accounts_receivable 数据")

    def test_tool_response_has_at_least_two_annual_periods(self, fact_result):
        """至少需要两期年报数据才能计算同比。"""
        for m in fact_result["messages"]:
            if not isinstance(m, ToolMessage):
                continue
            try:
                data = json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                continue
            balance = data.get("balance", [])
            annual = [r for r in balance if str(r.get("period", "")).endswith("1231")]
            if len(annual) >= 2:
                return
        pytest.fail("工具响应中年报期数不足 2 期")

    def test_final_response_mentions_receivable(self, fact_result):
        """fact_collector_agent 的最终回复应提及应收账款相关内容。"""
        final = fact_result["messages"][-1].content
        assert any(kw in final for kw in ["应收账款", "accounts_receivable"]), (
            f"最终回复未提及应收账款：{final[:300]}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Stage 2：derived_fact_agent 计算 accounts_receivable_yoy
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestDerivedFactStage:
    """验证 derived_fact_agent 能从 Stage 1 的事实数据计算 accounts_receivable_yoy。"""

    @pytest.fixture(scope="class")
    async def fact_result(self):
        from alphabee.agents.facts.agent import fact_collector_agent_factory
        agent = fact_collector_agent_factory()
        result = await agent.ainvoke({
            "messages": [HumanMessage(content=(
                f"请获取{TS_CODE}贵州茅台最近几期（至少两期年报）的财务数据，"
                "重点需要包含应收账款（accounts_receivable）数据。"
            ))]
        })
        return result

    @pytest.fixture(scope="class")
    def ar_facts(self, fact_result):
        """从 fact_collector 结果中提取两期 AR 值。"""
        facts = _extract_ar_from_financial_fact(fact_result["messages"])
        if facts is None:
            pytest.skip("无法从 fact_collector 结果中提取应收账款数据，跳过本阶段测试")
        return facts  # (ar_current, ar_prev)

    @pytest.fixture(scope="class")
    def expected_yoy(self, ar_facts):
        ar_current, ar_prev = ar_facts
        if ar_prev == 0:
            pytest.skip("上期应收账款为 0，无法计算同比")
        return (ar_current - ar_prev) / abs(ar_prev) * 100

    @pytest.fixture(scope="class")
    async def derived_result(self, ar_facts):
        from alphabee.agents.derived_facts.agent import derived_fact_agent_factory
        ar_current, ar_prev = ar_facts
        agent = derived_fact_agent_factory()
        result = await agent.ainvoke({
            "messages": [HumanMessage(content=(
                f"基于以下事实数据，请计算贵州茅台的应收账款同比增速并给出质量评估：\n\n"
                f"- 当期应收账款（accounts_receivable）：{ar_current} 元\n"
                f"- 上期应收账款（accounts_receivable_prev）：{ar_prev} 元\n\n"
                "请调用 evaluate_derived_facts，计算 accounts_receivable_yoy，"
                "并根据档位给出应收账款质量判断。"
            ))]
        })
        return result

    def test_derived_agent_called_evaluate_derived_facts(self, derived_result):
        calls = _tool_calls(derived_result["messages"])
        names = [c["name"] for c in calls]
        assert "evaluate_derived_facts" in names, (
            f"derived_fact_agent 应调用 evaluate_derived_facts，实际：{names}"
        )

    def test_derived_agent_used_correct_rule(self, derived_result):
        calls = _tool_calls(derived_result["messages"])
        for c in calls:
            if c["name"] == "evaluate_derived_facts":
                rules = c["args"].get("rule_names", [])
                assert "accounts_receivable_yoy" in rules, (
                    f"rule_names 应包含 accounts_receivable_yoy，实际：{rules}"
                )
                return
        pytest.fail("未找到 evaluate_derived_facts 调用")

    def test_derived_agent_passed_correct_fact_values(self, derived_result, ar_facts):
        ar_current, ar_prev = ar_facts
        calls = _tool_calls(derived_result["messages"])
        for c in calls:
            if c["name"] == "evaluate_derived_facts":
                fv = c["args"].get("fact_values", {})
                assert "accounts_receivable" in fv, "fact_values 缺少 accounts_receivable"
                assert "accounts_receivable_prev" in fv, "fact_values 缺少 accounts_receivable_prev"
                assert math.isclose(fv["accounts_receivable"], ar_current, rel_tol=1e-3), (
                    f"accounts_receivable 应为 {ar_current}，实际：{fv['accounts_receivable']}"
                )
                return
        pytest.fail("未找到 evaluate_derived_facts 调用")

    def test_tool_response_contains_correct_yoy_value(self, derived_result, expected_yoy):
        """工具响应中的 yoy 值与手动计算结果误差应在 0.1% 以内。"""
        responses = _tool_responses(derived_result["messages"], "accounts_receivable_yoy")
        assert responses, "未找到包含 accounts_receivable_yoy 的工具响应"

        output = responses[0]
        # 提取"计算值"行中的第一个浮点数
        value_line = next(
            (line for line in output.splitlines() if "计算值" in line),
            output,
        )
        match = re.search(r"-?\d+(?:\.\d+)?", value_line)
        assert match, f"无法从工具输出中解析数值：{output}"
        actual = float(match.group())
        assert math.isclose(actual, expected_yoy, rel_tol=0.001), (
            f"yoy 计算值 {actual:.4f}% 与预期 {expected_yoy:.4f}% 偏差过大"
        )

    def test_final_response_provides_quality_assessment(self, derived_result):
        """最终回复应给出应收账款质量相关的判断。"""
        final = derived_result["messages"][-1].content
        quality_keywords = [
            "应收账款", "账款质量", "回款", "增速", "同比",
            "fast", "slow", "moderate", "high_risk", "healthy", "low_risk",
        ]
        assert any(kw in final for kw in quality_keywords), (
            f"最终回复应包含质量判断，实际：{final[:300]}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3：全链路断言——两段输出的一致性
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestEndToEndConsistency:
    """验证 fact_collector 和 derived_fact 两段链路的数据一致性。"""

    @pytest.fixture(scope="class")
    async def full_chain(self):
        """运行完整链路，返回 (fact_result, derived_result, ar_facts)。"""
        from alphabee.agents.facts.agent import fact_collector_agent_factory
        from alphabee.agents.derived_facts.agent import derived_fact_agent_factory

        # Stage 1
        fact_agent = fact_collector_agent_factory()
        fact_result = await fact_agent.ainvoke({
            "messages": [HumanMessage(content=(
                f"请分析{TS_CODE}贵州茅台的应收账款情况，"
                "获取至少两期年报的应收账款数据。"
            ))]
        })

        ar_facts = _extract_ar_from_financial_fact(fact_result["messages"])
        if ar_facts is None:
            pytest.skip("无法提取应收账款数据")

        ar_current, ar_prev = ar_facts

        # Stage 2（用 QUESTION 作为起点，注入 Stage 1 取到的数据）
        derived_agent = derived_fact_agent_factory()
        derived_result = await derived_agent.ainvoke({
            "messages": [HumanMessage(content=(
                f"用户问题：{QUESTION}\n\n"
                f"事实收集员已获取贵州茅台（{TS_CODE}）的应收账款数据：\n"
                f"- 当期应收账款：{ar_current} 元\n"
                f"- 上期应收账款：{ar_prev} 元\n\n"
                "请计算 accounts_receivable_yoy，并结合档位判断给出应收账款质量结论。"
            ))]
        })

        return fact_result, derived_result, ar_facts

    def test_data_flows_from_fact_collector_to_derived_agent(self, full_chain):
        """从 fact_collector 取到的 AR 数据应与 derived_agent 调用工具时使用的值一致。"""
        _, derived_result, (ar_current, ar_prev) = full_chain
        calls = _tool_calls(derived_result["messages"])
        for c in calls:
            if c["name"] == "evaluate_derived_facts":
                fv = c["args"].get("fact_values", {})
                # LLM 可能用 accounts_receivable / accounts_receivable_current 等别名
                ar_val = (
                    fv.get("accounts_receivable")
                    or fv.get("accounts_receivable_current")
                    or next(
                        (v for k, v in fv.items()
                         if "receivable" in k and "prev" not in k and "prior" not in k),
                        None,
                    )
                )
                assert ar_val is not None, (
                    f"fact_values 中未找到 accounts_receivable 相关键，实际键：{list(fv.keys())}"
                )
                assert math.isclose(float(ar_val), ar_current, rel_tol=1e-3), (
                    f"两段链路的 accounts_receivable 值不一致：{ar_val} vs {ar_current}"
                )
                return
        pytest.fail("derived_fact_agent 未调用 evaluate_derived_facts")

    def test_final_answer_addresses_original_question(self, full_chain):
        """最终回复应回答用户关于应收账款质量的问题。"""
        _, derived_result, _ = full_chain
        final = derived_result["messages"][-1].content
        assert len(final) > 50, "最终回复内容过短"
        assert any(kw in final for kw in ["应收账款", "回款", "质量", "增速"]), (
            f"最终回复未回答应收账款质量问题：{final[:300]}"
        )
