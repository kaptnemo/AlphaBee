"""resolve_midterm_decision node — 方案 A + 决策点 6(a) 的中期决策接线节点。

职责（只映射 + 调用，不做业务计算）：

1. 从主链产物读取 ``symbol`` / ``INSIGHT_ANALYSIS`` / ``THESIS_ANALYSIS`` /
   ``CONFLICTS_RESULT``，映射为证据日志与 ``get_decision`` 输入；
2. 产出 ``CompanyStateArtifact`` → ``Artifact(type=MIDTERM_DECISION)`` 存入 artifacts 列表
   （不在 ``OrchestratorState`` 加专用字段）；
3. 整体 try/except：LLM/网络失败只记 ``Issue(category=midterm_decision_failed)``，
   报告路径照常（不中断主链）。

映射契约（方案 A + 决策点 2/3/4，§9 降级洞察当弱证据处理）：

- ``thesis H`` = ``insight.core_view``；当 insight 缺失或 ``degraded=True`` 时降级用
  ``thesis.overall_judgment`` 构造 H，再缺省空串走 Stage B neutral 退化。
- ``prior_confidence`` = insight/thesis confidence 显式映射：low/medium/high → 0.3/0.5/0.7；
  数字置信度原样透传（clamp 0-1）；降级洞察额外施加贝叶斯阻尼（fallback_tier 越高越保守）。
- ``window_texts`` = 已验证冲突 explanation（当前主链不提供财报/公告/研报原文，
  ``FACT_COLLECTION.raw_response`` 是叙事摘要，不作为窗口文本）；无任何窗口文本时传
  ``None``（合法输入：只跑数值证据）。
- ``include_market=True``。
- 改造 A（洞察力注入）：``insight.supporting_evidence/counter_evidence``（带 weight 的
  正反证据）经 ``adapt_insight_evidence`` 映射为 ``EvidenceEvent``（纯规则、零 LLM），
  与 ``collect_evidence`` 结果合并去重后作为独立证据源传入 ``get_decision``。

注意：本模块刻意不 import ``alphabee.orchestrator.collectors`` / ``state`` 的运行时符号，
而是本地实现 ``_make_id`` / ``_finalize_step`` 等三个小 helper——collectors 会传递性触发
tushare 的 ``set_token`` 副作用（在无 token 时写 ``~/tk.csv``），本节点保持可离线 import。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ConflictAnalysisResult
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.midterm.decision_model import collect_evidence, get_decision
from alphabee.midterm.evidence_extractor import dedupe_events
from alphabee.midterm.insight_evidence_adapter import adapt_insight_evidence
from alphabee.orchestrator.contracts import (
    InsightArtifact,
    ThesisArtifact,
    find_artifact_model,
)
from alphabee.utils.pipeline import make_id

if TYPE_CHECKING:
    from alphabee.orchestrator.state import OrchestratorState

# 定性置信度 → 先验 P(H) 的显式映射（禁止在调用方各自重新定义导致口径分裂）。
_CONFIDENCE_PRIOR: dict[str, float] = {"low": 0.3, "medium": 0.5, "high": 0.7}

# 降级洞察当弱证据处理（§9 贝叶斯阻尼）：fallback_tier 越高，先验越保守。
# tier 0=完整不阻尼；tier 1=宽松救援轻度下压；tier 2=确定性兜底中度下压；
# tier 3=最小骨架强下压。下压仅作用于先验 P(H)，不改变离散映射本身。
_DEGRADED_PRIOR_DAMPING: dict[int, float] = {0: 1.0, 1: 0.9, 2: 0.7, 3: 0.5}


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _finalize_step(step: Step, issues: list[Issue], artifacts: list[Artifact]) -> Step:
    """与 collectors._finalize_step 同语义的本地实现（避免传递性 tushare import）。"""
    if issues and not artifacts:
        status = StepStatus.FAILED
    elif issues:
        status = StepStatus.PARTIAL
    else:
        status = StepStatus.SUCCEEDED
    return step.model_copy(update={"status": status, "outputs": [a.id for a in artifacts]})


def _map_prior_confidence(confidence: Any, *, damping: float = 1.0) -> float | None:
    """把 insight/thesis 的置信度映射为 ``prior_confidence``（0-1）。

    支持字符串（low/medium/high，显式映射）与数字（原样透传并 clamp 0-1）；
    无法识别的值返回 ``None``（交由决策模型走无先验保守退化）。
    """
    if confidence is None:
        return None
    if isinstance(confidence, str):
        value = _CONFIDENCE_PRIOR.get(confidence.strip().lower())
        if value is None:
            return None
    elif isinstance(confidence, (int, float)):
        value = float(confidence)
    else:
        return None
    value = max(0.0, min(1.0, value))
    return max(0.0, min(1.0, value * damping))


def _thesis_overall_judgment(thesis: ThesisArtifact | None) -> str:
    """从 THESIS_ANALYSIS 提取 ``thesis.overall_judgment``（可能缺省为空串）。"""
    if thesis is None:
        return ""
    inner = thesis.thesis or {}
    return str(inner.get("overall_judgment", "") or "").strip()


def _judgment_to_hypothesis(judgment: str) -> str:
    """把确定性整体判断翻译成可判方向的中文假设 H（决策点 2 降级路径）。

    空/中性走空串：``collect_evidence`` 的 Stage B 按符号判定、定性 neutral 退化。
    """
    if not judgment:
        return ""
    j = judgment.strip().lower()
    if j in ("strong_positive", "positive"):
        return "公司基本面与预期改善，中期看多（thesis 整体判断偏积极）"
    if j in ("strong_negative", "negative"):
        return "公司基本面与预期走弱，中期看空（thesis 整体判断偏消极）"
    return ""


def _resolve_hypothesis(insight: InsightArtifact | None, thesis: ThesisArtifact | None) -> str:
    """计算核心假设 H：insight.core_view 优先，缺失/降级时用 thesis.overall_judgment 构造。"""
    if insight is not None and not insight.degraded and insight.core_view.strip():
        return insight.core_view.strip()
    return _judgment_to_hypothesis(_thesis_overall_judgment(thesis))


def _prior_confidence(insight: InsightArtifact | None, thesis: ThesisArtifact | None) -> float | None:
    """映射 prior_confidence：优先 insight.confidence，缺失时回退 thesis 维度置信度均值。

    降级洞察施加 fallback_tier 阻尼；均无法识别返回 ``None``。
    """
    source: Any = None
    damping = 1.0
    if insight is not None:
        source = insight.confidence
        if insight.degraded:
            damping = _DEGRADED_PRIOR_DAMPING.get(insight.fallback_tier, 0.5)
    if source is None and thesis is not None and thesis.thesis:
        dims = (thesis.thesis or {}).get("dimensions") or {}
        confidences = [d.get("confidence") for d in dims.values() if isinstance(d, dict)]
        numeric = [float(c) for c in confidences if isinstance(c, (int, float))]
        if numeric:
            source = sum(numeric) / len(numeric)
    return _map_prior_confidence(source, damping=damping)


def _conflict_explanations(artifacts: list[Artifact]) -> list[str]:
    """收集已验证冲突（verified/partial）的假设 explanation 作为 window_texts。"""
    conflicts = find_artifact_model(artifacts, ArtifactType.CONFLICTS_RESULT, ConflictAnalysisResult)
    if conflicts is None:
        return []
    explanations: list[str] = []
    for conflict in conflicts.conflicts:
        for hypothesis in conflict.hypotheses:
            if hypothesis.status in ("verified", "partial") and hypothesis.explanation.strip():
                explanations.append(f"{conflict.theme}: {hypothesis.explanation.strip()}")
    return explanations


def _window_texts(artifacts: list[Artifact]) -> list[str] | None:
    """组装 window_texts：仅含已验证冲突 explanation。

    原 fact_text 组件被移除：主链的 ``FACT_COLLECTION.raw_response`` 是叙事摘要而非
    财报/公告/研报原文，塞进窗口会污染 Stage A/B 抽取。当前主链不提供真正原文，
    故该组件置空；若未来主链能提供原文，再以显式原文字段补回。
    无任何窗口文本时传 ``None``（合法输入：只跑数值证据）。
    """
    return _conflict_explanations(artifacts) or None


async def resolve_midterm_decision(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """把主链产物映射为中期决策并落 MIDTERM_DECISION artifact（失败只降级不中断）。"""
    del config
    run = state.get("run")
    raw_symbol = run.context.get("symbol") if run else None
    symbol = str(raw_symbol) if raw_symbol else None

    step = Step(
        id="resolve_midterm_decision",
        kind="resolve_midterm_decision",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    if not symbol:
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed]}

    artifacts = state.get("artifacts", [])
    new_issues: list[Issue] = []
    new_artifacts: list[Artifact] = []

    # ── 读取上游 artifact + 映射 + 调用决策模型，整段同一 try/except ──
    # 任何异常（含上游 artifact 无法 model_validate）都只记 Issue 并正常返回，
    # 保证主链不被中断（报告照常）。
    try:
        insight = find_artifact_model(artifacts, ArtifactType.INSIGHT_ANALYSIS, InsightArtifact)
        thesis = find_artifact_model(artifacts, ArtifactType.THESIS_ANALYSIS, ThesisArtifact)

        hypothesis = _resolve_hypothesis(insight, thesis)
        prior = _prior_confidence(insight, thesis)
        window_texts = _window_texts(artifacts)

        # 收集证据：数值类规则 + 定性 Stage A/B（LLM，失败降级 → 无数值/定性证据）
        try:
            evidence = collect_evidence(symbol, thesis=hypothesis, window_texts=window_texts)
        except Exception:
            evidence = []  # 抽取全挂 → 只保留 insight 证据 + state_prior 保守退化

        # 改造 A：把 insight 的结构化正反证据（supporting/counter_evidence，带 weight）
        # 作为独立证据源合并进证据日志，不再被边界丢弃。纯规则映射、零 LLM（midterm 只消费）。
        # 事件日取 run.context 的 as_of_date（真实日期）；无日期时 fallback 今天
        # （date.today()，有效 YYYY-MM-DD）。id 仍由 statement 唯一（hash(date+kind+subject)），
        # 去重不受影响。
        as_of_date = str(run.context.get("as_of_date") or "") if run else ""
        if not as_of_date:
            as_of_date = date.today().isoformat()
        insight_evidence = adapt_insight_evidence(insight, symbol=symbol, date=as_of_date)
        evidence = dedupe_events([*evidence, *insight_evidence])

        # 改造 D：insight.materiality_rank 作为 EV materiality 修正传入（critical 下行变量
        # 加深 bear）；insight 缺失时用 []（无修正）。LLM 边界不变：洞察层产出，midterm 只消费。
        insight_materiality = insight.materiality_rank if insight is not None else []

        decision = get_decision(
            symbol,
            evidence=evidence,
            include_market=True,
            prior_confidence=prior,
            thesis=hypothesis,
            insight_materiality=insight_materiality,
        )
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type=ArtifactType.MIDTERM_DECISION,
                producer_step=step.id,
                value=decision.model_dump(mode="json"),
            )
        )
    except Exception as exc:
        # 降级纪律：LLM/网络失败只记 Issue，报告照常（不产出 MIDTERM_DECISION，
        # 下游 finalize/recorder 需容忍其缺失，详见 Phase 2/3 接线）。
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="midterm_decision_failed",
                message=f"resolve_midterm_decision failed: {exc}",
                related_step=step.id,
            )
        )
    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed_step],
        "artifacts": new_artifacts,
        "issues": new_issues,
    }
