"""节点契约登记表：每节点的前置/后置条件、出口检测器、恢复阶梯与放大标注（F1a / §14.2-A）。

本模块是「偏离分类 → 即时检测 → 阶梯恢复 → 放大审计 → 预算升级」协议里**契约层**的唯一所有者：

* :class:`AmplificationMode` —— 放大标注三态（§8）：``VERBATIM``（α=1 转述）/
  ``WEIGHTED``（α>1 加权，必须被审计）/ ``OVERRIDE``（可推翻上游）；
* :class:`NodeContract` —— 单节点契约（§6.1）：前置条件、后置条件、出口检测器名、
  允许的恢复阶梯 Tier（§7.1）、消费边的放大标注（§8.1）、回环预算；
* :data:`NODE_CONTRACTS` —— 全部流水线节点登记（键集 == :data:`NODE_ORDER`，现 16 条，含 F0b 的 run 尾部账本 sink ``record_deviations``）；
* :data:`AMPLIFICATION_AUDIT` —— 每条 ``WEIGHTED`` 边的审计覆盖表（§8.1「审计要求」列的代码投影，
  由 F3 的 ``audit_amplification`` 实装）；
* :data:`NODE_BUDGETS` —— 回环预算与 ``OrchestratorState`` 计数器的绑定（§14.2-A 断言 2）；
* :func:`get_contract` / :func:`validate_contracts` —— 查询与契约自检（CI 断言）。

契约自检（:func:`validate_contracts`，§14.2-A / §15.7）覆盖：

1. ``set(NODE_CONTRACTS) == set(NODE_ORDER)``，且与 ``agent.py`` **图 builder 实际注册的节点与边**一致
   （读源码 AST，**不调** ``get_graph()``：编译图会剪掉源节点暂不可达的边，例如
   ``review_report → record_deviations``，那正是 F0b 记在案的坑）；
2. ``max_retries > 0`` 的节点必须在 ``OrchestratorState`` 有对应计数器（``supplement_round`` /
   ``report_review_round``），且**不得超过**对应的 ``max_*_rounds`` 硬上限（超限 = 未登记预算，CI 报红）；
3. ``recovery_ladder`` 取值域合法（:data:`RECOVERY_TIERS`）、无重复，且 ``max_retries>0``
   ⇒ 阶梯必须含 Tier 4（受控回环，§7.2 规则 4）；
4. 每条 ``WEIGHTED`` 边在 :data:`AMPLIFICATION_AUDIT` 中有审计条目，审计节点必须已登记契约、
   且与消费方**不同源**（§8.2 规则 2：加权发生地不能自己给自己开绿灯），审计动作、权重、
   §8.1 边集齐备性一并核对；
5. 每个 ``detectors`` 名都在 ``detectors.py`` 的 ``DETECTORS`` 注册表中存在
   （F1b 落地前整体延后，见 :func:`_detector_module_available`）。

工程约束（§14.0）：

* **无 IO、无配置读取**：只在 import 期构造不可变声明数据，不触发 LLM/DB/网络；
* **不 import agent**：图结构经 AST 从 ``agent.py`` 源码读取，避免 import 期 ``StateGraph``
  构图与 tushare token 副作用；
* 契约是**只读声明**，不参与运行时行为决策；F1b 的 ``with_deviation_detection`` 只消费它。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel, Field

from alphabee.orchestrator.services.deviation import NODE_ORDER

__all__ = [
    "AMPLIFICATION_AUDIT",
    "NODE_BUDGETS",
    "NODE_CONTRACTS",
    "RECOVERY_TIERS",
    "AmplificationAuditBinding",
    "AmplificationMode",
    "NodeBudget",
    "NodeContract",
    "get_contract",
    "validate_contracts",
]

_AGENT_SOURCE = Path(__file__).with_name("agent.py")
_COLLECTORS_SOURCE = Path(__file__).with_name("collectors.py")
_GRAPH_VAR = "_graph"
_DETECTORS_MODULE = "alphabee.orchestrator.detectors"


# ── 枚举与模型（§6.1 / §8） ─────────────────────────────────────────────────


class AmplificationMode(StrEnum):
    """上游 artifact → 下游消费这条边的传输模式（§8）。"""

    VERBATIM = "verbatim"  # α=1：下游只搬运，不改判断方向
    WEIGHTED = "weighted"  # α>1：下游用上游的量去乘/扣/投，改变判断强度 → 必须有审计
    OVERRIDE = "override"  # 方向可变：下游可以推翻上游


#: §7 定义的全部恢复阶梯 Tier（0 完整通过 / 1 局部修复 / 2 降级产出 / 3 骨架跳过 /
#: 4 受控回环 / 5 升级）。契约只能声明该取值域内的 Tier。
RECOVERY_TIERS: tuple[int, ...] = (0, 1, 2, 3, 4, 5)


class NodeContract(BaseModel):
    """单节点契约（§6.1）：出/入口断言 + 检测器 + 阶梯 + 放大标注 + 回环预算。"""

    node_id: str
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    detectors: list[str] = Field(default_factory=list)  # detectors.py 注册名
    recovery_ladder: tuple[int, ...] = ()  # §7.1 允许的 Tier；值域见 RECOVERY_TIERS
    amplification_labels: dict[str, AmplificationMode] = Field(default_factory=dict)
    max_retries: int = 0  # 0 = 禁止 Tier 4（§7.2 规则 4）


@dataclass(frozen=True)
class NodeBudget:
    """回环预算与 ``OrchestratorState`` 计数器的绑定（§14.2-A 断言 2）。"""

    node_id: str
    counter: str  # 本 run 已用轮次，例如 "supplement_round"
    max_counter: str  # 硬上限字段，例如 "max_supplement_rounds"
    max_value: int  # 硬上限的默认值（从 state builder 源码读取）


@dataclass(frozen=True)
class AmplificationAuditBinding:
    """一条 ``WEIGHTED`` 边的审计要求（§8.1「审计要求」列的代码投影）。"""

    edge: str
    auditor: str  # 必须已登记契约的审计节点
    check: str  # 审计动作名（F3 的 audit_amplification 分支）
    weight: str  # 显式权重（§8.2 规则 1：权重不得埋在 prompt 里）
    requirement: str  # §8.1 原文要求（CI 失败时用于定位设计条目）


# ── 契约登记（16 节点；阶梯值见 §7.1，放大标注见 §8.1） ─────────────────────

NODE_CONTRACTS: dict[str, NodeContract] = {
    "collect_raw_facts": NodeContract(
        node_id="collect_raw_facts",
        preconditions=[
            "run 已建立且 symbol / query 可用",
            "数据源适配器可用（不可用只能降级，不得伪造数值）",
        ],
        postconditions=[
            "fact_values / financial_facts / market_facts 已写入 state",
            "结构化数据缺失时显式产 missing_data issue，而非静默返回空",
            "有界补充采集最多一次：supplement_round 消耗后必须带 gap 收口",
        ],
        # §6.2 未给本节点配检测器（数据缺失已由节点自身 issue 上报），显式留空避免「检测器比业务重」。
        detectors=[],
        recovery_ladder=(0, 4, 2),  # §7.1：数据缺失优先补采一次，仍缺则降级带 gap
        amplification_labels={},
        max_retries=1,  # §7.1：1 次补充采集
    ),
    "resolve_industry_context": NodeContract(
        node_id="resolve_industry_context",
        preconditions=["fact_values 已提供行业/公司标识", "行业知识资产可读（不可用走降级）"],
        postconditions=[
            "IndustryContextArtifact schema 合法（find_artifact_model 可校验）",
            "行业基准缺失时显式产 industry_benchmarks_missing issue 并标注 degraded",
        ],
        detectors=["artifact_schema_valid"],
        recovery_ladder=(0, 2, 3),
        amplification_labels={},
    ),
    "resolve_company_track": NodeContract(
        node_id="resolve_company_track",
        preconditions=["fact_values 已提供公司标识", "公司跟踪库可读（不可用走降级）"],
        postconditions=[
            "CompanyStateArtifact schema 合法",
            "跟踪缺失或陈旧时显式产 company_track_missing / company_track_stale issue",
        ],
        detectors=["artifact_schema_valid"],
        recovery_ladder=(0, 2, 3),
        amplification_labels={},
    ),
    "resolve_driver_profile": NodeContract(
        node_id="resolve_driver_profile",
        preconditions=["fact_values 已提供公司标识", "驱动因子画像资产可读（不可用走降级）"],
        postconditions=[
            "DriverProfile schema 合法",
            "画像不可用即显式降级（degraded=true + driver_profile_degraded issue）",
        ],
        detectors=["artifact_schema_valid"],
        recovery_ladder=(0, 2, 3),
        amplification_labels={},
    ),
    "run_analysis_engines": NodeContract(
        node_id="run_analysis_engines",
        preconditions=["fact_values 非空且键为 canonical 字段名"],
        postconditions=[
            "DerivedFactsArtifact.results 与 SignalAnalysisArtifact.results 非空（真无数据必须显式区分）",
            "空 derived facts 必须在出口立即报 D1，而不是等到 report gate",
        ],
        detectors=["derived_facts_nonempty", "artifact_schema_valid"],
        recovery_ladder=(0, 2, 3),  # §7.1：确定性引擎无重试价值，缺数据降级
        # §8.1 第 2 行：anomaly 投影回 fact_values 后会触发 signal 规则（α>1）
        amplification_labels={"anomaly->fact_values->signal": AmplificationMode.WEIGHTED},
    ),
    "explore_conflicts": NodeContract(
        node_id="explore_conflicts",
        preconditions=["fact_values / derived facts 可用（缺任一仍可探索，但结论必须标注不确定）"],
        postconditions=[
            "ConflictAnalysisResult schema 合法",
            "探索失败不得阻断 run：降级为无冲突记录并显式标注",
            "本节点新登记的 AssumptionEntry 状态为 active",
        ],
        detectors=["artifact_schema_valid"],
        recovery_ladder=(0, 2, 3),
        amplification_labels={},
    ),
    "verify_hypotheses": NodeContract(
        node_id="verify_hypotheses",
        preconditions=["explore_conflicts 证据存在或显式为空", "被验证假设已登记（active）"],
        postconditions=[
            "VerificationResultItem 逐条带 settled 状态（verified / partial / rejected）",
            "验证失败保持 unknown 语义（不得默认 verified）",
            "证伪某假设时登记 invalidated 并写 D4 偏离",
        ],
        detectors=["artifact_schema_valid"],
        recovery_ladder=(0, 1, 2),
        amplification_labels={},
    ),
    "synthesize_insights": NodeContract(
        node_id="synthesize_insights",
        preconditions=[
            "anomaly / conflict / verification 结果已就绪（缺失即标注缺失，不静默跳过）",
            "假设登记簿可读（用于剔除已 invalidated 的前提）",
        ],
        postconditions=[
            "InsightAnalysisArtifact 存在且 core_view 非空",
            "Tier>=2 降级产物带 degraded=true + degradation_reason（§7.2 规则 1）",
            "已 invalidated 的假设不得再作为核心观点前提（除非显式引用反驳证据）",
        ],
        detectors=["insight_artifacts_present", "artifact_schema_valid", "assumption_still_valid"],
        recovery_ladder=(0, 1, 2, 3),  # §7.1：现状四级降级已实现，此处仅协议化声明
        amplification_labels={},
    ),
    "run_thesis": NodeContract(
        node_id="run_thesis",
        preconditions=[
            "InsightAnalysisArtifact 存在且 core_view 非空",
            "signal results 可读（放大方向审计需要）",
        ],
        postconditions=[
            "ThesisArtifact 逐维度有 verdict 与 confidence",
            "LLM 增强失败退回引擎确定性结果，不得产空 thesis",
            "消费 degraded insight 时 confidence 保守化（×0.85 一档封顶，§7.2 规则 2）",
            "本节点产出的 Decision 必须带 evidence_refs / based_on（D3 断言）",
        ],
        detectors=["evidence_refs_present", "artifact_schema_valid", "assumption_still_valid"],
        recovery_ladder=(0, 1, 2),  # §7.1：引擎确定性为主，LLM 增强失败退引擎结果
        # §8.1 前两行：α>1 的乘法都发生在 thesis 引擎内（insight 的 confidence 乘法、
        # verified conflict 的维度扣分），消费方在此声明、审计落在下游 review 节点。
        amplification_labels={
            "insight->thesis": AmplificationMode.WEIGHTED,
            "verified_conflict->dimension_score": AmplificationMode.WEIGHTED,
        },
    ),
    "review_thesis": NodeContract(
        node_id="review_thesis",
        preconditions=[
            "ThesisArtifact 存在且至少一个维度 verdict",
            "conflicts 结果可读（缺失必须显式记录，不得默认无冲突）",
        ],
        postconditions=[
            "逐维度产出 Decision 且带 evidence_refs（verdict 类结论无证据即 D3）",
            "审计 insight → thesis 加权方向与 signal / conflict 证据方向是否一致，落 Decision(maker=amplification_audit)",
            "审计 verified conflict → 维度扣分的一致性（扣分幅度 vs 冲突严重度）",
            "论点与已验证冲突矛盾时报 thesis_conflict（D3）而不是静默通过",
        ],
        detectors=["evidence_refs_present", "artifact_schema_valid"],
        recovery_ladder=(0, 1, 2),
        # 评审对 thesis 有裁决权（可推翻上游加权结论），因此该边在本节点是 OVERRIDE 而非 WEIGHTED；
        # 加权发生在上游 run_thesis，审计落在本节点（§8.1 第 1 行）。
        amplification_labels={"insight->thesis": AmplificationMode.OVERRIDE},
    ),
    "resolve_midterm_decision": NodeContract(
        node_id="resolve_midterm_decision",
        preconditions=["已过 thesis 评审", "CompanyStateArtifact 上游输入存在"],
        postconditions=[
            "状态迁移单调（不得回到更早阶段），迁移理由写入 artifact",
            "决策失败只降级为 midterm_decision_failed，不阻断 run",
            "行动类输出永不自动执行（§9.4：只允许 Tier 0 或 Tier 5）",
        ],
        detectors=["artifact_schema_valid"],
        recovery_ladder=(0, 5),  # §7.1 宏观环行动建议行：仓位/状态迁移只允许 0 或 5
        amplification_labels={},
    ),
    "midterm_decision_reporter": NodeContract(
        node_id="midterm_decision_reporter",
        preconditions=["CompanyStateArtifact 存在且至少一条状态迁移记录"],
        postconditions=[
            "Step 记入决策摘要（完整落日志，暂不进报告）",
            "渲染失败降级为 midterm_decision_report_failed issue，不阻断 run",
            "行动类输出永不自动执行（§9.4）",
        ],
        detectors=[],
        recovery_ladder=(0, 5),
        amplification_labels={},
    ),
    "generate_report": NodeContract(
        node_id="generate_report",
        preconditions=[
            "ThesisArtifact 存在",
            "InsightAnalysisArtifact 存在",
            "report payload 的 thesis / insight / anomaly / conflict 四段可读（空段必须显式降级）",
        ],
        postconditions=[
            "ReportOutput schema 合法（find_artifact_model 可校验）",
            "下游输入缺失段以显式降级分支处理，并在正文标注缺失",
            "report_review_round 触顶后不得再回环（预算耗尽 → escalate）",
        ],
        detectors=["downstream_inputs_present", "artifact_schema_valid"],
        recovery_ladder=(0, 2, 4),  # §7.1：现状已实现，此处协议化声明
        amplification_labels={
            # §8.1 第 4 行：core_view 主导全文表达（α 由观点强度决定），审计落在 review_report
            "insight->report": AmplificationMode.WEIGHTED,
            "thesis->report": AmplificationMode.VERBATIM,
            "conflict->report": AmplificationMode.VERBATIM,
        },
        max_retries=2,  # §7.1：<=2（= max_report_review_rounds）
    ),
    "review_report": NodeContract(
        node_id="review_report",
        preconditions=["ReportOutput 存在", "本 run 的 decisions / issues 可读"],
        postconditions=[
            "产出 EvaluationAssessment 且含 cross_source_consistency 判定",
            "需要重写时 report_review_round 递增并受 max_report_review_rounds 约束",
            "审计 insight → report 主线与 thesis 判断方向是否一致（§8.1 第 4 行）",
            "重写请求以 report_rewrite_needed / report_rewrite_reason 显式表达",
        ],
        detectors=["artifact_schema_valid"],
        recovery_ladder=(0, 2),  # 与生成侧同属报告阶段；回环预算登记在 generate_report
        # report gate 对本 run 的最终报告有裁决权（可要求重写后在下一轮替换），
        # 因此 report / verdict → 最终产物这条边在本节点是 OVERRIDE；
        # insight → report 的加权消费在 generate_report，审计落在本节点（§8.1 第 4 行）。
        amplification_labels={
            "report->final_report": AmplificationMode.OVERRIDE,
            "report_gate_verdict->final_report": AmplificationMode.OVERRIDE,
        },
    ),
    "record_deviations": NodeContract(
        node_id="record_deviations",
        preconditions=["state['issues'] 可读（本节点只读该字段）"],
        postconditions=[
            "账本写入 fail-open：任何异常只 warning，绝不打断 run",
            "不得修改 state['issues']（只写自己的 Step）",
            "written + failed + skipped == 本 run issue 数",
        ],
        detectors=[],
        recovery_ladder=(0,),  # F0b 约定：账本写入失败只记 Step，不消费预算、不降级产物
        amplification_labels={},
    ),
    "finalize_message": NodeContract(
        node_id="finalize_message",
        preconditions=["run 对象存在", "最终报告 artifact 可定位"],
        postconditions=[
            "产出的 AIMessage 含最终 JSON 负载",
            "序列化失败必须显式暴露，不得产出空消息",
        ],
        detectors=[],
        recovery_ladder=(0,),
        amplification_labels={},
    ),
}


def get_contract(node_id: str | None) -> NodeContract | None:
    """按节点名取契约；未登记节点/空值 → ``None``（不猜测、不兜底造契约）。"""
    if not node_id:
        return None
    return NODE_CONTRACTS.get(node_id)


# ── 放大审计覆盖表（§8.1；F3 实装，本表是 CI 可执行投影） ─────────────────────
#
# **两个面必须分开读（F3-1 结转，t64 登记）**：
#
# * ``auditor`` —— **设计面**（§8.1 表「审计要求」列指定的节点）。取值受 F1 已认证测试
#   ``tests/orchestrator/test_node_contracts.py::_EXPECTED_AUDITORS`` 钉住，**不得改动**；
# * ``requirement`` 里标注的 **实际发射点** —— **实现面**（哪个节点真正产出
#   ``Decision(maker="amplification_audit")``）。F3 把§8.1 **四条边**的审计分支统一实现在
#   ``reviewer.audit_amplification``，并由 ``review_thesis`` 节点发射 Decision。
#
# 因此对 ``anomaly->fact_values->signal`` 与 ``insight->report`` 两条边：``auditor`` 写的是
# ``review_report``，而其 ``review_report`` 侧**零接线**（该节点不产出 audit Decision）——
# 已在对应条目的 ``requirement`` 中**逐条显式登记**为**具名顺延项**（F4/F5 结转），
# 避免下游读者或 F5 的 per-node 画像误以为这两条边由 ``review_report`` 审计。

AMPLIFICATION_AUDIT: dict[str, AmplificationAuditBinding] = {
    "insight->thesis": AmplificationAuditBinding(
        edge="insight->thesis",
        auditor="review_thesis",
        check="weighted_direction_consistency",
        weight="insight.confidence 分档乘法 {high: 1.0, medium: 0.92, low: 0.85}（INSIGHT_CONFIDENCE_WEIGHTS）",
        requirement="review_thesis 必须检查加权方向是否与信号/冲突证据一致（现状是盲传）",
    ),
    "anomaly->fact_values->signal": AmplificationAuditBinding(
        edge="anomaly->fact_values->signal",
        auditor="review_report",
        check="anomaly_projection_trace",
        weight="异常投影触发 signal 规则；投影必须记 source=anomaly_engine",
        requirement=(
            "投影时记录 source=anomaly_engine，review 抽查伪异常率"
            "（F3-1 登记：**实际发射点 = review_thesis** —— F3 经该节点的 audit_amplification "
            "分支统一审计并发射 Decision；`review_report` 侧为**具名顺延项（零接线）**，F4/F5 结转）"
        ),
    ),
    "verified_conflict->dimension_score": AmplificationAuditBinding(
        edge="verified_conflict->dimension_score",
        auditor="review_thesis",
        check="conflict_penalty_consistency",
        weight="与冲突严重度对应的离散扣分（-1 档等，必须显式）",
        requirement="补扣分幅度 vs 冲突严重度的一致性检查（结算状态已是先决条件）",
    ),
    "insight->report": AmplificationAuditBinding(
        edge="insight->report",
        auditor="review_report",
        check="core_view_direction_consistency",
        weight="core_view 主导全文表达（无显式系数，α 由观点强度决定）",
        requirement=(
            "report gate 已有 cross_source_consistency，补主线 vs thesis 判断方向一致性检查"
            "（F3-1 登记：**实际发射点 = review_thesis** —— F3 经该节点的 audit_amplification "
            "分支做主线方向代理判读并发射 Decision，报告产物此时尚未生成；"
            "`review_report` 侧为**具名顺延项（零接线）**，F4/F5 结转）"
        ),
    ),
}


# ── 预算绑定（读 state builder 源码；不 import state/collectors） ──────────────


#: §14.2-A 断言 2 的期望绑定：可回环节点 → ``(计数器, 硬上限字段)``。
#: 字段名逐字取自 ``OrchestratorState``（计数器是单数、上限是复数，刻意不拼字符串猜名字）。
_BUDGET_COUNTER_BY_NODE: dict[str, tuple[str, str]] = {
    "collect_raw_facts": ("supplement_round", "max_supplement_rounds"),
    "generate_report": ("report_review_round", "max_report_review_rounds"),
}


def _literal_return_dicts(path: Path) -> list[dict[str, Any]]:
    """按源码顺序取出所有 ``return {<字面量键>: <字面量值>}`` 的 dict。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        mapping: dict[str, Any] = {}
        for key, value in zip(node.value.keys, node.value.values, strict=False):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                mapping[key.value] = value.value if isinstance(value, ast.Constant) else None
        literals.append(mapping)
    return literals


