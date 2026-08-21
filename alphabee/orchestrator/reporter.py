"""Report generation node — single-LLM-call report from structured thesis + review."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ReportOutput
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.contracts import (
    ReportArtifact,
    ReportGenerationPayload,
)
from alphabee.orchestrator.prompts import REPORT_GENERATOR_PROMPT
from alphabee.orchestrator.services.payload_builders import (
    build_report_generation_payload,
)
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import create_chat_model, json_instruction
from alphabee.utils.pipeline import extract_text, make_id, parse_json


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _markdown_list(items: list[str], limit: int = 6) -> str:
    lines = []
    for idx, item in enumerate(items or []):
        if idx >= limit:
            lines.append(f"- … 共 {len(items)} 项")
            break
        lines.append(f"- {item}")
    return "\n".join(lines) or "无"


def build_deterministic_report(payload: ReportGenerationPayload, failure_reason: str = "") -> dict:
    """LLM 报告生成失败时，用结构化 payload 拼装一份可消费的确定性报告。

    与 ``_fallback_report`` 的区别：这里把信号/异常/冲突/论点/审查问题等真实分析
    结果渲染进各章节，而不是只剩一句错误信息。这样即使模型侧连续空输出，
    最终交付的报告也包含实质内容，可被 review gate 与 CLI 正常展示。
    """
    company = payload.company
    symbol = company.symbol or "标的"

    metric_lines = [
        f"- {m.name} = {m.value}（{m.level or 'none'}）{m.interpretation}" for m in payload.metrics.top_metrics
    ]
    key_metrics = "\n".join(metric_lines) or "无关键衍生指标"

    signal_lines = [
        f"- [{s.level}] {s.signal_id}: {s.interpretation}"
        for s in payload.signals.signals
        if s.level not in ("none", "unknown", "")
    ]
    signal_analysis = "\n".join(signal_lines) or "无风险信号触发"

    anomaly_lines = [
        f"- {a.get('metric')}: level={a.get('level')} z_score={a.get('z_score')}"
        for a in payload.anomaly.anomalies
        if a.get("level") != "none"
    ]
    anomaly_lines += [
        f"- 模式 {p.get('pattern_name')} severity={p.get('severity')}" for p in payload.anomaly.pattern_matches
    ]
    anomaly_detection = "\n".join(anomaly_lines) or "未检出异常"

    conflict_lines: list[str] = []
    if payload.conflict_analysis:
        for c in payload.conflict_analysis.conflicts:
            conflict_lines.append(f"- [{c.severity}] {c.theme}: {c.description[:150]}")
            for h in c.hypotheses:
                conflict_lines.append(f"    - 假设({h.verification_status}): {h.explanation[:120]}")
    conflict_analysis = "\n".join(conflict_lines) or "无冲突"

    dim_lines: list[str] = []
    for dim_id, d in (payload.thesis or {}).get("dimensions", {}).items():
        dim_lines.append(
            f"- {dim_id}: judgment={d.get('judgment')} score={d.get('score')} confidence={d.get('confidence')}"
        )
    dimension_analysis = "\n".join(dim_lines) or "无维度分析"

    insight = payload.insight
    exec_summary = (
        (insight.core_view if insight and insight.core_view else "")
        or (payload.thesis or {}).get("summary", "")
        or "确定性分析结果汇总。"
    )
    viewpoint = (insight.base_case if insight else "") or ((payload.thesis or {}).get("viewpoint") or "")
    scenario = ""
    if insight:
        scenario = f"基准情景：{insight.base_case}\n乐观情景：{insight.bull_case}\n悲观情景：{insight.bear_case}"
    falsification = _markdown_list(insight.what_would_change_my_mind if insight else [])

    high_msgs = [i.message for i in payload.issues if i.severity in ("high", "critical")]
    medium_msgs = [i.message for i in payload.issues if i.severity == "medium"]
    risks = "\n".join(f"- {m}" for m in high_msgs[:8]) or "无高危风险项"

    # 公司赛道/对标组章节（COMPANY_TRACK Phase F5）：有 track 时注入对比基准
    track_lines: list[str] = []
    track_section = ""
    if payload.company_track is not None:
        track = payload.company_track
        if track.track_label:
            track_lines.append(f"- 真实赛道: {track.track_label}（商业模式: {track.business_model or '—'}）")
        if track.dominant_segment:
            track_lines.append(f"- 主导业务线: {track.dominant_segment}")
        if track.peer_benchmarks:
            bench = "  ".join(f"{key}={value:.3g}" for key, value in track.peer_benchmarks.items() if value is not None)
            track_lines.append(f"- 对标组基准: {bench}")
        if track.peer_group:
            track_lines.append(f"- 对标组: {', '.join(track.peer_group)}")
        if track.stale:
            track_lines.append("- ⚠ 公司赛道数据可能过期，请以最新报告期为准")
        track_section = "\n".join(track_lines) or "无公司赛道数据"

    review_findings = "\n".join(f"- [{i.severity}] {i.message}" for i in payload.issues[:10]) or "无审查问题"
    if failure_reason:
        review_findings += f"\n\n（LLM 报告生成失败，已降级为确定性报告：{failure_reason}）"

    return ReportArtifact(
        title=f"{symbol} 财报质量体检报告（确定性降级版）",
        sections={
            "executive_summary": exec_summary[:800],
            "investment_viewpoint": viewpoint[:800],
            "scenario_analysis": scenario[:800] or "无情景分析数据",
            "key_metrics": key_metrics[:1000],
            "signal_analysis": signal_analysis[:1000],
            "anomaly_detection": anomaly_detection[:800],
            "conflict_analysis": conflict_analysis[:1000],
            "company_track": track_section[:1000],
            "dimension_analysis": dimension_analysis[:1000],
            "review_findings": review_findings[:1000],
            "falsification_conditions": falsification[:800] or "无明确证伪条件",
            "risks": risks[:800],
            "disclaimer": "本报告由 AlphaBee 自动生成，不构成投资建议。",
        },
        summary=exec_summary[:200],
        risk_count={"high": len(high_msgs), "medium": len(medium_msgs), "low": 0},
        overall_confidence="unknown" if failure_reason else "medium",
        disclosed_issue_ids=[i.id for i in payload.issues if i.severity in ("high", "critical")],
    ).model_dump(mode="json")


def _fallback_report(summary: str) -> dict:
    return ReportArtifact(
        title="财报质量体检报告",
        sections={
            "executive_summary": summary,
            "investment_viewpoint": "",
            "scenario_analysis": "",
            "key_metrics": "",
            "signal_analysis": "",
            "anomaly_detection": "",
            "conflict_analysis": "",
            "dimension_analysis": "",
            "review_findings": "",
            "falsification_conditions": "",
            "risks": "",
            "disclaimer": "",
        },
        summary=summary,
        risk_count={},
        overall_confidence="unknown",
        disclosed_issue_ids=[],
    ).model_dump(mode="json")


async def generate_report(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Generate the final report from structured thesis, review, and data artifacts.

    Makes a single LLM call with all structured context, producing a
    template-driven Markdown report.
    """
    step = Step(
        id="generate_report",
        kind="generate_report",
        inputs={
            "artifact_count": len(state.get("artifacts", [])),
            "rewrite_reason": state.get("report_rewrite_reason"),
        },
        status=StepStatus.RUNNING,
    )

    payload = build_report_generation_payload(state)
    prompt_text = payload.model_dump_json(indent=2)
    # 公司赛道语境指令（COMPANY_TRACK Phase F5）：有 track 时要求报告含对标组对比
    track_hint = ""
    if payload.company_track is not None:
        track_hint = (
            "\n报告必须包含「公司赛道/对标组」章节：公司真实赛道为"
            f"「{payload.company_track.track_label or '未识别'}」，结合对标组基准"
            "（company_track.peer_benchmarks）说明公司相对真对手（而非申万行业）的位置。"
        )
        if payload.company_track.stale:
            track_hint += "公司赛道数据已过期，必须显式写出「行业/赛道上下文可能过期」。"
    rewrite_reason = state.get("report_rewrite_reason")
    new_issues: list[Issue] = []
    prior_report = None
    if rewrite_reason:
        # 质量 gate 触发重写时，会把上一版报告一并交给模型。
        # 这样重写动作更像“定向修补”而不是完全重新生成，能减少风格漂移。
        for artifact in reversed(state.get("artifacts", [])):
            if artifact.type == ArtifactType.REPORT and isinstance(artifact.value, dict):
                prior_report = artifact.value
                break

    try:
        # max_tokens 放宽，避免推理型模型把输出预算耗在思考过程、正文被截成空。
        model = create_chat_model("agent.report", max_tokens=8192)
        raw_text = ""
        parse_error: Exception | None = None
        for attempt in range(2):
            # 首次空输出或解析失败时重试一次，并追加“纯 JSON 输出”的强硬提示。
            # 推理型模型偶发只输出推理过程、不输出最终正文，重试可显著提高成功率。
            retry_hint = (
                ""
                if attempt == 0
                else (
                    "\n\n## 重要：上一次输出为空或无法解析为 JSON。\n"
                    "请只输出一个完整的、合法的纯 JSON 对象，不要输出任何分析文字、"
                    "Markdown 代码块标记或前后缀说明。"
                )
            )
            response = model.invoke(
                [
                    SystemMessage(content=REPORT_GENERATOR_PROMPT),
                    HumanMessage(
                        content=(
                            json_instruction(ReportOutput)
                            + "\n\n"
                            + track_hint
                            + (
                                "请基于以下结构化数据生成财报质量体检报告。\n\n"
                                if not rewrite_reason
                                else "这是一次基于质量 gate 的重写，请优先修复以下问题后再生成新报告：\n"
                                f"- {rewrite_reason}\n\n"
                                "请保持所有判断忠实于输入 JSON，不要新增分析，只修复结构覆盖、风险披露和冲突呈现。\n\n"
                            )
                            + (
                                f"上一版报告：\n```json\n{json.dumps(prior_report, ensure_ascii=False, indent=2)}\n```\n\n"
                                if rewrite_reason and prior_report
                                else ""
                            )
                            + f"输入数据：\n```json\n{prompt_text}\n```"
                            + retry_hint
                        )
                    ),
                ]
            )
            raw_text = extract_text(response.content)
            try:
                report_value = ReportArtifact.model_validate(parse_json(raw_text)).model_dump(mode="json")
                break
            except Exception as exc:
                parse_error = exc
                report_value = None
        if report_value is None:
            new_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="parse_error",
                    message=f"ReportOutput parse failed after retry: {parse_error}",
                    related_step=step.id,
                )
            )
            # LLM 连续空输出时，用结构化 payload 拼一份可消费的确定性报告，
            # 保证最终交付仍有实质内容（信号/异常/冲突/论点/审查问题）。
            report_value = build_deterministic_report(
                payload,
                failure_reason=f"LLM 返回空文本或无法解析。原始输出：{raw_text[:200]}",
            )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"Report generation failed: {exc}",
                related_step=step.id,
            )
        )
        report_value = build_deterministic_report(payload, failure_reason=str(exc))

    report_artifact = Artifact(
        id=_make_id("artifact"),
        type=ArtifactType.REPORT,
        producer_step=step.id,
        value=report_value,
    )

    completed_step = step.model_copy(
        update={
            "status": StepStatus.SUCCEEDED,
            "outputs": [report_artifact.id],
        }
    )

    return {
        "steps": [completed_step],
        "artifacts": [report_artifact],
        "issues": new_issues,
        "final_artifact_id": report_artifact.id,
        "report_rewrite_needed": False,
        "report_rewrite_reason": None,
    }
