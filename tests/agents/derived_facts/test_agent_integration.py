"""
集成测试：验证 derived_fact_agent 能否结合真实 Tushare 数据计算 accounts_receivable_yoy。

运行方式（需要 TUSHARE_TOKEN 和 LLM API Key 配置）：
    poetry run pytest tests/agents/derived_facts/test_agent_integration.py -v -m integration

跳过集成测试（只跑单元测试）：
    poetry run pytest tests/ -m "not integration"
"""

import math
import re

import pytest
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, ToolMessage

load_dotenv()


# ── 辅助：从工具输出中提取第一个浮点数 ─────────────────────────────────────

def _extract_first_float(text: str) -> float | None:
    """从字符串中提取第一个出现的浮点数（含负号）。"""
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


# ═══════════════════════════════════════════════════════════════════════════
# Step 1 测试：Tushare 数据获取层（不依赖 LLM）
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestTushareFactCollection:
    """验证能从 Tushare 正确取到两期 accounts_receivable。"""

    TS_CODE = "600519.SH"       # 贵州茅台，数据稳定可靠
    START_DATE = "20220101"

    @pytest.fixture(scope="class")
    def balancesheet_df(self):
        from alphabee.collectors.tushare.helper import TuShareHelper
        with TuShareHelper() as helper:
            df = helper.balancesheet(
                ts_code=self.TS_CODE,
                fields="ts_code,end_date,accounts_receiv",
                start_date=self.START_DATE,
            ).data
        return df

    def test_returns_canonical_column_names(self, balancesheet_df):
        """Tushare adapter 应将 accounts_receiv 映射为 accounts_receivable。"""
        assert "accounts_receivable" in balancesheet_df.columns
        assert "period" in balancesheet_df.columns

    def test_has_at_least_two_annual_periods(self, balancesheet_df):
        annual = balancesheet_df[balancesheet_df["period"].str.endswith("1231")]
        assert len(annual) >= 2, "需要至少两期年报才能计算同比"

    def test_receivable_values_are_numeric(self, balancesheet_df):
        annual = balancesheet_df[balancesheet_df["period"].str.endswith("1231")].head(2)
        for val in annual["accounts_receivable"]:
            assert isinstance(float(val), float)
            assert not math.isnan(float(val))