def _state_counter_facts(path: Path = _COLLECTORS_SOURCE) -> dict[str, int]:
    """抽出 ``max_*_rounds -> 默认值``（回环预算硬上限）。

    ``collectors.py::collect_raw_facts`` 返回
    ``{"supplement_round": 0, "max_supplement_rounds": 1, "report_review_round": 0,
    "max_report_review_rounds": 2, ...}``（state 的唯一初始化点），因此可直接从字面量读到硬上限。
    调用点被重构而读不到时返回空表 → 声明了 ``max_retries>0`` 的契约即判为「预算未绑定」并报红。
    """
    facts: dict[str, int] = {}
    for mapping in _literal_return_dicts(path):
        for counter, max_counter in _BUDGET_COUNTER_BY_NODE.values():
            value, used = mapping.get(max_counter), mapping.get(counter)
            if isinstance(value, int) and value > 0 and isinstance(used, int):
                facts[max_counter] = value
    return facts


@lru_cache(maxsize=1)
def _budget_bindings() -> dict[str, NodeBudget]:
    """回环预算绑定表：节点 → state 计数器（读不到 → 空表，由调用方保守判定）。"""
    facts = _state_counter_facts()
    bindings: dict[str, NodeBudget] = {}
    for node_id, (counter, max_counter) in _BUDGET_COUNTER_BY_NODE.items():
        if max_counter not in facts:
            continue
        bindings[node_id] = NodeBudget(
            node_id=node_id,
            counter=counter,
            max_counter=max_counter,
            max_value=facts[max_counter],
        )
    return bindings


