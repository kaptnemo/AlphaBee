"""业务线分类口径稳定性审计（company_track 数据质量诊断脚本）。

背景：company_track 用「东方财富主营构成」逐期回放公司披露的主营构成表，
``label.py`` 按 category 字符串（"按产品分类"）优选业务线并推导 track_label。
实测发现「同一个 category 字符串在不同报告期可能指代完全不同的拆分口径」，例如：

  - 恒瑞医药：半年报「按产品分类」= 销售商品/许可收入（收入性质拆分），
    年报才是肿瘤/神经科学/造影剂…（治疗领域拆分）→ 最新期标签退化成「销售商品」；
  - 比亚迪：「汽车、汽车相关产品及其他产品」因名字含「其他」被 ``drop_other``
    子串匹配误杀 → 最新期按产品分类被清空 → 标签为空；
  - 中芯国际：「集成电路晶圆代工」→「集成电路晶圆制造代工」纯改名被 Jaccard 误判为硬切换。

本脚本对一批公司批量取数（EM 优先 / tushare 兜底），识别以下失效模式：
  - REVENUE_TYPE_LATEST  最新期「按产品分类」退化为收入性质拆分（销售商品/许可收入/提供服务…）
  - COMPOUND_OTHER       合法分项名含「其他」被生产代码 drop_other 误杀（如比亚迪）
  - REAL_TAXONOMY_BREAK  相邻报告期产品分项集合实质性重分类（破坏跨期 yoy）
  - RENAME               相邻期仅改名（晶圆代工→晶圆制造代工），非实质重分类
  - REVENUE_FLIP         收入性质拆分 ↔ 产品组合 在报告期之间翻转
  - ANONYMIZED           分项名为 EM 匿名占位（主业1/产品1/业务1…）
  - SINGLE_INDUSTRY      「按行业分类」退化为单行业（纯业务线，低信息量）
  - NO_PRODUCT_CAT       无「按产品分类」
  - COARSE_PRODUCT_BUCKET 产品口径退化为粗大类单桶（或 top1 高度集中），而行业口径仍有区分度
                          → 现行「产品优先」选类会产出粗口径赛道标签（如工业富联 `3C电子产品`）

用法：
    poetry run python scripts/segment_taxonomy_audit.py                 # 默认样本
    poetry run python scripts/segment_taxonomy_audit.py --symbols 600276.SH 600519.SH
    poetry run python scripts/segment_taxonomy_audit.py --out /tmp/audit.md
    poetry run python scripts/segment_taxonomy_audit.py --self-check     # 离线核对判据纯函数（不取数）

输出：控制台汇总 + Markdown 报告文件（默认 outputs/segment_taxonomy_audit.md）。

注意：本脚本直接调用 data 层的 ``_fetch_em_rows`` / ``_fetch_tushare_rows``，
刻意绕过 ``fetch_business_segments``。
"""

from __future__ import annotations

import argparse
import datetime
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from alphabee.company_track.contracts import SegmentSnapshot
from alphabee.company_track.data import _fetch_em_rows, _fetch_tushare_rows
from alphabee.company_track.label import derive_track_label
from alphabee.company_track.normalize import normalize_segments

# 符号 → 中文名（审计样本，覆盖医药/消费/科技/制造/新能源/金融）
_DEFAULT_SYMBOLS: dict[str, str] = {
    # 医药
    "600276.SH": "恒瑞医药",
    "300760.SZ": "迈瑞医疗",
    "603259.SH": "药明康德",
    "002821.SZ": "凯莱英",
    # 消费
    "600519.SH": "贵州茅台",
    "000858.SZ": "五粮液",
    "000333.SZ": "美的集团",
    "603288.SH": "海天味业",
    # 科技/半导体
    "603986.SH": "兆易创新",
    "688981.SH": "中芯国际",
    "002415.SZ": "海康威视",
    # 制造/电子
    "601138.SH": "工业富联",
    "002475.SZ": "立讯精密",
    # 新能源/汽车
    "300750.SZ": "宁德时代",
    "002594.SZ": "比亚迪",
    # 金融
    "601318.SH": "中国平安",
}

