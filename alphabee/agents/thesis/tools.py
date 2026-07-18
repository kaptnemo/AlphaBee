"""Thesis tools — 对外暴露为 agent function call 的 Thesis 合成接口。

工具设计原则（与 SignalAgent 工具对齐）：
  - list_thesis_dimensions : 发现工具，列出维度定义，供 agent 确认输入格式
  - synthesize_thesis      : 主计算工具，ThesisEngine + CriticEngine + 可选 LLM 增强
"""

from __future__ import annotations

from alphabee.agents.thesis.critic import CriticEngine
from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.models import (
    CRITIC_CATEGORY_LABELS,
    CRITIC_SEVERITY_LABELS,
    JUDGMENT_LABELS,
    CompanyContext,
)
from alphabee.agents.thesis.registry import DIMENSION_DEFS, ensure_loaded

_initialized = False


def _ensure_loaded() -> None:
    global _initialized
    if not _initialized:
        ensure_loaded()
        _initialized = True


# ── 发现工具 ───────────────────────────────────────────────────────────


def list_thesis_dimensions() -> str:
    """列出所有已加载的 Thesis 维度定义，含 ID、名称、描述和对应信号 impact key。

    在调用 synthesize_thesis 之前，可先调用此工具了解可用维度和 signal_results
    的期望格式，以便确认上游信号覆盖情况。

    Returns:
        Markdown 格式的维度目录。
    """
    _ensure_loaded()

    lines = [
        "## 可用 Thesis 维度定义\n",
        "| 维度 ID | 名称 | 信号 impact key | 说明 |",
        "|---------|------|-----------------|------|",
    ]
    for dim_id, dim_def in sorted(DIMENSION_DEFS.items()):
        desc = " ".join(dim_def.description.split())[:60] + "…"
        lines.append(f"| `{dim_id}` | {dim_def.name} | `{dim_def.signal_dimension_key}` | {desc} |")

    lines.append(
        "\n> 💡 **使用提示**：signal_results 中每条信号的 `thesis_impact` "
        "字典 key 需与上表的「信号 impact key」一致，才能被对应维度纳入计算。"
    )
    return "\n".join(lines)


# ── 主合成工具 ─────────────────────────────────────────────────────────


def synthesize_thesis(
    symbol: str,
    period: str,
    signal_results: dict[str, dict],
    anomaly_report: dict | None = None,
    conflict_analysis: dict | None = None,
    verification_results: list[dict] | None = None,
    company_context: dict | None = None,
    user_intent: str = "",
    fact_summary: str = "",
    use_llm_enhancement: bool = False,
) -> str:
    """根据 SignalEngine 的评估结果，生成结构化的 InvestmentThesis 报告。

    本工具整合两步处理：
    1. 确定性：ThesisEngine（维度聚合）+ CriticEngine（质疑追问）
    2. 可选 LLM 增强：跨信号模式识别 + 行业语境化 + 用户意图自适应

    适用场景：
    - 财报质量体检流程的 Thesis 生成阶段
    - 将多维度财务风险信号整合为结构化投资判断
    - 生成待审查的初步投资论点供后续 Critic 质疑

    Args:
        symbol: 股票代码（Tushare 格式），如 ``"600519.SH"``。
        period: 分析周期描述，如 ``"2023年报"``、``"2024Q3"``。
        signal_results: SignalEngine.run() 的返回值。
        anomaly_report: 可选的异常检测报告，命中的异常模式会直接进入 thesis 证据。
        conflict_analysis: 可选的冲突分析结果，已验证高严重度冲突会下调相关维度。
        verification_results: 可选的假设验证结果，rejected/unknown 会进入反证或缺失证据。
        company_context: 可选的公司背景字典，含 industry / lifecycle_stage /
            market_cap_category / business_model_summary。
        user_intent: 用户分析目标，如 "长期投资价值" / "短期风险排查"。
        fact_summary: 事实层关键数据摘要（可选）。
        use_llm_enhancement: 是否启用 LLM 增强层。

    Returns:
        Markdown 格式的投资论点报告。
    """
    _ensure_loaded()

    if not signal_results:
        return (
            "> ⚠️ signal_results 为空，无法生成 Thesis。\n> 请先通过 `evaluate_signals` 获取信号评估结果后再调用本工具。"
        )

    # ── 确定性引擎 ────────────────────────────────────────────────────
    try:
        thesis_engine = ThesisEngine()
        thesis = thesis_engine.run(
            symbol=symbol,
            period=period,
            signal_results=signal_results,
            anomaly_report=anomaly_report,
            conflict_analysis=conflict_analysis,
            verification_results=verification_results,
            company_context=company_context,
        )
    except Exception as e:
        return f"> ❌ ThesisEngine 运行失败：{e}"

    try:
        critic_engine = CriticEngine()
        thesis = critic_engine.enrich(thesis, signal_results)
    except Exception as e:
        return f"> ❌ CriticEngine 运行失败：{e}"

    # ── 可选 LLM 增强 ─────────────────────────────────────────────────
    enhanced_md = ""
    if use_llm_enhancement:
        try:
            from alphabee.agents.thesis.enhancer import ThesisEnhancer

            enhancer = ThesisEnhancer()
            enhanced = enhancer.enhance(
                thesis=thesis,
                signal_results=signal_results,
                company_context=CompanyContext(**(company_context or {})),
                user_intent=user_intent,
                fact_summary=fact_summary,
            )
            enhanced_md = _render_enhanced(enhanced)
        except Exception as e:
            enhanced_md = (
                f"\n\n---\n\n## Enhanced Analysis (LLM)\n\n> ⚠️ LLM 增强层运行失败：{e}\n> 以下仅包含确定性分析结论。\n"
            )

    # ── 渲染报告 ─────────────────────────────────────────────────────
    return _render_thesis(thesis) + enhanced_md


