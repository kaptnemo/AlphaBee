"""CriticEngine — 为 InvestmentThesis 生成质疑追问清单。

Critic 的职责：不为结论背书，而是系统性地质疑：
  1. 证据是否足够（evidence_gap）
  2. 是否有反向证据（counter_evidence）
  3. 是否只是行业周期（industry_cycle）
  4. 是否需要同行对比（comparison）
  5. 是否有会计政策变化（accounting_policy）

追问来源（按优先级）：
  A. 各信号的 critic_questions（已内嵌在 SignalEngine 评估结果中）
  B. 各维度定义中、依据判断档位触发的 critic_questions（来自 YAML）
  C. 系统级通用追问（当整体判断偏积极时，补充证伪性追问）

去重逻辑：相同 question 文字只保留最高 severity 的一条。
"""

from __future__ import annotations

import structlog

from alphabee.agents.thesis.models import (
    CRITIC_SEVERITY_LABELS,
    InvestmentThesis,
    CriticQuestion,
)
from alphabee.agents.thesis.registry import DIMENSION_DEFS, ensure_loaded

logger = structlog.get_logger(__name__)

# 严重度排序（数字越大越严重）
_SEVERITY_RANK: dict[str, int] = {"critical": 3, "important": 2, "minor": 1}

# 当整体判断为积极时，补充系统级别的证伪性追问
_POSITIVE_BIAS_QUESTIONS: list[dict] = [
    {
        "question": "当前财务数据是否覆盖了完整的经济周期，乐观评估是否依赖了顺周期数据？",
        "category": "evidence_gap",
        "severity": "important",
    },
    {
        "question": "是否有尚未反映在财报中的潜在负债或或有事项（诉讼、担保、对赌协议）？",
        "category": "counter_evidence",
        "severity": "important",
    },
    {
        "question": "管理层激励结构（股权、奖金）是否会导致短期财务指标粉饰？",
        "category": "accounting_policy",
        "severity": "minor",
    },
]


class CriticEngine:
    """为 InvestmentThesis 生成 CriticQuestion 列表。

    用法::

        critic = CriticEngine()
        thesis = critic.enrich(thesis, signal_results)
        # thesis.critic_questions 已填充
    """

    def __init__(self) -> None:
        ensure_loaded()

    def enrich(
        self,
        thesis: InvestmentThesis,
        signal_results: dict[str, dict],
    ) -> InvestmentThesis:
        """向 thesis 填充 critic_questions，返回同一个对象（in-place 修改）。

        Args:
            thesis: ThesisEngine.run() 的输出，dimensions 和 overall_judgment 已填充。
            signal_results: SignalEngine.run() 的原始输出，包含各信号的 critic_questions。

        Returns:
            已填充 critic_questions 的 InvestmentThesis（与入参同一对象）。
        """
        raw: list[tuple[str, str, str, str]] = []
        # (question, source, category, severity)

        # ── A. 信号级追问 ──────────────────────────────────────────────
        for signal_id, result in signal_results.items():
            level = result.get("level", "")
            if level in ("none", "blocked", "missing_fact", "invalid", "unknown"):
                continue  # 未触发或异常的信号不贡献追问
            for q_text in result.get("critic_questions", []):
                raw.append((q_text, signal_id, "general", "minor"))

        # ── B. 维度级追问（依判断档位触发）────────────────────────────
        for dim_id, dim_result in thesis.dimensions.items():
            dim_def = DIMENSION_DEFS.get(dim_id)
            if dim_def is None:
                continue
            judgment = dim_result.judgment
            for cq in dim_def.critic_questions:
                if not cq.trigger_on or judgment in cq.trigger_on:
                    raw.append((cq.question, dim_id, cq.category, cq.severity))

        # ── C. 系统级正向偏差追问 ──────────────────────────────────────
        if thesis.overall_judgment in ("positive", "strong_positive"):
            for item in _POSITIVE_BIAS_QUESTIONS:
                raw.append(
                    (
                        item["question"],
                        "system",
                        item["category"],
                        item["severity"],
                    )
                )

        # ── 去重：相同问题保留最高 severity ──────────────────────────
        best: dict[str, tuple[str, str, str, str]] = {}
        for question, source, category, severity in raw:
            key = question.strip()
            if key not in best:
                best[key] = (question, source, category, severity)
            else:
                existing_sev = best[key][3]
                if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(existing_sev, 0):
                    best[key] = (question, source, category, severity)

        # ── 排序：critical → important → minor，同级按来源稳定排序 ─────
        sorted_items = sorted(
            best.values(),
            key=lambda x: (-_SEVERITY_RANK.get(x[3], 0), x[1]),
        )

        thesis.critic_questions = [
            CriticQuestion(
                question=q,
                source=src,
                category=cat,
                severity=sev,
            )
            for q, src, cat, sev in sorted_items
        ]

        logger.info(
            "critic_questions_generated",
            symbol=thesis.symbol,
            period=thesis.period,
            total=len(thesis.critic_questions),
            critical=sum(1 for cq in thesis.critic_questions if cq.severity == "critical"),
        )

        return thesis