#: 回环预算绑定（惰性求值；测试断言其与 ``OrchestratorState`` 硬上限一致）。
NODE_BUDGETS: dict[str, NodeBudget] = _budget_bindings()


#: ``detectors.py::DETECTORS`` 注册表（F1b 前该模块不存在 → 空表）。
@lru_cache(maxsize=1)
def _detector_registry() -> dict[str, Any]:
    return dict(getattr(_detector_module(), "DETECTORS", None) or {})


def _detector_module() -> ModuleType | None:
    """按需 import ``detectors.py``；模块不存在（F1b 未落地）→ ``None``。

    刻意区分「模块不存在」与「模块存在但注册表为空」：前者是 F1b 尚未落地，
    检测器名核对整体延后（:func:`_validate_detectors` 只报告一次"注册表不可用"）；
    后者是真 bug（注册装饰器没生效），F1b 落地后即由 CI 捕获。
    """
    try:
        return import_module(_DETECTORS_MODULE)
    except ModuleNotFoundError:
        return None


def _detector_module_available() -> bool:
    """``detectors.py`` 是否已存在于代码树（不 import，避免落地前的 import 副作用）。"""
    try:
        return find_spec(_DETECTORS_MODULE) is not None
    except (ImportError, ValueError):  # pragma: no cover - 环境异常时保守视为可用
        return True


