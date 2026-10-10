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
    ActivatedPrimitive,
    DriverEvidence,
    DriverHypothesis,
    DriverObservable,
    DriverProfile,
    ResearchQuestion,
)
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
    （``fetch_local_report_fragments``，上限 ``max_report_chars`` 字符）。

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
        "notes": [
            "数字只能来自 tushare / 年报工具或本输入包的结构化事实；搜索结果不得当财务数据使用。",
            "分部结构必须与盈利公式自洽；矛盾必须写进 research_agenda。",
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
        novel_drivers=_coerce_str_list(parsed.get("novel_drivers")),
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
            output.novel_drivers,
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


def _margin_contradictions(texts: Sequence[str], financials: Mapping[str, float]) -> list[str]:
    """C3：公司整体毛利率口径的声称值与 ``fact_values.gross_margin`` 相差超过容差 ⇒ 矛盾。"""
    actual = financials.get("gross_margin")
    if actual is None:
        return []
    conflicts: list[str] = []
    for text in texts:
        for match in _MARGIN_RE.finditer(text or ""):
            claimed = float(match.group("value"))
            if abs(claimed - actual) > _MARGIN_TOLERANCE_PP:
                conflicts.append(f"毛利率: 假设声称 {claimed:.1f}% vs 实测 {actual:.1f}%")
    return conflicts


def _match_primitive(variable: str, primitives: Mapping[str, ActivatedPrimitive]) -> ActivatedPrimitive | None:
    """C4：驱动变量与 primitive 的 ``key_variables`` 语义对齐（双向子串，确定性取首个命中）。"""
    for primitive in primitives.values():
        if any(_contains(variable, key) for key in primitive.key_variables):
            return primitive
    return None


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
    | C2 证据可溯 | 每条假设 ≥1 条 ``evidence`` 且 ``ref`` 非空 | 移入 ``unverified_drivers``，**不得进 primary_drivers** |
    | C3 事实交叉校验 | ``observables``/``company_form`` 的占比、毛利率与结构事实相差超容差 | 降权（conf ≤ 0.3）+ MEDIUM issue |
    | C4 原语对齐 | ``variable`` 与 primitive ``key_variables`` 对齐 | 填 ``matched_primitive`` + **无条件继承** ``causal_paths``（追加去重）/``disconfirming_signals``；对不上进 ``novel_drivers`` |
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

    # ── C3：事实交叉校验（矛盾 ⇒ 降权 + 显式 issue，不静默）────────────
    shares = _segment_shares(research_input)
    financials = _financial_facts(research_input)
    checked: list[ResearchHypothesis] = []
    for hypothesis in verified:
        texts = [hypothesis.company_form, hypothesis.variable, *hypothesis.observables]
        conflicts = _share_contradictions(texts, shares) + _margin_contradictions(texts, financials)
        if conflicts:
            hypothesis = hypothesis.model_copy(
                update={"confidence": min(hypothesis.confidence, _CONTRADICTION_MAX_CONFIDENCE)}
            )
            issues.append(
                {
                    "severity": "medium",
                    "category": "driver_profile_research_fact_conflict",
                    "message": f"{hypothesis.variable}: " + "；".join(conflicts),
                }
            )
        checked.append(hypothesis)

    # ── C4：原语对齐（对齐 ⇒ 复用 YAML 因果链/证伪信号；对不上 ⇒ novel）──
    primitives = {primitive.id: primitive for primitive in profile.activated_primitives}
    candidates: list[_Candidate] = []
    novel: list[str] = []
    matched_ids: list[str] = []
    for hypothesis in checked:
        primitive = _match_primitive(hypothesis.variable, primitives)
        if primitive is None:
            novel.append(hypothesis.variable)
            candidates.append(_Candidate(hypothesis=hypothesis, novel=True))
            continue
        matched_ids.append(primitive.id)
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
            "issues": [dict(item) for item in issues],
        }
    )
    updated = profile.model_copy(
        update={
            "driver_hypotheses": driver_hypotheses,
            "research_agenda": agenda,
            "novel_drivers": _dedup([*output.novel_drivers, *novel]),
            "candidates": _dedup(output.candidates),
            "unverified_drivers": _dedup([*unverified, *output.open_questions]),
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
