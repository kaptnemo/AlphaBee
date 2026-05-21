from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from deepagents import create_deep_agent, CompiledSubAgent
from langchain_openai import ChatOpenAI

from alphabee.agents.cross.prompts import CROSS_ANALYSIS_AGENT_PROMPT
from alphabee.agents.fundamental.agent import fundamental_agent
from alphabee.agents.market.agent import market_agent
from alphabee.agents.risk.agent import risk_agent
from alphabee.config import settings
from alphabee.tools.common import web_search



cross_agent = create_deep_agent(
    model=ChatOpenAI(
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    ),
    system_prompt=CROSS_ANALYSIS_AGENT_PROMPT,
    subagents=[
        CompiledSubAgent(
            name="FundamentalAgent",
            description=(
                "获取公司/行业多期财务数据：营收、净利润、ROE、ROA、毛利率、"
                "现金流、自由现金流、成长性指标（同比增速）等基本面信息。"
            ),
            runnable=fundamental_agent,
        ),
        CompiledSubAgent(
            name="MarketAgent",
            description=(
                "获取股票最新行情数据：当前价格、涨跌幅、成交额、换手率、"
                "市盈率PE、市净率PB、市值、北向资金、主力资金净流向等。"
            ),
            runnable=market_agent,
        ),
        CompiledSubAgent(
            name="RiskAgent",
            description=(
                "评估公司综合风险：财务杠杆风险、利润波动风险、市场价格风险、"
                "流动性风险、近期负面舆情与事件风险等。"
            ),
            runnable=risk_agent,
        ),
    ],
    middleware=[
        web_search_guard,
        check_message_limit,
    ],
    tools=[
        web_search,
    ]
)