# ── 图结构（读 agent.py 源码；不调 get_graph()） ─────────────────────────────


def _graph_call_nodes(path: Path = _AGENT_SOURCE) -> list[str]:
    """按书写顺序取图 builder 的 ``_graph.add_node("name", ...)`` 节点名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_node":
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == _GRAPH_VAR):
            continue
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            names.append(call.args[0].value)
    return names


def _graph_call_edges(path: Path = _AGENT_SOURCE) -> list[tuple[str, str]]:
    """取图 builder **生效的**边（含 conditional_edges 映射；跳过注释掉的边）。

    刻意不调 ``get_graph()``：编译图会剪掉源节点暂不可达的边（``review_report → record_deviations``），
    而契约校验必须看到 builder 声明的全部边（F0b 已记录该坑）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    edges: list[tuple[str, str]] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        func = call.func
        if not isinstance(func, ast.Attribute) or not (
            isinstance(func.value, ast.Name) and func.value.id == _GRAPH_VAR
        ):
            continue
        if func.attr == "add_edge":
            if len(call.args) >= 2:
                first, second = call.args[0], call.args[1]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and isinstance(second, ast.Constant)
                    and isinstance(second.value, str)
                ):
                    edges.append((first.value, second.value))
            continue
        if func.attr != "add_conditional_edges" or len(call.args) < 3:
            continue
        source, mapping = call.args[0], call.args[2]
        if not (isinstance(source, ast.Constant) and isinstance(source.value, str)) or not isinstance(
            mapping, ast.Dict
        ):
            continue
        for target in mapping.values:
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                edges.append((source.value, target.value))
    return edges


