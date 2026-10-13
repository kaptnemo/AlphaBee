"""Tier B 模型研究层 + Tier C 确定性收敛（DRIVER_PROFILE_RESEARCH_DESIGN §7/§8）。

三级瀑布里的第 2、3 级，职责边界与 Tier A（规则路由，D0 已实现）互不重叠：

- **Tier B（模型研究，可关）**：``run_driver_research`` 在稀疏区域
  （无专用框架 / 弱锚 / 多主线 / 身份漂移）让 LLM 基于**本公司**的年报与分部结构提出
  标的特异的驱动假设 + 可执行研究议程。有缓存、有工具/时长硬预算、有失败降级。
- **Tier C（确定性收敛，纯函数）**：``converge`` 按 C1–C6 收敛 LLM 原始输出
  （schema 校验 / 证据可溯 / 事实交叉校验 / 原语对齐 / 结构约束 / 派生），
  它**只做守卫、不生成内容**，因此可以脱离 LLM 单测。
- **降级阶梯（Tier 0–3）**：``finalize_research_artifact`` 是降级元数据的唯一落地点
  （委托 ``orchestrator.services.degradation.apply_degradation``），并记录
  ``research_meta["fallback_tier"]``，保证"研究失败"永远有显式留痕、绝不静默。

本模块不接触 LangGraph state / 节点，也不在 import 期读配置：输入是已落地产物
（COMPANY_TRACK / fact_values / DriverProfile），输出是 ``DriverProfile`` 的 artifact payload。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from alphabee import PROJECT_ROOT
from alphabee.agents.driver_profile.output import (
    DriverResearchOutput,
    ResearchAgendaItem,
    ResearchHypothesis,
)
from alphabee.config import DomainContextResearchSettings, get_settings
from alphabee.domain_context.context_router import RouterInput
from alphabee.domain_context.contracts import (
    DriverEvidence,
    DriverHypothesis,
    DriverObservable,
    DriverProfile,
    ResearchQuestion,
)
from alphabee.domain_context.loader import load_playbooks, load_primitives
from alphabee.utils.pipeline import parse_json
from alphabee.utils.storage import normalize_symbol

if TYPE_CHECKING:  # 运行期不拉起 company_track 全家桶（domain_context 保持轻量 import）
    from alphabee.company_track.contracts import CompanyTrackArtifact

logger = logging.getLogger(__name__)

__all__ = [
    "DriverResearchError",
    "DriverResearchResult",
    "build_research_input",
    "converge",
    "count_tool_calls",
    "extract_research_text",
    "finalize_research_artifact",
    "load_cached_research",
    "parse_research_output",
    "research_cache_key",
    "research_enabled",
    "research_settings",
    "run_driver_research",
    "salvage_research_output",
    "save_cached_research",
    "should_research",
]

# ── 触发阈值（§D1.5）─────────────────────────────────────────────────────
#: 主力分部占比低于该值 ⇒ 单一框架不足以刻画（多主线公司，进 Tier B）。
_MULTI_SEGMENT_SHARE = 0.50

# ── 输入包口径（§D1.6）──────────────────────────────────────────────────
#: 关键财务项（canonical 字段名，缺失不写）：与 fact_values 同口径，不发明字段名。
_KEY_FACT_FIELDS = (
    "revenue",
    "net_profit",
    "gross_margin",
    "net_margin",
    "roe",
    "roa",
    "debt_to_assets",
    "current_ratio",
    "revenue_yoy",
    "net_profit_yoy",
    "inventory_yoy",
)
#: 行业/对标基准的键前缀（peer_* / industry_*），原样带进输入包供交叉校验。
_BENCHMARK_PREFIXES = ("peer_", "industry_")
#: 分部结构：最多带几个报告期、每期几条分项（多期的价值在"占比/同比变化"，不需要全量分项）。
_MAX_SEGMENT_PERIODS = 3
_MAX_SEGMENTS_PER_PERIOD = 8
#: 本地年报 MD&A 片段字符上限（§D1.6：上限 12000 字符）。
MAX_REPORT_FRAGMENT_CHARS = 12_000

# ── Tier C 收敛参数（§D1.7）────────────────────────────────────────────
#: C3 事实矛盾时的置信度上界（"降权"而非丢弃：矛盾本身是研究线索）。
_CONTRADICTION_MAX_CONFIDENCE = 0.3
#: C3 占比矛盾容差（百分点）：小于它的一点点出入不视为矛盾（数据口径差异很常见）。
_SHARE_TOLERANCE_PP = 20.0
#: C3 毛利率矛盾容差（百分点）。
_MARGIN_TOLERANCE_PP = 15.0
#: C5 角色约束：primary 条数上限（超过即按 confidence 降级为 secondary）。
_MAX_PRIMARY = 3
#: 研究层最大重试次数（附解析错误 re-ask 一次；LLM 级重试仓库无现成机制，§7.7-5）。
_MAX_LLM_ATTEMPTS = 2

_SHARE_RE = re.compile(r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9/·\-]{2,24}?)\s*(?:收入)?占比\s*(?P<value>\d+(?:\.\d+)?)\s*%")
_MARGIN_RE = re.compile(r"毛利率\s*(?P<value>-?\d+(?:\.\d+)?)\s*%")
_OBSERVABLE_SOURCE_RE = re.compile(
    r"^(?P<name>.+?)\s*[（(]\s*(?:来源|source)\s*[:：]\s*(?P<source>[^）)]+?)\s*[）)]\s*$",
    re.IGNORECASE,
)

# ══════════════════════════════════════════════════════════════════════════
# P0-1/P0-2 修复：数值声明抽取与口径感知校验（药明康德 run 暴露的收敛层缺口）
# ══════════════════════════════════════════════════════════════════════════

#: P0-1A：同一 (基数, 期别) 的隐含总量相对差超过此值 ⇒ 判定互相矛盾。
_IMPLIED_TOTAL_TOLERANCE = 0.05
#: P0-1B：输入包权威值兜底的容差（占比/毛利率用绝对百分点，金额用相对差）。
_INPUT_PCT_TOLERANCE_PP = 2.0
_INPUT_AMOUNT_TOLERANCE = 0.03
#: P0-1B：不检查的单位（运营性口径，非财务声明）。
_IGNORED_UNITS = {"倍", "kL", "L", "个"}

# 期别归一：年份 + 报告期形态
_YEAR_RE = re.compile(r"(20\d{2})")
_END_DATE_RE = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")

# 「X 亿(元/人民币)」——隐含总量的分子
_AMOUNT_YI_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*亿(?:元|人民币)?")

# 「占<base><收入|营收>?<P>%」——隐含总量的分母声明（base 归一后作为分组键）
# 注：只覆盖"占+基数+数字%相邻"的形态；「占持续经营收入 434.2 亿的约 72%」这类
# 分母金额在前、占比在后的形态**故意不覆盖**（保守漏检，见 §9）。
_ZHAN_RE = re.compile(r"占(?P<base>[\u4e00-\u9fa5]{1,12}?)(?:收入|营收)?\s*(?P<pct>\d+(?:\.\d+)?)\s*%")
#: 「占比」形态中 base 会误抓到的废词（"占比86.46%" → base="比"）。
_ZHAN_BASE_STOP = {"比", "比重", "例", "比例", "率"}

# 财务类数字声明（P0-1B 的声明单元；company_form 侧的声明必须带单位）
_NUMBER_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>亿(?:元|人民币)?|万(?:元)?|%|％|倍|kL|L|个)")
#: 证据侧的裸数值抽取（tushare ref 的值是无单位形态："netprofit_margin=42.53"，
#: 不能用 _NUMBER_RE；裸抽取会顺带收进 603259/end_date 等无害串，不影响同值匹配）
_RAW_NUMBER_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)")

# 具名毛利率（P0-2）：「<名>业务/分部?毛利率 P%」；名称为集团口径词时走集团校验。
# ``suffix`` 单独捕获（展示用）：不改写匹配语义，只让冲突文案带上被消费的「业务/分部」。
_MARGIN_NAMED_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9/·\-]{2,20}?)\s*(?P<suffix>业务|分部)?毛利率\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
)
_GROUP_SCOPE_NAMES = ("集团", "公司", "整体", "综合", "当年", "上年")

#: 「毛利率」前缀里的期别/语境 token（不是业务名）：出现即剥离，剥空 ⇒ 该声明按
#: 「无名/集团口径」走残余文本检查（P0-2 修复二轮：旧实现把「2025年毛利率30%」的
#: 名判成 "2025年" 并吞掉 span，导致与集团值的偏差被静默漏检）。
#: 三轮补全：词表与 :func:`_period_tags` 的既有期别词对齐（H1/H2、Q1–Q4、半年报/年报/
#: 季报/一季报/三季报/中报/月报、本月/本季/上季/本报告期…），避免任何期别标签形态漏网。
_PERIOD_CONTEXT_TOKENS = (
    "本报告期",
    "报告期内",
    "报告期",
    "上年同期",
    "去年同期",
    "第一季度",
    "第二季度",
    "第三季度",
    "第四季度",
    "前三季度",
    "一季度",
    "二季度",
    "三季度",
    "四季度",
    "上半年",
    "下半年",
    "半年度",
    "半年报",
    "一季报",
    "三季报",
    "本年度",
    "年报",
    "中报",
    "季报",
    "月报",
    "本季",
    "上季",
    "本月",
    "年度",
    "全年",
    "中期",
    "季度",
    "当期",
    "本期",
    "半年",
    "报告",
    "H1",
    "H2",
    "Q1",
    "Q2",
    "Q3",
    "Q4",
)
#: 前缀/内嵌的期别形态：``2025``、``2025年``、``2026H1``、``2025Q3``、``1-9月``、``9月``。
_PERIOD_TOKEN_RE = re.compile(
    r"(?:20\d{2}|\d{4})(?:年|H[12]|Q[1-4])?"
    r"|\d{1,2}\s*[-~至]\s*\d{1,2}\s*月"
    r"|\d{1,2}\s*月"
)
#: 期别量词字符：剥离后残余若只由它们构成（「2025年报」⇒「报」、「半年报」⇒「报」），
#: 仍不是业务名；长度 < 2 同理（P0-2 修复三轮的兜底规则）。
_PERIOD_QUANTIFIER_CHARS = frozenset("年月季报度期")
#: 业务名最短长度（1 字残余按"非业务名"处理，走残余文本与集团值比）。
_MIN_BUSINESS_NAME_LEN = 2

_SENTENCE_SPLIT_RE = re.compile(r"[。；;，,\n]")

#: 重试时的追加指令（把解析错误回灌给模型：只说"错了"模型无法自我纠正）。
_RETRY_INSTRUCTION = (
    "上一次的回复无法被解析为合法 JSON（错误：{error}）。"
    "请重新输出**一个** JSON 对象：以 {{ 开头、以 }} 结尾，不要输出任何自然语言、"
    "不要用 Markdown 代码块包裹，字段与系统提示中的 schema 指令严格一致。"
)

#: 单次调用的用户消息模板：输入包（结构事实 + 年报片段）以 JSON 原样喂给模型。
_USER_MESSAGE_TEMPLATE = (
    "以下是本次研究可用的输入包（结构性事实与一手材料片段，均为已落地数据）：\n"
    "{research_input_json}\n\n"
    "请按系统提示的三段式要求完成研究，并只输出一个 JSON 对象。"
)


# ══════════════════════════════════════════════════════════════════════════
# 配置与开关（flag 门控起步）
# ══════════════════════════════════════════════════════════════════════════


def research_settings() -> DomainContextResearchSettings:
    """当前配置的研究层预算/开关（缺段 ⇒ 内置默认：``enabled=False``）。"""
    return get_settings().domain_context.research


def research_enabled(settings: DomainContextResearchSettings | None = None) -> bool:
    """Tier B 的 flag 门控（§14.2 决议 1：flag 门控起步，验证后再默认开）。

    ``enabled=False`` 时节点必须**严格 no-op**（不产 LLM 调用、``provenance="rule"``、
    行为与 D0 逐字段一致）——本函数只回答"开关是否打开"，不做任何副作用。
    """
    return (settings or research_settings()).enabled


# ══════════════════════════════════════════════════════════════════════════
# D1.5 触发条件
# ══════════════════════════════════════════════════════════════════════════


def should_research(
    inp: RouterInput,
    anchor_strength: str,
    dominant_share: float | None,
    track_drift: bool,
) -> tuple[bool, str]:
    """是否进 Tier B 模型研究（返回 ``(是否, 原因)``；原因进 ``research_meta["trigger"]``）。

    四个触发分支（按"稀疏程度"从高到低，首个命中即返回）：

    1. ``no_anchor``：无专用框架但有身份信号 ⇒ 线上绝大多数标的所处的稀疏区域；
    2. ``weak_anchor``：弱锚（仅 archetype 或宽 L1 行业命中）⇒ 框架可能是误配，需复核；
    3. ``multi_segment``：主力分部占比 < 50% ⇒ 单一框架不足以刻画（比亚迪式多主线）；
    4. ``identity_drift``：COMPANY_TRACK 有漂移信号 ⇒ 过渡态，旧框架可能已过期。

    全不命中则 ``(False, "")``：**强锚且结构清晰的公司跳过 Tier B**（保护既有 golden + 省成本）。
    ``enabled`` 开关与缓存命中不在这里判断（前者是调用方的门控，后者在
    :func:`run_driver_research` 内以 ``research_meta["cached"]`` 体现）。
    """
    if anchor_strength == "none" and inp.has_identity_signals():
        return True, "no_anchor"
    if anchor_strength == "weak":
        return True, "weak_anchor"
    if dominant_share is not None and dominant_share < _MULTI_SEGMENT_SHARE:
        return True, "multi_segment"
    if track_drift:
        return True, "identity_drift"
    return False, ""


# ══════════════════════════════════════════════════════════════════════════
# D1.6 输入包
# ══════════════════════════════════════════════════════════════════════════


def _segment_item(seg: Any) -> dict[str, Any]:
    """一条分部快照 → 输入包条目（只带研究用得上的口径，None 不写）。"""
    item: dict[str, Any] = {"segment_name": str(getattr(seg, "segment_name", "") or "")}
    for field_name in ("revenue_share", "revenue_yoy", "gross_margin", "revenue"):
        value = getattr(seg, field_name, None)
        if isinstance(value, int | float):
            item[field_name] = float(value)
    source = str(getattr(seg, "source", "") or "")
    if source:
        item["source"] = source
    return item


def _segment_blocks(track: CompanyTrackArtifact | None) -> list[dict[str, Any]]:
    """多期分部结构（按报告期倒序，最多 ``_MAX_SEGMENT_PERIODS`` 期）。

    业务动机：单期占比只能说"现在驱动是什么"，多期才能看出"驱动正在怎么变"
    （占比迁移、增速差、分部毛利率变化）——这正是 LLM 提出**标的特异**假设的关键材料。
    """
    if track is None or not track.segments:
        return []
    by_period: dict[str, list[dict[str, Any]]] = {}
    for seg in track.segments:
        by_period.setdefault(str(seg.report_date or ""), []).append(_segment_item(seg))
    blocks: list[dict[str, Any]] = []
    for report_date in sorted(by_period, reverse=True)[:_MAX_SEGMENT_PERIODS]:
        items = sorted(
            by_period[report_date],
            key=lambda item: float(item.get("revenue_share") or -1.0),
            reverse=True,
        )
        blocks.append({"report_date": report_date, "items": items[:_MAX_SEGMENTS_PER_PERIOD]})
    return blocks


def _key_financials(fact_values: Mapping[str, Any] | None) -> dict[str, float]:
    """关键财务项 + peer_*/industry_* 行业基准（canonical 键，缺失不写）。"""
    values = fact_values or {}
    picked: dict[str, float] = {}
    for name in _KEY_FACT_FIELDS:
        value = values.get(name)
        if isinstance(value, int | float):
            picked[name] = float(value)
    for name, value in values.items():
        if isinstance(value, int | float) and str(name).startswith(_BENCHMARK_PREFIXES):
            picked[str(name)] = float(value)
    return picked


def _anchor_primitives(profile: DriverProfile | None) -> list[dict[str, Any]]:
    """Tier A 激活原语的先验知识（``key_variables`` / ``causal_paths`` / ``disconfirming_signals``）。

    这三块 YAML 内容此前**从未被任何消费方读取**（G-7 的另一面）：LLM 拿到它们才知道
    "这个框架认为哪些变量关键、因果链长什么样、什么信号会证伪"，从而写出的假设能与框架对齐
    （C4 对齐目标），而不是凭空发挥。
    """
    if profile is None:
        return []
    return [
        {
            "id": prim.id,
            "key_variables": list(prim.key_variables),
            "causal_paths": list(prim.causal_paths),
            "disconfirming_signals": list(prim.disconfirming_signals),
        }
        for prim in profile.activated_primitives
    ]


def _load_report_fragments(symbol: str, max_chars: int) -> tuple[list[str], dict[str, Any]]:
    """本地年报 MD&A 片段（fail-open：无本地报告 ⇒ 空片段 + meta.note，不编造）。"""
    try:
        from alphabee.company_track.peer_report import fetch_local_report_fragments

        fragments, meta = fetch_local_report_fragments(symbol, max_chars=max_chars)
    except Exception as exc:  # noqa: BLE001 —— 一手材料属增强，失败不能让研究层崩
        return [], {"note": f"本地财报片段不可用: {type(exc).__name__}", "error": str(exc)}
    return list(fragments), dict(meta)


def build_research_input(
    symbol: str,
    router_input: RouterInput,
    *,
    track: CompanyTrackArtifact | None = None,
    fact_values: Mapping[str, Any] | None = None,
    profile: DriverProfile | None = None,
    report_fragments: Sequence[str] | None = None,
    max_report_chars: int = MAX_REPORT_FRAGMENT_CHARS,
) -> dict[str, Any]:
    """组装 Tier B 的输入包（§7.2：把材料"给到"模型，而不是让模型自己去翻）。

    组成：公司身份 / **多期分部结构**（最关键）/ ``fact_values`` 关键项 + ``peer_*`` 基准 /
    Tier A 激活原语（``key_variables`` + ``causal_paths`` + ``disconfirming_signals``，
    **首次真正被消费**）/ ``COMPANY_TRACK.review_notes``（漂移线索）/ 本地年报 MD&A 片段
    （``fetch_local_report_fragments``，上限 ``max_report_chars`` 字符）/ **可用 playbook 目录**
    （P1-3：``available_playbooks`` = id + description 截断 160 字，``candidates`` 的合法取值域）。

    ``report_fragments`` 显式给出时不读盘（测试与离线回放用）。
    """
    segments = _segment_blocks(track)
    latest_items: list[dict[str, Any]] = segments[0]["items"] if segments else []
    if report_fragments is None:
        fragments, report_meta = _load_report_fragments(symbol, max_report_chars)
    else:
        fragments, report_meta = list(report_fragments), {"note": "injected"}

    joined = "\n\n".join(fragment for fragment in fragments if fragment)
    joined = joined[:max_report_chars]

    return {
        "symbol": symbol,
        "identity": {
            "track_label": router_input.track_label,
            "sw_industry": router_input.industry,
            "sub_industry": router_input.sub_industry,
            "business_model": router_input.business_model,
            "sw_code": router_input.sw_code,
            "playbook": profile.playbook if profile else "",
            "anchor_strength": profile.anchor_strength if profile else "",
        },
        "segments": segments,
        "latest_report_date": segments[0]["report_date"] if segments else "",
        "latest_segments": latest_items,
        "financials": _key_financials(fact_values),
        "anchor_primitives": _anchor_primitives(profile),
        "review_notes": list(track.review_notes) if track else [],
        "report_fragments": joined,
        "report_meta": report_meta,
        # P1-3：把**目录内可用的 playbook id** 显式给到模型——否则模型只能凭记忆编框架名
        # （药明康德 run 的 candidates 混进目录里不存在的 id 正是这么来的），收敛层的闭包
        # 校验就只能事后踢人而无法事前约束。
        "available_playbooks": [
            {"id": playbook_id, "description": (playbook.description or "")[:160]}
            for playbook_id, playbook in sorted(load_playbooks().items())
        ],
        "notes": [
            "数字只能来自 tushare / 年报工具或本输入包的结构化事实；搜索结果不得当财务数据使用。",
            "分部结构必须与盈利公式自洽；矛盾必须写进 research_agenda。",
            "candidates 只能引用 available_playbooks 中的 id；新框架提案走 proposed_frameworks。",
        ],
    }


# ══════════════════════════════════════════════════════════════════════════
# D1.8 缓存 / 预算 / 超时 / 重试
# ══════════════════════════════════════════════════════════════════════════


class DriverResearchError(RuntimeError):
    """Tier B 不可恢复失败（超时 / agent 异常 / 解析彻底失败）⇒ 调用方走降级阶梯。

    ``tier`` 是建议的恢复档位（Tier 2 = 回 Tier A + D0 结构性兜底；Tier 3 = 最小骨架），
    ``truncated`` 标记"因预算耗尽而收口"（进 ``research_meta["truncated"]``）。
    """

    def __init__(self, reason: str, *, tier: int = 2, truncated: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tier = int(tier)
        self.truncated = bool(truncated)


class DriverResearchResult(BaseModel):
    """Tier B 的返回值：LLM 原始输出 + 研究元信息（缓存/预算/截断/重试）。"""

    output: DriverResearchOutput = Field(default_factory=DriverResearchOutput)
    meta: dict[str, Any] = Field(default_factory=dict)


def research_cache_key(symbol: str, report_period: str, track_label: str, playbook_version: int) -> str:
    """缓存 key = ``symbol + 最新报告期 + track_label + playbook_version``（§14.2 决议 5）。

    失效条件即 key 的组成：新报告期 / track 漂移 / playbook 版本升级。行情无关的驱动假设
    在报告期内是稳定的，所以缓存能显著摊薄 LLM 成本。
    """
    return "|".join(
        [
            (symbol or "").strip(),
            (report_period or "").strip(),
            (track_label or "").strip(),
            str(int(playbook_version)),
        ]
    )


def _cache_root(cache_dir: str | Path | None) -> Path:
    raw = Path(cache_dir) if cache_dir is not None else Path(research_settings().cache_dir)
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def _cache_file(cache_dir: str | Path | None, symbol: str) -> Path:
    return _cache_root(cache_dir) / f"{normalize_symbol(symbol)}.json"


def _is_expired(created_at: str, ttl_days: int) -> bool:
    """缓存是否过期（时间戳不可解析 ⇒ 视为过期，宁可重算不用脏数据）。"""
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created > timedelta(days=max(0, int(ttl_days)))


def load_cached_research(
    cache_dir: str | Path | None,
    symbol: str,
    *,
    cache_key: str,
    ttl_days: int | None = None,
) -> DriverResearchResult | None:
    """读缓存（key 不符 / 过期 / 结构损坏 ⇒ ``None`` = 未命中，绝不抛穿）。"""
    path = _cache_file(cache_dir, symbol)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("cache_key") != cache_key:
        return None
    ttl = research_settings().cache_ttl_days if ttl_days is None else ttl_days
    if _is_expired(str(raw.get("created_at") or ""), ttl):
        return None
    try:
        output = DriverResearchOutput.model_validate(raw.get("output") or {})
    except ValidationError:
        return None
    return DriverResearchResult(
        output=output,
        meta={
            "cached": True,
            "cache_key": cache_key,
            "cache_path": str(path),
            "created_at": str(raw.get("created_at") or ""),
            "tier": 0,
            "truncated": False,
            "tool_calls": 0,
            "retry_count": 0,
            "elapsed_ms": 0,
        },
    )


def save_cached_research(
    cache_dir: str | Path | None,
    symbol: str,
    *,
    cache_key: str,
    output: DriverResearchOutput,
    track_label: str = "",
    report_period: str = "",
    playbook_version: int = 1,
) -> Path | None:
    """原子写缓存（fail-open：写失败只 warning，绝不打断 run）。"""
    path = _cache_file(cache_dir, symbol)
    payload = {
        "cache_key": cache_key,
        "symbol": symbol,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "track_label": track_label,
        "report_period": report_period,
        "playbook_version": int(playbook_version),
        "output": output.model_dump(mode="json"),
    }
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("driver_profile research cache write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return path


def _text_blocks(content: Any) -> str:
    """只取 ``text`` 块的内容（``thinking`` 块刻意丢弃，见 :func:`extract_research_text`）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if isinstance(block, Mapping) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


