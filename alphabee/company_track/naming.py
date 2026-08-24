"""分项名语义判定（company_track 共享工具）。

口径问题的根因之一：分项名语义判定散落各处且用子串匹配，导致
「汽车、汽车相关产品及其他产品」这类复合名被误判成「其他」整段丢弃。
这里统一三类判定，供 ``normalize`` / ``label`` / 漂移检测复用：

1. :func:`is_other_placeholder` —— 精确「其他」占位项（仅当名字核心就是「其他」）；
2. :func:`is_revenue_type_segment` —— 收入性质分项（销售商品/许可收入/提供服务/利息/手续费…）；
3. :func:`is_anonymized_segment` —— EM 匿名占位名（主业1/产品2/业务3…）。

纯函数层，无 IO、无依赖副作用。
"""

from __future__ import annotations

import re

# 精确的「其他」占位项（避免子串误杀复合名）。「其他(补充)」是 EM 的平衡项。
_OTHER_EXACT: frozenset[str] = frozenset(
    {
        "其他",
        "其它",
        "其他业务",
        "其它业务",
        "其他主营业务",
        "其他收入",
        "其他类",
        "其他产品",
        "其他服务",
    }
)
_OTHER_PREFIXES: tuple[str, ...] = ("其他(", "其它(", "其他（", "其它（")

# 收入性质拆分关键词（区别于产品组合/治疗领域/剂型/产品线）
_REVENUE_MARKERS: tuple[str, ...] = (
    "销售商品",
    "商品销售",
    "许可收入",
    "授权收入",
    "技术许可",
    "许可使用费",
    "特许权",
    "提供服务",
    "提供劳务",
    "利息收入",
    "手续费",
    "佣金",
    "保费",
    "建造合同",
    "工程施工",
)

# EM 匿名占位名（公司披露名未解析时 EM 的回退命名，如「主业1」「产品2」「业务3」）
_ANONYMIZED_RE = re.compile(r"(主业|产品|业务|板块)\s*\d+")


def is_other_placeholder(name: str) -> bool:
    """精确「其他」占位项：仅当名字核心就是「其他」时才算占位。

    避免子串匹配把「汽车、汽车相关产品及其他产品」「手机部件、组装及其他产品」
    这类合法分项误杀成「其他」。
    """
    n = (name or "").strip()
    if not n:
        return False
    if n in _OTHER_EXACT:
        return True
    return n.startswith(_OTHER_PREFIXES)


def is_revenue_type_segment(name: str) -> bool:
    """收入性质分项（如「销售商品」「许可收入」「提供服务」）。"""
    return any(marker in (name or "") for marker in _REVENUE_MARKERS)


def is_anonymized_segment(name: str) -> bool:
    """EM 匿名占位名（「主业1」「产品2」「业务3」「板块4」）。"""
    return bool(_ANONYMIZED_RE.search(name or ""))
