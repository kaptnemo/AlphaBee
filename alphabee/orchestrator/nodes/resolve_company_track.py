"""resolve_company_track node — COMPANY_TRACK Phase F 在线注入（完整赛道）。

职责（只解析 + 注入，完整研究走离线 build_company_track）：
1. 组装完整 ``CompanyTrackArtifact``（业务线分项 + 真实赛道标签 + 商业模式 + 漂移）；
2. 读对标组存储 → 计算并注入 ``peer_*`` 基准（fact_values），写 COMPANY_TRACK artifact；
   存储未命中时走**在线兜底**（REPORT_QUALITY_FIX_ROADMAP §11 P2-①）：首选本地财报
   「管理层讨论与分析」章节作业务描述 → LLM 推断同环节 A 股对标；无本地报告时退而同行业
   成分股闭集（``IndustryContextArtifact.peer_universe``）LLM 择优 → ``build_peer_group``
   持久化 → 成功即注入，失败才降级 ``peer_group_missing``；
3. 降级分级（显式留痕，不静默）：

| 场景 | 产物 | issue |
|---|---|---|
| 无业务线数据（无 track） | 无 artifact | ``company_track_missing``（MEDIUM） |
| 有 track 但无对标组（存储未命中且在线兜底无果） | 全量 artifact（无 peer_*） | ``peer_group_missing``（LOW） |
| 对标组计算失败 | artifact（degraded）+ 无 peer_* | ``peer_group_benchmarks_missing``（MEDIUM） |
| 对标组保留数 < ``min_peers``（默认 2） | artifact（无 peer_*） | ``peer_group_missing``（LOW，『对标组不足、中位数不可比』） |
| track 过期 | artifact（stale=True） | ``company_track_stale``（MEDIUM，进报告披露检查） |

注入的 ``peer_*`` canonical 字段：peer_avg_roe / peer_avg_debt_ratio / peer_avg_gross_margin /
peer_revenue_yoy / peer_median_pe_ttm / peer_median_pb。
"""

from __future__ import annotations

from datetime import date
from typing import cast

from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.state import OrchestratorState


def _min_peers() -> int:
    """消费侧最小对标数（``company_track.peer_quality.min_peers`` 配置项；缺段/异常 ⇒ 默认 2）。"""
    from alphabee.company_track.peer_judge import MIN_PEERS_DEFAULT

    try:
        from alphabee.config import get_settings

        return int(get_settings().company_track.peer_quality.min_peers)
    except Exception:
        return MIN_PEERS_DEFAULT


def _is_stale(stale_after: str | None) -> bool:
    if not stale_after:
        return False
    try:
        return date.today() > date.fromisoformat(stale_after)
    except ValueError:
        return False


