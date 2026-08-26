"""申万行业分类匹配（industry-context Phase 2，从 agents.facts.tools.industry_fact 迁入）。

行业包自包含原则：本模块只依赖 ``pandas`` 与轻量工具，不触发 Tushare/AkShare 的
import 副作用，供 ``industry_fact`` 工具与多来源交叉校验（``crosscheck``）共用。

匹配策略（修复 L1-only 前缀 contains 匹配对子行业名恒失败的问题，见
docs/industry/industry-context-phase1-design.md §2.1 姊妹问题）：
1. **成分表精确归属优先**（``extract_sw_member``）：``index_member_all(ts_code=...)``
   直接返回个股申万 L1/L2/L3 归属，不依赖 ``stock_basic.industry`` 的证监会口径名；
2. 成分不可用时按**行业名匹配兜底**（``match_sw_industry``），精确优先层级由细到粗
   （L3 → L2 → L1），再前缀匹配（如 "白酒" → "白酒Ⅱ"）；
3. 都不中 → ``(None, None)``，调用方降级。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

# 申万分类层级（由细到粗，匹配优先级）
_SW_LEVELS = ("L3", "L2", "L1")


def _safe_str(value: Any, default: str = "") -> str:
    """宽容转 str（与 agents.facts.tools._utils.safe_str 同语义，内联避免拉起
    alphabee.agents.facts 包——其 import 链会触发 tushare token 初始化副作用）。"""
    if value is None:
        return default
    try:
        s = str(value).strip()
    except (TypeError, ValueError):
        return default
    return default if s in ("nan", "None", "") else s


def _classify_columns(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """从分类 DataFrame 中找行业名列与代码列。

    分类帧经 Tushare adapter 重命名后只有 canonical 列名（``industry_name`` /
    ``sw_code``），此处不再回退外部列名（Phase 2 字段治理：外部字段只在 adapter 层）。
    """
    name_col = "industry_name" if "industry_name" in df.columns else None
    code_col = "sw_code" if "sw_code" in df.columns else None
    return name_col, code_col


def match_sw_industry(industry: str, frames: dict[str, Any]) -> tuple[str | None, str | None]:
    """在申万 L1/L2/L3 分类中匹配行业名 → ``(sw_code, level)``。

    Args:
        industry: 公司所属行业名（stock_basic.industry，如 "半导体"）。
        frames: ``{level: DataFrame}``，level ∈ {"L1", "L2", "L3"}，
            列名为 canonical（``industry_name`` / ``sw_code``）。

    Returns:
        (sw_code, level)；无法匹配时 (None, None)。
    """
    if not industry:
        return None, None
    for mode in ("exact", "prefix"):
        for level in _SW_LEVELS:
            frame = frames.get(level)
            if frame is None or frame.empty:
                continue
            name_col, code_col = _classify_columns(frame)
            if name_col is None or code_col is None:
                continue
            names = frame[name_col].astype(str)
            if mode == "exact":
                matched = frame[names == industry]
            else:
                matched = frame[names.str.startswith(industry, na=False)]
            if not matched.empty:
                return _safe_str(matched.iloc[0].get(code_col)), level
    return None, None


def _is_active_member(row: Any) -> bool:
    """成分行是否仍在该申万指数内（out_date 为空/NaN）。"""
    out = row.get("out_date")
    if out is None:
        return True
    if isinstance(out, float) and out != out:  # NaN
        return True
    return str(out).strip() in ("", "nan", "None")


def extract_sw_member(member_df: Any) -> dict[str, str] | None:
    """从 ``index_member_all(ts_code=...)`` 单只查询结果解析申万归属层级路径。

    **权威归属**：``index_member_all`` 按个股直接返回其所属申万 L1/L2/L3 代码与名称，
    避免用 ``stock_basic.industry``（证监会口径名，如 "电气设备"）去猜申万名
    （申万无此名，对应 L1 实为 "电力设备"）。输出列（Tushare 官方契约）：
    ``l1_code / l1_name / l2_code / l2_name / l3_code / l3_name / ts_code /
    in_date / out_date / is_new``。

    解析优先级 L3 → L2 → L1（子行业成分股更同质）；仅取当前有效行
    （``out_date`` 为空，即仍在指数内）。

    Args:
        member_df: ``index_member_all(ts_code=..., is_new="Y")`` 的原始 DataFrame，
            未走 adapter 重命名（该接口无 mapping）。

    Returns:
        解析成功返回 dict：
          ``sw_code`` / ``sw_level`` / ``industry_name`` —— 解析到的归属（L3 优先）；
          ``l1_code`` / ``l1_name`` / ``l2_code`` / ``l2_name`` / ``l3_code`` / ``l3_name``
          —— 完整申万层级路径（缺失层为空字符串）。
        无法解析返回 None。
    """
    if member_df is None or getattr(member_df, "empty", True):
        return None
    active = [r for _, r in member_df.iterrows() if _is_active_member(r)]
    if not active:
        return None
    row = active[0]
    path: dict[str, str] = {
        "l1_code": _safe_str(row.get("l1_code")),
        "l1_name": _safe_str(row.get("l1_name")),
        "l2_code": _safe_str(row.get("l2_code")),
        "l2_name": _safe_str(row.get("l2_name")),
        "l3_code": _safe_str(row.get("l3_code")),
        "l3_name": _safe_str(row.get("l3_name")),
    }
    for level, code_col, name_col in (
        ("L3", "l3_code", "l3_name"),
        ("L2", "l2_code", "l2_name"),
        ("L1", "l1_code", "l1_name"),
    ):
        if code_col not in member_df.columns:
            continue
        code = _safe_str(row.get(code_col))
        if code:
            path["sw_code"] = code
            path["sw_level"] = level
            path["industry_name"] = _safe_str(row.get(name_col))
            return path
    return None