# ── 契约自检 ────────────────────────────────────────────────────────────────

#: §8.1 表格里列出的全部 WEIGHTED 边（文档即期望，独立于实现书写）。
_DOCUMENTED_WEIGHTED_EDGES: frozenset[str] = frozenset(
    {
        "insight->thesis",
        "anomaly->fact_values->signal",
        "verified_conflict->dimension_score",
        "insight->report",
    }
)

#: §8.1「审计要求」列逐行对应的审计动作名（审计覆盖表必须使用这些名字之一）。
_AUDIT_CHECKS: frozenset[str] = frozenset(
    {
        "weighted_direction_consistency",
        "anomaly_projection_trace",
        "conflict_penalty_consistency",
        "core_view_direction_consistency",
    }
)


def _collect_weighted_edges(contracts: dict[str, NodeContract]) -> dict[str, list[str]]:
    """``WEIGHTED`` 边 → 消费该边的节点清单（按 :data:`NODE_ORDER` 顺序）。"""
    consumers: dict[str, list[str]] = {}
    for node_id in NODE_ORDER:
        contract = contracts.get(node_id)
        if contract is None:
            continue
        for edge, mode in contract.amplification_labels.items():
            if mode is AmplificationMode.WEIGHTED:
                consumers.setdefault(edge, []).append(node_id)
    return consumers


