from deepagents import create_deep_agent, CompiledSubAgent
from langchain.agents.middleware import ToolRetryMiddleware

from alphabee.agents.orchestrator.prompts import ALPHABEE_SYSTEM_PROMPT
from alphabee.agents.fundamental.agent import fundamental_agent
from alphabee.agents.market.agent import market_agent
from alphabee.agents.risk.agent import risk_agent
from alphabee.agents.cross.agent import cross_agent
from alphabee.agents.industry.agent import industry_agent
from alphabee.tools.common import web_search, extract_symbols_from_query
from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.utils import create_chat_model


model = create_chat_model("agent.orchestrator")


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
        CompiledSubAgent(
            name="IndustryAgent",
            description=(
                "行业/产业基本面分析师：分析特定行业的整体景气度、估值水平（PE/PB历史分位）、"
                "近期价格表现（近1周/1月/3月/6月/1年涨跌）、行业总市值规模及成分股结构。"
                "适用场景：某个行业现在贵不贵、哪个行业最近表现最好、某行业的龙头股有哪些。"
                "不负责单只股票的个股分析。"
            ),
            runnable=industry_agent,
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
        web_search_guard,
        check_message_limit,
    ],
    tools=[
        extract_symbols_from_query,
        web_search,
    ]
)