# ═══════════════════════════════════════════════════════════════════════════
# Step 2 测试：Agent 端到端（依赖 LLM + Tushare）
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestAgentComputesArYoy:
    """
    全链路集成测试：
    Tushare balancesheet → accounts_receivable / accounts_receivable_prev
    → derived_fact_agent (LLM + evaluate_derived_facts)
    → accounts_receivable_yoy
    """

    TS_CODE = "600519.SH"
    START_DATE = "20220101"

    @pytest.fixture(scope="class")
    def tushare_facts(self):
        """从 Tushare 取最近两期年报应收账款，返回 (ar_current, ar_prev, period_current)。"""
        from alphabee.collectors.tushare.helper import TuShareHelper
        with TuShareHelper() as helper:
            df = helper.balancesheet(
                ts_code=self.TS_CODE,
                fields="ts_code,end_date,accounts_receiv",
                start_date=self.START_DATE,
            ).data
        annual = (
            df[df["period"].str.endswith("1231")]
            .head(2)
            .reset_index(drop=True)
        )
        assert len(annual) >= 2, "Tushare 数据不足，无法取到两期年报"

        ar_current = float(annual.iloc[0]["accounts_receivable"])
        ar_prev = float(annual.iloc[1]["accounts_receivable"])
        period_current = str(annual.iloc[0]["period"])

        if ar_prev == 0:
            pytest.skip("上期应收账款为 0，无法计算同比（除零场景由单元测试覆盖）")

        return ar_current, ar_prev, period_current

    @pytest.fixture(scope="class")
    def expected_yoy(self, tushare_facts):
        ar_current, ar_prev, _ = tushare_facts
        return (ar_current - ar_prev) / abs(ar_prev) * 100

    @pytest.fixture(scope="class")
    def expected_yoy_str(self, expected_yoy):
        """与 _format_result 的 :.4g 格式对齐。"""
        return f"{expected_yoy:.4g}"

    # ── 实际 Agent 调用（仅执行一次，结果共享给后续断言） ──────────────────

    @pytest.fixture(scope="class")
    async def agent_result(self, tushare_facts):
        from alphabee.agents.derived_facts.agent import derived_fact_agent_factory
        ar_current, ar_prev, period_current = tushare_facts

        agent = derived_fact_agent_factory()
        result = await agent.ainvoke({
            "messages": [HumanMessage(content=(
                f"请帮我计算贵州茅台（{self.TS_CODE}）的应收账款同比增速。\n\n"
                f"以下是从 Tushare 获取的应收账款数据：\n"
                f"- 当期（{period_current}）应收账款：{ar_current} 元\n"
                f"- 上期应收账款：{ar_prev} 元\n\n"
                "请调用 evaluate_derived_facts 工具，"
                "使用规则 accounts_receivable_yoy，"
                "fact_values 包含 accounts_receivable 和 accounts_receivable_prev。"
            ))]
        })
        return result

    # ── 断言 ─────────────────────────────────────────────────────────────

    async def test_agent_called_evaluate_derived_facts(self, agent_result):
        """Agent 应调用过 evaluate_derived_facts 工具。"""
        messages = agent_result["messages"]
        tool_names = [
            tc["name"]
            for m in messages
            if hasattr(m, "tool_calls") and m.tool_calls
            for tc in m.tool_calls
        ]
        assert "evaluate_derived_facts" in tool_names, (
            f"Agent 未调用 evaluate_derived_facts，实际调用了：{tool_names}"
        )

    async def test_tool_was_called_with_correct_rule(self, agent_result):
        """evaluate_derived_facts 的 rule_names 参数应包含 accounts_receivable_yoy。"""
        messages = agent_result["messages"]
        for m in messages:
            if not (hasattr(m, "tool_calls") and m.tool_calls):
                continue
            for tc in m.tool_calls:
                if tc["name"] == "evaluate_derived_facts":
                    args = tc.get("args", {})
                    rule_names = args.get("rule_names", [])
                    assert "accounts_receivable_yoy" in rule_names, (
                        f"rule_names 中缺少 accounts_receivable_yoy，实际为：{rule_names}"
                    )
                    return
        pytest.fail("未找到 evaluate_derived_facts 的工具调用记录")

    async def test_tool_was_called_with_correct_fact_values(self, agent_result, tushare_facts):
        """evaluate_derived_facts 的 fact_values 应包含从 Tushare 取到的两期应收账款。"""
        ar_current, ar_prev, _ = tushare_facts
        messages = agent_result["messages"]
        for m in messages:
            if not (hasattr(m, "tool_calls") and m.tool_calls):
                continue
            for tc in m.tool_calls:
                if tc["name"] == "evaluate_derived_facts":
                    fact_values = tc.get("args", {}).get("fact_values", {})
                    assert "accounts_receivable" in fact_values, \
                        "fact_values 缺少 accounts_receivable"
                    assert "accounts_receivable_prev" in fact_values, \
                        "fact_values 缺少 accounts_receivable_prev"
                    assert math.isclose(
                        fact_values["accounts_receivable"], ar_current, rel_tol=1e-3
                    ), f"accounts_receivable 值不匹配：{fact_values['accounts_receivable']} vs {ar_current}"
                    assert math.isclose(
                        fact_values["accounts_receivable_prev"], ar_prev, rel_tol=1e-3
                    ), f"accounts_receivable_prev 值不匹配：{fact_values['accounts_receivable_prev']} vs {ar_prev}"
                    return
        pytest.fail("未找到 evaluate_derived_facts 的工具调用记录")

    async def test_tool_response_contains_yoy_value(
        self, agent_result, expected_yoy, expected_yoy_str
    ):
        """工具返回结果中应包含正确的 accounts_receivable_yoy 数值。"""
        messages = agent_result["messages"]
        tool_responses = [
            m for m in messages
            if isinstance(m, ToolMessage) and "accounts_receivable_yoy" in str(m.content)
        ]
        assert tool_responses, "未找到包含 accounts_receivable_yoy 的工具响应"

        tool_output = tool_responses[0].content
        actual = _extract_first_float(
            # 提取 "计算值：xxx" 那一行
            next(
                (line for line in tool_output.splitlines() if "计算值" in line),
                tool_output,
            )
        )
        assert actual is not None, f"无法从工具输出中解析数值：{tool_output}"
        assert math.isclose(actual, expected_yoy, rel_tol=0.001), (
            f"计算值 {actual:.4f}% 与预期 {expected_yoy:.4f}% 偏差超过 0.1%"
        )

    async def test_final_response_mentions_yoy(self, agent_result):
        """Agent 最终回复应提及应收账款同比增速相关内容。"""
        final_msg = agent_result["messages"][-1].content
        assert any(
            keyword in final_msg
            for keyword in ["accounts_receivable_yoy", "应收账款", "同比", "增速"]
        ), f"最终回复未提及相关内容：{final_msg[:200]}"