def extract_research_text(result: Mapping[str, Any]) -> str:
    """取最后一条消息的**纯文本**内容。

    刻意**不复用** ``_extract_final_text``（其底层 ``utils.pipeline.extract_text`` 是宽口径，
    会把 ``{"type": "thinking"}`` 块拼进正文）：推理模型下思考文本会污染 raw_text、
    直接降低 ``parse_json`` 成功率（§7.7-6）。
    """
    messages = result.get("messages")
    if not isinstance(messages, Sequence) or not messages:
        return ""
    last = messages[-1]
    content = last.get("content") if isinstance(last, Mapping) else getattr(last, "content", "")
    return _text_blocks(content).strip()


def count_tool_calls(result: Mapping[str, Any]) -> int:
    """统计本轮消息里的工具调用次数（每个 ``ToolMessage`` 对应一次工具返回）。"""
    messages = result.get("messages")
    if not isinstance(messages, Sequence):
        return 0
    total = 0
    for message in messages:
        kind = message.get("type") if isinstance(message, Mapping) else getattr(message, "type", "")
        if kind == "tool":
            total += 1
    return total


def _build_research_agent() -> Any:
    """构造研究代理（延迟 import：重依赖只在真正要跑 LLM 时加载）。"""
    from alphabee.agents.driver_profile.agent import driver_profile_research_agent_factory

    return driver_profile_research_agent_factory()


