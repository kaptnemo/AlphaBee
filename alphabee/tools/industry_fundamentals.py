"""行业基本面数据工具

数据来源：
  - akshare（东方财富）：行业列表快照、历史价格走势、成分股明细
  - tushare（申万）：PE/PB 历史估值数据
  - 以上任一来源不可用时自动降级，仅返回可获取的部分数据。
"""

import datetime
import json
import math
from typing import Optional

import pandas as pd
from pydantic import BaseModel, Field

from alphabee.collectors.akshare.helper import AkShareHelper
from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.utils import tracked_chat_completion


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class IndustryValuation(BaseModel):
    """行业估值（单期）"""
    date: str = Field(description="日期（YYYYMMDD）")
    pe_ttm: float = Field(description="行业整体市盈率 TTM（倍）")
    pb: float = Field(description="行业整体市净率（倍）")
    total_mv: float = Field(description="行业总市值（亿元）")


class IndustryPerformance(BaseModel):
    """行业价格表现"""
    current_price: float = Field(description="行业指数最新价格（点）")
    change_pct_today: float = Field(description="今日涨跌幅（%）")
    change_pct_1w: float = Field(description="近1周涨跌幅（%）")
    change_pct_1m: float = Field(description="近1月涨跌幅（%）")
    change_pct_3m: float = Field(description="近3月涨跌幅（%）")
    change_pct_6m: float = Field(description="近6月涨跌幅（%）")
    change_pct_1y: float = Field(description="近1年涨跌幅（%）")
    up_count: int = Field(description="今日上涨家数")
    down_count: int = Field(description="今日下跌家数")
    total_mv: float = Field(description="行业总市值（亿元）")
    turnover_rate: float = Field(description="今日换手率（%）")


class ConstituentStock(BaseModel):
    """行业成分股"""
    code: str = Field(description="股票代码")
    name: str = Field(description="股票名称")
    change_pct: float = Field(description="今日涨跌幅（%）")
    market_cap: float = Field(description="总市值（亿元）")
    pe_ttm: float = Field(description="动态市盈率（倍），0表示亏损或暂无")
    pb: float = Field(description="市净率（倍）")
    change_pct_ytd: float = Field(description="年初至今涨跌幅（%）")


class IndustrySummary(BaseModel):
    """行业基本面分析摘要（大模型生成）"""
    overview: str = Field(description="行业整体状况概述（2-3句）")
    valuation_comment: str = Field(description="当前估值水平评价（偏高/合理/偏低）及依据")
    strengths: list[str] = Field(description="行业主要优势或机会")
    risks: list[str] = Field(description="行业主要风险或挑战")
    outlook: str = Field(description="基于近期走势和估值的投资展望（1-2句）")


class IndustryFundamentals(BaseModel):
    """行业基本面数据汇总"""
    industry_name: str = Field(description="行业名称（东方财富板块）")
    industry_code: str = Field(description="东方财富行业板块代码")
    sw_code: Optional[str] = Field(default=None, description="申万行业指数代码（如 801020.SI），用于估值查询")
    performance: IndustryPerformance = Field(description="行业价格表现")
    valuation_history: list[IndustryValuation] = Field(
        description="历史估值数据（PE/PB/市值），来自申万行业指数，按时间倒序"
    )
    top_stocks: list[ConstituentStock] = Field(
        description="行业成分股列表（按总市值降序排列，最多50只）"
    )
    summary: IndustrySummary = Field(description="AI 生成的行业基本面综合分析摘要")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value, default: float = 0.0) -> float:
    try:
        v = float(value)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _col(df: pd.DataFrame, *candidates: str, default=None):
    """Return the first matching column value from a row or series."""
    for c in candidates:
        if c in df.columns:
            return c
    return default


