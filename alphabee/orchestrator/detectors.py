"""后置检测器注册表与首批 6 个确定性检测器（F1b / §6.2、§14.2-B）。

**职责**：在节点出口立即执行契约声明的检测器（检测时延 O(流水线) → O(1)），把"偏离"
转成统一的 :class:`~alphabee.core.schemas.Issue`（经 ``services.deviation.record_deviation``）。

工程纪律（§14.0 约定 3 / C2 / C3）：

1. **检测器只读**：只读 :class:`NodeContext`，不得修改 ``state`` / ``update``；检测器**不修数据**
   （隐式恢复一律禁止，恢复必须走 §7 阶梯）；
2. **fail-open**：单个检测器抛异常 → ``logger.warning`` + 跳过该检测器，**不影响同节点其他检测器**，
   更不打断节点返回；
3. **无 LLM 检测器**（v1）：全部是确定性断言；
4. **注册表缺失 = 严格 no-op**：``assumption_still_valid`` 在假设登记簿 artifact 不存在时判 **pass**
   （不得误报 D4——F1 上线时多数 run 并无登记簿）；
5. **不 import 节点/图**：只依赖 ``contracts``（typed payload）与 ``core``，避免 import 环。

**期次说明（重要，勿误判为死代码）**：假设登记簿的**生产者**（``conflicts.py`` 探索阶段登记
``active``、``verification.py`` 结算层重建为 ``invalidated`` / ``confirmed``）与 gate 消费者
（``gates.py`` 的 ``assumption_based_claim`` 检查）**顺延至 F1c 阶段**——PR4 的期次口径明确排除它们，
且 ``deviation.detection.enabled`` 开关在 F2 才落地，塞进 F1 会让"一键回滚"成为空头承诺。
因此 F1 期间 ``assumption_still_valid`` **没有输入**——但它本身是**活的**：只要 state 里存在含
``status="invalidated"`` 条目的登记簿 artifact，它就会产出 D4 偏离（见
``tests/orchestrator/test_detectors.py::test_assumption_still_valid_triggers_on_invalidated_entry``）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from alphabee.core.schemas import Artifact, ArtifactType, DeviationClass, Issue, IssueScope, IssueSeverity, Step
from alphabee.orchestrator.contracts import (
    AnomalyReportArtifact,
    AssumptionRegistryArtifact,
    ConflictAnalysisArtifact,
    DerivedFactsArtifact,
    FactCollectionArtifact,
    InsightArtifact,
    ReportArtifact,
    SignalAnalysisArtifact,
    ThesisArtifact,
    VerificationArtifact,
    find_artifact_model,
)
from alphabee.orchestrator.services.deviation import record_deviation

logger = logging.getLogger(__name__)

__all__ = [
    "DETECTORS",
    "NON_VERDICT_MAKERS",
    "VERDICT_MAKERS",
    "DetectionResult",
    "Detector",
    "NodeContext",
    "assumption_still_valid",
    "artifact_schema_valid",
    "derived_facts_nonempty",
    "detector",
    "DETECTOR_CATEGORIES",
    "detector_categories",
    "downstream_inputs_present",
    "evidence_refs_present",
    "insight_artifacts_present",
    "registry_artifact_present",
    "run_detectors",
    "scope_for_node",
]


@dataclass(frozen=True)
class NodeContext:
    """检测器入参：本节点的步、本节点新增产物、以及 ``state ⊕ update`` 的只读合并视图。"""

    node_id: str
    step: Step | None
    new_artifacts: list[Artifact] = field(default_factory=list)
    view: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DetectionResult:
    """检测结果：``passed=True`` 即无偏离；否则由 :func:`run_detectors` 转成 ``Issue``。"""

    detector: str
    passed: bool
    deviation_class: DeviationClass | None = None
    severity: IssueSeverity = IssueSeverity.LOW
    category: str = ""  # 稳定名（不参与指纹的随机部分）
    message: str = ""
    related_artifact: str | None = None


Detector = Callable[[NodeContext], DetectionResult]

#: 检测器注册表（契约 ``detectors`` 字段引用的名字必须在这里登记，§14.2-A 断言 4）。
DETECTORS: dict[str, Detector] = {}

#: 检测器内部名 → 稳定 category（``Issue.category``，供 F0 分类法解析）。
_DETECTOR_CATEGORIES: dict[str, str] = {}
#: 公开别名（同一对象）：下游（F5 telemetry / 覆盖守卫）需要的 name→category 权威视图。
DETECTOR_CATEGORIES: dict[str, str] = _DETECTOR_CATEGORIES


def detector(name: str, category: str) -> Callable[[Detector], Detector]:
    """注册装饰器：把函数登记进 :data:`DETECTORS`，并绑定稳定 ``category``。"""

    def _register(func: Detector) -> Detector:
        DETECTORS[name] = func
        _DETECTOR_CATEGORIES[name] = category
        return func

    return _register


def detector_categories() -> dict[str, str]:
    """只读快照：``检测器名 → 稳定 category``（供覆盖守卫/CI 断言使用，勿修改返回值）。"""
    return dict(_DETECTOR_CATEGORIES)


#: 可校验 artifact 类型 → typed model（仅登记**有 typed 契约**的类型；其余跳过，不误报 D2）。
_ARTIFACT_MODELS: dict[ArtifactType, type] = {
    ArtifactType.FACT_COLLECTION: FactCollectionArtifact,
    ArtifactType.DERIVED_FACTS: DerivedFactsArtifact,
    ArtifactType.SIGNAL_ANALYSIS: SignalAnalysisArtifact,
    ArtifactType.ANOMALY_REPORT: AnomalyReportArtifact,
    ArtifactType.CONFLICT_ANALYSIS: ConflictAnalysisArtifact,
    ArtifactType.VERIFICATION_RESULTS: VerificationArtifact,
    ArtifactType.INSIGHT_ANALYSIS: InsightArtifact,
    ArtifactType.THESIS_ANALYSIS: ThesisArtifact,
    ArtifactType.REPORT: ReportArtifact,
    ArtifactType.ASSUMPTION_REGISTRY: AssumptionRegistryArtifact,
}

#: 报告生成所需的下游段落（§6.2 ``downstream_inputs_present``）。
_REPORT_SECTIONS: tuple[str, ...] = ("thesis", "insight", "anomaly", "conflict_analysis")

#: 必须带证据引用的**结论性** Decision maker（§6.2：维度 verdict 对应的 Decision 必须带
#: ``based_on`` / ``evidence_refs``）。用**闭合集合**而非子串猜测，避免把普通中间结论误判成 D3。
#: 前瞻项：``amplification_audit`` 的生产者随 F3（§14.4）落地，当前无 `maker=` 字面量。
VERDICT_MAKERS: frozenset[str] = frozenset(
    {
        "thesis_reviewer",  # 逐维度 verdict（agent.py 已带 based_on=review_evidence_ids）
        "amplification_audit",  # F3：加权方向一致性审计结论（前瞻登记项）
    }
)

#: 明确**非** verdict 的 maker（中间结论 / 环境打分 / 报告门决策）。
#: 与 :data:`VERDICT_MAKERS` 一起构成**完备分类**：全仓每个 ``maker=`` 字面量都必须落在两者之一，
#: 未归类即由 ``test_detectors.py`` 的全仓守卫报红 —— 防"新增 verdict 产出者却被静默漏检"。
NON_VERDICT_MAKERS: frozenset[str] = frozenset(
    {
        "conflict_verifier",  # 假设"已排除"记录，非维度 verdict
        "report_quality_gate",  # 报告门决策
        "market_score_engine",  # 市场分（环境打分）
    }
)

#: 向后兼容别名（内部旧引用）。
_VERDICT_MAKERS: frozenset[str] = VERDICT_MAKERS


def _passed(name: str) -> DetectionResult:
    return DetectionResult(detector=name, passed=True)


def _failed(
    name: str,
    deviation_class: DeviationClass,
    severity: IssueSeverity,
    message: str,
    *,
    related_artifact: str | None = None,
) -> DetectionResult:
    return DetectionResult(
        detector=name,
        passed=False,
        deviation_class=deviation_class,
        severity=severity,
        category=_DETECTOR_CATEGORIES.get(name, name),
        message=message,
        related_artifact=related_artifact,
    )


def registry_artifact_present(view: dict[str, Any] | None) -> bool:
    """本 run 是否已存在假设登记簿 artifact（不存在 = 生产者顺延/未登记 → 一切相关检查 no-op）。"""
    artifacts = (view or {}).get("artifacts") or []
    return any(getattr(artifact, "type", None) == ArtifactType.ASSUMPTION_REGISTRY for artifact in artifacts)


def _fact_values_empty(view: dict[str, Any] | None) -> bool:
    return not ((view or {}).get("fact_values") or {})


def scope_for_node(node_id: str) -> IssueScope:
    """按节点所属阶段标注 ``Issue.scope``（§4 既有枚举，不新增成员）。

    * 数据类节点 → ``DATA``（采集/引擎/冲突/验证）；
    * 论证类节点 → ``REPORT``（insight/thesis 及其评审——属于结论与表达阶段）；
    * 报告 gate/输出类节点 → ``REVIEW`` / ``REPORT``。
    """
    if node_id in {"synthesize_insights", "run_thesis", "review_thesis"}:
        return IssueScope.REPORT
    if node_id in {"review_report", "generate_report"}:
        return IssueScope.REVIEW
    return IssueScope.DATA


# ── 检测器 1：derived_facts_nonempty（§6.2，D1） ─────────────────────────────


@detector("derived_facts_nonempty", "derived_facts_empty")
def derived_facts_nonempty(ctx: NodeContext) -> DetectionResult:
    """``DerivedFactsArtifact.results`` 非空；空则区分"真无数据"与"引擎故障"（§16 反模式模板）。"""
    name = "derived_facts_nonempty"
    artifact = find_artifact_model(ctx.new_artifacts, ArtifactType.DERIVED_FACTS, DerivedFactsArtifact)
    if artifact is None or artifact.results:
        return _passed(name)
    if _fact_values_empty(ctx.view):
        # 产物为空 + 上游 fact_values 同样为空 → 可能真的没有数据：低严重度，不误判引擎故障
        return _failed(
            name,
            DeviationClass.D1_DATA,
            IssueSeverity.LOW,
            "derived facts 为空，且上游 fact_values 同样为空（可能确实无可用数据，需人工确认口径）",
        )
    return _failed(
        name,
        DeviationClass.D1_DATA,
        IssueSeverity.HIGH,
        f"derived facts 为空但 fact_values 非空（{len(ctx.view.get('fact_values') or {})} 项）→ 疑似引擎故障",
    )


# ── 检测器 2：artifact_schema_valid（§6.2，D2） ─────────────────────────────


@detector("artifact_schema_valid", "artifact_schema_invalid")
def artifact_schema_valid(ctx: NodeContext) -> DetectionResult:
    """本节点新增 artifact 均可用其 typed model 校验；无 typed 契约的类型跳过（不误报）。"""
    name = "artifact_schema_valid"
    for artifact in ctx.new_artifacts:
        artifact_type = getattr(artifact, "type", None)
        model = _ARTIFACT_MODELS.get(artifact_type) if artifact_type is not None else None
        if model is None:
            continue  # 未登记 typed 契约的类型 → 跳过
        payload = getattr(artifact, "value", None)
        if payload is None:
            continue
        try:
            model.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 校验失败即偏离，不向上抛
            return _failed(
                name,
                DeviationClass.D2_STRUCTURE,
                IssueSeverity.MEDIUM,
                f"artifact schema 校验失败（{artifact_type}）：{type(exc).__name__}: {exc}",
                related_artifact=getattr(artifact, "id", None),
            )
    return _passed(name)


# ── 检测器 3：insight_artifacts_present（§6.2，D2） ─────────────────────────


@detector("insight_artifacts_present", "insight_missing")
def insight_artifacts_present(ctx: NodeContext) -> DetectionResult:
    """``synthesize_insights`` 出口断言：``INSIGHT_ANALYSIS`` 存在且 ``core_view`` 非空。"""
    name = "insight_artifacts_present"
    artifact = find_artifact_model(ctx.new_artifacts, ArtifactType.INSIGHT_ANALYSIS, InsightArtifact)
    if artifact is None:
        return _failed(name, DeviationClass.D2_STRUCTURE, IssueSeverity.MEDIUM, "未产出 insight artifact")
    if not (artifact.core_view or "").strip():
        return _failed(
            name,
            DeviationClass.D2_STRUCTURE,
            IssueSeverity.MEDIUM,
            "insight artifact 的 core_view 为空（四级降级应当已给出确定性兜底内容）",
        )
    return _passed(name)


# ── 检测器 4：evidence_refs_present（§6.2，D3） ─────────────────────────────


@detector("evidence_refs_present", "verdict_without_evidence")
def evidence_refs_present(ctx: NodeContext) -> DetectionResult:
    """结论性 Decision 必须带 ``basis``/``evidence_refs``（ROADMAP P0 项的检测器化）。

    **已知限制（显式非目标）**：§14.2-B 要求检查「**本节点新增** Decision」，而本实现的扫描面是
    ``ctx.view["decisions"]`` —— **视图级**（本 run 的全量合并视图，仅按 ``maker`` 过滤）。差异与方向：

    * **差异**：本节点**之外**的节点产出的 verdict 也会被计入；
    * **方向：过报（误归属）** —— 「别的节点产的无证据 verdict」会在**本节点**被判 D3，
      且 ``Issue.detected_at_step`` 记为**检测节点**；
    * **根因**：``Decision`` 无节点关联字段，且 ``NodeContext``（§14.2-C）只提供 ``new_artifacts``
      ⇒ 现接口**无法**区分"本次新增"的 decision；
    * **处置**：本轮**只披露、不改行为**（改动会连带影响 :func:`assumption_still_valid` 的
      Decision 侧口径，属 R2-4 已裁定的"后续期"范围）。
    * **F3 后必须复核**：``run_thesis`` 亦声明了本检测器，而 F3 将引入 ``amplification_audit``
      生产者 ⇒ 跨节点误归属届时**成活**并污染 F5 的 per-node 画像。正解是给 ``NodeContext``
      增补 ``new_decisions``（包装器取 ``list(update.get("decisions") or [])``，与 ``new_artifacts``
      同模式），届时本检测器与 ``assumption_still_valid`` 应一并切换。
    """
    name = "evidence_refs_present"
    decisions = ctx.view.get("decisions") or []
    verdicts = [
        decision for decision in decisions if (getattr(decision, "maker", "") or "").strip().lower() in VERDICT_MAKERS
    ]
    if not verdicts:
        return _passed(name)
    missing = [
        decision
        for decision in verdicts
        if not (getattr(decision, "based_on", None) or getattr(decision, "evidence_refs", None))
    ]
    if not missing:
        return _passed(name)
    return _failed(
        name,
        DeviationClass.D3_ARGUMENT,
        IssueSeverity.MEDIUM,
        f"{len(missing)}/{len(verdicts)} 条结论性 Decision 缺少证据引用（based_on / evidence_refs 均为空）",
    )


# ── 检测器 5：downstream_inputs_present（§6.2，D1/D2） ──────────────────────


@detector("downstream_inputs_present", "report_input_missing")
def downstream_inputs_present(ctx: NodeContext) -> DetectionResult:
    """``generate_report`` 出口：报告载荷的 thesis / insight / anomaly / conflict 四段非空。"""
    name = "downstream_inputs_present"
    try:
        from alphabee.orchestrator.services.payload_builders import build_report_generation_payload
    except Exception as exc:  # noqa: BLE001 - 载荷构建不可用 → 跳过（不误报）
        logger.warning("downstream_inputs_present: payload builder unavailable: %s", exc)
        return _passed(name)

    try:
        payload = build_report_generation_payload(dict(ctx.view))
    except Exception as exc:  # noqa: BLE001 - 构建失败不是"缺段"，跳过
        logger.warning("downstream_inputs_present: payload build failed (skipped): %s", exc)
        return _passed(name)

    missing = [section for section in _REPORT_SECTIONS if not getattr(payload, section, None)]
    if not missing:
        return _passed(name)
    return _failed(
        name,
        DeviationClass.D1_DATA,
        IssueSeverity.MEDIUM,
        f"报告载荷缺少下游输入段：{'、'.join(missing)}（应以显式降级分支处理，不得静默省略）",
    )


# ── 检测器 6：assumption_still_valid（§6.2，D4） ────────────────────────────


def _node_payloads_referencing(ctx: NodeContext, assumption_ids: set[str]) -> set[str]:
    """本节点**本次新增载荷**中出现的假设 id（§14.2-B「被本节点引用」）。

    扫描面 = ``ctx.new_artifacts``；**排除 ``ASSUMPTION_REGISTRY`` 自身载荷**（其天然含全部
    假设 id，不排除会自命中、把"登记"误判成"引用"）。判定 = 序列化后 **id 包含判定**，
    不解析语义、不做模糊匹配（假设 id 为 uuid 形态，误命中可忽略）。
    """
    referenced: set[str] = set()
    for artifact in ctx.new_artifacts:
        if getattr(artifact, "type", None) == ArtifactType.ASSUMPTION_REGISTRY:
            continue
        blob = json.dumps(getattr(artifact, "value", None), ensure_ascii=False, default=str)
        referenced.update(aid for aid in assumption_ids if aid and aid in blob)
    return referenced


@detector("assumption_still_valid", "assumption_invalidated")
def assumption_still_valid(ctx: NodeContext) -> DetectionResult:
    """假设登记簿里是否存在**已被证伪**的假设（§6.3）。

    **注册表缺失 = 严格 no-op（判 pass）**：F1 上线时生产者刚落地，多数 run 没有登记簿，
    若此时报偏离就会全链路误报 D4。

    **"被本节点引用"（§14.2-B）**：只有"登记簿存在 **且** 其中 ``invalidated`` 假设的 id 出现在
    **本节点本次新增载荷**中"才算状态偏离 —— 节点不依赖该假设时不得报 D4（§16 反模式：误报）。
    判定见 :func:`_node_payloads_referencing`。
    **已知限制（显式非目标）**：本节点新增 ``Decision`` 的 ``evidence_refs``/``based_on`` **未纳入**
    扫描面 —— ``Decision`` 无节点关联字段、包装器亦不提供"本次新增 decisions"，故方向取
    **保守（少报而非误报）**；后续期如需支持，须给 ``NodeContext`` 增补 ``new_decisions``。

    **生产者顺延至 F1c**（见模块 docstring"期次说明"）：F1 期间本检测器无输入，但并非死代码——
    ``test_assumption_still_valid_triggers_on_invalidated_entry`` 用"构造含注册表的 state → 触发 D4"
    证明其逻辑是活的。
    """
    name = "assumption_still_valid"
    if not registry_artifact_present(ctx.view):
        return _passed(name)
    registry = find_artifact_model(
        ctx.view.get("artifacts") or [],
        ArtifactType.ASSUMPTION_REGISTRY,
        AssumptionRegistryArtifact,
    )
    if registry is None:
        return _passed(name)  # 载荷不可解析 → 交给 artifact_schema_valid，不在此越权判定
    invalidated = [entry for entry in registry.invalidated if entry.id]
    if not invalidated:
        return _passed(name)
    referenced = _node_payloads_referencing(ctx, {entry.id for entry in invalidated})
    if not referenced:
        return _passed(name)  # 本节点未引用任何已证伪假设 → 不报（§14.2-B）
    invalidated = [entry for entry in invalidated if entry.id in referenced]
    statements = "；".join(entry.statement or entry.id for entry in invalidated[:3])
    return _failed(
        name,
        DeviationClass.D4_STATE,
        IssueSeverity.MEDIUM,
        f"存在 {len(invalidated)} 条已证伪假设仍处于登记簿中：{statements}（不得再作为论证前提，除非显式引用反驳证据）",
    )


# ── 执行器 ─────────────────────────────────────────────────────────────────


def run_detectors(contract: Any, ctx: NodeContext) -> list[Issue]:
    """执行契约声明的检测器，返回新增 ``Issue`` 列表（fail-open：异常只 warning + 跳过）。

    单个检测器异常**不影响**同节点其他检测器；检测器**不修改** ``ctx``（只读）。
    ``Issue.scope`` 按节点所属阶段标注（数据/论证/报告控制），便于 F5 按阶段聚合偏离画像。
    """
    names = list(getattr(contract, "detectors", None) or [])
    scope = scope_for_node(ctx.node_id)
    issues: list[Issue] = []
    for name in names:
        func = DETECTORS.get(name)
        if func is None:
            logger.warning("detector %r 未在 DETECTORS 注册表中（node=%s）", name, ctx.node_id)
            continue
        try:
            result = func(ctx)
        except Exception as exc:  # noqa: BLE001 - 检测器必须可失败，绝不打断节点
            logger.warning("detector %r 执行失败（node=%s, fail-open）: %s", name, ctx.node_id, exc)
            continue
        if result.passed:
            continue
        issues.append(
            record_deviation(
                result.deviation_class or DeviationClass.D2_STRUCTURE,
                result.severity,
                result.message,
                detected_at_step=ctx.node_id,
                related_step=ctx.node_id,
                related_artifact=result.related_artifact,
                category=result.category or name,
                scope=scope,
                recovery_action="detected",
            )
        )
    return issues