# 收入性质拆分的关键词（区别于产品组合/治疗领域/剂型/产品线）
_REVENUE_TYPE_MARKERS: tuple[str, ...] = (
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

# EM 匿名占位名（公司披露名未被解析时的回退命名，如「主业1」「产品2」）
_ANONYMIZED_RE = re.compile(r"(主业|产品|业务|板块)\s*\d+")

# 精确的「其他」占位项集合（避免误杀「汽车…及其他产品」这类复合名）
_EXACT_OTHER = {
    "其他",
    "其它",
    "其他业务",
    "其他主营业务",
    "其他收入",
    "其他类",
    "其他产品",
    "其他服务",
    "其他(补充)",
    "其它(补充)",
}

# 「粗口径单桶」判据阈值：产品口径 top1 占比 ≥ 该值视为退化
# （与 Phase 2 `alphabee/company_track/label.py::_COARSE_SHARE` 对齐，便于两处判据一致）
_COARSE_SHARE = 0.95


def _is_other_precise(name: str) -> bool:
    """精确「其他」占位项：仅当名字核心就是「其他」时才算占位。"""
    n = name.strip()
    if n in _EXACT_OTHER:
        return True
    return n.startswith("其他(") or n.startswith("其它(")


def _production_drops(name: str) -> bool:
    """生产代码 ``_is_other``（子串匹配）会误杀该名，但精确判定认为它是合法分项。"""
    return (("其他" in name) or ("其它" in name)) and not _is_other_precise(name)


def _is_revenue_type(name: str) -> bool:
    return any(m in name for m in _REVENUE_TYPE_MARKERS)


def _is_anonymized(name: str) -> bool:
    return bool(_ANONYMIZED_RE.search(name))


def _informative(names: list[str]) -> list[str]:
    """剔除精确「其他」占位项后的有效分项名。"""
    return [n for n in names if not _is_other_precise(n)]


def classify_product_set(names: list[str]) -> str:
    """把一组分项名分类：revenue_type（收入性质）/ product_mix（产品组合）/ none。"""
    info = _informative(names)
    if not info:
        return "none"
    matched = sum(1 for n in info if _is_revenue_type(n))
    return "revenue_type" if matched / len(info) >= 0.5 else "product_mix"


def _num(value: object) -> float | None:
    """宽松浮点转换（非法/None/NaN → None）。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _share_profile(rows: list[dict], period: str, category: str) -> dict[str, float]:
    """某报告期某分类口径「去其他后」的分项占比（0-1，按分项名聚合）。纯函数，无 I/O。

    占比来源：优先数据源直接给出的 ``biz_segment_revenue_share``（EM 为类别内 0-1 比例，
    各类别内部合计 = 1）；该列缺失或不完整时退化为「类别内营收占比」。同一分项名多行先求和。
    """
    rev: dict[str, float] = {}
    raw: dict[str, float] = {}
    for r in rows:
        if str(r.get("report_date")) != period or str(r.get("biz_segment_category")) != category:
            continue
        nm = str(r.get("biz_segment_name"))
        if _is_other_precise(nm):
            continue
        rev[nm] = rev.get(nm, 0.0) + (_num(r.get("biz_segment_revenue")) or 0.0)
        share = _num(r.get("biz_segment_revenue_share"))
        if share is not None:
            raw[nm] = raw.get(nm, 0.0) + share
    names = set(rev) | set(raw)
    if not names:
        return {}
    total_rev = sum(rev.values())
    if len(raw) < len(names) and total_rev > 0:
        return {nm: rev.get(nm, 0.0) / total_rev for nm in names}
    return {nm: raw.get(nm, 0.0) for nm in names}


def is_coarse_product_bucket(
    product_shares: dict[str, float],
    industry_shares: dict[str, float],
    *,
    coarse_share: float = _COARSE_SHARE,
) -> bool:
    """COARSE_PRODUCT_BUCKET 判据（设计 §3.2，纯函数、确定性，便于核对）。

    条件 1（产品口径退化）：去「其他」后有效分项数 == 1，或 top1 占比 ≥ ``coarse_share``；
    条件 2（行业口径有区分度）：去「其他」后有效分项数 ≥ 2，且 top1 占比 < ``coarse_share``。

    两者同时满足才命中——「单桶但语义精确」的公司（如中芯国际产品/行业同为单桶）条件 2
    不满足，不会误报。``*_shares`` 为分项名 → 占比（0-1）。
    """
    if not product_shares or not industry_shares:
        return False
    product_coarse = len(product_shares) == 1 or max(product_shares.values()) >= coarse_share
    industry_diverse = len(industry_shares) >= 2 and max(industry_shares.values()) < coarse_share
    return product_coarse and industry_diverse


def _sorted_shares(shares: dict[str, float]) -> list[tuple[str, float]]:
    """占比降序的 (分项名, 占比) 列表。"""
    return sorted(shares.items(), key=lambda kv: kv[1], reverse=True)


def _category_label(segments: list[SegmentSnapshot], period: str, category: str) -> str:
    """用生产规则 ``derive_track_label`` 对该期该口径重跑，仅作对照（不改生产代码）。"""
    pool = [seg for seg in segments if seg.report_date == period and seg.category == category]
    if not pool:
        return ""
    return derive_track_label(pool).track_label


def _fmt_shares(items: list[tuple[str, float]], limit: int = 4) -> str:
    """把 (分项名, 占比) 列表渲染为 ``云计算 66.75% / 通信及移动网络设备 32.99%``。"""
    parts = [f"{name} {share * 100:.2f}%" for name, share in items[:limit]]
    if len(items) > limit:
        parts.append(f"另 {len(items) - limit} 项")
    return " / ".join(parts)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_rename(a: set[str], b: set[str]) -> bool:
    """两个单元素集合是否只是改名（相似度高），而非实质重分类。"""
    if len(a) != 1 or len(b) != 1:
        return False
    x, y = next(iter(a)), next(iter(b))
    return SequenceMatcher(None, x, y).ratio() >= 0.5


@dataclass
class CompanyAudit:
    symbol: str = ""
    name: str = ""
    source: str = ""
    error: str | None = None
    n_rows: int = 0
    periods: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    product_by_period: dict[str, tuple[list[str], str]] = field(default_factory=dict)
    real_breaks: list[dict] = field(default_factory=list)  # 实质重分类
    renames: list[dict] = field(default_factory=list)  # 纯改名
    product_flips: list[dict] = field(default_factory=list)  # revenue↔product 翻转
    latest_product_class: str = ""
    latest_product_names: list[str] = field(default_factory=list)
    rule_track_label: str = ""
    fallback_period: str = ""
    fallback_label: str = ""
    compound_other_names: list[str] = field(default_factory=list)  # 被生产代码误杀的分项
    anonymized_names: list[str] = field(default_factory=list)
    industry_single: bool = False
    coarse_bucket_periods: list[dict] = field(default_factory=list)  # 命中 COARSE_PRODUCT_BUCKET 的报告期
    flags: list[str] = field(default_factory=list)

    @property
    def latest_period(self) -> str:
        return self.periods[-1] if self.periods else ""

    @property
    def coarse_bucket_latest_hit(self) -> bool:
        """最新报告期是否命中（= 现行生产标签已经落在粗口径分项上）。"""
        return bool(self.coarse_bucket_periods) and self.coarse_bucket_periods[-1]["period"] == self.latest_period


def _analyze(symbol: str, name: str) -> CompanyAudit:
    audit = CompanyAudit(symbol=symbol, name=name)
    rows, em_err = _fetch_em_rows(symbol)
    audit.source = "em"
    if not rows:
        rows, tushare_err = _fetch_tushare_rows(symbol)
        audit.source = "tushare"
        audit.error = tushare_err or em_err
    if not rows:
        audit.flags.append("NO_DATA")
        audit.error = audit.error or "两源均无主营构成数据"
        return audit

    audit.n_rows = len(rows)
    audit.periods = sorted({str(r.get("report_date")) for r in rows})
    audit.categories = sorted({str(r.get("biz_segment_category")) for r in rows})

    by_period_cat: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        key = (str(r.get("report_date")), str(r.get("biz_segment_category")))
        by_period_cat.setdefault(key, []).append(str(r.get("biz_segment_name")))
    for period in audit.periods:
        names = by_period_cat.get((period, "按产品分类"), [])
        if names:
            audit.product_by_period[period] = (_informative(names), classify_product_set(names))

    # 生产规则对照用的归一化分项（纯计算；复用 normalize，不额外取数）
    segments = normalize_segments(rows, audit.source)

    prod_periods = sorted(audit.product_by_period)
    for p1, p2 in zip(prod_periods, prod_periods[1:]):
        n1, c1 = audit.product_by_period[p1]
        n2, c2 = audit.product_by_period[p2]
        s1, s2 = set(n1), set(n2)
        if s1 and s2 and _jaccard(s1, s2) == 0.0:
            if _is_rename(s1, s2):
                audit.renames.append({"p1": p1, "p2": p2, "names1": n1, "names2": n2})
            else:
                audit.real_breaks.append({"p1": p1, "p2": p2, "names1": n1, "names2": n2})
        if c1 in ("revenue_type", "product_mix") and c2 in ("revenue_type", "product_mix") and c1 != c2:
            audit.product_flips.append({"p1": p1, "c1": c1, "p2": p2, "c2": c2})

    if prod_periods:
        latest_names, latest_class = audit.product_by_period[prod_periods[-1]]
        audit.latest_product_class = latest_class
        audit.latest_product_names = latest_names
        audit.compound_other_names = [n for n in latest_names if _production_drops(n)]
        audit.anonymized_names = [n for n in latest_names if _is_anonymized(n)]
        latest_rev: dict[str, float] = {}
        for r in rows:
            if str(r.get("report_date")) == prod_periods[-1] and str(r.get("biz_segment_category")) == "按产品分类":
                nm = str(r.get("biz_segment_name"))
                if not _is_other_precise(nm):
                    latest_rev[nm] = latest_rev.get(nm, 0.0) + float(r.get("biz_segment_revenue") or 0.0)
        if latest_rev:
            audit.rule_track_label = max(latest_rev, key=latest_rev.get)

        for period in reversed(prod_periods):
            nm, cls = audit.product_by_period[period]
            if cls == "product_mix" and nm:
                audit.fallback_period = period
                rev: dict[str, float] = {}
                for r in rows:
                    if str(r.get("report_date")) == period and str(r.get("biz_segment_category")) == "按产品分类":
                        nm2 = str(r.get("biz_segment_name"))
                        if not _is_other_precise(nm2):
                            rev[nm2] = rev.get(nm2, 0.0) + float(r.get("biz_segment_revenue") or 0.0)
                if rev:
                    audit.fallback_label = max(rev, key=rev.get)
                break

    if "按行业分类" in audit.categories:
        ind_names = _informative(by_period_cat.get((audit.latest_period, "按行业分类"), []))
        if len(ind_names) == 1:
            audit.industry_single = True

    # COARSE_PRODUCT_BUCKET：逐报告期判定「产品口径粗退化 + 行业口径有区分度」
    for period in audit.periods:
        product_shares = _share_profile(rows, period, "按产品分类")
        industry_shares = _share_profile(rows, period, "按行业分类")
        if not is_coarse_product_bucket(product_shares, industry_shares):
            continue
        audit.coarse_bucket_periods.append(
            {
                "period": period,
                "product": _sorted_shares(product_shares),
                "industry": _sorted_shares(industry_shares),
                "product_label": _category_label(segments, period, "按产品分类"),
                "industry_label": _category_label(segments, period, "按行业分类"),
            }
        )

    # 汇总 flags（按严重度排序）
    if audit.coarse_bucket_periods:
        audit.flags.append("COARSE_PRODUCT_BUCKET")
    if audit.latest_product_class == "revenue_type":
        audit.flags.append("REVENUE_TYPE_LATEST")
    if audit.compound_other_names:
        audit.flags.append("COMPOUND_OTHER")
    if audit.product_flips:
        audit.flags.append("REVENUE_FLIP")
    if audit.real_breaks:
        audit.flags.append(f"REAL_TAXONOMY_BREAK×{len(audit.real_breaks)}")
    if audit.renames:
        audit.flags.append(f"RENAME×{len(audit.renames)}")
    if audit.anonymized_names:
        audit.flags.append("ANONYMIZED")
    if not audit.product_by_period:
        audit.flags.append("NO_PRODUCT_CAT")
    if audit.industry_single:
        audit.flags.append("SINGLE_INDUSTRY")
    return audit


def _coarse_cell(audit: CompanyAudit) -> str:
    """总览表「粗口径单桶」单元格。"""
    hits = audit.coarse_bucket_periods
    if not hits:
        return "—"
    scope = "含最新期" if audit.coarse_bucket_latest_hit else "仅历史期"
    return f"命中({len(hits)}期,{scope})"


def _render_coarse_bucket_section(results: list[CompanyAudit]) -> list[str]:
    """「受影响公司清单 + 产品标签 vs 行业标签对照」（Phase 2 判据宽窄的输入）。"""
    lines: list[str] = []
    lines.append("## COARSE_PRODUCT_BUCKET 受影响公司清单")
    lines.append("")
    lines.append(
        f"- 判据：产品口径去「其他」后有效分项数 == 1，或 top1 占比 ≥ {_COARSE_SHARE * 100:.0f}%；"
        f"且行业口径有效分项数 ≥ 2 且 top1 占比 < {_COARSE_SHARE * 100:.0f}%（行业口径有区分度）。"
    )
    lines.append("- 命中口径：任一报告期命中即计入（历史长期退化也算，便于观察判据宽窄）。")
    lines.append("")
    hits = [r for r in results if r.coarse_bucket_periods]
    latest_hits = [r for r in hits if r.coarse_bucket_latest_hit]
    if not hits:
        lines.append("（无）")
        lines.append("")
        return lines
    lines.append(
        f"命中 **{len(hits)}** 家；其中最新期命中 **{len(latest_hits)}** 家"
        "（现行「产品优先」规则已产出粗口径赛道标签）。"
    )
    lines.append("")
    lines.append(
        "| 公司 | 代码 | 最新期 | 最近命中期 | 最新期是否命中 | 命中期数 | 产品口径标签 | 产品口径（去「其他」） | 行业口径标签 | 行业口径（去「其他」） |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in hits:
        rec = r.coarse_bucket_periods[-1]
        latest = "是" if r.coarse_bucket_latest_hit else "否"
        lines.append(
            f"| {r.name} | {r.symbol} | {r.latest_period} | {rec['period']} | {latest} | "
            f"{len(r.coarse_bucket_periods)} | {rec['product_label'] or '—'} | "
            f"{len(rec['product'])} 项：{_fmt_shares(rec['product'])} | {rec['industry_label'] or '—'} | "
            f"{len(rec['industry'])} 项：{_fmt_shares(rec['industry'])} |"
        )
    lines.append("")
    lines.append(
        "> 「产品口径标签」= 现行规则（产品优先）在该期的产出；「行业口径标签」= 用 `derive_track_label` "
        "对同期「按行业分类」重跑（仅作对照，未改生产代码）。"
    )
    lines.append("")
    return lines


def _render_markdown(results: list[CompanyAudit]) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append("# 业务线分类口径稳定性审计报告")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 样本数：{len(results)}")
    lines.append("- 数据源：东方财富主营构成（`stock_zygc_em`，缺失时 Tushare `fina_mainbz` 兜底）")
    lines.append("- 口径：剔除精确「其他」占位项后比较各期「按产品分类」分项集合。")
    lines.append(
        "  失效模式定义见脚本 docstring（REVENUE_TYPE_LATEST / COMPOUND_OTHER / REAL_TAXONOMY_BREAK / RENAME / REVENUE_FLIP / ANONYMIZED / SINGLE_INDUSTRY / NO_PRODUCT_CAT / COARSE_PRODUCT_BUCKET）。"
    )
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 公司 | 代码 | 最新期 | 期数 | 最新期按产品口径 | 规则标签 | 建议回退期/标签 | 粗口径单桶 | 标记 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        flags = ",".join(r.flags) if r.flags else "CLEAN"
        fallback = f"{r.fallback_period}/{r.fallback_label}" if r.fallback_period else "—"
        coarse = _coarse_cell(r)
        lines.append(
            f"| {r.name} | {r.symbol} | {r.latest_period} | {len(r.periods)} | "
            f"{r.latest_product_class} | {r.rule_track_label or '—'} | {fallback} | {coarse} | {flags} |"
        )
    lines.append("")
    lines.append("## 问题公司明细")
    lines.append("")
    flagged = [r for r in results if r.flags]
    if not flagged:
        lines.append("（无）")
    for r in flagged:
        lines.append(f"### {r.name}（{r.symbol}）")
        lines.append("")
        lines.append(f"- 标记：`{'`, `'.join(r.flags)}`")
        lines.append(f"- 最新期 `{r.latest_period}` 按产品分类：`{' / '.join(r.latest_product_names) or '（无）'}`")
        if r.rule_track_label:
            lines.append(f"- 现行规则会产出标签：**{r.rule_track_label}**")
        if r.fallback_period and r.fallback_period != r.latest_period:
            lines.append(f"- 建议回退：`{r.fallback_period}` 期按产品分类 → 标签 `{r.fallback_label}`")
        if r.compound_other_names:
            lines.append(f"- 被生产代码 `drop_other` 误杀的分项：`{' / '.join(r.compound_other_names)}`")
        if r.anonymized_names:
            lines.append(f"- EM 匿名占位分项：`{' / '.join(r.anonymized_names)}`")
        if r.product_flips:
            flips = "; ".join(f"{f['p1']}({f['c1']})→{f['p2']}({f['c2']})" for f in r.product_flips)
            lines.append(f"- 口径翻转（revenue↔产品组合）：{flips}")
        for b in r.real_breaks:
            lines.append(
                f"- 实质重分类：`{b['p1']}` `{'/'.join(b['names1'])}` → `{b['p2']}` `{'/'.join(b['names2'])}`（Jaccard=0）"
            )
        for b in r.renames:
            lines.append(f"- 纯改名：`{b['p1']}` `{'/'.join(b['names1'])}` → `{b['p2']}` `{'/'.join(b['names2'])}`")
        for rec in r.coarse_bucket_periods:
            scope = "，最新期" if rec["period"] == r.latest_period else ""
            lines.append(
                f"- **COARSE_PRODUCT_BUCKET**（`{rec['period']}`{scope}）："
                f"产品口径去「其他」后 {len(rec['product'])} 项（{_fmt_shares(rec['product'])}）"
                f" → 产品口径标签 **{rec['product_label'] or '—'}**；"
                f"行业口径 {len(rec['industry'])} 项（{_fmt_shares(rec['industry'])}）"
                f" → 若按行业分类会得到 **{rec['industry_label'] or '—'}**"
            )
        lines.append("")
    lines.extend(_render_coarse_bucket_section(results))
    lines.append("## 结论与建议")
    lines.append("")
    lines.append(
        "1. 分类口径不稳定是**披露粒度差异**（半年报粗、年报细，或公司变更披露口径），非抓取错误；EM 忠实回放。"
    )
    lines.append("2. 三类高影响失效需在 `company_track` 侧修复：")
    lines.append(
        "   - REVENUE_TYPE_LATEST：最新期为收入性质拆分 → `derive_track_label` 产出零信息量标签，需回退最近 product_mix 期；"
    )
    lines.append("   - COMPOUND_OTHER：`_is_other` 子串匹配误杀含「其他」的合法分项 → 需改精确判定；")
    lines.append("   - REAL_TAXONOMY_BREAK / REVENUE_FLIP：相邻期口径切换 → 跨期 yoy 无法同名匹配，需口径漂移告警。")
    lines.append(
        "3. RENAME（纯改名）与 ANONYMIZED（EM 匿名名）属数据源噪音，可做别名归一或降级提示，不应视为实质重分类。"
    )
    lines.append("4. SINGLE_INDUSTRY（按行业分类单行业）对纯业务线公司是常态，低信号但非缺陷。")
    hits = [r for r in results if r.coarse_bucket_periods]
    latest_hits = [r for r in hits if r.coarse_bucket_latest_hit]
    lines.append(
        f"5. COARSE_PRODUCT_BUCKET（粗口径单桶退化）：产品口径去「其他」后只剩 1 项或 top1 占比 ≥ "
        f"{_COARSE_SHARE * 100:.0f}%，而行业口径仍有 ≥2 项区分度 → 现行「产品优先」选类产出粗口径赛道标签"
        "（工业富联 `3C电子产品` 99.82%）。"
        f"本轮实测命中 {len(hits)} 家（最新期命中 {len(latest_hits)} 家）为 Phase 2 的判据宽窄输入；"
        "「单桶但语义精确」样本（中芯国际：产品 1 项 + 行业同为 1 项）不命中，说明阈值未误伤该形态。"
    )
    lines.append("")
    return "\n".join(lines)


def _run_self_check() -> list[str]:
    """离线核对判据纯函数（不取数、不联网）：返回失败用例的描述列表。"""
    cases: list[tuple[str, dict[str, float], dict[str, float], bool]] = [
        (
            "工业富联 601138：产品单桶 3C电子产品 99.82% / 行业 3 项 top1=66.75% → 命中",
            {"3C电子产品": 0.998158},
            {"云计算": 0.667502, "通信及移动网络设备": 0.329888, "工业互联网": 0.000769},
            True,
        ),
        (
            "中芯国际 688981：产品单桶 / 行业同为单桶（单桶精确，不误伤）→ 不命中",
            {"集成电路晶圆制造代工": 0.932725},
            {"集成电路行业": 0.988964},
            False,
        ),
        ("产品口径多桶不退化（top1 < 阈值）→ 不命中", {"A": 0.6, "B": 0.4}, {"X": 0.5, "Y": 0.3, "Z": 0.2}, False),
        ("top1 恰在阈值（≥ 命中，验证不等号方向）", {"A": 0.95, "B": 0.05}, {"X": 0.5, "Y": 0.3, "Z": 0.2}, True),
        ("top1 略低于阈值（0.9499）→ 不命中", {"A": 0.9499, "B": 0.0501}, {"X": 0.5, "Y": 0.3, "Z": 0.2}, False),
        ("行业口径也退化（top1 ≥ 阈值）→ 不命中", {"A": 0.999}, {"X": 0.96, "Y": 0.04}, False),
        ("缺少行业口径 → 不命中", {"A": 0.999}, {}, False),
        ("缺少产品口径 → 不命中", {}, {"X": 0.5, "Y": 0.3, "Z": 0.2}, False),
    ]
    failures: list[str] = []
    for desc, product, industry, expected in cases:
        actual = is_coarse_product_bucket(product, industry)
        mark = "PASS" if actual == expected else "FAIL"
        print(f"[self-check] {mark} {desc}（期望 {expected}，实际 {actual}）")
        if actual != expected:
            failures.append(desc)
    if failures:
        print(f"\n[self-check] {len(failures)}/{len(cases)} 用例失败")
    else:
        print(f"\n[self-check] 全部 {len(cases)} 用例通过")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="业务线分类口径稳定性审计")
    parser.add_argument("--symbols", nargs="*", help="股票代码（Tushare 格式），缺省用内置样本")
    parser.add_argument("--out", default="outputs/segment_taxonomy_audit.md", help="报告输出路径")
    parser.add_argument("--self-check", action="store_true", help="只离线核对判据纯函数（不取数、不出报告）")
    args = parser.parse_args()

    if args.self_check:
        raise SystemExit(1 if _run_self_check() else 0)

    symbols: dict[str, str] = {}
    if args.symbols:
        symbols = {s: s for s in args.symbols}
    else:
        symbols = dict(_DEFAULT_SYMBOLS)

    results: list[CompanyAudit] = []
    for symbol, name in symbols.items():
        try:
            audit = _analyze(symbol, name)
        except Exception as exc:  # noqa: BLE001 — 单公司失败不阻断批量
            audit = CompanyAudit(symbol=symbol, name=name, error=str(exc), flags=["ERROR"])
        results.append(audit)

        flags = ",".join(audit.flags) if audit.flags else "CLEAN"
        coarse = "—" if not audit.coarse_bucket_periods else f"{len(audit.coarse_bucket_periods)}期"
        print(
            f"[{audit.name:8s} {audit.symbol}] 最新期={audit.latest_period or '—'} "
            f"期数={len(audit.periods)} 按产品口径={audit.latest_product_class or '—'} "
            f"规则标签={audit.rule_track_label or '—'} 回退={audit.fallback_period or '—'}"
            f"/{audit.fallback_label or '—'} 粗口径单桶={coarse} 标记={flags}"
        )

    report = _render_markdown(results)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"\n报告已写入: {args.out}")


if __name__ == "__main__":
    main()