async def _invoke_agent(
    agent: Any,
    messages: Sequence[Any],
    *,
    recursion_limit: int,
    wall_timeout_s: float | None,
) -> Mapping[str, Any]:
    """单次调用：显式 ``recursion_limit`` + ``asyncio.wait_for`` 墙钟超时。

    ``wall_timeout_s`` 是**本次 attempt 的剩余预算**（由 :func:`run_driver_research`
    的单一 deadline 推出；``None`` = 未配置预算，不设超时）——重试不得另起一份预算。

    超时/异常一律包成 :class:`DriverResearchError`（不把底层异常类型泄漏给节点，
    节点只按"研究失败 ⇒ 降级"处理）。
    """
    timeout: float | None = float(wall_timeout_s) if wall_timeout_s is not None and wall_timeout_s > 0 else None
    try:
        result = await asyncio.wait_for(
            agent.ainvoke({"messages": list(messages)}, config={"recursion_limit": int(recursion_limit)}),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise DriverResearchError(f"wall_timeout:{wall_timeout_s}s", tier=2, truncated=True) from exc
    except Exception as exc:  # noqa: BLE001 —— 任何调用失败都走降级，不抛穿 run
        raise DriverResearchError(f"agent_failure:{type(exc).__name__}: {exc}", tier=2) from exc
    if not isinstance(result, Mapping):
        raise DriverResearchError("agent_returned_non_mapping", tier=2)
    return result


def _remaining_seconds(deadline: float | None) -> float | None:
    """距离单标的墙钟 deadline 的剩余秒数（未配置 deadline ⇒ ``None`` = 不限时）。"""
    if deadline is None:
        return None
    return deadline - time.monotonic()


async def run_driver_research(
    symbol: str,
    research_input: Mapping[str, Any],
    *,
    settings: DomainContextResearchSettings | None = None,
    cache_dir: str | Path | None = None,
    track_label: str = "",
    report_period: str = "",
    playbook_version: int = 1,
    trigger: str = "",
    use_cache: bool = True,
    agent_factory: Callable[[], Any] | None = None,
) -> DriverResearchResult:
    """跑一次 Tier B 研究（缓存 → LLM → 解析 → 预算收口）。

    流程与硬约束（§7.6/§7.7）：

    1. **缓存**：key = ``symbol + 最新报告期 + track_label + playbook_version``；
       命中直接返回（``research_meta["cached"]=True``），**不构造 agent、不调 LLM**；
    2. **超时（覆盖重试全程）**：循环前算**一次** ``deadline = time.monotonic() + wall_timeout_s``，
       每个 attempt 只把**剩余时间**交给 :func:`_invoke_agent`；剩余 ≤ 0 时不再 re-ask，
       直接抛 :class:`DriverResearchError`（``tier=2``，``truncated=True``）走降级链
       —— 否则重试等于凭空多出一份墙钟预算，单标的耗时上界形同虚设；
    3. **工具预算（累计）**：``tool_calls += count_tool_calls(raw)`` 跨 attempt 累加，
       累计值超过 ``max_tool_calls`` ⇒ ``truncated=True`` + LOW issue，**无论本 attempt
       是否解析成功都不落缓存**（截断产物缓存 90 天会把噪声固化进后续 run），并停止 re-ask；
    4. **重试一次**：解析/校验失败时把错误回灌给模型 re-ask 一次（``ToolRetryMiddleware``
       只管工具、不管 LLM，故必须在这里自行实现并计入同一份预算）；
    5. **显式 recursion_limit**：从配置读（默认 40），不依赖 LangGraph 默认值。

    ``agent_factory`` 是唯一注入缝（默认 ``driver_profile_research_agent_factory``）：
    测试与离线回放据此替换 agent，无需拉起重依赖，也便于断言"缓存命中时工厂未被调用"。

    Raises:
        DriverResearchError: 超时（含重试前预算已尽）/ agent 异常 / 重试后仍无法解析出可用输出。
    """
    cfg = settings or research_settings()
    key = research_cache_key(symbol, report_period, track_label, playbook_version)
    if use_cache:
        cached = load_cached_research(cache_dir, symbol, cache_key=key, ttl_days=cfg.cache_ttl_days)
        if cached is not None:
            return cached

    started = time.monotonic()
    # 单标的墙钟预算只算一次：重试是"在同一份预算内再问一次"，不是新开一份预算。
    deadline = started + float(cfg.wall_timeout_s) if cfg.wall_timeout_s and cfg.wall_timeout_s > 0 else None
    issues: list[dict[str, Any]] = []
    user_text = _USER_MESSAGE_TEMPLATE.format(
        research_input_json=json.dumps(research_input, ensure_ascii=False, indent=2)
    )
    messages: list[Any] = [_human_message(user_text)]
    agent = (agent_factory or _build_research_agent)()

    retry_count = 0
    tool_calls = 0
    truncated = False
    last_error = "unknown"
    tier = 0
    for attempt in range(1, _MAX_LLM_ATTEMPTS + 1):
        remaining = _remaining_seconds(deadline)
        if remaining is not None and remaining <= 0:
            raise DriverResearchError(
                f"wall_timeout:{cfg.wall_timeout_s}s:budget_exhausted_before_attempt{attempt}",
                tier=2,
                truncated=True,
            )
        raw = await _invoke_agent(
            agent,
            messages,
            recursion_limit=cfg.recursion_limit,
            wall_timeout_s=remaining,
        )
        # 累计计数（不是取 max）：两次 attempt 各 15 次调用 = 30 次，已超出 20 的预算。
        tool_calls += count_tool_calls(raw)
        if tool_calls > int(cfg.max_tool_calls) and not truncated:
            truncated = True
            issues.append(
                {
                    "severity": "low",
                    "category": "driver_profile_research_truncated",
                    "message": (f"驱动研究累计工具调用 {tool_calls} 次超过预算 {cfg.max_tool_calls}，按已有输出收口"),
                }
            )
        text = extract_research_text(raw)
        output, tier, last_error = parse_research_output(text)
        if output is not None:
            meta: dict[str, Any] = {
                "cached": False,
                "cache_key": key,
                "trigger": trigger,
                "tier": tier,
                "tool_calls": tool_calls,
                "truncated": truncated,
                "retry_count": retry_count,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "model_attempts": attempt,
                "issues": issues,
            }
            result = DriverResearchResult(output=output, meta=meta)
            # 只缓存"完整且未截断"的结果：半截输出缓存 90 天会把噪声固化进后续 run。
            if use_cache and tier == 0 and not truncated:
                save_cached_research(
                    cache_dir,
                    symbol,
                    cache_key=key,
                    output=output,
                    track_label=track_label,
                    report_period=report_period,
                    playbook_version=playbook_version,
                )
            return result

        if truncated:
            # 预算已耗尽：不再 re-ask（重试会继续烧工具调用），直接降级。
            break
        if attempt < _MAX_LLM_ATTEMPTS:
            retry_count += 1
            messages = [
                *messages,
                _human_message(_RETRY_INSTRUCTION.format(error=last_error)),
            ]

    raise DriverResearchError(f"parse_failed:{last_error}", tier=2, truncated=truncated)


def _human_message(content: str) -> Any:
    """构造用户消息（延迟 import，避免 domain_context 在 import 期拉起 langchain 消息栈）。"""
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=content)


