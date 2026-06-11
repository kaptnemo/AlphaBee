"""ThesisEngine — 将 SignalEngine 输出聚合为投资论点各维度判断。

执行流程：
1. 读取 SignalEngine 返回的 signal_results（每条信号的 level + thesis_impact）
2. 按照 thesis_impact key（如 financial_quality / earnings_quality / credit_risk）
   将有效信号分组到各维度
3. 对每个维度计算加权综合评分：
   score = mean(signal_level_score × impact_direction_score)
   其中 signal_level_score ∈ [0, 1]，impact_direction_score ∈ [-1, 1]
4. 将 score 映射到判断档位（strong_positive / positive / neutral / negative / strong_negative）
5. 从 YAML 维度定义取解释文字
6. 计算置信度（基于可用信号数 / 全部维度信号总数）
"""

from __future__ import annotations

import structlog

from alphabee.agents.thesis.models import (
    IMPACT_TO_DIRECTION,
    JUDGMENT_LABELS,
    SIGNAL_LEVEL_TO_SCORE,
    EvidenceItem,
    InvestmentThesis,
    ThesisDimension,
    score_to_judgment,
)
from alphabee.agents.thesis.registry import (
    DIMENSION_DEFS,
    ThesisDimensionDef,
    ensure_loaded,
)

logger = structlog.get_logger(__name__)

# 触发信号（level != "none"）对应的最小触发强度阈值，低于此值视为无显著贡献
_TRIGGER_THRESHOLD = 0.05


class ThesisEngine:
    """将 SignalEngine 评估结果聚合为 InvestmentThesis。

    用法::

        engine = ThesisEngine()
        thesis = engine.run(
            symbol="600519.SH",
            period="2023年报",
            signal_results={
                "revenue_quality_risk": {
                    "level": "high",
                    "interpretation": "...",
                    "thesis_impact": {"financial_quality": "negative", "earnings_quality": "negative"},
                },
                ...
            },
        )
    """

    def __init__(self, dimension_defs: dict[str, ThesisDimensionDef] | None = None):
        ensure_loaded()
        self.dimension_defs: dict[str, ThesisDimensionDef] = (
            dict(dimension_defs) if dimension_defs is not None else dict(DIMENSION_DEFS)
        )

    # ── 主入口 ────────────────────────────────────────────────────────────

    def run(
        self,
        symbol: str,
        period: str,
        signal_results: dict[str, dict],
    ) -> InvestmentThesis:
        """根据信号评估结果生成 InvestmentThesis。

        Args:
            symbol: 股票代码，如 "600519.SH"。
            period: 分析周期，如 "2023年报"。
            signal_results: SignalEngine.run() 的返回值，格式为
                {signal_id: {"level": str, "interpretation": str,
                             "thesis_impact": {dim_key: impact_str}, ...}}

        Returns:
            InvestmentThesis，包含各维度判断、主要风险和 Critic 追问。
        """
        # ── 1. 统计信号基础数据 ────────────────────────────────────────
        total_signals = len(signal_results)
        triggered_signals = sum(
            1
            for r in signal_results.values()
            if r.get("level") in SIGNAL_LEVEL_TO_SCORE and r["level"] != "none"
        )

        # ── 2. 按维度分组信号证据 ──────────────────────────────────────
        # dim_key → list of (signal_id, level_score, impact_direction, evidence)
        dim_contributions: dict[str, list[tuple[float, float, EvidenceItem]]] = {
            dim_def.signal_dimension_key: []
            for dim_def in self.dimension_defs.values()
        }

        for signal_id, result in signal_results.items():
            level = result.get("level", "")
            level_score = SIGNAL_LEVEL_TO_SCORE.get(level)
            if level_score is None:
                # blocked / missing_fact / invalid — 跳过，不参与 thesis 计算
                continue

            thesis_impact: dict[str, str] = result.get("thesis_impact") or {}
            interpretation = result.get("interpretation", "")

            for dim_key, impact_str in thesis_impact.items():
                if dim_key not in dim_contributions:
                    continue
                direction = IMPACT_TO_DIRECTION.get(impact_str, 0.0)
                evidence = EvidenceItem(
                    signal_id=signal_id,
                    signal_name=signal_id,  # tools 层会用 registry 做翻译
                    level=level,
                    impact=impact_str,
                    interpretation=" ".join(interpretation.split()) if interpretation else "",
                )
                dim_contributions[dim_key].append((level_score, direction, evidence))

        # ── 3. 计算各维度综合评分并构建 ThesisDimension ───────────────
        dimensions: dict[str, ThesisDimension] = {}

        for dim_def in self.dimension_defs.values():
            dim_key = dim_def.signal_dimension_key
            contribs = dim_contributions.get(dim_key, [])

            if not contribs:
                # 无任何信号覆盖，维度评分默认中性，置信度为 0
                score = 0.0
                judgment = "neutral"
                evidence_list: list[EvidenceItem] = []
                confidence = 0.0
            else:
                # 加权平均：每个信号的贡献 = level_score × impact_direction
                effective_scores = [ls * d for ls, d, _ in contribs]
                score = sum(effective_scores) / len(effective_scores)
                judgment = score_to_judgment(score)
                evidence_list = [e for _, _, e in contribs]
                confidence = min(1.0, len(contribs) / max(1, total_signals))

            interpretation = dim_def.get_interpretation(judgment)

            dimensions[dim_def.id] = ThesisDimension(
                id=dim_def.id,
                name=dim_def.name,
                judgment=judgment,
                score=score,
                evidence=evidence_list,
                interpretation=interpretation,
                confidence=confidence,
            )

        # ── 4. 汇总整体判断 ────────────────────────────────────────────
        overall_judgment, overall_score = self._compute_overall(dimensions)

        # ── 5. 提取主要风险 ────────────────────────────────────────────
        primary_risks = self._extract_primary_risks(dimensions, signal_results)

        thesis = InvestmentThesis(
            symbol=symbol,
            period=period,
            dimensions=dimensions,
            primary_risks=primary_risks,
            overall_judgment=overall_judgment,
            overall_score=overall_score,
            signal_count=total_signals,
            triggered_signal_count=triggered_signals,
        )

        logger.info(
            "thesis_generated",
            symbol=symbol,
            period=period,
            overall_judgment=overall_judgment,
            overall_score=round(overall_score, 3),
            dimensions=list(dimensions.keys()),
        )

        return thesis

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _compute_overall(
        self,
        dimensions: dict[str, ThesisDimension],
    ) -> tuple[str, float]:
        """等权平均各维度评分，计算整体判断。"""
        scored = [d for d in dimensions.values() if d.confidence > 0]
        if not scored:
            return "neutral", 0.0
        overall_score = sum(d.score for d in scored) / len(scored)
        return score_to_judgment(overall_score), overall_score

    def _extract_primary_risks(
        self,
        dimensions: dict[str, ThesisDimension],
        signal_results: dict[str, dict],
    ) -> list[str]:
        """提取主要风险：level=high 的信号解释 + negative/strong_negative 维度名称。"""
        risks: list[str] = []

        # 高风险信号的解释
        for signal_id, result in signal_results.items():
            if result.get("level") == "high":
                interp = result.get("interpretation", "")
                if interp:
                    risks.append(" ".join(interp.split()))

        # 负面维度名称
        for dim in dimensions.values():
            if dim.judgment in ("negative", "strong_negative") and dim.confidence > 0:
                label = JUDGMENT_LABELS.get(dim.judgment, dim.judgment)
                risks.append(f"{dim.name}评估：{label}")

        return risks