def _find_em_board(boards_df: pd.DataFrame, industry: str) -> Optional[pd.Series]:
    """Fuzzy-match ``industry`` against EM board names (板块名称 column)."""
    name_col = _col(boards_df, "板块名称", "name")
    if name_col is None or boards_df.empty:
        return None
    # Exact match
    exact = boards_df[boards_df[name_col] == industry]
    if not exact.empty:
        return exact.iloc[0]
    # Substring match: user input contained in board name
    for _, row in boards_df.iterrows():
        board_name = str(row[name_col])
        if industry in board_name or board_name in industry:
            return row
    # Partial match: any common character sequence ≥ 2 chars
    for _, row in boards_df.iterrows():
        board_name = str(row[name_col])
        if any(
            industry[i: i + 2] in board_name
            for i in range(len(industry) - 1)
        ):
            return row
    return None


def _compute_perf_from_hist(hist_df: pd.DataFrame, today_change_pct: float) -> dict:
    """Compute rolling returns from daily history DataFrame.

    Expects columns: 日期 (str YYYY-MM-DD or datetime), 收盘 (float).
    Returns a dict with keys 1w, 1m, 3m, 6m, 1y (percentage changes).
    """
    result = {k: 0.0 for k in ("1w", "1m", "3m", "6m", "1y")}
    if hist_df is None or hist_df.empty:
        return result

    close_col = _col(hist_df, "收盘", "close", "Close")
    date_col = _col(hist_df, "日期", "date", "Date")
    if close_col is None or date_col is None:
        return result

    df = hist_df[[date_col, close_col]].copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col, ascending=False).reset_index(drop=True)

    if df.empty:
        return result

    current_price = _safe_float(df.iloc[0][close_col])
    if current_price == 0:
        return result
    latest_date: datetime.datetime = df.iloc[0][date_col]

    for label, days in [("1w", 7), ("1m", 30), ("3m", 90), ("6m", 180), ("1y", 365)]:
        target = latest_date - datetime.timedelta(days=days)
        past = df[df[date_col] <= target]
        if past.empty:
            result[label] = 0.0
        else:
            past_price = _safe_float(past.iloc[0][close_col])
            result[label] = (
                round((current_price / past_price - 1) * 100, 2) if past_price != 0 else 0.0
            )
    return result


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_em_snapshot(helper: AkShareHelper) -> Optional[pd.DataFrame]:
    """Fetch East Money industry board snapshot (all boards)."""
    try:
        return helper.stock_board_industry_name_em().data
    except Exception:
        return None


def _fetch_em_history(helper: AkShareHelper, board_name: str, start_date: str) -> Optional[pd.DataFrame]:
    """Fetch historical daily price for an EM board."""
    try:
        end_date = datetime.date.today().strftime("%Y%m%d")
        return helper.stock_board_industry_hist_em(
            symbol=board_name,
            period="日k",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        ).data
    except Exception:
        return None


def _fetch_em_constituents(helper: AkShareHelper, board_name: str) -> Optional[pd.DataFrame]:
    """Fetch constituent stocks for an EM board."""
    try:
        return helper.stock_board_industry_cons_em(symbol=board_name).data
    except Exception:
        return None


def _find_sw_code(ts_helper: TuShareHelper, industry_name: str) -> Optional[str]:
    """Find Shenwan L1 industry index code that matches ``industry_name``."""
    try:
        df = ts_helper.index_classify(level="L1", src="SW2021").data
        if df is None or df.empty:
            return None
        name_col = _col(df, "industry_name", "name")
        code_col = _col(df, "index_code", "ts_code", "code")
        if name_col is None or code_col is None:
            return None
        # Exact match
        exact = df[df[name_col] == industry_name]
        if not exact.empty:
            return str(exact.iloc[0][code_col])
        # Substring / common-prefix match
        for _, row in df.iterrows():
            sw_name = str(row[name_col])
            if industry_name in sw_name or sw_name in industry_name:
                return str(row[code_col])
        return None
    except Exception:
        return None