def _validate_amplification(
    violations: list[str],
    contracts: dict[str, NodeContract],
    audit: dict[str, AmplificationAuditBinding] | None,
) -> None:
    bindings = AMPLIFICATION_AUDIT if audit is None else dict(audit)
    consumers_by_edge = _collect_weighted_edges(contracts)
    weighted_edges = set(consumers_by_edge)
    for edge in sorted(weighted_edges | set(bindings)):
        binding = bindings.get(edge)
        if binding is None:
            violations.append(f"WEIGHTED 边无审计覆盖：{edge}（消费方 {'、'.join(consumers_by_edge[edge])}）")
            continue
        if binding.auditor not in contracts:
            violations.append(f"WEIGHTED 边 {edge} 的审计节点未登记契约：{binding.auditor}")
        elif binding.auditor in consumers_by_edge.get(edge, []):
            # §8.2 规则 2：审计必须落在 review 节点上，消费方自审不算独立审计
            violations.append(f"WEIGHTED 边 {edge} 的审计节点与消费方同源：{binding.auditor}")
        if binding.check not in _AUDIT_CHECKS:
            violations.append(f"WEIGHTED 边 {edge} 的审计动作不在 §8.1 审计要求集合内：{binding.check}")
        if binding.edge != edge:
            violations.append(f"审计覆盖表的键与 edge 字段不一致：{edge} != {binding.edge}")
        if not binding.weight:
            violations.append(f"WEIGHTED 边 {edge} 未声明显式权重（§8.2 规则 1）")
        if edge not in _DOCUMENTED_WEIGHTED_EDGES:
            violations.append(f"审计表登记了 §8.1 未列出的边：{edge}")
    for edge in sorted(set(bindings) - weighted_edges):
        violations.append(f"审计表登记了没有 WEIGHTED 消费者的边：{edge}")
    for edge in sorted(weighted_edges - _DOCUMENTED_WEIGHTED_EDGES):
        violations.append(f"契约声明了 §8.1 未列出的 WEIGHTED 边（需补文档登记）：{edge}")
    for edge in sorted(_DOCUMENTED_WEIGHTED_EDGES - weighted_edges):
        violations.append(f"§8.1 登记的高风险 WEIGHTED 边未在契约中声明：{edge}")


