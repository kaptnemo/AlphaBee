"""web_search 使用边界守卫中间件

拦截所有 web_search 工具调用，在三个层面强制执行使用边界：

1. **调用前（pre-call）**：检测 query 中是否包含被禁止的意图关键词
   （股价、价格、财务数字类词汇），若触发则直接短路，返回拒绝消息，
   不发起实际网络请求。

2. **调用后·免责声明（post-call disclaimer）**：统一在结果末尾追加
   数据来源声明，提醒模型该结果仅供定性参考。

3. **调用后·数值扫描（post-call numeric scan）**：扫描返回内容中
   是否包含价格、估值、财务数字等量化数据片段；若检测到，则注入
   结构化「核验指令」，要求模型在生成最终答案前必须调用相应的
   专用工具（get_market_data / get_fundamentals / get_industry_fundamentals）
   获取权威数据。
"""

import re
from dataclasses import dataclass

from langchain.agents.middleware import ToolCallRequest, wrap_tool_call
from langchain_core.messages import ToolMessage

# ---------------------------------------------------------------------------
# Pre-call：禁止通过 web_search 查询的关键词模式
# 匹配到任意一条 → 短路拦截
# ---------------------------------------------------------------------------

_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("股票价格/涨跌", re.compile(r"(股价|现价|最新价|收盘价|开盘价|涨跌幅|涨停|跌停|今日.*价|当前.*价|price)", re.I)),
    ("市值/估值指标", re.compile(r"(pe|pb|ps|市盈率|市净率|市销率|总市值|流通市值|估值)", re.I)),
    ("财务数字", re.compile(r"(营收|净利润|毛利率|roe|roa|eps|每股收益|现金流|负债率|利润率)", re.I)),
    ("行业行情数据", re.compile(r"(行业.*涨跌|板块.*涨幅|行业.*pe|行业.*估值|板块.*市值)", re.I)),
]

# 结果末尾追加的免责声明
_RESULT_DISCLAIMER = (
    "\n\n---\n"
    "【web_search 数据免责声明】\n"
    "以上内容来自互联网搜索，仅供定性参考。\n"
    "⚠️ 其中任何价格、财务数字、估值指标均可能已过时或不准确，\n"
    "请勿将其作为分析依据，相关数字必须以 MarketAgent / FundamentalAgent / IndustryAgent 返回值为准。"
)

# ---------------------------------------------------------------------------
# Post-call：数值型数据扫描规则
# 每条规则：(分类标签, 正则, 推荐工具函数名, 推荐 Agent 名)
# ---------------------------------------------------------------------------


@dataclass
class _ScanRule:
    category: str
    pattern: re.Pattern
    tool: str
    agent: str


_NUMERIC_SCAN_RULES: list[_ScanRule] = [
    _ScanRule(
        category="股票价格",
        pattern=re.compile(
            r"(?:股价|收盘|最新价|现价)[^\d\n]{0,6}(\d+(?:\.\d+)?)\s*元"
            r"|(\d+(?:\.\d+)?)\s*元(?:/股|每股)"
            r"|(?:涨跌幅|上涨|下跌)[^\d\n]{0,6}[+-]?(\d+(?:\.\d+)?)\s*%",
            re.I,
        ),
        tool="get_market_data",
        agent="MarketAgent",
    ),
    _ScanRule(
        category="PE/PB 估值",
        pattern=re.compile(
            r"(?:PE|市盈率)[^\d\n]{0,8}(\d+(?:\.\d+)?)"
            r"|(?:PB|市净率)[^\d\n]{0,8}(\d+(?:\.\d+)?)"
            r"|(?:估值)[^\d\n]{0,6}(\d+(?:\.\d+)?)\s*倍",
            re.I,
        ),
        tool="get_market_data",
        agent="MarketAgent",
    ),
    _ScanRule(
        category="营收/利润",
        pattern=re.compile(
            r"(?:营收|营业收入|总收入)[^\d\n]{0,8}(\d+(?:\.\d+)?)\s*(?:亿|万|百亿|千亿)"
            r"|(?:净利润|归母净利|利润)[^\d\n]{0,8}(\d+(?:\.\d+)?)\s*(?:亿|万)",
            re.I,
        ),
        tool="get_fundamentals",
        agent="FundamentalAgent",
    ),
    _ScanRule(
        category="ROE/ROA/毛利率",
        pattern=re.compile(
            r"(?:ROE|净资产收益率)[^\d\n]{0,6}(\d+(?:\.\d+)?)\s*%"
            r"|(?:ROA|总资产收益率)[^\d\n]{0,6}(\d+(?:\.\d+)?)\s*%"
            r"|(?:毛利率|净利率|净利润率)[^\d\n]{0,6}(\d+(?:\.\d+)?)\s*%",
            re.I,
        ),
        tool="get_fundamentals",
        agent="FundamentalAgent",
    ),
    _ScanRule(
        category="市值",
        pattern=re.compile(
            r"(?:总市值|流通市值|市值)[^\d\n]{0,6}(\d+(?:\.\d+)?)\s*(?:亿|万亿|百亿)",
            re.I,
        ),
        tool="get_market_data",
        agent="MarketAgent",
    ),
    _ScanRule(
        category="行业估值/涨跌",
        pattern=re.compile(
            r"(?:行业|板块)[^\d\n]{0,10}(?:PE|PB|涨跌幅|涨幅|跌幅)[^\d\n]{0,6}(\d+(?:\.\d+)?)",
            re.I,
        ),
        tool="get_industry_fundamentals",
        agent="IndustryAgent",
    ),
]