def _fetch_sw_valuation_history(
    ts_helper: TuShareHelper, sw_code: str, start_date: str
) -> list[IndustryValuation]:
    """Fetch PE/PB history from Tushare index_dailybasic for a SW industry index."""
    try:
        end_date = datetime.date.today().strftime("%Y%m%d")
        df = ts_helper.index_dailybasic(
            ts_code=sw_code,
            start_date=start_date,
            end_date=end_date,
            fields="ts_code,trade_date,total_mv,pe,pe_ttm,pb",
        ).data
        if df is None or df.empty:
            return []
        date_col = _col(df, "trade_date")
        pe_col = _col(df, "pe_ttm", "pe")
        pb_col = _col(df, "pb")
        mv_col = _col(df, "total_mv")
        if date_col is None:
            return []
        df = df.sort_values(date_col, ascending=False).reset_index(drop=True)
        result = []
        # Sample ~monthly: keep one row per ~20 trading days to limit output size
        step = max(1, len(df) // 24)
        for i in range(0, len(df), step):
            row = df.iloc[i]
            result.append(
                IndustryValuation(
                    date=str(row[date_col]),
                    pe_ttm=_safe_float(row[pe_col]) if pe_col else 0.0,
                    pb=_safe_float(row[pb_col]) if pb_col else 0.0,
                    total_mv=round(_safe_float(row[mv_col]) / 1e4, 2) if mv_col else 0.0,
                )
            )
        return result[:24]  # cap at 24 data points (≈2 years monthly)
    except Exception:
        return []


def _parse_em_snapshot_row(row: pd.Series) -> dict:
    """Extract standardised fields from an EM board snapshot row."""

    def _get(*keys):
        for k in keys:
            if k in row.index and row[k] is not None:
                return row[k]
        return None

    name = str(_get("板块名称", "name") or "")
    code = str(_get("板块代码", "code") or "")
    price = _safe_float(_get("最新价", "close", "price"))
    change_pct = _safe_float(_get("涨跌幅", "change_pct"))
    total_mv = _safe_float(_get("总市值", "total_mv"))
    # Total market value from EM is typically in 亿元 already; if > 1e8 treat as 元
    if total_mv > 1e8:
        total_mv = round(total_mv / 1e8, 2)
    else:
        total_mv = round(total_mv, 2)
    up_count = _safe_int(_get("上涨家数", "up_count"))
    down_count = _safe_int(_get("下跌家数", "down_count"))
    turnover = _safe_float(_get("换手率", "turnover_rate"))
    return dict(
        name=name,
        code=code,
        price=price,
        change_pct=change_pct,
        total_mv=total_mv,
        up_count=up_count,
        down_count=down_count,
        turnover=turnover,
    )


def _parse_constituents(cons_df: pd.DataFrame, max_stocks: int = 50) -> list[ConstituentStock]:
    """Parse EM constituent stock DataFrame into ConstituentStock list."""
    if cons_df is None or cons_df.empty:
        return []

    code_col = _col(cons_df, "代码", "code", "ts_code")
    name_col = _col(cons_df, "名称", "name")
    chg_col = _col(cons_df, "涨跌幅", "change_pct")
    pe_col = _col(cons_df, "市盈率(动)", "市盈率TTM", "pe_ttm", "pe")
    pb_col = _col(cons_df, "市净率", "pb")
    mv_col = _col(cons_df, "总市值", "market_cap", "total_mv")
    ytd_col = _col(cons_df, "年初至今涨跌幅", "ytd_change")

    if code_col is None or name_col is None:
        return []

    # Sort by market cap descending when available
    if mv_col:
        cons_df = cons_df.copy()
        cons_df[mv_col] = pd.to_numeric(cons_df[mv_col], errors="coerce").fillna(0)
        cons_df = cons_df.sort_values(mv_col, ascending=False)

    stocks = []
    for _, row in cons_df.head(max_stocks).iterrows():
        raw_mv = _safe_float(row[mv_col]) if mv_col else 0.0
        # EM total_mv is usually in 元; convert to 亿
        if raw_mv > 1e8:
            mv_yi = round(raw_mv / 1e8, 2)
        else:
            mv_yi = round(raw_mv, 2)
        stocks.append(
            ConstituentStock(
                code=str(row[code_col]),
                name=str(row[name_col]),
                change_pct=_safe_float(row[chg_col]) if chg_col else 0.0,
                market_cap=mv_yi,
                pe_ttm=_safe_float(row[pe_col]) if pe_col else 0.0,
                pb=_safe_float(row[pb_col]) if pb_col else 0.0,
                change_pct_ytd=_safe_float(row[ytd_col]) if ytd_col else 0.0,
            )
        )
    return stocks


# ---------------------------------------------------------------------------
# LLM summary generation
# ---------------------------------------------------------------------------

async def _generate_industry_summary(
    industry_name: str,
    performance: IndustryPerformance,
    valuation_history: list[IndustryValuation],
    top_stocks: list[ConstituentStock],
) -> IndustrySummary:
    # Build context for the prompt
    perf_dict = {
        "今日涨跌幅(%)": round(performance.change_pct_today, 2),
        "近1周(%)": round(performance.change_pct_1w, 2),
        "近1月(%)": round(performance.change_pct_1m, 2),
        "近3月(%)": round(performance.change_pct_3m, 2),
        "近6月(%)": round(performance.change_pct_6m, 2),
        "近1年(%)": round(performance.change_pct_1y, 2),
        "总市值(亿元)": round(performance.total_mv, 0),
        "换手率(%)": round(performance.turnover_rate, 2),
        "今日上涨家数": performance.up_count,
        "今日下跌家数": performance.down_count,
    }

    # Latest valuation snapshot
    latest_val = valuation_history[0] if valuation_history else None
    val_dict = {}
    if latest_val:
        val_dict = {
            "最新PE_TTM": round(latest_val.pe_ttm, 2),
            "最新PB": round(latest_val.pb, 2),
        }
    # Historical PE range for percentile context
    if len(valuation_history) >= 4:
        pe_values = [v.pe_ttm for v in valuation_history if v.pe_ttm > 0]
        if pe_values:
            val_dict["PE历史最低"] = round(min(pe_values), 2)
            val_dict["PE历史最高"] = round(max(pe_values), 2)
            val_dict["PE历史均值"] = round(sum(pe_values) / len(pe_values), 2)

    # Top 10 stocks summary
    top10 = [
        {
            "代码": s.code,
            "名称": s.name,
            "市值(亿元)": s.market_cap,
            "今日涨跌(%)": s.change_pct,
            "PE_TTM": s.pe_ttm,
            "PB": s.pb,
        }
        for s in top_stocks[:10]
    ]

    prompt = (
        "你是一位专业的A股行业研究员。请根据以下数据，对该行业进行简明扼要的基本面分析。\n\n"
        f"行业：{industry_name}\n\n"
        f"价格表现：\n{json.dumps(perf_dict, ensure_ascii=False, indent=2)}\n\n"
    )
    if val_dict:
        prompt += f"估值数据：\n{json.dumps(val_dict, ensure_ascii=False, indent=2)}\n\n"
    if top10:
        prompt += f"行业龙头股（按市值排序）：\n{json.dumps(top10, ensure_ascii=False, indent=2)}\n\n"

    prompt += (
        "请综合价格走势、估值水平和龙头股表现，严格按以下JSON格式返回分析结果"
        "（不要包含任何JSON以外的内容）：\n"
        "{\n"
        '  "overview": "2-3句话，行业整体概述含近期走势",\n'
        '  "valuation_comment": "估值水平评价：偏高/合理/偏低，并给出理由",\n'
        '  "strengths": ["优势/机会1", "优势/机会2", "优势/机会3"],\n'
        '  "risks": ["风险1", "风险2"],\n'
        '  "outlook": "1-2句话，基于以上数据的投资展望"\n'
        "}"
    )

    response = await tracked_chat_completion(
        component="tool.industry_fundamentals.summary",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    raw = (response.choices[0].message.content or "").strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    parsed = json.loads(raw)
    return IndustrySummary(
        overview=parsed.get("overview", ""),
        valuation_comment=parsed.get("valuation_comment", ""),
        strengths=parsed.get("strengths", []),
        risks=parsed.get("risks", []),
        outlook=parsed.get("outlook", ""),
    )


# ---------------------------------------------------------------------------
# Main tool function
# ---------------------------------------------------------------------------

async def get_industry_fundamentals(
    industry: str,
    periods: int = 8,
) -> IndustryFundamentals:
    """获取指定A股行业的基本面数据，包括行情表现、估值历史和成分股分析。

    当用户需要了解某个行业的整体状况、估值水平或近期走势时调用，包括：
    行业涨跌表现（近1周/1月/3月/6月/1年）、行业PE/PB估值历史与当前水平、
    行业成分股列表（市值排序）、综合分析摘要。不适用于查询单只股票数据。

    Args:
        industry: 行业名称，支持申万/东方财富常见叫法，如：
                  "银行"、"医药生物"、"食品饮料"、"新能源"、"半导体"、
                  "计算机"、"白酒"、"电力设备"、"汽车"、"房地产" 等。
        periods:  估值历史月数（默认8个月；设为 24 可查看约2年历史）

    Returns:
        IndustryFundamentals，包含：
        - industry_name / industry_code：行业名称与东方财富代码
        - sw_code：申万行业指数代码（如可获取）
        - performance：今日快照 + 近期各时间段价格表现
        - valuation_history：历史PE/PB/市值（按月采样，最多24个点）
        - top_stocks：成分股列表（按市值降序，最多50只）
        - summary：AI生成的行业综合分析（估值评价、优势、风险、展望）
    """
    periods = max(1, min(periods, 24))
    # History window: periods months + 30-day buffer
    start_date = (
        datetime.date.today() - datetime.timedelta(days=periods * 30 + 30)
    ).strftime("%Y%m%d")
    # For 1-year performance we need at least 400 days of price history
    hist_start = (
        datetime.date.today() - datetime.timedelta(days=400)
    ).strftime("%Y%m%d")

    with AkShareHelper() as ak_helper, TuShareHelper() as ts_helper:
        # ── 1. Get EM board snapshot ─────────────────────────────────────────
        boards_df = _fetch_em_snapshot(ak_helper)
        board_row = _find_em_board(boards_df, industry) if boards_df is not None else None

        if board_row is None:
            raise ValueError(
                f"未找到行业 '{industry}'，请检查名称是否正确。"
                "常见行业名称示例：银行、医药生物、食品饮料、半导体、新能源、汽车、计算机"
            )

        snap = _parse_em_snapshot_row(board_row)
        matched_name: str = snap["name"]

        # ── 2. Historical price for rolling returns ─────────────────────────
        hist_df = _fetch_em_history(ak_helper, matched_name, hist_start)
        perf_changes = _compute_perf_from_hist(hist_df, snap["change_pct"])

        performance = IndustryPerformance(
            current_price=snap["price"],
            change_pct_today=snap["change_pct"],
            change_pct_1w=perf_changes["1w"],
            change_pct_1m=perf_changes["1m"],
            change_pct_3m=perf_changes["3m"],
            change_pct_6m=perf_changes["6m"],
            change_pct_1y=perf_changes["1y"],
            up_count=snap["up_count"],
            down_count=snap["down_count"],
            total_mv=snap["total_mv"],
            turnover_rate=snap["turnover"],
        )

        # ── 3. Constituent stocks ────────────────────────────────────────────
        cons_df = _fetch_em_constituents(ak_helper, matched_name)
        top_stocks = _parse_constituents(cons_df)

        # ── 4. SW industry PE/PB history (Tushare) ──────────────────────────
        sw_code = _find_sw_code(ts_helper, matched_name)
        valuation_history: list[IndustryValuation] = []
        if sw_code:
            valuation_history = _fetch_sw_valuation_history(ts_helper, sw_code, start_date)

    # ── 5. AI summary ────────────────────────────────────────────────────────
    summary = await _generate_industry_summary(
        industry_name=matched_name,
        performance=performance,
        valuation_history=valuation_history,
        top_stocks=top_stocks,
    )

    return IndustryFundamentals(
        industry_name=matched_name,
        industry_code=snap["code"],
        sw_code=sw_code,
        performance=performance,
        valuation_history=valuation_history,
        top_stocks=top_stocks,
        summary=summary,
    )
