"""申万行业分类匹配（industry-context Phase 2，从 agents.facts.tools.industry_fact 迁入）。

行业包自包含原则：本模块只依赖 ``pandas`` 与轻量工具，不触发 Tushare/AkShare 的
import 副作用，供 ``industry_fact`` 工具与多来源交叉校验（``crosscheck``）共用。

匹配策略（修复 L1-only 前缀 contains 匹配对子行业名恒失败的问题，见
docs/industry-context-phase1-design.md §2.1 姊妹问题）：
1. **精确匹配优先**，层级由细到粗（L3 → L2 → L1）；
2. 无精确命中时**前缀匹配**，同样层级由细到粗（如 "白酒" → "白酒Ⅱ"）；
3. 都不中 → ``(None, None)``，调用方降级。
"""

from __future__ import annotations

from typing import Any

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


def _classify_columns(df) -> tuple[str | None, str | None]:
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
