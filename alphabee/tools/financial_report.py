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
  ``company_code`` 至少提供一个，否则无法确定目标公司，查询会返回带
  ``REPORT_NOT_FOUND`` 原因码的提示文本。
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

import re
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from alphabee import PROJECT_ROOT
from alphabee.financial_report.fetch_deepagents import create_report_fetch_agent
from alphabee.tools.cache import SyncTTLCache

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


# ── 查询失败原因码 ─────────────────────────────────────────────────
# query_financial_report 不再返回 None，而是返回带原因码前缀的文本，
# 让 agent 能区分「确定性失败（报告不存在，别重试）」与
# 「非确定性失败（检索没答出来，可改问题重试）」。
REASON_REPORT_NOT_FOUND = "REPORT_NOT_FOUND"
REASON_AGENT_NO_ANSWER = "AGENT_NO_ANSWER"

# 根目录定位结果缓存：同一 (公司, 年份, 报告类型) 的定位结果复用，
# 未命中的结果也会缓存（负缓存），避免 agent 对不存在的报告反复重试。
_REPORT_LOCATE_CACHE = SyncTTLCache(ttl_seconds=600.0)


class FinancialReportRequest(BaseModel):
    """财务报表查询请求参数。

    用于描述「查哪家公司的哪份报告、想从中获得什么信息」。
    定位报告目录时，``company_name``/``company_code`` 至少需要提供一个；
    仅提供 ``year``/``report_type`` 而无法确定公司时，查询会返回带
    ``REPORT_NOT_FOUND`` 原因码的提示文本。
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
    （失败时返回带 ``REPORT_NOT_FOUND`` / ``AGENT_NO_ANSWER`` 原因码的
    提示文本，不再返回 ``None``），并未返回本模型；该模型仅为兼容旧接口保留。
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


def _normalize_locator_code(code: str) -> str:
    """归一化代码用于匹配：去交易所后缀（``300750.SZ`` → ``300750``）。"""
    code = (code or "").strip().upper()
    if "." in code:
        code = code.split(".")[0]
    return code


def _extract_code(company_dir_name: str) -> str:
    """从公司目录名 ``宁德时代(300750)`` 提取 6 位代码；无括号则返回空串。"""
    match = re.search(r"\((\d{6})\)", company_dir_name)
    return match.group(1) if match else ""


def _iter_report_candidates(root: Path):
    """产出 ``(company_str, dir_code, leaf_str, dir_path)`` 元组。

    覆盖两种结构：

    - 新嵌套：``<公司(code)>/财报/<报告期+类型>/`` → company_str 为 ``公司(code)``、
      leaf_str 为 ``报告期+类型``；
    - 旧平铺：``<报告名>/`` → company_str 与 leaf_str 均为目录名、dir_code 为空。
    """
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        financial_dir = entry / "财报"
        if financial_dir.is_dir():
            for leaf in sorted(financial_dir.iterdir()):
                if leaf.is_dir():
                    yield entry.name, _extract_code(entry.name), leaf.name, leaf
        else:
            yield entry.name, "", entry.name, entry


def decide_root_path(request: FinancialReportRequest) -> str | None:
    """根据请求参数定位目标报告的根目录路径。

    在 ``reports/`` 下同时支持**新嵌套结构**（``<公司>(<代码>)/财报/<报告期>/``）
    与**旧平铺结构**（``<报告名>/``），按「公司名 + 年份 + 报告类型」进行子串匹配
    打分，返回唯一命中的报告文件夹绝对路径；任何条件未全部命中或有多个候选
    （例如只给了年份、命中多家公司）时返回 ``None``。

    匹配逻辑要点：
    - 公司匹配：公司名命中目录名子串，或提供的代码命中 ``<公司>(<代码>)`` 括号内
      代码（旧平铺目录无代码，只能靠公司名命中）。
    - 若只提供 ``company_code``，会先通过 :func:`resolve_company_name_by_code`
      反查公司名再匹配；若代码反查失败仍可尝试代码直接匹配嵌套目录。
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
        '/data/freedom/AlphaBee/reports/宁德时代(300750)/财报/2026年半年度报告'
        >>> decide_root_path(FinancialReportRequest(company_name="宁德时代", year=2024, query="x"))
        None  # 该年份无报告
        >>> decide_root_path(FinancialReportRequest(year=2026, query="x"))
        None  # 仅年份命中多家公司，存在歧义
        >>> decide_root_path(FinancialReportRequest(company_name="宁德时代", report_type=["annual", "semiannual"], query="x"))
        '/data/freedom/AlphaBee/reports/宁德时代(300750)/财报/2026年半年度报告'
    """
    company_name = request.company_name
    company_code = request.company_code
    year = str(request.year) if request.year is not None else None
    report_types = _normalise_report_types(request.report_type)

    # company_code 优先反查 company_name（报告目录名以公司名为主），并保留归一化代码
    # 用于命中新嵌套目录的 ``<公司>(<代码>)``。
    code_norm = _normalize_locator_code(company_code) if company_code else ""
    if company_code and not company_name:
        company_name = resolve_company_name_by_code(company_code)

    want_company = bool(company_name or code_norm)
    want_year = bool(year)
    want_type = bool(report_types)
    score_threshold = sum([want_company, want_year, want_type])
    if score_threshold == 0:
        return None

    matches: list[str] = []
    best_match_score = 0
    for company_str, dir_code, leaf_str, dir_path in _iter_report_candidates(REPORT_DIR):
        score = 0
        if want_company and (
            (company_name and company_name in company_str) or (code_norm and dir_code and code_norm == dir_code)
        ):
            score += 1
        if want_year and year and year in leaf_str:
            score += 1
        if want_type and _detect_report_type_text(leaf_str) in report_types:
            score += 1

        if score > best_match_score:
            best_match_score = score
            matches = [str(dir_path)]
        elif score == best_match_score and score > 0:
            matches.append(str(dir_path))

    # 全部条件都必须命中，且不能有歧义（例如仅 year 命中多家公司）
    if best_match_score < score_threshold or len(matches) != 1:
        return None
    return matches[0]


def _normalise_locator_key(request: FinancialReportRequest) -> tuple:
    """生成报告定位的规范化缓存键。

    键为 ``(company_name, year, tuple(report_types))``，其中公司名优先用
    ``company_name``，否则用 ``company_code`` 反查；报告类型归一化为中文片段
    元组。同一家公司无论用代码还是名称请求，都会命中同一条缓存。
    """
    company = request.company_name
    if not company and request.company_code:
        company = resolve_company_name_by_code(request.company_code)
    return (company, request.year, tuple(_normalise_report_types(request.report_type)))


def _list_available_reports(company_name: str | None) -> list[str]:
    """列出 ``reports/`` 下与该公司相关的报告（不含年份筛选）。

    用于构造 REPORT_NOT_FOUND 的提示信息，让 agent 能挑一个真实存在的
    年份/类型去查，而不是盲目重试同一组合。嵌套结构显示为
    ``<公司>(<代码>)/财报/<报告期>``，旧平铺结构显示为目录名。
    """
    names: list[str] = []
    for company_str, _dir_code, leaf_str, _dir_path in _iter_report_candidates(REPORT_DIR):
        if company_name and company_name not in company_str:
            continue
        display = leaf_str if company_str == leaf_str else f"{company_str}/财报/{leaf_str}"
        names.append(display)
    return sorted(names)


def build_not_found_reason(request: FinancialReportRequest) -> str:
    """为「报告目录定位失败」构造带原因码的提示文本。

    区分三种可诊断的情况：
    - 缺少公司信息（company_name/company_code 均未提供或反查失败）；
    - 公司存在但年份/类型未命中；
    - 匹配不唯一（只给了年份/类型，命中多家公司）。
    """
    company = request.company_name
    if not company and request.company_code:
        company = resolve_company_name_by_code(request.company_code)

    if not company:
        return (
            f"{REASON_REPORT_NOT_FOUND}: 无法定位报告目录——未提供有效的公司信息"
            "（company_name / company_code 为空或代码无法反查）。请补充公司名称或正确代码后重试。"
        )

    year = str(request.year) if request.year is not None else None
    report_types = _normalise_report_types(request.report_type)

    available = _list_available_reports(company)
    available_text = (
        "本地可用报告：" + "、".join(available) + "。请选择其中存在的年份/报告类型后重试。"
        if available
        else "本地 reports/ 下暂无该公司的已解析报告。可改用 web_search / query_tushare / eastmoney 工具获取财报。"
    )

    target_parts = [part for part in (company, year, "/".join(report_types) or None) if part]
    return f"{REASON_REPORT_NOT_FOUND}: 未找到满足「{' '.join(target_parts)}」的唯一报告。{available_text}"


async def query_financial_report(request: FinancialReportRequest) -> str:
    """查询本地已解析的财务报表，返回针对 ``query`` 的答案文本（核心入口）。

    流程：先用 :func:`decide_root_path` 定位报告目录 → 在该目录上构建一个
    受限的 deep agent（只能读取该报告文件夹内的 markdown 文件）→ 用
    ``request.query`` 作为问题驱动 agent 检索并回答。

    注意：
    - 这是异步函数，调用方须 ``await``。
    - 返回的是 agent 生成的**最终文本答案**（字符串），非结构化对象。
    - 失败时**不返回 None**，而是返回带原因码前缀的文本：
      - ``REPORT_NOT_FOUND:`` 报告目录定位失败（无唯一命中），此时不应
        以相同公司/年份/报告类型重试，应换年份/类型或改用其它工具；
      - ``AGENT_NO_ANSWER:`` 报告存在但检索代理未产出最终答案，可把
        问题收窄/拆分后重试。
    - 依赖 LLM（``config.yaml`` 中的 ``financial_report`` 组件），首次调用
      及 agent 检索过程可能耗时数秒到数十秒。

    效率与成本：为减少检索步数与 LLM 调用开销，``query`` 应尽量具体——
    明确「公司 + 报告期 + 想了解的指标/章节/事件」，避免泛泛提问；多个不相关
    的问题请拆成多次调用，每次聚焦单一主题。

    Args:
        request: 查询请求。``company_name``/``company_code`` 至少提供一个，
            ``query`` 必填。

    Returns:
        str: 报告内容相关的文本答案；失败时返回带原因码
        （``REPORT_NOT_FOUND`` / ``AGENT_NO_ANSWER``）的提示文本，供
        agent 据此决定是换问题、换报告还是换工具。

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
    root_path = _REPORT_LOCATE_CACHE.get_or_compute(
        _normalise_locator_key(request),
        lambda: decide_root_path(request),
    )
    if not root_path:
        return build_not_found_reason(request)
    agent = create_report_fetch_agent(root_path)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=request.query)]},
        config={"recursion_limit": 40},
    )
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            return msg.content if isinstance(msg.content, str) else ""
    return (
        f"{REASON_AGENT_NO_ANSWER}: 报告检索代理未产出最终文本答案"
        "（可能问题过于宽泛、超出 40 步检索上限，或模型未收敛）。"
        "建议将问题收窄到「公司 + 报告期 + 具体指标/章节/事件」，"
        "或将多个不相关问题拆分为多次调用后重试。"
    )


if __name__ == "__main__":
    code = "300750"
    print(f"Company name for code {code}: {resolve_company_name_by_code(code)}")

    root_path = decide_root_path(
        FinancialReportRequest(company_code=code, year=2026, report_type="semiannual", query="最新的海外订单情况")
    )
    print(f"Root path for the financial report: {root_path}")
