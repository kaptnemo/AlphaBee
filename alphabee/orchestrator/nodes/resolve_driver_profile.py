"""resolve_driver_profile node — DOMAIN_CONTEXT P0 第 5 步在线注入。

职责（只路由 + 注入 + 按需研究，不重复取数，研究内核在 domain_context）：
1. 读 ``INDUSTRY_CONTEXT`` + ``COMPANY_TRACK`` + ``fact_values`` → 构造 ``RouterInput``
   （身份信号 + 结构信号：sw_code / 分部结构 / 财务结构）；
2. ``ContextRouter.route`` → ``build_driver_profile`` → 落 ``DRIVER_PROFILE`` artifact；
3. **Tier B 研究层（flag 门控，默认关）**：开关打开且触发条件命中时，在稀疏区域
   （无专用框架 / 弱锚 / 多主线 / 身份漂移）让 LLM 基于本公司年报与分部结构提出
   **标的特异的驱动假设 + 可执行研究议程**，再经 Tier C 确定性收敛进画像；
   整个研究段 try/except：任何失败走降级阶梯（``apply_degradation`` + MEDIUM issue），
   **绝不抛穿**；flag 关时严格 no-op（不 import 研究模块、不构造 agent，产物与 D0 逐字段一致）；
4. 降级契约（显式留痕，不静默）：
   - 身份信号全空 → ``generic_fundamental`` 兜底 + ``degraded=True`` + MEDIUM issue；
   - 普通未命中（有信号但无专用框架）→ ``generic_fundamental`` 兜底 + ``fallback=True``（非异常，不产 issue）；
   - 画像构建本身失败（primitive/playbook 加载异常等）→ 落降级画像 + MEDIUM issue，
     **绝不抛穿** LangGraph（否则整条 run 因一个可选的分析框架而中断）；
   - 研究层四类失败（研究失败 / 截断 / 证据不足 / 事实矛盾）→ artifact 载荷内 issue
     + 真 ``Issue`` 对象（供偏离账本消费）；因本节点恒产 artifact，故落 ``PARTIAL`` 而非 FAILED。

默认不阻塞：``DRIVER_PROFILE`` 恒产出（哪怕 fallback / 研究降级），下游 ``synthesize_insights``
总是能消费到一个 driver_profile 上下文。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.domain_context import GENERIC_FALLBACK_ID, DriverProfile, RouterInput, build_driver_profile
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.contracts import IndustryContextArtifact, find_artifact_model
from alphabee.orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# ── Tier B 研究层的运行期开关 ────────────────────────────────────────────
#: state 里显式注入的开关 key（``{"enabled": bool}``）。**刻意不加进 ``OrchestratorState``
#: 声明**：它是一次 run 的运行期开关（与 ``midterm`` 同构），用 Mapping 读取即可；
#: 加声明字段会改动 state schema 并牵连契约测试。
_RESEARCH_SWITCH_KEY = "driver_research"
#: 缺配置段/字段时的默认值：flag 门控起步（§14.2 决议 1），未验证前一律关。
_DEFAULT_RESEARCH_ENABLED = False

#: 研究层载荷内 issue 记录的 severity 名 → ``IssueSeverity``（未知值按 MEDIUM 处理）。
_SEVERITY_BY_NAME: dict[str, IssueSeverity] = {
    "low": IssueSeverity.LOW,
    "medium": IssueSeverity.MEDIUM,
    "high": IssueSeverity.HIGH,
    "critical": IssueSeverity.CRITICAL,
}

# ── 结构信号口径 ────────────────────────────────────────────────────────
# ``RouterInput.financial_structure`` 的键只允许是 canonical 字段名（schemas/*.yaml），
# 由 fact_values（规范化数值事实）直接取值；取不到就不写（"没拿到口径" ≠ "口径不达标"）。
# 注意：设计稿举例的 rnd_ratio / inventory_ratio 在 canonical schema 中并不存在，
# 故不入表——避免发明字段名（schema-steward 纪律：业务代码只用 canonical 字段）。
_FINANCIAL_STRUCTURE_FIELDS = (
    "gross_margin",
    "net_margin",
    "roe",
    "roa",
    "debt_to_assets",
    "current_ratio",
    "inventory_yoy",
    "revenue_yoy",
    "net_profit_yoy",
)

# 毛利率行业基准：优先真对标组（peer_avg_gross_margin），回退申万行业基准
# （industry_avg_gross_margin）。两者都是 canonical 字段。
_PEER_GROSS_MARGIN_FIELDS = ("peer_avg_gross_margin", "industry_avg_gross_margin")
# RouterInput 内部键名（context_router 的结构性兜底驱动按此键读取，见编码设计 §7.1）。
_PEER_GROSS_MARGIN_KEY = "peer_gross_margin"

# 分部摘要条数上限：仅供路由/报告做"结构画像"，不需要全量分项。
_SEGMENT_SUMMARY_LIMIT = 5


def _latest_segments(track: CompanyTrackArtifact) -> list[SegmentSnapshot]:
    """取**最新报告期**的分项快照（跨期混用会让占比/增速失真）。"""
    if not track.segments:
        return []
    latest = max(seg.report_date for seg in track.segments)
    return [seg for seg in track.segments if seg.report_date == latest]


def _effective_share(seg: SegmentSnapshot, rows: list[SegmentSnapshot]) -> float | None:
    """分项占比（%）：数据源直接给就用；否则按同报告期收入相对占比近似。

    与 ``company_track.label._effective_share`` 同口径（tushare 分项无占比时由收入推导）。
    """
    if seg.revenue_share is not None:
        return float(seg.revenue_share)
    total = sum(row.revenue or 0.0 for row in rows)
    if total > 0 and seg.revenue is not None:
        return float(seg.revenue) / total * 100.0
    return None


def _segment_stat(rows: list[SegmentSnapshot], name: str | None) -> tuple[float | None, float | None]:
    """按分项名取 (占比%, 同比%)——名字缺失或查不到即 (None, None)。"""
    if not name:
        return None, None
    for seg in rows:
        if seg.segment_name == name:
            yoy = float(seg.revenue_yoy) if seg.revenue_yoy is not None else None
            return _effective_share(seg, rows), yoy
    return None, None


def _segment_summary(rows: list[SegmentSnapshot]) -> list[str]:
    """分部结构摘要：``["云计算/服务器 42%(+58%)", ...]``（按占比降序，截断）。"""
    ranked = sorted(
        rows,
        key=lambda seg: (_effective_share(seg, rows) or 0.0, seg.revenue or 0.0),
        reverse=True,
    )
    summary: list[str] = []
    for seg in ranked[:_SEGMENT_SUMMARY_LIMIT]:
        text = seg.segment_name
        share = _effective_share(seg, rows)
        if share is not None:
            text += f" {share:.0f}%"
        if seg.revenue_yoy is not None:
            text += f"({seg.revenue_yoy:+.0f}%)"
        summary.append(text)
    return summary


def _financial_structure(fact_values: dict[str, float]) -> dict[str, float]:
    """从 canonical ``fact_values`` 抽出财务结构（全部 canonical 键，缺失不写）。"""
    structure: dict[str, float] = {}
    for field in _FINANCIAL_STRUCTURE_FIELDS:
        value = fact_values.get(field)
        if isinstance(value, int | float):
            structure[field] = float(value)
    for field in _PEER_GROSS_MARGIN_FIELDS:
        value = fact_values.get(field)
        if isinstance(value, int | float):
            # 单位归一：``peer_avg_gross_margin`` / ``industry_avg_gross_margin`` 是 **RATIO**，
            # 而 ``gross_margin`` 是 **PERCENT**（schemas/financial.yaml vs industry.yaml）。
            # 结构性兜底驱动按百分数拼「毛利率 x%（行业基准 y%，差 z pp）」，故这里把基准
            # RATIO → PERCENT（×100），避免「行业基准 0.3%」这类单位错配。
            structure[_PEER_GROSS_MARGIN_KEY] = float(value) * 100.0
            break
    return structure


def _degraded_profile(symbol: str, generated_at: str, reason: str) -> DriverProfile:
    """构建失败的降级画像：仍是合法 DriverProfile，下游可照常消费（读不到内容即跳过）。"""
    return DriverProfile(
        symbol=symbol,
        generated_at=generated_at,
        playbook=GENERIC_FALLBACK_ID,
        fallback=True,
        degraded=True,
        degraded_reason=reason,
    )


# ── Tier B 研究层接线（开关门控 / 输入组装 / 降级留痕）────────────────────


def _research_switches(state: OrchestratorState) -> dict[str, Any]:
    """研究层运行开关：config 为底（默认关），state 显式携带时覆盖。

    - 读 ``domain_context.research.enabled``（默认 ``False``，flag 门控起步）；
    - state 的 ``driver_research`` 键（``{"enabled": bool}``）显式存在时以其为准，
      便于 CLI/测试按 run 打开或关闭而无需改配置文件；
    - 逐层 ``getattr`` + 异常容忍（照 ``orchestrator/services/detection.py::detection_switches``
      的先例）：缺 ``domain_context`` / ``research`` 段的旧 config ⇒ 保持默认关；
    - **模块级不读配置**，且研究模块只在 ``enabled`` 为真时才延迟 import
      —— 这正是"flag 关 = 严格 no-op"的实现基础。
    """
    enabled = _DEFAULT_RESEARCH_ENABLED
    try:
        from alphabee.config import get_settings

        research_cfg = getattr(getattr(get_settings(), "domain_context", None), "research", None)
        enabled = bool(getattr(research_cfg, "enabled", _DEFAULT_RESEARCH_ENABLED))
    except Exception:  # noqa: BLE001 —— 配置不可读 ⇒ 研究层保持关闭（可选增强，不该影响主链路）
        enabled = _DEFAULT_RESEARCH_ENABLED

    raw = dict(state).get(_RESEARCH_SWITCH_KEY)
    if isinstance(raw, Mapping) and "enabled" in raw:
        enabled = bool(raw["enabled"])
    return {"enabled": enabled}


def _track_drift_detected(track: CompanyTrackArtifact | None) -> bool:
    """COMPANY_TRACK 是否有身份漂移信号（review_notes 或跨期漂移检测的结论）。

    漂移是 Tier B 的第四个触发分支：主线换人时旧框架已过期，值得让研究层复核。
    """
    if track is None:
        return False
    if track.review_notes:
        return True
    if not track.segments:
        return False
    from alphabee.company_track.label import detect_track_drift

    return bool(detect_track_drift(track.segments))


def _research_report_period(track: CompanyTrackArtifact | None, rows: list[SegmentSnapshot]) -> str:
    """研究缓存 key 用的"最新报告期"：优先 ``COMPANY_TRACK.as_of_date``，退回最新分部期。"""
    if track is not None and track.as_of_date:
        return track.as_of_date
    return max((seg.report_date for seg in rows), default="")


def _research_issues(payload: Mapping[str, Any], step_id: str) -> list[Issue]:
    """把研究层载荷内的 issue 记录转成真 ``Issue`` 对象（进偏离账本 / 供下游消费）。

    研究层的四类失败（研究失败 / 截断 / 证据不足 / 事实矛盾）都先落 artifact 载荷
    （``research_meta["issues"]``，唯一真相），此处再镜像成 ``Issue``；两者同时存在是刻意的
    —— 载荷保证"画像自洽可回放"，``Issue`` 保证"偏离账本能查到"。
    """
    raw_meta = payload.get("research_meta")
    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    records = meta.get("issues")
    issues: list[Issue] = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, Mapping):
            continue
        category = str(record.get("category") or "driver_profile_research_degraded")
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=_SEVERITY_BY_NAME.get(str(record.get("severity") or "").lower(), IssueSeverity.MEDIUM),
                category=category,
                message=str(record.get("message") or category),
                related_step=step_id,
            )
        )
    return issues


def _research_tier_reason(tier: int) -> str:
    """成功分支里 ``tier >= 1`` 时的**人类可读**降级原因（Tier 1 = 宽松救援）。

    Tier 0（严格解析）返回空串（无降级）；Tier 1 只丢弃了非法条目、保留了合法条目，
    故给"schema 已被救援"这一可读结论——降级 issue 的 message 里绝不能出现 ``unknown``。
    """
    if tier <= 0:
        return ""
    if tier == 1:
        return "research_schema_salvaged"
    return f"research_degraded_tier{tier}"


class _ResearchImportSentinel(Exception):
    """占位异常类型：研究层 import 成功前用作"永不匹配"的分类器。

    为什么需要它：研究段的 import 也在 try 内（import 失败必须降级而非中断 run），
    若处理器直接写 ``except DriverResearchError``，import 失败时该名字尚未绑定，
    处理器求值本身就会抛 ``UnboundLocalError``——防护形同虚设。
    """


async def _run_research_layer(
    *,
    symbol: str,
    profile: DriverProfile,
    router_input: RouterInput,
    track: CompanyTrackArtifact | None,
    fact_values: Mapping[str, float],
    rows: list[SegmentSnapshot],
    step_id: str,
) -> tuple[DriverProfile, dict[str, Any] | None, list[Issue]]:
    """执行 Tier B + Tier C（仅在 flag 开时调用；**整段共用一个防护**，绝不抛穿）。

    防护范围（把"研究段"作为一个整体来兜）：延迟 import、``should_research`` 触发判定、
    漂移检测、输入组装、LLM 调用、Tier C 收敛、降级载荷构造、issue 镜像——任一环节出错
    都不得让 run 中断（研究层是可选增强，``DRIVER_PROFILE`` 必须恒产出）。

    两级分类 + 兜底：

    * ``DriverResearchError`` → 按其 ``tier`` / ``reason`` / ``truncated`` 降级；
    * 其它任何异常（含 import 失败）→ ``tier=2`` + ``reason="research_failed:<类型>: <消息>"``；
    * 连降级载荷都构造不出来（研究层整体不可用）→ 回 **Tier A 原画像**载荷，用 orchestrator
      的降级唯一写入点补 ``degraded`` 元数据 + 一条 MEDIUM issue，仍不抛穿。

    Returns:
        ``(profile, payload, issues)``：``payload`` 非空表示本次确实走了研究层（成功或降级），
        节点据此决定 artifact 载荷；``issues`` 是研究层留痕的镜像 ``Issue``。
    """
    base_profile = profile
    # import 成功前分类器指向"永不匹配"的占位类型（见 _ResearchImportSentinel 的说明）。
    research_error_type: type[Exception] = _ResearchImportSentinel
    try:
        # import 也进 try：研究层的 import/构造问题同样必须降级而不是中断 run。
        from alphabee.domain_context.research import (
            DriverResearchError,
            build_research_input,
            converge,
            finalize_research_artifact,
            run_driver_research,
            should_research,
        )

        research_error_type = DriverResearchError

        # 单位边界（唯一处）：``RouterInput.dominant_share`` 是 canonical **百分数**（0~100，
        # D0-c 兜底驱动字符串还依赖 ``{share:.0f}%`` 格式化，故不改进它），而 §D1.5 的
        # ``_MULTI_SEGMENT_SHARE`` 口径是 **0~1 分数** ⇒ 在接缝处归一化，两处口径不再能静默错开。
        raw_share = router_input.dominant_share
        multi_segment_share = raw_share / 100.0 if raw_share is not None else None
        should, trigger = should_research(
            router_input,
            profile.anchor_strength,
            multi_segment_share,
            _track_drift_detected(track),
        )
        if not should:
            return profile, None, []

        research_input = build_research_input(
            symbol,
            router_input,
            track=track,
            fact_values=fact_values,
            profile=profile,
        )
        research_result = await run_driver_research(
            symbol,
            research_input,
            track_label=router_input.track_label,
            report_period=_research_report_period(track, rows),
            playbook_version=profile.playbook_version,
            trigger=trigger,
        )
        profile = converge(profile, research_result, research_input=research_input, trigger=trigger)
        tier = int(research_result.meta.get("tier", 0))
        payload = finalize_research_artifact(profile, tier=tier, reason=_research_tier_reason(tier))
        return profile, payload, _research_issues(payload, step_id)
    except Exception as exc:  # noqa: BLE001 —— 研究层属可选增强，任何异常都必须降级而非中断 run
        if isinstance(exc, research_error_type):
            # 一级：研究层显式失败（超时 / agent 异常 / 重试后仍解析失败 / 预算耗尽）。
            tier = int(getattr(exc, "tier", 2))
            reason = str(getattr(exc, "reason", exc))
            truncated = bool(getattr(exc, "truncated", False))
        else:
            # 二级：其它异常（含 import 失败 / should_research 抛错 / 漂移检测 / 输入组装 / 收敛 / 镜像）。
            tier, reason, truncated = 2, f"research_failed:{type(exc).__name__}: {exc}", False

    try:
        payload = finalize_research_artifact(profile, tier=tier, reason=reason, meta={"truncated": truncated})
        return profile, payload, _research_issues(payload, step_id)
    except Exception as exc:  # noqa: BLE001 —— 连降级载荷都构造失败（如研究层整体 import 不可用）
        # 最后兜底：回 Tier A 规则画像，用 orchestrator 的降级唯一写入点补元数据 + 一条 MEDIUM issue。
        # 这里不再依赖研究层（它已不可用），但仍不让 run 中断、也不丢"研究失败"的留痕。
        logger.warning("driver research degradation payload failed: %s", exc)
        payload = dict(base_profile.model_dump(mode="json"))
        try:
            from alphabee.orchestrator.services.degradation import apply_degradation

            payload = apply_degradation(payload, tier, reason, threshold=1)
        except Exception as inner:  # noqa: BLE001 —— 降级元数据也写不进去时，至少保住 artifact + issue
            logger.warning("driver research degradation metadata unavailable: %s", inner)
        meta: dict[str, Any] = dict(payload.get("research_meta") or {})
        meta["fallback_tier"] = int(tier)
        meta["truncated"] = bool(truncated)
        meta["issues"] = [
            *(meta.get("issues") or []),
            {
                "severity": "medium",
                "category": "driver_profile_research_degraded",
                "message": f"驱动研究降级落地失败（{reason}），保留 Tier A 规则画像",
            },
        ]
        payload["research_meta"] = meta
        return base_profile, payload, _research_issues(payload, step_id)


async def resolve_driver_profile(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """解析公司驱动画像（DriverProfile）并落 artifact。"""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="resolve_driver_profile",
        kind="resolve_driver_profile",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    if not symbol:
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed]}

    artifacts = state.get("artifacts", [])
    new_issues: list[Issue] = []

    # ── 1. 读上游身份/结构信号（缺失即空，交由 router 兜底）──────────────
    # 业务含义：路由只消费「已落地产物」（INDUSTRY_CONTEXT 的申万行业/代码、COMPANY_TRACK 的
    # 真实赛道 + 分部结构、fact_values 的规范化财务事实），绝不重新取数——行业识别、业务线
    # 解构与本公司的财务口径都是前面节点的职责，本节点只做"把已有信号翻译成分析框架"，
    # 保持单一职责与可回放。
    ind = find_artifact_model(artifacts, ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    track = find_artifact_model(artifacts, ArtifactType.COMPANY_TRACK, CompanyTrackArtifact)
    fact_values: dict[str, float] = dict(state.get("fact_values") or {})

    rows = _latest_segments(track) if track else []
    dominant_share, _ = _segment_stat(rows, track.dominant_segment if track else None)
    _, fastest_yoy = _segment_stat(rows, track.fastest_segment if track else None)

    router_input = RouterInput(
        symbol=symbol,
        track_label=(track.track_label if track else ""),
        industry=(ind.industry if ind else ""),
        sub_industry=(ind.sub_industry if ind else ""),
        business_model=(track.business_model if track else ""),
        sw_code=((ind.sw_code or "") if ind else ""),
        dominant_segment=((track.dominant_segment or "") if track else ""),
        dominant_share=dominant_share,
        fastest_segment=((track.fastest_segment or "") if track else ""),
        fastest_yoy=fastest_yoy,
        segment_summary=_segment_summary(rows),
        financial_structure=_financial_structure(fact_values),
    )

    # ── 2. 路由 + 组装（恒产出，哪怕 fallback）─────────────────────────
    # 业务含义：DRIVER_PROFILE 一定产出，即使公司命中不了专用框架（回退 generic_fundamental）。
    # 这样下游 synthesize_insights 的 context 里"永远有 driver_profile 这一块"，报告主线
    # 不会因为某家公司没有专用框架而整段缺失——最坏也退回通用财务维度并显式说明。
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        profile = build_driver_profile(symbol, router_input, generated_at=generated_at)
    except Exception as exc:  # noqa: BLE001 —— 画像属可选增强，失败必须降级而非中断 run
        # G-10：build_driver_profile 内部要读 playbook/primitive 目录（磁盘 IO + 校验），
        # 任何异常都不该让整条 LangGraph run 崩掉；落降级画像 + MEDIUM issue 显式留痕。
        profile = _degraded_profile(
            symbol,
            generated_at,
            reason=f"driver_profile_build_failed:{type(exc).__name__}",
        )
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="driver_profile_degraded",
                message=f"公司驱动画像构建失败（{type(exc).__name__}: {exc}），回退 {profile.playbook}",
                related_step=step.id,
            )
        )

    # ── 3. Tier B 研究层（flag 门控：默认关 = 严格 no-op）───────────────
    # 业务含义：Tier A 只回答"该用哪套框架"；稀疏区域（无专用框架 / 弱锚 / 多主线 /
    # 身份漂移）连框架本身都不可靠，需要模型基于**本公司**的年报与分部结构提出标的特异的
    # 驱动假设与研究议程。flag 关时不 import 研究模块、不构造 agent —— 产物与 D0 逐字段一致；
    # flag 开但未触发（强锚 + 单一主线 + 无漂移）同样零 LLM 成本，不把"开着开关"当"必须调用"。
    research_payload: dict[str, Any] | None = None
    research_issues: list[Issue] = []
    if _research_switches(state)["enabled"]:
        profile, research_payload, research_issues = await _run_research_layer(
            symbol=symbol,
            profile=profile,
            router_input=router_input,
            track=track,
            fact_values=fact_values,
            rows=rows,
            step_id=step.id,
        )

    # ── 4. 落 artifact（研究层跑了就用其载荷：含降级元数据与 research_meta）──
    new_artifacts = [
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.DRIVER_PROFILE,
            producer_step=step.id,
            value=research_payload if research_payload is not None else profile.model_dump(mode="json"),
        )
    ]

    # ── 5. 降级留痕：只有「身份信号全空」才算策略降级，普通未命中不算 ────────
    # 先于研究层留痕判定，保证"输入缺失"这一 D0 事实不会被研究层的 issue 顶掉。
    if profile.degraded and not new_issues:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="driver_profile_degraded",
                message=(f"公司驱动画像降级（{profile.degraded_reason or '输入缺失'}），回退 {profile.playbook}"),
                related_step=step.id,
            )
        )

    # ── 6. 研究层留痕：把载荷内 issue 镜像成真 Issue（偏离账本可查）────────
    new_issues.extend(research_issues)

    completed = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed],
        "artifacts": new_artifacts,
        "issues": new_issues,
    }
