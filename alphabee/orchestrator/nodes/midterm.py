"""resolve_midterm_decision node — 方案 A + 决策点 6(a) 的中期决策接线节点。

职责（只映射 + 调用，不做业务计算）：

1. 从主链产物读取 ``symbol`` / ``INSIGHT_ANALYSIS`` / ``THESIS_ANALYSIS`` /
   ``CONFLICTS_RESULT`` / ``FACT_COLLECTION``，映射为 ``get_decision_with_evidence`` 输入；
2. 产出 ``CompanyStateArtifact`` → ``Artifact(type=MIDTERM_DECISION)`` 存入 artifacts 列表
   （不在 ``OrchestratorState`` 加专用字段）；
3. 整体 try/except：LLM/网络失败只记 ``Issue(category=midterm_decision_failed)``，
   报告路径照常（不中断主链）。

映射契约（方案 A + 决策点 2/3/4，§9 降级洞察当弱证据处理）：

- ``thesis H`` = ``insight.core_view``；当 insight 缺失或 ``degraded=True`` 时降级用
  ``thesis.overall_judgment`` 构造 H，再缺省空串走 Stage B neutral 退化。
- ``prior_confidence`` = insight/thesis confidence 显式映射：low/medium/high → 0.3/0.5/0.7；
  数字置信度原样透传（clamp 0-1）；降级洞察额外施加贝叶斯阻尼（fallback_tier 越高越保守）。
- ``window_texts`` = fact_text + 已验证冲突 explanation；无任何原始财报章节文本时传
  ``None``（合法输入：只跑数值证据）。
- ``include_market=True``。

注意：本模块刻意不 import ``alphabee.orchestrator.collectors`` / ``state`` 的运行时符号，
而是本地实现 ``_make_id`` / ``_finalize_step`` 等三个小 helper——collectors 会传递性触发
tushare 的 ``set_token`` 副作用（在无 token 时写 ``~/tk.csv``），本节点保持可离线 import。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ConflictAnalysisResult
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.midterm.decision_model import get_decision_with_evidence
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


def _find_artifact_value(artifacts: list[Artifact], artifact_type: str) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        if artifact.type != artifact_type:
            continue
        if isinstance(artifact.value, dict):
            return artifact.value
    return None


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


def _window_texts(state: dict[str, Any], artifacts: list[Artifact]) -> list[str] | None:
    """组装 window_texts：fact_text + 已验证冲突 explanation；均无 → ``None``（只跑数值证据）。"""
    fact_value = _find_artifact_value(artifacts, ArtifactType.FACT_COLLECTION)
    fact_text = (fact_value or {}).get("raw_response", "") or ""
    texts: list[str] = []
    if fact_text.strip():
        texts.append(fact_text.strip())
    texts.extend(_conflict_explanations(artifacts))
    return texts or None


async def resolve_midterm_decision(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """把主链产物映射为中期决策并落 MIDTERM_DECISION artifact（失败只降级不中断）。"""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else state.get("symbol")

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

    insight = find_artifact_model(artifacts, ArtifactType.INSIGHT_ANALYSIS, InsightArtifact)
    thesis = find_artifact_model(artifacts, ArtifactType.THESIS_ANALYSIS, ThesisArtifact)

    hypothesis = _resolve_hypothesis(insight, thesis)
    prior = _prior_confidence(insight, thesis)
    window_texts = _window_texts(state, artifacts)

    new_artifacts: list[Artifact] = []
    try:
        decision = get_decision_with_evidence(
            symbol,
            thesis=hypothesis,
            window_texts=window_texts,
            include_market=True,
            prior_confidence=prior,
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
