"""本地已解析财务报表的查询工具。

本模块提供一个用于从本地已经解析好的财务报表目录（``reports/``）中
检索信息的工具，供模型作为 tool 调用。核心入口是 :func:`query_financial_report`。

典型用法（异步，供 Agent/工具层调用）::

    from alphabee.tools.financial_report import (
        FinancialReportRequest,
        query_financial_report,
    )

    req = FinancialReportRequest(
        company_code="300750",   # 也可以直接用 company_name="宁德时代"
        year=2026,
        query="请从 2026 年半年报中提取海外业务的最新进展与订单情况",
    )
    answer = await query_financial_report(req)

说明：
- 数据源是本地 ``reports/<公司名>：<年份><报告期>.md`` 目录（由 report_parser 拆分生成），
  并非实时网络数据；查询前请先确认目标公司/年份的报告已存在。
- ``company_code`` 会自动反查公司名称后再定位报告目录；``company_name`` 与
  ``company_code`` 至少提供一个，否则无法确定目标公司。
- 报告类型 ``report_type`` 使用英文键（如 ``semiannual``/``annual``/``quarterly``），
  内部会自动映射为中文目录名片段。

查询效率与成本提示：
- **查询应尽可能具体**：``query`` 需明确到「公司、报告期、关注的具体指标/章节/
  事件」，如 ``"宁德时代 2026 半年报海外业务的最新订单情况"``，避免宽泛提问
  （如 ``"这份报告讲了什么"``）。agent 需要调用大模型在报告内检索，问题越具体，
  需要的搜索与推理步数越少，耗时与成本越低，答案质量也越高。
- 若一次要问多个不相关的问题，请拆分为多次调用本工具，每次聚焦单一主题。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from alphabee import PROJECT_ROOT
from alphabee.financial_report.fetch_deepagents import create_report_fetch_agent

REPORT_DIR = PROJECT_ROOT / "reports"  # Default directory for financial reports

REPORT_TYPE_MAPPING = {
    "annual": ["年度报告", "年报"],
    "quarterly": ["季度报告", "季报"],
    "semiannual": ["半年度报", "半年报"],
    # 一季度
    "first_quarter": ["一季度报告", "一季报"],
    # 二季度
    "second_quarter": ["二季度报告", "二季报"],
    # 三季度
    "third_quarter": ["三季度报告", "三季报"],
    # 四季度
    "fourth_quarter": ["四季度报告", "四季报"],
}


class FinancialReportRequest(BaseModel):
    """财务报表查询请求参数。

    用于描述「查哪家公司的哪份报告、想从中获得什么信息」。
    定位报告目录时，``company_name``/``company_code`` 至少需要提供一个；
    仅提供 ``year``/``report_type`` 而无法确定公司时，查询将返回 ``None``。
    """

    company_name: str | None = Field(
        None,
        description="公司名称（中文全称，如 '宁德时代'）。用于在 reports/ 下定位报告目录；"
        "与 company_code 至少提供一个。",
    )
    company_code: str | None = Field(
        None,
        description="公司/股票代码，支持 '300750' 或 '300750.SZ' 格式。"
        "内部会自动反查对应公司名称后定位报告目录；"
        "与 company_name 至少提供一个。",
    )
    query: str = Field(
        ...,
        description="需要从报告中提取/回答的问题，用自然语言描述，如 "
        "'请从 2026 年半年报中提取海外业务的最新进展'。必填。",
    )
    year: int | None = Field(
        None,
        description="报告所属年份（如 2026）。可选；不填则按公司名定位，若该公司只有一份报告则命中该份。",
    )
    report_type: str | list[str] | None = Field(
        None,
        description="报告类型（可给单个或多个）。可选值：annual（年报）、semiannual（半年报）、"
        "quarterly（季报）、first_quarter/second_quarter/third_quarter/fourth_quarter"
        '（一/二/三/四季报）。例如 \'semiannual\' 或 ["annual", "semiannual"]。'
        "内部自动映射为中文目录名片段，任一命中即视为匹配。",
    )


class FinancialReportResponse(BaseModel):
    """财务报表查询结果（保留模型）。

    注意：当前 :func:`query_financial_report` 直接返回文本字符串
    （``str | None``），并未返回本模型；该模型仅为兼容旧接口保留。
    """

    report_data: dict = Field(..., description="The financial report data")


def walk_with_depth_limit(root_dir: Path, max_depth: int):
    """Depth-first walk through a directory tree with a maximum depth limit."""
    root_dir = Path(root_dir)
    # (当前路径, 当前深度)
    stack = [(root_dir, 0)]

    while stack:
        current_path, depth = stack.pop()
        if depth > max_depth:
            continue

        # 只做一次 iterdir()，避免重复遍历
        try:
            entries = list(current_path.iterdir())
        except OSError:
            continue
        dirs = sorted(d for d in entries if d.is_dir())
        files = sorted(f for f in entries if f.is_file())
        yield current_path, dirs, files

        if depth < max_depth:
            for d in dirs:
                stack.append((d, depth + 1))


@lru_cache(maxsize=1)
def _load_stock_mapping() -> dict[str, str]:
    """Load `company_code -> company_name` mappings from the static stock list."""
    csv_path = Path(__file__).resolve().parents[1] / "static" / "all_stocks.csv"
    if not csv_path.exists():
        return {}

    import pandas as pd

    # 必须以字符串读取：pandas 默认会把 symbol 列解析为 int，吃掉 000001/002594 这类前导零
    df = pd.read_csv(
        csv_path,
        dtype={"stock_code": str, "symbol": str, "company_name": str},
    )
    mapping: dict[str, str] = {}
    for _, row in df.iterrows():
        code = str(row.get("stock_code", "")).strip().upper()
        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("company_name", "")).strip()
        if not name or name.lower() == "nan":
            continue
        if code:
            mapping[code] = name
        if symbol:
            mapping[symbol] = name
    return mapping


def resolve_company_name_by_code(company_code: str | None) -> str | None:
    """根据公司/股票代码反查公司名称。

    用于把用户提供的 ``company_code`` 转换成 ``company_name``，从而在
    ``reports/`` 目录中定位报告文件夹（报告目录名只含公司名，不含代码）。

    支持的代码格式：
    - 纯 6 位代码，如 ``"300750"``
    - 带交易所后缀，如 ``"300750.SZ"``、``"600519.SH"``

    Args:
        company_code: 公司/股票代码。为空时返回 ``None``。

    Returns:
        str | None: 匹配到的公司名称（如 ``"宁德时代"``）；
        未找到或输入为空时返回 ``None``。

    Example:
        >>> resolve_company_name_by_code("300750")
        '宁德时代'
        >>> resolve_company_name_by_code("300750.SZ")
        '宁德时代'
        >>> resolve_company_name_by_code("000001")  # 平安银行
        '平安银行'
        >>> resolve_company_name_by_code("999999")  # 不存在
        None
    """
    if not company_code:
        return None
    code = str(company_code).strip().upper()
    return _load_stock_mapping().get(code)


_ALL_REPORT_TYPE_TEXTS: list[str] = sorted(
    {t for texts in REPORT_TYPE_MAPPING.values() for t in texts},
    key=len,
    reverse=True,
)


def _detect_report_type_text(dir_path_str: str) -> str:
    """从报告目录名判断其实际的报告类型中文片段。

    目录名形如     ``宁德时代：2026年半年度报告``，报告类型位于年份标记 ``\\d{4}年`` 之后
    （如 ``半年度报告``）。在所有类型同义词里找出命中该片段者，按「长度优先、
    位置靠前次之」取最具体的一个——例如 ``半年度报告`` 中同时含 ``年度报告``
    与 ``半年度报``，会正确判定为 ``半年度报`` 而非 ``年度报告``。
    """
    import re as _re

    year_match = _re.search(r"\d{4}年", dir_path_str)
    tail = dir_path_str[year_match.end() :] if year_match else dir_path_str
    best: str = ""
    best_key: tuple[int, int] = (-1, -1)
    for t in _ALL_REPORT_TYPE_TEXTS:
        idx = tail.find(t)
        if idx == -1:
            continue
        key = (len(t), -idx)
        if key > best_key:
            best_key = key
            best = t
    return best


def _normalise_report_types(report_type: str | list[str] | None) -> list[str]:
    """把 ``report_type`` 归一化为中文片段列表，支持多个类型。

    ``REPORT_TYPE_MAPPING`` 中每个键对应一个中文片段列表（如 ``"annual"`` →
    ``["年度报告", "年报"]``），这里展平并去重：
    - 单个字符串：映射一次并展开全部同义词（如 ``"semiannual"`` → ``["半年度报", "半年报"]``）。
    - 列表：逐项映射并展平去重（如 ``["annual", "semiannual"]``）。
    - 未知键保留原样；``None`` / 空列表返回空列表。
    """
    if not report_type:
        return []
    raw_types = [report_type] if isinstance(report_type, str) else list(report_type)
    texts: list[str] = []
    for t in raw_types:
        if not t:
            continue
        mapped = REPORT_TYPE_MAPPING.get(t, [t])
        if isinstance(mapped, str):
            mapped = [mapped]
        for m in mapped:
            if m and m not in texts:
                texts.append(m)
    return texts


def decide_root_path(request: FinancialReportRequest) -> str | None:
    """根据请求参数定位目标报告的根目录路径。

    在 ``reports/`` 下按「公司名 + 年份 + 报告类型」进行子串匹配打分，
    返回唯一命中的报告文件夹绝对路径；任何条件未全部命中或有多个候选
    （例如只给了年份、命中多家公司）时返回 ``None``。

    匹配逻辑要点：
    - 报告目录名形如 ``宁德时代：2026年半年度报告``，只包含公司名称。
    - 若只提供 ``company_code``，会先通过 :func:`resolve_company_name_by_code`
      反查公司名再匹配；若代码反查失败则无法定位，返回 ``None``。
    - ``report_type`` 支持单个字符串（如 ``"semiannual"``）或多个类型组成的列表
      （如 ``["annual", "semiannual"]``）；英文键先经 ``REPORT_TYPE_MAPPING`` 映射为
      中文片段，**任一**给定类型命中目录即视为该条件满足（只计 1 分，不会因多个
      类型而抬高分阈值）。

    Args:
        request: 查询请求，须至少提供 ``company_name`` 或 ``company_code``。

    Returns:
        str | None: 命中的报告目录绝对路径；无唯一命中时返回 ``None``。

    Example:
        >>> from alphabee.tools.financial_report import FinancialReportRequest, decide_root_path
        >>> decide_root_path(FinancialReportRequest(company_code="300750", year=2026, query="x"))
        '/data/freedom/AlphaBee/reports/宁德时代：2026年半年度报告'
        >>> decide_root_path(FinancialReportRequest(company_name="宁德时代", year=2024, query="x"))
        None  # 该年份无报告
        >>> decide_root_path(FinancialReportRequest(year=2026, query="x"))
        None  # 仅年份命中多家公司，存在歧义
        >>> decide_root_path(FinancialReportRequest(company_name="宁德时代", report_type=["annual", "semiannual"], query="x"))
        '/data/freedom/AlphaBee/reports/宁德时代：2026年半年度报告'
    """
    # walk REPORT_DIR to find the report folder based on company_name, company_code, year, and type

    company_name = request.company_name
    company_code = request.company_code
    year = str(request.year) if request.year is not None else None
    report_types = _normalise_report_types(request.report_type)

    # company_code 只用于反查 company_name（报告目录名不含股票代码），再按名称匹配
    if company_code and not company_name:
        company_name = resolve_company_name_by_code(company_code)

    # 每个「匹配组」贡献 1 分：公司名、年份各一组；report_type 作为一组，
    # 命中任一给定类型即得 1 分，从而支持同时给出多个可接受的报告类型。
    match_groups: list[list[str]] = []
    if company_name:
        match_groups.append([company_name])
    if year:
        match_groups.append([year])
    if report_types:
        match_groups.append(report_types)
    if not match_groups:
        return None
    score_threshold = len(match_groups)

    # 选择符合的报告文件夹的最上层目录作为根路径，使用分数来选择最匹配的报告文件夹，快速定位到报告文件夹
    matches: list[str] = []
    best_match_score = 0
    for dir_path, _, _ in walk_with_depth_limit(REPORT_DIR, max_depth=1):
        if dir_path == REPORT_DIR:
            continue
        dir_path_str = str(dir_path)
        score = 0
        for group in match_groups:
            if group is report_types:
                # 报告类型用「目录实际类型」匹配，避免 "年度报告" 命中 "半年度报告" 这类子串误判
                if _detect_report_type_text(dir_path_str) in report_types:
                    score += 1
            elif any(item in dir_path_str for item in group):
                score += 1

        if score > best_match_score:
            best_match_score = score
            matches = [dir_path_str]
        elif score == best_match_score and score > 0:
            matches.append(dir_path_str)

    # 全部条件都必须命中，且不能有歧义（例如仅 year 命中多家公司）
    if best_match_score < score_threshold or len(matches) != 1:
        return None
    return matches[0]


async def query_financial_report(request: FinancialReportRequest) -> str | None:
    """查询本地已解析的财务报表，返回针对 ``query`` 的答案文本（核心入口）。

    流程：先用 :func:`decide_root_path` 定位报告目录 → 在该目录上构建一个
    受限的 deep agent（只能读取该报告文件夹内的 markdown 文件）→ 用
    ``request.query`` 作为问题驱动 agent 检索并回答。

    注意：
    - 这是异步函数，调用方须 ``await``。
    - 返回的是 agent 生成的**最终文本答案**（字符串），非结构化对象。
    - 报告目录定位失败（无唯一命中）时返回 ``None``，此时应提示用户
      确认目标公司/年份/报告类型是否已存在本地报告中。
    - 依赖 LLM（``config.yaml`` 中的 ``financial_report`` 组件），首次调用
      及 agent 检索过程可能耗时数秒到数十秒。

    效率与成本：为减少检索步数与 LLM 调用开销，``query`` 应尽量具体——
    明确「公司 + 报告期 + 想了解的指标/章节/事件」，避免泛泛提问；多个不相关
    的问题请拆成多次调用，每次聚焦单一主题。

    Args:
        request: 查询请求。``company_name``/``company_code`` 至少提供一个，
            ``query`` 必填。

    Returns:
        str | None: 报告内容相关的文本答案；目录未命中时返回 ``None``。

    Example:
        import asyncio
        from alphabee.tools.financial_report import (
            FinancialReportRequest,
            query_financial_report,
        )

        req = FinancialReportRequest(
            company_code="300750",
            year=2026,
            query="2026 半年报中海外业务的最新进展与订单情况如何？",
        )
        answer = asyncio.run(query_financial_report(req))
    """
    root_path = decide_root_path(request)
    if not root_path:
        return None
    agent = create_report_fetch_agent(root_path)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=request.query)]},
        config={"recursion_limit": 40},
    )
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            return msg.content if isinstance(msg.content, str) else ""
    return None


if __name__ == "__main__":
    code = "300750"
    print(f"Company name for code {code}: {resolve_company_name_by_code(code)}")

    root_path = decide_root_path(
        FinancialReportRequest(company_code=code, year=2026, report_type="semiannual", query="最新的海外订单情况")
    )
    print(f"Root path for the financial report: {root_path}")