def _validate_node_registry(violations: list[str], contracts: dict[str, NodeContract]) -> None:
    declared = set(contracts)
    for node_id in sorted(set(NODE_ORDER) - declared):
        violations.append(f"节点未登记契约：{node_id}")
    for node_id in sorted(declared - set(NODE_ORDER)):
        violations.append(f"契约登记了 NODE_ORDER 之外的节点：{node_id}")
    if _AGENT_SOURCE.exists():
        registered = set(_graph_call_nodes(_AGENT_SOURCE))
        for node_id in sorted(registered - declared):
            violations.append(f"图 builder 注册但契约未登记的节点：{node_id}")
        for node_id in sorted(declared - registered):
            violations.append(f"契约登记但图 builder 未注册的节点：{node_id}")
        for source, target in _graph_call_edges(_AGENT_SOURCE):
            if source not in declared or target not in declared:
                violations.append(f"图 builder 的边涉及未登记节点：{source} -> {target}")
    for node_id, contract in contracts.items():
        if contract.node_id != node_id:
            violations.append(f"契约键与 node_id 不一致：{node_id} != {contract.node_id}")


def _validate_recovery_ladders(violations: list[str], contracts: dict[str, NodeContract]) -> None:
    for node_id, contract in sorted(contracts.items()):
        ladder = tuple(contract.recovery_ladder)
        if not ladder:
            violations.append(f"recovery_ladder 为空：{node_id}")
        for tier in sorted({tier for tier in ladder if tier not in RECOVERY_TIERS}):
            violations.append(f"recovery_ladder 含非法 Tier {tier}：{node_id}")
        if len(set(ladder)) != len(ladder):
            violations.append(f"recovery_ladder 含重复 Tier：{node_id}")
        if contract.max_retries > 0 and 4 not in ladder:
            violations.append(f"max_retries>0 但阶梯未声明 Tier 4：{node_id}")


