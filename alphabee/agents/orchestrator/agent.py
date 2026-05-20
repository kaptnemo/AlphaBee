from deepagents import create_deep_agent, CompiledSubAgent
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_openai import ChatOpenAI

from alphabee.agents.orchestrator.prompts import ALPHABEE_SYSTEM_PROMPT
from alphabee.config import settings
from alphabee.agents.fundamental.agent import fundamental_agent
from alphabee.agents.market.agent import market_agent
from alphabee.agents.risk.agent import risk_agent
from alphabee.agents.cross.agent import cross_agent
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit


model = ChatOpenAI(
    model=settings.llm.model,
    api_key=settings.llm.api_key,
    base_url=settings.llm.base_url,
)


alphabee_agent = create_deep_agent(
    model=model,
    system_prompt=ALPHABEE_SYSTEM_PROMPT,
    subagents=[
        CompiledSubAgent(
            name="FundamentalAgent",
            description="负责分析公司的基本面，包括财务数据、业务模式、竞争优势等。",
            runnable=fundamental_agent,
        ),
        CompiledSubAgent(
            name="MarketAgent",
            description="负责分析市场数据，包括股票价格、交易量、市场趋势等。",
            runnable=market_agent,
        ),
        CompiledSubAgent(
            name="RiskAgent",
            description="负责分析公司的风险，包括财务风险、市场风险、运营风险等。",
            runnable=risk_agent,
        ),
        CompiledSubAgent(
            name="CrossAnalysisAgent",
            description=(
                "交叉比对分析师：同时调用基本面、行情、风险三个子代理，"
                "系统性发现各维度之间的矛盾、背离、异常信号与潜在机会。"
                "适合综合分析、寻找投资机会、发现风险信号等场景。"
            ),
            runnable=cross_agent,
        ),
    ],
    middleware=[
        # TodoListMiddleware(),
        # LLMToolSelectorMiddleware(
        #     model=model,
        #     # Qwen API requires the word "json" in the prompt when response_format=json_object
        #     system_prompt="Select the most relevant tools for the user's query. Respond in JSON format.",
        # ),
        ToolRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
        check_message_limit,
    ],
    tools=[web_search]
)