# ══════════════════════════════════════════════════════════════════════════
# Tier B 输出解析（Tier 0 严格 / Tier 1 宽松）
# ══════════════════════════════════════════════════════════════════════════


def _coerce_items[M: BaseModel](items: Any, model: type[M]) -> tuple[list[M], int]:
    """逐条校验：合法条目保留，非法条目丢弃并计数（C1"单条丢弃"的确定性实现）。"""
    kept: list[M] = []
    dropped = 0
    if not isinstance(items, list):
        return kept, dropped
    for item in items:
        try:
            kept.append(model.model_validate(item))
        except ValidationError:
            dropped += 1
    return kept, dropped


def _coerce_str_list(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def salvage_research_output(parsed: Any) -> tuple[DriverResearchOutput, int] | None:
    """Tier 1 宽松救援：只修结构、不补内容——丢弃非法单条假设/议程，保留合法条目。

    返回 ``(output, 丢弃条数)``；当"有内容的条目全被丢弃且没有任何其他字段"时返回 ``None``
    （= 救援失败，调用方走 Tier 2/3 降级链），避免把空壳当成研究结论。
    """
    if not isinstance(parsed, Mapping):
        return None
    hypotheses, dropped_h = _coerce_items(parsed.get("driver_hypotheses"), ResearchHypothesis)
    agenda, dropped_a = _coerce_items(parsed.get("research_agenda"), ResearchAgendaItem)
    output = DriverResearchOutput(
        profit_formula=str(parsed.get("profit_formula") or ""),
        driver_hypotheses=hypotheses,
        research_agenda=agenda,
        candidates=_coerce_str_list(parsed.get("candidates")),
        proposed_frameworks=_coerce_str_list(parsed.get("proposed_frameworks")),
        novel_concepts=_coerce_str_list(parsed.get("novel_concepts")),
        open_questions=_coerce_str_list(parsed.get("open_questions")),
    )
    has_raw_items = bool(parsed.get("driver_hypotheses")) or bool(parsed.get("research_agenda"))
    if has_raw_items and not hypotheses and not agenda:
        return None
    if not any(
        (
            hypotheses,
            agenda,
            output.profit_formula,
            output.candidates,
            output.proposed_frameworks,
            output.novel_concepts,
            output.open_questions,
        )
    ):
        return None
    return output, dropped_h + dropped_a


def parse_research_output(raw_text: str) -> tuple[DriverResearchOutput | None, int, str]:
    """解析 LLM 原始输出：Tier 0 严格 → Tier 1 宽松。

    Returns:
        ``(output, tier, error)``：成功时 ``tier`` 为 0（严格）或 1（宽松救援）；
        失败时 ``output=None``、``error`` 为回灌给模型的原因。
    """
    if not raw_text.strip():
        return None, 2, "empty_response"
    try:
        parsed = parse_json(raw_text)
    except Exception as exc:  # noqa: BLE001 —— parse_json 的失败形态不稳定，统一收敛成原因串
        return None, 2, f"json_parse_failed: {exc}"
    try:
        return DriverResearchOutput.model_validate(parsed), 0, ""
    except ValidationError as exc:
        rescued = salvage_research_output(parsed)
        if rescued is None:
            return None, 2, f"schema_invalid: {exc}"
        output, dropped = rescued
        return output, 1, f"schema_invalid_salvaged:{dropped}"


# ══════════════════════════════════════════════════════════════════════════
# Tier C：确定性收敛（C1–C6）
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class _Candidate:
    """收敛过程中的一条候选假设（带对齐结果，仍在变化中）。"""

    hypothesis: ResearchHypothesis
    matched_primitive: str = ""
    novel: bool = False


def _dedup(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = (item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _contains(a: str, b: str) -> bool:
    """双向子串匹配（大小写不敏感）：驱动变量的叫法口径常不一致（"猪价" vs "生猪价格"）。"""
    left, right = (a or "").strip().lower(), (b or "").strip().lower()
    if not left or not right:
        return False
    return left in right or right in left


@dataclass
class _ShareClaim:
    """一条「X 亿 + 占<base>Y%」配对声明：隐含总量 = X / Y × 100。"""

    amount_yi: float | None  # 分子（亿元）；同句找不到分子 ⇒ None（不参与校验）
    pct: float  # 占比（%）
    base: str  # 归一化基数名（"集团"/"化学业务"…）
    period: str | None  # 单一期别标签；None = 一句含多个期别（歧义，跳过）
    hypothesis_index: int  # 归属假设序号（降权定位）
    snippet: str  # 所在句（issue 展示）


def _period_tags(text: str) -> set[str]:
    """提取文本中的期别标签，归一为 ``{year}{H1|Q1|Q3|FY}`` 集合。

    顺序敏感：先判半年报（"2026年半年度报告" 含 "年度" 子串）、再判季度、其余归全年；
    ``end_date`` 形态（``20260630``）按月份映射，且其区间**遮蔽**年份扫描，避免
    "2026" 被同时归成 FY 与 H1 两个标签。
    """
    text = text or ""
    masked: list[tuple[int, int]] = []
    tags: set[str] = set()
    for match in _END_DATE_RE.finditer(text):
        masked.append(match.span())
        year, month = match.group(1), int(match.group(2))
        if month <= 3:
            tags.add(f"{year}Q1")
        elif month <= 6:
            tags.add(f"{year}H1")
        elif month <= 9:
            tags.add(f"{year}Q3")
        else:
            tags.add(f"{year}FY")
    for match in _YEAR_RE.finditer(text):
        if any(start <= match.start() < end for start, end in masked):
            continue
        year = match.group(1)
        window = text[max(0, match.start() - 8) : match.end() + 12]
        if re.search(r"半年度|上半年|半年报|中期|H1", window):
            tags.add(f"{year}H1")
        elif re.search(r"第一季度|一季度|Q1", window):
            tags.add(f"{year}Q1")
        elif re.search(r"第三季度|三季度|Q3", window):
            tags.add(f"{year}Q3")
        else:
            tags.add(f"{year}FY")
    return tags


def _period_mix_sentences(hypotheses: Sequence[ResearchHypothesis]) -> list[str]:
    """P2-6 辅助检测：同一句内出现 ≥2 个期别标签且 ≥2 处数字声明 ⇒ 疑似期别混用。

    只查同句（确定性可判）；跨句的期别对照是正常的趋势讨论，不做机器裁决——
    主要防线在 prompt 的标注条款（药明康德 case 里模型在 agenda 中已展示它知道期别，
    只是 company_form 没写清：FY2025 的 51.89% 与 H1 同比 +7.48pp 无标注并列、
    年化 ROE 27.69% 与半年 ROE 13.59% 相邻放置）。

    判据刻意与 P0-1A/P0-1B 的「一句多期别 ⇒ 跳过校验」互补：那边为避免误报而放弃，
    这边只留一条 LOW 留痕，把"期别没写清"这件事显式交给下游裁决。
    """
    hits: list[str] = []
    for hypothesis in hypotheses:
        for sentence in _SENTENCE_SPLIT_RE.split(hypothesis.company_form or ""):
            periods = sorted(_period_tags(sentence))
            numbers = list(_NUMBER_RE.finditer(sentence))
            if len(periods) >= 2 and len(numbers) >= 2:
                hits.append(f"「{hypothesis.variable}」：{sentence.strip()[:120]}")
    return hits


def _extract_share_claims(text: str, hypothesis_index: int) -> list[_ShareClaim]:
    """从 company_form 抽取「X 亿 + 占<base>P%」配对（P0-1A 的分子分母素材）。

    配对规则：同一句内，「占…%」向左找最近的「X 亿」，没有再向右找最近的；
    无分子 ⇒ 该声明不参与隐含总量校验（交由 P0-1B 出处检查兜底）。
    一句含多个期别 ⇒ 整句声明 ``period=None``（归属歧义，不参与跨句比较）。
    """
    claims: list[_ShareClaim] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        sentence_periods = sorted(_period_tags(sentence))
        if len(sentence_periods) > 1:
            continue  # 期别归属歧义：宁可漏检不可误报
        period = sentence_periods[0] if sentence_periods else ""
        for zhan in _ZHAN_RE.finditer(sentence):
            base = (zhan.group("base") or "").strip()
            if base in _ZHAN_BASE_STOP:
                continue
            amount: float | None = None
            for match in _AMOUNT_YI_RE.finditer(sentence):
                if match.end() <= zhan.start():
                    amount = float(match.group("value"))
                else:
                    break
            if amount is None:
                for match in _AMOUNT_YI_RE.finditer(sentence):
                    if match.start() >= zhan.end():
                        amount = float(match.group("value"))
                        break
            claims.append(
                _ShareClaim(
                    amount_yi=amount,
                    pct=float(zhan.group("pct")),
                    base=base,
                    period=period,
                    hypothesis_index=hypothesis_index,
                    snippet=sentence.strip()[:120],
                )
            )
    return claims


def _implied_total_contradictions(
    hypotheses: Sequence[ResearchHypothesis],
) -> list[dict[str, Any]]:
    """P0-1A：同一 (基数, 期别) 下「X 亿 + 占Y%」的隐含总量必须自洽。

    药明康德真实案例：H3「美国客户收入 140.3 亿、占集团收入 67.45%」⇒ 隐含集团 ≈ 208 亿；
    H1「化学业务收入 249.9 亿（占集团 86.46%）」⇒ 隐含集团 ≈ 289 亿——同一基数互相矛盾。
    本检查只**发现并降权**（无法判定孰对孰错），不替下游裁决。
    """
    groups: dict[tuple[str, str], list[_ShareClaim]] = {}
    for index, hypothesis in enumerate(hypotheses):
        for claim in _extract_share_claims(hypothesis.company_form, index):
            if claim.amount_yi and claim.amount_yi > 0 and claim.pct > 0:
                groups.setdefault((claim.base, claim.period or ""), []).append(claim)

    findings: list[dict[str, Any]] = []
    for (base, period), claims in sorted(groups.items()):
        if len(claims) < 2:
            continue
        # 入组时已按 truthy 过滤（amount_yi 非 None），这里的 `is not None` 只为 mypy 收窄
        # float | None；两种过滤下实际参与计算的集合相同，运行语义不变。
        totals = [claim.amount_yi / claim.pct * 100.0 for claim in claims if claim.amount_yi is not None]
        lo, hi = min(totals), max(totals)
        if lo <= 0 or (hi - lo) / lo <= _IMPLIED_TOTAL_TOLERANCE:
            continue
        findings.append(
            {
                "base": base,
                "period": period or "期别未标注",
                "implied_totals": [round(value, 1) for value in totals],
                "hypothesis_indices": sorted({claim.hypothesis_index for claim in claims}),
                "snippets": [claim.snippet for claim in claims],
            }
        )
    return findings


@dataclass
class _NumericClaim:
    """company_form 里的一条财务类数字声明（P0-1B 的声明单元）。"""

    value: str  # 原样数值串（issue 展示）
    unit: str
    period: str  # 单一期别标签；"" = 未标注（同值即算有出处）
    in_share_clause: bool  # 该数字位于「占…%」从句内部 ⇒ 无出处时升级为降权
    snippet: str


def _normalize_number(text: str) -> str:
    """归一化数值串（去尾零）："140.3" 与 "140.30" 视为同值。"""
    value = (text or "").strip()
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value or "0"


def _input_pack_numbers(research_input: Mapping[str, Any] | None) -> dict[str, list[float]]:
    """输入包中的权威数值（P0-1B 出处兜底：结构化事实与证据同权）。

    ``pct`` 桶：financials 全部值 + 分部 revenue_share / gross_margin；
    ``amount_yi`` 桶：分部 revenue（元）折算亿元。
    """
    out: dict[str, list[float]] = {"pct": [], "amount_yi": []}
    if not research_input:
        return out
    financials = research_input.get("financials")
    if isinstance(financials, Mapping):
        for value in financials.values():
            if isinstance(value, int | float):
                out["pct"].append(float(value))
    items = research_input.get("latest_segments")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            for key in ("revenue_share", "gross_margin"):
                value = item.get(key)
                if isinstance(value, int | float):
                    out["pct"].append(float(value))
            revenue = item.get("revenue")
            if isinstance(revenue, int | float) and revenue > 0:
                out["amount_yi"].append(float(revenue) / 1e8)
    return out


def _uncovered_claims(
    hypothesis: ResearchHypothesis,
    research_input: Mapping[str, Any] | None,
) -> list[_NumericClaim]:
    """P0-1B：company_form 的财务类数字声明必须有出处。

    出处判定（按序，任一命中即视为覆盖）：
    1. **证据同值同期**：数值出现在任一 evidence 的 quote/ref 中，且声明期别
       ∈ 证据期别集（未标注期别的声明只看同值）；
    2. **输入包同值**：数值与输入包权威值之差在容差内（期别无关：输入包即结构化事实）。
    规则：运营单位（倍/kL/个）不查；一句含多个期别 ⇒ 整句跳过；数字 0 跳过。
    """
    evidence_periods: set[str] = set()
    evidence_numbers: set[str] = set()
    for item in hypothesis.evidence:
        evidence_periods |= _period_tags(item.ref)
        evidence_periods |= _period_tags(item.quote)
        # 裸数值抽取：tushare ref（"netprofit_margin=42.53"）与 quote 中的数字都收进来；
        # 顺带收进的 symbol/end_date 等无害串不会与任何财务声明同值。
        for match in _RAW_NUMBER_RE.finditer(f"{item.ref} {item.quote}"):
            evidence_numbers.add(_normalize_number(match.group("value")))

    input_numbers = _input_pack_numbers(research_input)
    uncovered: list[_NumericClaim] = []
    for sentence in _SENTENCE_SPLIT_RE.split(hypothesis.company_form or ""):
        sentence_periods = sorted(_period_tags(sentence))
        if len(sentence_periods) > 1:
            continue  # 期别归属歧义：宁可漏检不可误报
        period = sentence_periods[0] if sentence_periods else ""
        zhan_spans = [match.span() for match in _ZHAN_RE.finditer(sentence)]
        for match in _NUMBER_RE.finditer(sentence):
            unit = match.group("unit")
            if unit in _IGNORED_UNITS:
                continue
            normalized = _normalize_number(match.group("value"))
            if normalized == "0":
                continue
            # ① 证据同值同期
            if normalized in evidence_numbers and (not period or period in evidence_periods):
                continue
            # ② 输入包同值
            numeric = float(match.group("value"))
            if unit in {"%", "％"}:
                if any(abs(numeric - ref) <= _INPUT_PCT_TOLERANCE_PP for ref in input_numbers["pct"]):
                    continue
            elif unit.startswith("亿"):
                if any(
                    ref > 0 and abs(numeric - ref) / ref <= _INPUT_AMOUNT_TOLERANCE
                    for ref in input_numbers["amount_yi"]
                ):
                    continue
            elif unit.startswith("万"):
                amount_yi = numeric / 1e4
                if any(
                    ref > 0 and abs(amount_yi - ref) / ref <= _INPUT_AMOUNT_TOLERANCE
                    for ref in input_numbers["amount_yi"]
                ):
                    continue
            uncovered.append(
                _NumericClaim(
                    value=match.group("value"),
                    unit=unit,
                    period=period,
                    in_share_clause=any(start <= match.start() < end for start, end in zhan_spans),
                    snippet=sentence.strip()[:120],
                )
            )
    return uncovered


def _segment_shares(research_input: Mapping[str, Any] | None) -> dict[str, float]:
    """最新报告期的分部占比（C3 事实交叉校验的基准；无结构事实 ⇒ 空表 = 不校验）。"""
    if not research_input:
        return {}
    items = research_input.get("latest_segments")
    if not isinstance(items, list):
        return {}
    shares: dict[str, float] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("segment_name") or "").strip()
        share = item.get("revenue_share")
        if name and isinstance(share, int | float):
            shares[name] = float(share)
    return shares


def _financial_facts(research_input: Mapping[str, Any] | None) -> dict[str, float]:
    if not research_input:
        return {}
    financials = research_input.get("financials")
    if not isinstance(financials, Mapping):
        return {}
    return {str(key): float(value) for key, value in financials.items() if isinstance(value, int | float)}


def _share_contradictions(texts: Sequence[str], shares: Mapping[str, float]) -> list[str]:
    """C3：假设里声称的分部占比与实测占比相差超过容差 ⇒ 记为矛盾。"""
    conflicts: list[str] = []
    for text in texts:
        for match in _SHARE_RE.finditer(text or ""):
            claimed = float(match.group("value"))
            name = match.group("name").strip()
            for actual_name, actual in shares.items():
                if not _contains(name, actual_name):
                    continue
                if abs(claimed - actual) > _SHARE_TOLERANCE_PP:
                    conflicts.append(f"{actual_name}: 假设声称占比 {claimed:.0f}% vs 实测 {actual:.0f}%")
                break
    return conflicts


def _segment_margins(research_input: Mapping[str, Any] | None) -> dict[str, float]:
    """最新报告期分部毛利率（P0-2：分部口径校验的基准；无 ⇒ 空表 = 不校验分部口径）。"""
    if not research_input:
        return {}
    items = research_input.get("latest_segments")
    if not isinstance(items, list):
        return {}
    margins: dict[str, float] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("segment_name") or "").strip()
        margin = item.get("gross_margin")
        if name and isinstance(margin, int | float):
            margins[name] = float(margin)
    return margins


def _without_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """挖掉给定区间后的残余文本（具名毛利率已被口径分流，无名检查只看残余）。"""
    out: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _is_business_name(name: str) -> bool:
    """残余名是否可能是业务名（P0-2 修复三轮的兜底规则）。

    仅由期别量词字符（年|月|季|报|度|期）构成，或长度 < 2 ⇒ **不是**业务名：
    「2025年报」剥成年「报」、「半年报」剥成「报」都不该被当成具名分部去匹配
    ``segment_margins``（匹配不上就会把整句静默吞掉）。
    """
    text = (name or "").strip()
    if len(text) < _MIN_BUSINESS_NAME_LEN:
        return False
    return any(char not in _PERIOD_QUANTIFIER_CHARS for char in text)


def _strip_period_tokens(name: str) -> str:
    """剥离毛利率前缀名里的期别/语境 token，返回**业务名**（无业务名 ⇒ ``""``）。

    「2025年」/「2026H1」/「1-9月」/「半年度」/「前三季度」/「上年同期」/「2025年报」
    都是期别口径，不是业务名：它们充当 name 时会被当成"具名分部"，既吞掉 named span
    又在 ``segment_margins`` 里匹配不上 ⇒ 静默漏检（既不比分部也不比集团）。
    剥空或残余只剩期别量词字符（≥ :func:`_is_business_name`）⇒ 归零，调用方交回
    残余文本按「无名/集团口径」与集团值比较（保留 C3 闸门）。
    期别 token 与业务名粘连时（「2026H1化学业务毛利率」）只剥前缀，业务名照常做同名比对。
    """
    text = (name or "").strip()
    changed = True
    while changed and text:
        changed = False
        stripped = _PERIOD_TOKEN_RE.sub("", text).strip()
        if stripped != text:
            text, changed = stripped, True
        for token in _PERIOD_CONTEXT_TOKENS:
            if text.startswith(token):
                text, changed = text[len(token) :].strip(), True
    return text if _is_business_name(text) else ""


def _margin_contradictions(
    texts: Sequence[str],
    financials: Mapping[str, float],
    segment_margins: Mapping[str, float] | None = None,
) -> list[str]:
    """C3（P0-2 修复）：毛利率**口径感知**校验——分部值与集团值不再混比。

    - 「<名>业务毛利率 P%」：只与**同名分部**的毛利率基准比；无同名分部基准 ⇒ 跳过
      （不比较 ≠ 不矛盾；旧实现拿分部值比集团值，是药明康德 FX 假设误报的根源）；
    - 名字里只有期别/语境 token（「2025年毛利率30%」「半年度毛利率」）⇒ **不算具名**：
      不吞 named span，回到残余文本按「无名/集团口径」与集团值比（否则整句被静默漏检）；
    - 「毛利率 P%」（无名）与集团口径（集团/公司/整体/当年…）：与 ``financials.gross_margin`` 比
      （保留原行为）。
    """
    segment_margins = segment_margins or {}
    actual_group = financials.get("gross_margin")
    conflicts: list[str] = []
    for text in texts:
        named_spans: list[tuple[int, int]] = []
        for match in _MARGIN_NAMED_RE.finditer(text or ""):
            suffix = (match.group("suffix") or "").strip()
            name = _strip_period_tokens(match.group("name"))
            if not name:
                continue  # 期别/语境 token 不是业务名：不记 span，交残余检查比集团值
            named_spans.append(match.span())
            claimed = float(match.group("value"))
            if any(_contains(name, scope) for scope in _GROUP_SCOPE_NAMES):
                if actual_group is not None and abs(claimed - actual_group) > _MARGIN_TOLERANCE_PP:
                    conflicts.append(f"集团毛利率: 假设声称 {claimed:.1f}% vs 实测 {actual_group:.1f}%")
                continue
            actual = None
            for segment_name, margin in segment_margins.items():
                if _contains(name, segment_name):
                    actual = margin
                    break
            if actual is None:
                continue  # 未知分部/无分部基准 ⇒ 跳过（不误报）
            if abs(claimed - actual) > _MARGIN_TOLERANCE_PP:
                conflicts.append(f"{name}{suffix}毛利率: 假设声称 {claimed:.1f}% vs 实测 {actual:.1f}%")
        if actual_group is None:
            continue
        residual = _without_spans(text or "", named_spans)
        for match in _MARGIN_RE.finditer(residual):
            claimed = float(match.group("value"))
            if abs(claimed - actual_group) > _MARGIN_TOLERANCE_PP:
                conflicts.append(f"毛利率: 假设声称 {claimed:.1f}% vs 实测 {actual_group:.1f}%")
    return conflicts


#: P1-5：对齐的最短共享连续子串（字符数；< 4 视为偶合不匹配，如"产能"/"成本"这类双字词）。
_MATCH_MIN_CHARS = 4


def _longest_shared_substring(a: str, b: str) -> str:
    """两串的**最长公共连续子串**（滑动窗口，输入均为短串，无需 DP 优化）。

    只回报长度 ≥ ``_MATCH_MIN_CHARS`` 的共享子串：更短的（"产能"/"成本"这类双字词）
    属于普遍存在的偶合，一律返回 ``""``（判据下界与函数返回面一致，调用方不必再判长度）。

    语义对齐的判据是「连续子串」：变量「持续经营业务在手订单余额及其同比」与
    key_variable「新签订单与在手订单」共享「在手订单」（4 字）⇒ 对齐；
    旧实现的整串双向子串（``_contains``）要求整个 key_variable 成为子串，导致 7/7 未命中。
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return ""
    if len(a) > len(b):
        a, b = b, a
    for length in range(len(a), _MATCH_MIN_CHARS - 1, -1):
        for start in range(len(a) - length + 1):
            chunk = a[start : start + length]
            if chunk in b:
                return chunk  # 从最长到最短，首个命中即最长
    return ""


def _match_primitive(variable: str, primitives: Mapping[str, Any]) -> tuple[Any | None, str, str]:
    """C4（P1-5 重写）：驱动变量 → primitive 语义对齐。

    返回 ``(primitive|None, 命中 key_variable, 共享子串)``：取**全候选池**内共享子串
    最长的命中；< ``_MATCH_MIN_CHARS`` 视为未对齐；同长按 primitive id 升序决平
    （迭代 ``sorted(primitives)``，仅严格更长才替换 ⇒ 天然确定性，不依赖 loader 目录序）。

    池内元素同构取 ``key_variables``：目录 ``PrimitiveSchema`` 与激活快照
    ``ActivatedPrimitive`` 该字段同名同义，故类型取 ``Any``（mypy strict 下由调用方保证）。
    """
    best: tuple[int, Any | None, str, str] = (-1, None, "", "")
    for primitive_id in sorted(primitives):
        primitive = primitives[primitive_id]
        for key in primitive.key_variables:
            shared = _longest_shared_substring(variable, key)
            if len(shared) >= _MATCH_MIN_CHARS and len(shared) > best[0]:
                best = (len(shared), primitive, key, shared)
    return best[1], best[2], best[3]


def _enforce_primary_roles(items: list[_Candidate]) -> list[_Candidate]:
    """C5 角色约束：≥1 primary 且 primary ≤3（不足则提升最高置信条目，过多则降级尾部）。

    业务动机：``primary_drivers`` 是报告主线（``main_driver`` 来源），为空则老消费方读不到主线；
    primary 过多则"什么都重要"等于没有主线。
    """
    primaries = [index for index, item in enumerate(items) if item.hypothesis.role == "primary"]
    if not primaries and items:
        items[0].hypothesis = items[0].hypothesis.model_copy(update={"role": "primary"})
    elif len(primaries) > _MAX_PRIMARY:
        for index in primaries[_MAX_PRIMARY:]:
            items[index].hypothesis = items[index].hypothesis.model_copy(update={"role": "secondary"})
    return items


def _to_observable(text: str) -> DriverObservable:
    """字符串 observable → ``DriverObservable``（解析「指标名（来源：X）」这一约定写法）。"""
    raw = (text or "").strip()
    match = _OBSERVABLE_SOURCE_RE.match(raw)
    if match:
        return DriverObservable(name=match.group("name").strip(), source=match.group("source").strip())
    return DriverObservable(name=raw)


def _to_driver_hypothesis(candidate: _Candidate) -> DriverHypothesis:
    hypothesis = candidate.hypothesis
    return DriverHypothesis(
        variable=hypothesis.variable.strip(),
        role=hypothesis.role or "primary",
        mechanism=hypothesis.mechanism,
        company_form=hypothesis.company_form,
        observables=[_to_observable(item) for item in hypothesis.observables if (item or "").strip()],
        falsifiers=list(hypothesis.falsifiers),
        evidence=[
            DriverEvidence(kind=evidence.kind, ref=evidence.ref, quote=evidence.quote)
            for evidence in hypothesis.evidence
        ],
        confidence=max(0.0, min(1.0, float(hypothesis.confidence))),
        matched_primitive=candidate.matched_primitive,
    )


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2}


def _to_research_question(item: ResearchAgendaItem) -> ResearchQuestion:
    priority = (item.priority or "high").strip().lower()
    if priority not in _PRIORITY_RANK:
        priority = "high"
    return ResearchQuestion(
        question=item.question.strip(),
        why_matters=item.why_matters,
        decisive_evidence=list(item.decisive_evidence),
        preferred_sources=list(item.preferred_sources),
        priority=priority,
        status="open",
    )


def _has_tier_a_anchor(profile: DriverProfile) -> bool:
    """是否存在 Tier A 锚（决定 ``provenance``：有锚 = ``hybrid``，纯研究 = ``llm``）。"""
    return profile.anchor_strength in {"strong", "weak"} and not profile.degraded


def converge(
    profile: DriverProfile,
    result: DriverResearchResult,
    *,
    research_input: Mapping[str, Any] | None = None,
    settings: DomainContextResearchSettings | None = None,
    trigger: str = "",
) -> DriverProfile:
    """Tier C：把 LLM 原始输出**确定性收敛**进 ``DriverProfile``（§D1.7 的 C1–C6）。

    | 步骤 | 规则 | 失败行为 |
    |---|---|---|
    | C1 schema | 逐条重新校验（防御：宽松救援路径可能留下半结构条目） | 单条丢弃并计数 |
    | C2 证据可溯 | 每条假设 ≥1 条 ``evidence`` 且 ``ref`` 非空 | 移入 ``unverified_drivers``，**不得进 primary_drivers**（LLM 自述的 ``open_questions`` 独立落位，P2-7 不再混装） |
    | C3 事实交叉校验 | ``observables``/``company_form`` 的占比、毛利率与结构事实相差超容差 | 降权（conf ≤ 0.3）+ MEDIUM issue |
    | C3.1 期别混用（P2-6） | ``company_form`` 同一句内 ≥2 个期别标签且 ≥2 处数字声明 | 只留 LOW issue（不降权；主防线在 prompt 的期别标注条款） |
    | C4.0 candidates 闭包（P1-3） | ``candidates`` ⊆ domain_context 目录（playbook ⊕ primitive 两族 id） | 非法 id 移出 + ``research_meta["candidate_drops"]`` + LOW issue；提案走 ``proposed_frameworks`` |
    | C4 原语对齐（P1-5） | ``variable`` 与 primitive ``key_variables`` 的**最长公共连续子串 ≥4 字**对齐；候选池 = 全原语目录 ⊕ 激活原语同 id 覆盖 | 填 ``matched_primitive`` + **无条件继承** ``causal_paths``（追加去重）/``disconfirming_signals``；对不上进 ``novel_drivers``（**唯一来源 = C4 推导**；LLM 自述的 ``novel_concepts`` 只进 ``research_meta``，P1-4）；命中细节落 ``research_meta["match_details"]`` |
    | C5 结构约束 | ≥1 primary 且 primary ≤3；条数 ≤ ``max_hypotheses`` | 按 confidence 截断 + 角色修正 |
    | C6 派生 | ``primary_drivers``/``secondary_drivers`` ← ``driver_hypotheses`` | 无可用假设时保留 Tier A 规则驱动（覆盖率不塌） |

    无可用假设时（``driver_hypotheses`` 为空）：``provenance`` 保持/回退 ``"rule"``（内容确实全部
    来自规则），``research_meta`` 显式记录 ``llm_attempted`` / ``unverified_count`` / ``hypotheses_count``，
    并在载荷内补一条 MEDIUM issue（``category="driver_profile_research_evidence_insufficient"``，
    四类失败之一的"证据不足"）——偏离控制据此入账，不依赖 provenance 标签间接触发。

    收敛层**只读不改**入参对象（``model_copy`` 返回新实例），保持"可重放、可单测"。
    """
    cfg = settings or research_settings()
    output = result.output
    meta: dict[str, Any] = dict(result.meta)
    issues: list[Any] = [item for item in (meta.get("issues") or []) if isinstance(item, Mapping)]
    meta.pop("issues", None)

    # ── C1：逐条 schema 校验（非法/空变量条目单条丢弃）──────────────────
    hypotheses, dropped = _coerce_items(output.driver_hypotheses, ResearchHypothesis)
    agenda_items, dropped_agenda = _coerce_items(output.research_agenda, ResearchAgendaItem)
    clean: list[ResearchHypothesis] = []
    for hypothesis in hypotheses:
        if not hypothesis.variable.strip():
            dropped += 1
            continue
        clean.append(hypothesis.model_copy(update={"confidence": max(0.0, min(1.0, float(hypothesis.confidence)))}))
    meta["schema_dropped"] = dropped + dropped_agenda

    # ── C2：证据可溯（无证据者进 unverified_drivers，绝不进 main_driver）──
    verified: list[ResearchHypothesis] = []
    unverified: list[str] = []
    for hypothesis in clean:
        if any((evidence.ref or "").strip() for evidence in hypothesis.evidence):
            verified.append(hypothesis)
        else:
            unverified.append(hypothesis.variable)

    # ── C2.1：数字出处强度（P0-1B）──────────────────────────────────────
    # ref 非空只保证"有引用"，不保证 company_form 里的数字被引用覆盖
    # （药明康德 H3：2026H1 的 140.3 亿 / 67.45% 只有一条 2025 年报 quote 支撑）。
    uncovered_map: dict[int, list[_NumericClaim]] = {
        index: _uncovered_claims(hypothesis, research_input) for index, hypothesis in enumerate(verified)
    }

    # ── C3：事实交叉校验（矛盾 ⇒ 降权 + 显式 issue，不静默）────────────
    shares = _segment_shares(research_input)
    financials = _financial_facts(research_input)
    segment_margins = _segment_margins(research_input)  # P0-2 新增
    consistency = _implied_total_contradictions(verified)  # P0-1A 新增
    downgrade: dict[int, list[str]] = {}

    # P0-1A：同一基数/期别的占比声明互相矛盾 ⇒ 相关假设同时降权
    for finding in consistency:
        for index in finding["hypothesis_indices"]:
            downgrade.setdefault(index, []).append(
                f"内部不一致：基数「{finding['base']}」（{finding['period']}）隐含总量 {finding['implied_totals']} 亿"
            )
        issues.append(
            {
                "severity": "medium",
                "category": "driver_profile_research_internal_inconsistency",
                "message": (
                    f"同一基数「{finding['base']}」（{finding['period']}）的占比声明互相矛盾："
                    f"「{'」 vs 「'.join(finding['snippets'])}」⇒ 隐含总量分别为 "
                    f"{finding['implied_totals']} 亿（相对差 > {_IMPLIED_TOTAL_TOLERANCE:.0%}）。"
                    "无法判定孰对孰错，相关假设已同时降权，请下游据此裁决。"
                ),
            }
        )

    # P0-1B：占比从句内的无出处数字 ⇒ 降权；其余无出处数字只留痕
    for index, claims in uncovered_map.items():
        if not claims:
            continue
        escalated = [claim for claim in claims if claim.in_share_clause]
        if escalated:
            downgrade.setdefault(index, []).append(
                "数字无出处："
                + "；".join(f"{claim.value}{claim.unit}（{claim.period or '期别未标注'}）" for claim in escalated)
            )
        issues.append(
            {
                "severity": "medium",
                "category": "driver_profile_research_claim_uncovered",
                "message": (
                    f"假设「{verified[index].variable}」的 company_form 有 {len(claims)} 处"
                    "财务数字声明无出处（证据同值同期不覆盖，输入包也无同值）："
                    + "；".join(
                        f"{claim.value}{claim.unit}（{claim.period or '期别未标注'}）：{claim.snippet}"
                        for claim in claims
                    )
                    + ("。其中含占比从句数字，已降权。" if escalated else "。")
                ),
            }
        )

    checked: list[ResearchHypothesis] = []
    for index, hypothesis in enumerate(verified):
        texts = [hypothesis.company_form, hypothesis.variable, *hypothesis.observables]
        conflicts = _share_contradictions(texts, shares) + _margin_contradictions(texts, financials, segment_margins)
        if conflicts or downgrade.get(index):
            hypothesis = hypothesis.model_copy(
                update={"confidence": min(hypothesis.confidence, _CONTRADICTION_MAX_CONFIDENCE)}
            )
        if conflicts:
            issues.append(
                {
                    "severity": "medium",
                    "category": "driver_profile_research_fact_conflict",
                    "message": f"{hypothesis.variable}: " + "；".join(conflicts),
                }
            )
        checked.append(hypothesis)

    # ── C3.1（P2-6）：同句期别混用轻检测（主防线是 prompt 的期别标注硬条款）────
    # 只留 LOW 留痕：P0-1A/P0-1B 遇到"一句多期别"是**跳过校验**（宁可漏检不可误报），
    # 这里把同一现象显式登记出来，交给下游裁决"期别口径是否确未标注"。
    mix_hits = _period_mix_sentences(verified)
    if mix_hits:
        issues.append(
            {
                "severity": "low",
                "category": "driver_profile_research_period_mixing",
                "message": "同一句内混用多个报告期且含多处数字，期别口径疑似未标注：" + "；".join(mix_hits),
            }
        )

    # ── C4.0（P1-3）：candidates 闭包校验（非法 id 移出，提案不污染候选）────
    # loader 只读 YAML、带 lru_cache、无 import 期副作用（故可在模块顶部导入），
    # 这里的目录读取是纯函数级、可重放的。
    # 判定面 = 整个 domain_context 目录（playbook ⊕ primitive 两族 id）。为什么不是只认
    # playbooks：真机缓存的 candidates 里混着 primitive 级分析积木（603259 = generic_fundamental
    # + capacity_cycle/cost_curve/working_capital_stress），而 P1-3 的病根是「id 在两个目录
    # **都不存在**」（600519 的 consumer_brand_pricing_power/premium_liquor_brand_franchise、
    # 文档举例的 capacity_ramp_cdmo/policy_regulatory_risk）——只认 playbooks 会把合法积木误杀。
    # **有意的不对称（闭包源 ⊋ prompt 提示池）**：prompt 与输入包只给 available_playbooks
    # （playbook id，§2.2 不额外提供原语 id），闭包却接受 playbook ∪ 原语——这是"宽进严出"
    # 的设计：提示面窄（防止模型凭空编框架名），校验面宽（只拦"目录里根本不存在"的 id，
    # 不误杀真实 run 里合法出现的 primitive 级候选）。
    catalog_ids = {*load_playbooks(), *load_primitives()}
    valid_candidates: list[str] = []
    dropped_candidates: list[str] = []
    for candidate in _dedup(output.candidates):
        if candidate in catalog_ids:
            valid_candidates.append(candidate)
        elif candidate.strip():
            dropped_candidates.append(candidate)
    if dropped_candidates:
        meta["candidate_drops"] = dropped_candidates
        issues.append(
            {
                "severity": "low",
                "category": "driver_profile_research_invalid_candidate",
                "message": (
                    f"candidates 引用了目录（playbook 或原语）中不存在的 id：{dropped_candidates}，已移除。"
                    "（新框架提案请走 proposed_frameworks 字段，本轮提案已保留："
                    f"{_dedup(output.proposed_frameworks)}）"
                ),
            }
        )

    # ── C4：原语对齐（P1-5：全目录池 ⊕ 激活原语同 id 覆盖）────────────────
    # 候选池从"仅 activated_primitives"改为**全原语目录**：
    # fallback 时激活的只有通用 3 原语，project_delivery 等永远不可能被对齐
    # （药明康德 run 的 7/7 未命中原因之二）。激活原语按 id 覆盖目录条目——
    # 这既是"弱锚时锚定知识优先"的业务语义，也保留测试的注入缝
    # （test_research.py 的 _capacity_primitive() 自定义 fixture 仍生效）。
    primitives_pool: dict[str, Any] = {primitive.id: primitive for primitive in load_primitives().values()}
    for activated in profile.activated_primitives:
        primitives_pool[activated.id] = activated
    candidates: list[_Candidate] = []
    novel: list[str] = []
    matched_ids: list[str] = []
    match_details: list[dict[str, str]] = []  # P1-5 审计：为什么对齐/对齐到哪
    for hypothesis in checked:
        primitive, matched_key, shared = _match_primitive(hypothesis.variable, primitives_pool)
        if primitive is None:
            novel.append(hypothesis.variable)
            candidates.append(_Candidate(hypothesis=hypothesis, novel=True))
            continue
        matched_ids.append(primitive.id)
        match_details.append(
            {
                "variable": hypothesis.variable,
                "primitive": primitive.id,
                "key_variable": matched_key,
                "shared": shared,
            }
        )
        inherited = [
            *hypothesis.falsifiers,
            *[signal for signal in primitive.disconfirming_signals if signal not in hypothesis.falsifiers],
        ]
        # C4：对齐成功即**无条件继承**原语的因果链（§D1.7「继承其 causal_paths」原文）：
        # LLM 自写的 mechanism 排在最前（它是标的特异的解读），原语 causal_paths 去重后以
        # "；" 追加在其后；mechanism 为空时结果即原语因果链原文。
        mechanism = "；".join(_dedup([hypothesis.mechanism, *primitive.causal_paths]))
        candidates.append(
            _Candidate(
                hypothesis=hypothesis.model_copy(update={"mechanism": mechanism, "falsifiers": inherited}),
                matched_primitive=primitive.id,
            )
        )

    # ── C5：结构约束（条数 ≤ max_hypotheses，按 confidence 截断；角色修正）──
    limit = max(1, int(cfg.max_hypotheses))
    ranked = sorted(candidates, key=lambda item: item.hypothesis.confidence, reverse=True)[:limit]
    ranked = _enforce_primary_roles(ranked)

    # ── C6：派生 primary/secondary_drivers + 议程（老消费方零改动受益）──
    driver_hypotheses = [_to_driver_hypothesis(candidate) for candidate in ranked]
    primary_drivers = [item.variable for item in driver_hypotheses if item.role == "primary"]
    secondary_drivers = [item.variable for item in driver_hypotheses if item.role == "secondary"]

    questions = [_to_research_question(item) for item in agenda_items if item.question.strip()]
    # 议程是"下游要按优先级执行"的清单：先按优先级、再按原始顺序截断。
    ordered = sorted(range(len(questions)), key=lambda index: _PRIORITY_RANK[questions[index].priority])
    agenda = [questions[index] for index in ordered[: max(0, int(cfg.max_agenda_items))]]

    confidence = (
        round(sum(item.confidence for item in driver_hypotheses) / len(driver_hypotheses), 4)
        if driver_hypotheses
        else 0.0
    )
    # 无任何可用假设 ⇒ 主驱动实为 Tier A 规则产物，provenance 必须如实标 "rule"（否则 D2 的
    # ``driver_profile_evidence_present`` 会把规则产物误判成 LLM 产物）。而"Tier B 跑了但
    # 没产出可溯驱动"这一事实**不靠 provenance 间接触发**，而是作为四类失败之一
    # （证据不足）落一条 artifact 载荷内 issue + ``research_meta["llm_attempted"]``。
    llm_attempted = not bool(meta.get("cached"))
    provenance = "rule" if not driver_hypotheses else ("hybrid" if _has_tier_a_anchor(profile) else "llm")
    if not driver_hypotheses:
        issues.append(
            {
                "severity": "medium",
                "category": "driver_profile_research_evidence_insufficient",
                "message": (
                    f"驱动研究产出 {len(unverified)} 条无证据假设，全部隔离进 unverified_drivers，主驱动回退规则产物"
                    if unverified
                    else "驱动研究未产出可用驱动假设，主驱动回退规则产物"
                ),
            }
        )
    meta.update(
        {
            "trigger": trigger or str(meta.get("trigger") or ""),
            "hypotheses_count": len(driver_hypotheses),
            "unverified_count": len(unverified),
            "llm_attempted": llm_attempted,
            "matched_primitives": _dedup(matched_ids),
            # P1-5 审计：每条对齐的「变量 → primitive / 命中 key_variable / 共享子串」，
            # 让"为什么对齐到这套框架"可复核（matched_primitives 键保持不变）。
            "match_details": match_details,
            # P1-4：LLM 自述的「沉淀概念」只落 meta（契约 novel_drivers 唯一来源是 C4 推导），
            # 供 D3 沉淀工具消费，不混进契约字段。
            "novel_concepts": _dedup(output.novel_concepts),
            "issues": [dict(item) for item in issues],
        }
    )
    updated = profile.model_copy(
        update={
            "driver_hypotheses": driver_hypotheses,
            "research_agenda": agenda,
            # P1-4：契约 novel_drivers **只**保留 C4 推导的「未对齐原语的驱动变量」——
            # LLM 自述的 novel_concepts 不混入（药明康德 run 里 11 条中 7 条是假设变量
            # 逐字副本的根因），其内容进 research_meta 供沉淀工具使用。
            "novel_drivers": _dedup(novel),
            # P1-3：只落闭包校验通过的候选；提案（proposed_frameworks）另字段承接、
            # 不污染 candidates（两者语义分离，见合同与 prompt 条款）。
            "candidates": valid_candidates,
            "proposed_frameworks": _dedup(output.proposed_frameworks),
            # P2-7：unverified_drivers 只收 C2 剥夺证据资格的**驱动变量**（隔离区）；
            # open_questions（认知缺口/数据悬疑）语义独立落位，不再混装进同一字段。
            "unverified_drivers": _dedup(unverified),
            "open_questions": _dedup(output.open_questions),
            "provenance": provenance,
            "research_confidence": confidence,
            "research_meta": meta,
        }
    )
    # 只有本轮确实产出可用驱动时才改写规则驱动：否则保留 Tier A 的 primary_drivers，
    # 保证"primary_drivers 非空"这一覆盖率契约在研究层不给力时也不塌。
    if driver_hypotheses:
        updated = updated.model_copy(
            update={"primary_drivers": primary_drivers, "secondary_drivers": secondary_drivers}
        )
    return updated


# ══════════════════════════════════════════════════════════════════════════
# D1.9 降级阶梯（Tier 0/1/2/3）
# ══════════════════════════════════════════════════════════════════════════


def _shape_for_tier(profile: DriverProfile, tier: int) -> DriverProfile:
    """按档位塑形画像：Tier 2 = 回 Tier A + D0 结构性兜底（``provenance="rule"``）；Tier 3 = 最小骨架。"""
    if tier < 2:
        # Tier 0/1：Tier 1 的"丢弃非法条目"已在收敛阶段完成，此处不动画像内容，只标记降级。
        return profile
    if tier == 2:
        return profile.model_copy(
            update={
                "driver_hypotheses": [],
                "research_agenda": [],
                "novel_drivers": [],
                "candidates": [],
                "unverified_drivers": [],
                "open_questions": [],  # P2-7 新增：降级档位同样清零（与 unverified 分列）
                "proposed_frameworks": [],  # P1-3 新增
                "research_confidence": 0.0,
                "provenance": "rule",
            }
        )
    # Tier 3：Tier A 只有锚、没有可用结构事实 ⇒ 最小骨架（保留 playbook，空驱动）。
    return profile.model_copy(
        update={
            "activated_primitives": [],
            "primary_drivers": [],
            "secondary_drivers": [],
            "driver_hypotheses": [],
            "research_agenda": [],
            "novel_drivers": [],
            "candidates": [],
            "unverified_drivers": [],
            "open_questions": [],  # P2-7 新增
            "proposed_frameworks": [],  # P1-3 新增
            "research_confidence": 0.0,
            "provenance": "rule",
        }
    )


def _write_degradation(payload: dict[str, Any], tier: int, reason: str) -> dict[str, Any]:
    """降级元数据的**唯一写入点**（委托 ``apply_degradation``，字段名不得再手写）。

    延迟 import 的原因：``domain_context`` 是纯契约层，不应在 import 期就把
    ``orchestrator`` 拉进来（这也让 domain_context 的 import 保持零副作用）。
    """
    from alphabee.orchestrator.services.degradation import apply_degradation

    return apply_degradation(payload, tier, reason, threshold=1)


def finalize_research_artifact(
    profile: DriverProfile,
    *,
    tier: int = 0,
    reason: str = "",
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """降级阶梯的落地点：``DriverProfile`` → artifact payload（含降级元数据）。

    - ``degraded`` / ``degradation_reason`` **只**经 :func:`_write_degradation`
      （→ ``apply_degradation``）写入，``threshold=1`` 使 Tier 1 起即标降级；
    - ``research_meta["fallback_tier"]`` 记录实际档位（与 ``degraded`` 分离：
      档位是"发生了什么"，标记是"要不要保守化消费"）；
    - ``tier >= 1`` 追加 MEDIUM issue（``category="driver_profile_research_degraded"``），
      保证研究降级永远有显式留痕（四类失败：研究失败/截断/证据不足/事实矛盾）；
    - **Tier A 自身的降级（D0 的"输入缺失"）不被研究层的成功抹掉**：这是两条独立的轴
      （数据链路问题 vs 研究档位），故仍经唯一写入点按 Tier 1 补标，保留 D0 的留痕语义。
    """
    shaped = _shape_for_tier(profile, int(tier))
    payload = shaped.model_dump(mode="json")
    payload = _write_degradation(payload, int(tier), reason)
    if bool(shaped.degraded) and payload.get("degraded") is not True:
        payload = _write_degradation(payload, 1, shaped.degraded_reason or "tier_a_degraded")

    merged: dict[str, Any] = dict(payload.get("research_meta") or {})
    merged.update(dict(meta or {}))
    merged["fallback_tier"] = int(tier)
    if int(tier) >= 1:
        issues = [item for item in (merged.get("issues") or []) if isinstance(item, Mapping)]
        issues.append(
            {
                "severity": "medium",
                "category": "driver_profile_research_degraded",
                "message": f"驱动研究降级（tier={int(tier)}）: {(reason or 'unknown')[:200]}",
            }
        )
        merged["issues"] = [dict(item) for item in issues]
    payload["research_meta"] = merged
    return payload