async def resolve_company_track(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """解析公司赛道并注入 peer_* 基准（无 track → 显式降级留痕）。"""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="resolve_company_track",
        kind="resolve_company_track",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    if not symbol:
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed]}

    artifacts = state.get("artifacts", [])
    new_issues: list[Issue] = []

    # ── 0. 申万基线（B3 并存：公司赛道为修正字段）────────────────
    from alphabee.orchestrator.contracts import IndustryContextArtifact, find_artifact_model

    ind = find_artifact_model(artifacts, ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    sw_industry = ind.industry or "" if ind is not None else ""
    sw_code = ind.sw_code or "" if ind is not None else ""

    # ── 1. 完整赛道（segments + track_label + business_model，best-effort）──
    from alphabee.company_track import build_company_track

    track = build_company_track(symbol, use_llm=True, sw_industry=sw_industry, sw_code=sw_code)
    if track.degraded or not track.segments:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="company_track_missing",
                message=f"标的 {symbol} 无业务线数据（{track.degraded_reason or '未知原因'}），回退申万行业基线",
                related_step=step.id,
            )
        )
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed], "issues": new_issues}

    # ── 2. 对标组基准（有对标组 → peer_* 注入）───────────────────
    from alphabee.company_track.peer_group_store import PeerGroupStore

    store = PeerGroupStore()
    peer_group = store.load(symbol)
    # 空对标组且已判定「确无 A 股直接对标」（no_peers）时不再重试在线兜底，
    # 避免每次分析重复调用 LLM（如 301029 怡合达的目录平台业态确实无同模式对标）。
    if peer_group is None or (peer_group.is_empty() and not peer_group.no_peers):
        # 在线兜底（REPORT_QUALITY_FIX_ROADMAP §11 P2-①）：存储未命中 →
        # 首选**本地财报「管理层讨论与分析」章节**作片段（半年报/年报业务描述，最接近人工选股
        # 依据）→ LLM 抽取；无本地报告时退而同行业成分股闭集（artifact.peer_universe）LLM 择优。
        # 两者皆无可选 → 不编造，落到下方 peer_group_missing 降级。
        from alphabee.company_track import build_peer_group
        from alphabee.company_track.peer_report import fetch_local_report_fragments

        report_sections, _report_meta = fetch_local_report_fragments(symbol)
        business_description = report_sections[0] if report_sections else None
        universe_codes = list(ind.peer_universe) if ind is not None else []
        if business_description or universe_codes:
            built_group, _build_warnings = build_peer_group(
                symbol,
                business_description=business_description,
                universe_codes=universe_codes,
                industry=sw_industry,
                segments=track.segments,
                name=track.track_label,
                use_llm=True,
                store=store,
            )
            if not built_group.is_empty():
                peer_group = built_group

    peer_values: dict[str, float] = {}
    # 消费侧**最小数量闸**（设计 §3.3/§3.6/§8 决策 4）：1 只候选时中位数 = 该股本身，作基准无意义
    # ⇒ 不注入任何 peer_*，回退 industry 基线并记 issue/notes（期望空组走同一条路）。

    min_peers = _min_peers()
    peer_gate_blocked = False
    too_few = peer_group is not None and not peer_group.is_empty() and len(peer_group.codes) < min_peers
    if too_few:
        assert peer_group is not None  # 由 too_few 的定义保证
        message = (
            f"对标组不足（{len(peer_group.codes)} < {min_peers}），中位数不可比，不注入 peer_*，回退 industry 基线"
        )
        if message not in peer_group.notes:
            peer_group.notes.append(message)
            try:
                store.save(peer_group)  # 留痕；存储异常不影响降级路径（fail-open）
            except Exception:
                pass
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.LOW,
                category="peer_group_missing",
                message=f"标的 {symbol} {message}",
                related_step=step.id,
            )
        )
        peer_group = None
        peer_gate_blocked = True

    if peer_group is not None and not peer_group.is_empty():
        from alphabee.company_track import derive_peer_benchmarks

        peer_values, meta = derive_peer_benchmarks(peer_group.codes, industry=peer_group.name)
        track.peer_group = peer_group.codes
        track.peer_group_source = peer_group.source
        track.peer_benchmarks = cast(dict[str, float | None], peer_values)
        # 对标组置信度（设计 §3.7/§8 决策 5）：确定性合成三档（不额外调 LLM），
        # 落 artifact 字段 + review_notes 一行（含三档与合成口径）；低置信由报告层显式提示。
        from alphabee.company_track import peer_confidence_for_group

        confidence = peer_confidence_for_group(symbol, peer_group)
        track.peer_group_confidence = confidence.level
        track.peer_group_confidence_score = confidence.score
        track.peer_group_confidence_basis = confidence.basis()
        taxonomy_signal = confidence.signals.get("taxonomy_reliable")
        track.peer_group_taxonomy_reliable = None if taxonomy_signal is None else bool(taxonomy_signal)
        track.review_notes.append(confidence.note_line())
        if track.peer_group_taxonomy_reliable is False:
            track.review_notes.append("对标组分类兜底，未经业务核验（分类学不可信：残差桶/成分不足/快照缺失）")
        if meta.get("error") or not peer_values:
            track.degraded = True
            track.degraded_reason = meta.get("error") or "对标组基准不可得"
            new_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="peer_group_benchmarks_missing",
                    message=f"对标组基准计算失败: {meta.get('error') or '无可用基准'}",
                    related_step=step.id,
                )
            )
    elif not peer_gate_blocked:
        # 无对标组配置（存储未命中且在线兜底无果）。上方的 min_peers 闸已单独记 issue，不重复。
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.LOW,
                category="peer_group_missing",
                message=f"标的 {symbol} 无对标组配置（data/peer_groups/{symbol}.json），赛道判断仅用申万基线",
                related_step=step.id,
            )
        )

    # ── 3. 过期标记（进报告披露检查）─────────────────────────────
    if _is_stale(track.stale_after):
        track.stale = True
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="company_track_stale",
                message=f"公司赛道数据截至 {track.as_of_date}（已过期），报告需显式提示",
                related_step=step.id,
            )
        )

    new_artifacts = [
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.COMPANY_TRACK,
            producer_step=step.id,
            value=track.model_dump(mode="json"),
        )
    ]

    completed = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed],
        "artifacts": new_artifacts,
        "issues": new_issues,
        # None 不注入：缺失即回退 industry_* → 绝对阈值（registry.py 回退链）
        "fact_values": peer_values,
    }