def _validate_retry_budgets(
    violations: list[str],
    contracts: dict[str, NodeContract],
    budgets: dict[str, NodeBudget] | None,
) -> None:
    bindings = _budget_bindings() if budgets is None else dict(budgets)
    retryable = {node_id for node_id, contract in contracts.items() if contract.max_retries > 0}
    for node_id in sorted(retryable):
        binding = bindings.get(node_id)
        if binding is None:
            violations.append(f"max_retries>0 但 state 无对应计数器：{node_id}")
            continue
        # R2-7 口径定稿：§14.2-A 断言 2 原文即"`max_retries > 0` 的节点只允许 …，且**上限等于**
        # `OrchestratorState` 里的 `max_*_rounds`"——**双向相等是文档要求、非实现加强**；
        # §15.7 亦写"与 state 计数器不一致 → 测试失败"。单向 `>` 会漏掉"state 上限被调大
        # 而契约不同步"（违反 §7.2 规则 4），该缺口由 captain 提出的变异测试设计发现。
        # 双向核对是刻意的：若 state 上限被（人为或回归）调大而契约不同步，实际回环次数会
        # 超过契约声明的授权范围——这正是"未登记回环预算"（§7.2 规则 4），必须报红。
        declared, allowed = contracts[node_id].max_retries, binding.max_value
        if declared > allowed:
            violations.append(f"max_retries 超过 state 硬上限（{binding.max_counter}={allowed}）：{node_id}={declared}")
        elif declared < allowed:
            violations.append(
                f"回环预算未登记：state 允许 {binding.max_counter}={allowed} 次，但契约 max_retries={declared}（{node_id}）"
            )
    for node_id in sorted(set(bindings) - retryable):
        violations.append(f"state 有回环预算但契约未声明 max_retries：{node_id}")


def _validate_detectors(
    violations: list[str],
    contracts: dict[str, NodeContract],
    registry: dict[str, Any] | None,
) -> None:
    declared = sorted({name for contract in contracts.values() for name in contract.detectors})
    if not declared:
        return
    available = _detector_registry() if registry is None else dict(registry)
    if not available:
        if registry is None and not _detector_module_available():
            # F1a 阶段：detectors.py 尚未落地（F1b 交付），检测器名核对整体延后；
            # 落地后（模块存在而注册表为空）该分支不再进入，转为强制报错。
            return
        violations.append(f"契约声明了 {len(declared)} 个检测器，但 DETECTORS 注册表为空或不可用（F1b 未落地/未注册）")
        return
    for name in declared:
        if name not in available:
            violations.append(f"检测器未在 DETECTORS 注册表中登记：{name}")


def validate_contracts(
    *,
    contracts: dict[str, NodeContract] | None = None,
    budgets: dict[str, NodeBudget] | None = None,
    audit: dict[str, AmplificationAuditBinding] | None = None,
    detectors: dict[str, Any] | None = None,
) -> list[str]:
    """契约自检，返回违规清单（空 = 通过）；供 CI / 启动断言使用。

    四个关键字参数仅供测试注入（默认 ``None`` = 使用本模块的真实登记表与注册表），
    以便在不改动真实契约的前提下逐个构造「违规必须被捕获」的用例。

    覆盖：契约键集与 ``NODE_ORDER`` 及图 builder 注册节点一致、``recovery_ladder`` 合法、
    ``max_retries>0`` 有 state 计数器且不超硬上限、每条 ``WEIGHTED`` 边有审计覆盖、
    ``detectors`` 名都在 ``DETECTORS`` 注册表中。
    """
    resolved = dict(NODE_CONTRACTS if contracts is None else contracts)
    violations: list[str] = []
    _validate_node_registry(violations, resolved)
    _validate_recovery_ladders(violations, resolved)
    _validate_retry_budgets(violations, resolved, budgets)
    _validate_amplification(violations, resolved, audit)
    _validate_detectors(violations, resolved, detectors)
    return sorted(dict.fromkeys(violations))
