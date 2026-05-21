"""共享 Prompt 片段，供多个 Agent 引用。"""

WEB_SEARCH_BOUNDARY = """
## web_search 使用边界（必须严格遵守）

### ✅ 允许使用的场景（定性信息）
- 政策、监管动态（如"央行降息"、"行业新规"）
- 公司公告、并购、融资、人事变动等事件性信息
- 行业趋势、宏观经济背景描述
- 竞争格局、产品动态等定性分析素材

### ❌ 严禁使用的场景（定量数据）
- **股票价格**（当前价、历史价、涨跌幅）→ 必须调用 MarketAgent / get_market_data
- **财务数字**（营收、净利润、ROE、EPS、现金流等）→ 必须调用 FundamentalAgent / get_fundamentals
- **估值指标**（PE、PB、PS、市值）→ 必须调用 MarketAgent / get_market_data
- **行业估值与表现**（板块涨跌、行业PE）→ 必须调用 IndustryAgent / get_industry_fundamentals

### ⚠️ 使用时必须声明
凡是引用了 web_search 返回的内容，输出中必须标注 `"source": "web_search"` 并附上
`"warning": "以下为网络信息，仅作定性参考，数字类数据以结构化工具返回值为准"`。
"""