_SNIPPET_CONTEXT = 25  # 数值命中时，前后各取多少字符作为摘要片段


def _detect_forbidden(query: str) -> tuple[bool, str]:
    """检测 query 是否触碰禁止关键词。

    Returns:
        (triggered, reason): 是否触发，以及触发原因描述。
    """
    for label, pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(query):
            return True, label
    return False, ""


def _scan_numeric_hits(content: str) -> list[dict]:
    """扫描 web_search 结果中出现的数值型金融数据。

    Returns:
        命中列表，每条为 {category, snippets, tool, agent}。
        同一 category 只返回一条（去重），最多附带 2 个上下文片段。
    """
    hits: list[dict] = []
    seen: set[str] = set()

    for rule in _NUMERIC_SCAN_RULES:
        if rule.category in seen:
            continue
        snippets: list[str] = []
        for m in rule.pattern.finditer(content):
            start = max(0, m.start() - _SNIPPET_CONTEXT)
            end = min(len(content), m.end() + _SNIPPET_CONTEXT)
            snippet = content[start:end].replace("\n", " ").strip()
            snippets.append(snippet)
            if len(snippets) >= 2:
                break
        if snippets:
            seen.add(rule.category)
            hits.append(
                {
                    "category": rule.category,
                    "snippets": snippets,
                    "tool": rule.tool,
                    "agent": rule.agent,
                }
            )

    return hits


def _build_verify_directive(hits: list[dict]) -> str:
    """根据扫描结果生成结构化核验指令。"""
    # 按 tool 合并同类项，避免重复指示同一工具
    tool_groups: dict[str, dict] = {}
    for hit in hits:
        key = hit["tool"]
        if key not in tool_groups:
            tool_groups[key] = {
                "tool": hit["tool"],
                "agent": hit["agent"],
                "categories": [],
                "snippets": [],
            }
        tool_groups[key]["categories"].append(hit["category"])
        tool_groups[key]["snippets"].extend(hit["snippets"][:1])  # 每类取1条示例

    lines = [
        "\n\n---",
        "【⚠️ 数值核验指令 — 必须在生成最终回答前执行】",
        "web_search 结果中检测到以下可能过时的数值型数据，禁止直接引用，",
        "必须先通过专用工具获取权威数据：",
        "",
    ]
    for group in tool_groups.values():
        categories_str = " / ".join(group["categories"])
        lines.append(f"  📌 检测到数据类型：{categories_str}")
        for snippet in group["snippets"][:2]:
            lines.append(f"     示例片段：「...{snippet}...」")
        lines.append(f"     → 必须调用：{group['tool']}（{group['agent']}）核验")
        lines.append("")

    lines.append("完成以上核验后，以工具返回的数值为准，忽略 web_search 中的对应数字。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Middleware：pre-call 拦截 + post-call 声明 + post-call 数值扫描
# ---------------------------------------------------------------------------


@wrap_tool_call
async def web_search_guard(request: ToolCallRequest, handler):
    """web_search 使用边界守卫（async）。

    执行顺序：
    1. pre-call  — 禁词检测，命中则短路
    2. post-call — 追加免责声明
    3. post-call — 数值扫描，检测到量化数据则注入核验指令
    """
    tool_name = request.tool_call.get("name", "")
    if tool_name != "web_search":
        return await handler(request)

    query: str = request.tool_call.get("args", {}).get("query", "")

    # ── 1. Pre-call：禁词拦截 ────────────────────────────────────────────────
    triggered, reason = _detect_forbidden(query)
    if triggered:
        blocked_msg = (
            f"[web_search 已被拦截]\n"
            f"查询意图「{reason}」属于结构化数据范畴，禁止通过 web_search 获取。\n\n"
            f"请改用以下专用工具：\n"
            f"  • 股票价格 / 估值（PE/PB/市值）→ get_market_data（MarketAgent）\n"
            f"  • 财务数据（营收/利润/ROE/现金流）→ get_fundamentals（FundamentalAgent）\n"
            f"  • 行业估值与表现 → get_industry_fundamentals（IndustryAgent）\n\n"
            f"  • 其他数据可以尝试使用 query_tushare 或 eastmoney 工具\n\n"
            f"原始 query：「{query}」"
        )
        return ToolMessage(
            content=blocked_msg,
            tool_call_id=request.tool_call["id"],
        )

    # ── 2. 放行，执行真实调用 ────────────────────────────────────────────────
    result: ToolMessage = await handler(request)
    content = result.content if isinstance(result.content, str) else ""

    # ── 3. Post-call：追加免责声明 ───────────────────────────────────────────
    content += _RESULT_DISCLAIMER

    # ── 4. Post-call：数值扫描 + 核验指令注入 ───────────────────────────────
    hits = _scan_numeric_hits(content)
    if hits:
        content += _build_verify_directive(hits)

    return ToolMessage(
        content=content,
        tool_call_id=result.tool_call_id,
    )