# ── 渲染 ──────────────────────────────────────────────────────────────


def _render_thesis(thesis) -> str:
    """将 InvestmentThesis 对象渲染为 Markdown 报告。"""
    overall_label = JUDGMENT_LABELS.get(thesis.overall_judgment, thesis.overall_judgment)
    sections: list[str] = []
    source_labels = {
        "signal": "信号",
        "anomaly": "异常模式",
        "conflict": "冲突验证",
        "context": "语境校准",
    }

    # ── 标题与摘要 ──────────────────────────────────────────────────
    sections.append(f"# 投资论点（Thesis）— {thesis.symbol} · {thesis.period}\n")
    sections.append(f"**整体判断**：{overall_label}（评分：{thesis.overall_score:+.2f}）\n")

    coverage = f"{thesis.triggered_signal_count}/{thesis.signal_count}"
    sections.append(
        f"**信号覆盖**：共评估 {thesis.signal_count} 条信号，"
        f"其中 {thesis.triggered_signal_count} 条触发（{coverage}）\n"
    )

    # ── 各维度详情 ──────────────────────────────────────────────────
    sections.append("---\n\n## 各维度评估\n")
    for dim in thesis.dimensions.values():
        label = JUDGMENT_LABELS.get(dim.judgment, dim.judgment)
        conf_pct = f"{dim.confidence * 100:.0f}%"
        sections.append(f"### {dim.name}（`{dim.id}`）")
        sections.append(f"**判断**：{label}  **评分**：{dim.score:+.2f}  **置信度**：{conf_pct}\n")

        if dim.interpretation:
            sections.append(f"**解释**：{dim.interpretation}\n")

        if dim.evidence:
            sections.append("**支撑证据**：")
            for e in dim.evidence:
                level_icon = {"high": "🔴", "medium": "🟡", "low": "🟢", "none": "✅"}.get(e.level, "⚪")
                source_label = source_labels.get(getattr(e, "source_type", ""), "证据")
                sections.append(
                    f"  - {level_icon} `{e.signal_id}` [{source_label}] → 影响：{e.impact}"
                    + (f"（{e.interpretation[:60]}…）" if e.interpretation else "")
                )
            sections.append("")

        if dim.counter_evidence:
            sections.append("**反向证据**：")
            for item in dim.counter_evidence:
                sections.append(f"  - {item}")
            sections.append("")

        if dim.missing_evidence:
            sections.append("**缺失证据**：")
            for item in dim.missing_evidence:
                sections.append(f"  - {item}")
            sections.append("")

        if dim.context_notes:
            sections.append("**语境说明**：")
            for item in dim.context_notes:
                sections.append(f"  - {item}")
            sections.append("")

    # ── 主要风险清单 ─────────────────────────────────────────────────
    if thesis.primary_risks:
        sections.append("---\n\n## 主要风险\n")
        for risk in thesis.primary_risks:
            sections.append(f"- {risk}")
        sections.append("")

    # ── Critic 追问 ──────────────────────────────────────────────────
    if thesis.critic_questions:
        sections.append("---\n\n## Critic 质疑追问\n")
        sections.append("以下问题需在最终报告中逐一核实或说明，否则结论可信度将受到限制：\n")
        for cq in thesis.critic_questions:
            sev_label = CRITIC_SEVERITY_LABELS.get(cq.severity, cq.severity)
            cat_label = CRITIC_CATEGORY_LABELS.get(cq.category, cq.category)
            sections.append(f"- **{sev_label}**（{cat_label}）：{cq.question}")
        sections.append("")

    # ── 尾部提示 ────────────────────────────────────────────────────
    sections.append(
        "> ⚠️ 本 Thesis 为初步财务质量体检结论，仅基于结构化财务数据推导，"
        "不包含业务前景判断、管理层评估或市场估值分析。"
        "最终投资决策需结合更多信息综合判断。"
    )

    return "\n".join(sections)


def _render_enhanced(enhanced) -> str:
    """Render the LLM-enhanced portion as Markdown."""
    parts = ["\n---\n\n## Enhanced Analysis (LLM)\n"]

    if enhanced.cross_signal_patterns:
        parts.append("### Cross-Signal Patterns\n")
        for i, p in enumerate(enhanced.cross_signal_patterns, 1):
            sev_mod = {
                "amplified": " ⚠️ 风险放大",
                "mitigated": " ✅ 部分缓解",
            }.get(p.severity_modifier, "")
            parts.append(f"**{i}. {p.pattern_name}**{sev_mod}\n")
            parts.append(f"涉及的信号：{', '.join(f'`{s}`' for s in p.signals_involved)}\n")
            parts.append(f"\n{p.narrative}\n")

    if enhanced.context_notes:
        parts.append("### Context Notes\n")
        parts.append(f"{enhanced.context_notes}\n")

    if enhanced.intent_adjusted_summary:
        parts.append("### Intent-Adjusted Summary\n")
        parts.append(f"{enhanced.intent_adjusted_summary}\n")

    if enhanced.llm_confidence_note:
        parts.append("### LLM Confidence Note\n")
        parts.append(f"{enhanced.llm_confidence_note}\n")

    return "\n".join(parts)
