"""AlphaBee Agent — Task Entry Point

Usage:
    python main.py "帮我分析一下宁德时代的投资价值"
    python main.py --enhance "分析 600519.SH"          # 启用 LLM 增强层
    python main.py --llm-review --enhance "分析比亚迪"  # 全开
    python main.py                                      # 进入多轮对话模式
    python main.py --chat                               # 强制进入多轮对话模式
    python main.py --no-color                           # 禁用终端颜色
    python main.py --log-dir ./logs                     # 指定日志目录
"""
from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langfuse.langchain import CallbackHandler

from alphabee.orchestrator.agent import alphabee_agent
from alphabee.utils import configure_logging, get_logger
from alphabee.workflow import render_monitor_report, run_framework_monitor
from alphabee.tools.common import extract_symbols_from_query


# ---------------------------------------------------------------------------
# Terminal colors (ANSI, no extra dependency)
# ---------------------------------------------------------------------------

class _C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    GRAY    = "\033[90m"
    WHITE   = "\033[97m"


_USE_COLOR = True


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(codes) + text + _C.RESET


def _hr(char: str = "─", width: int = 70, color: str = _C.GRAY) -> str:
    return _c(char * width, color)


# ---------------------------------------------------------------------------
# Stage definitions (for progress tracking)
# ---------------------------------------------------------------------------

_STAGE_MAP: dict[str, tuple[str, str, str]] = {
    "collect_raw_facts":    ("📊", "事实采集",            _C.CYAN),
    "run_analysis_engines": ("⚙️ ", "规则引擎计算",        _C.CYAN),
    "explore_conflicts":    ("🔬", "冲突探索",            _C.MAGENTA),
    "verify_hypotheses":    ("🧪", "假设验证",            _C.MAGENTA),
    "run_thesis":           ("🏛 ", "投资论点生成",        _C.BLUE),
    "review_thesis":        ("🔍", "论点审查",            _C.MAGENTA),
    "generate_report":      ("📝", "报告生成",            _C.BLUE),
    "review_report":        ("🛡️", "报告质量门控",        _C.MAGENTA),
    "finalize_message":     ("✅", "完成",                _C.GREEN),
}


# ---------------------------------------------------------------------------
# Pretty console helpers
# ---------------------------------------------------------------------------

def _print_header(query: str, enhance: bool, llm_review: bool) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    flags = []
    if enhance:
        flags.append("增强层")
    if llm_review:
        flags.append("LLM审查")
    flag_str = f"  [{' + '.join(flags)}]" if flags else ""

    print()
    print(_hr("═", 70, _C.CYAN))
    print(_c("  🐝  AlphaBee  ", _C.BOLD, _C.CYAN) + _c(f"  {now}", _C.GRAY) + _c(flag_str, _C.YELLOW))
    print(_hr("═", 70, _C.CYAN))
    print(_c("  📝 问题：", _C.BOLD, _C.WHITE) + query)
    print(_hr())
    print()


def _print_stage_start(node_name: str, elapsed: float) -> None:
    """Print a pipeline stage transition indicator."""
    info = _STAGE_MAP.get(node_name)
    if info is None:
        return
    icon, label, color = info
    print()
    print(_hr("─", 60, _C.GRAY))
    print(f"  {icon}  {_c(label, _C.BOLD, color)}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    print(_hr("─", 60, _C.GRAY))
    print()


def _print_stage_done(node_name: str, elapsed: float) -> None:
    """Print stage completion line."""
    info = _STAGE_MAP.get(node_name)
    if info is None:
        return
    icon, label, color = info
    print(f"  {_c('─'*48, _C.GRAY)}")
    print(f"  {icon}  {_c(label, _C.BOLD, color)} 完成  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    print()


def _print_step_model_thinking(text: str, step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """LLM 正在推理 / 生成文字。"""
    indent = "  " * depth
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c("🤔 模型推理", _C.BOLD, _C.BLUE) + agent_tag
    print(f"{prefix}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    display = text.strip()
    if len(display) > 500:
        display = display[:500] + _c("  ...(已截断)", _C.DIM)
    for line in display.splitlines():
        print(indent + "       " + _c(line, _C.BLUE))
    print()


def _truncate_json(data: Any, limit: int = 200) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=None)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _classify_call(tool_name: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return (kind, display_name, display_args) for a tool/subagent call."""
    subagent_type = args.get("subagent_type")
    if tool_name == "task" and isinstance(subagent_type, str) and subagent_type.strip():
        display_args = {k: v for k, v in args.items() if k != "subagent_type"}
        return ("subagent", subagent_type.strip(), display_args)
    return ("tool", tool_name, args)


def _print_step_tool_call(tool_name: str, args: dict[str, Any], step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """LLM 决定调用某个工具/子代理。"""
    indent = "  " * depth
    kind, display_name, display_args = _classify_call(tool_name, args)
    title = "🤖 调用子代理" if kind == "subagent" else "🔧 调用工具"
    color = _C.MAGENTA if kind == "subagent" else _C.YELLOW
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c(title, _C.BOLD, color) + agent_tag
    print(f"{prefix}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    print(indent + "       " + _c(f"▶  {display_name}", _C.BOLD, color))
    if display_args:
        print(indent + "       " + _c(f"   入参: {_truncate_json(display_args)}", _C.DIM))
    print()


def _print_step_tool_result(tool_name: str, content: str, status: str, step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """工具调用结果返回（精简输出）。"""
    indent = "  " * depth
    is_subagent = tool_name.endswith("Agent")
    if is_subagent:
        icon = "✅" if status != "error" else "❌"
        title = f"{icon} 子代理结果"
        color = _C.MAGENTA if status != "error" else _C.RED
    else:
        icon = "✅" if status != "error" else "❌"
        title = f"{icon} 工具结果"
        color = _C.GREEN if status != "error" else _C.RED
    agent_tag = _c(f" [{agent_path}]", _C.MAGENTA if depth > 0 else _C.CYAN) if agent_path else ""
    prefix = indent + _c(f"[{step:02d}]", _C.GRAY) + " " + _c(title, _C.BOLD, color) + agent_tag
    print(f"{prefix}  {_c(f'+{elapsed:.1f}s', _C.GRAY)}")
    print(indent + "       " + _c(f"◀  {tool_name}", _C.BOLD, color))
    display = content.strip() if content else "(空)"
    if len(display) > 300:
        display = display[:300] + _c("  ...(已截断)", _C.DIM)
    lines = display.splitlines()
    for line in lines[:6]:
        print(indent + "       " + _c(line, color if status != "error" else _C.RED))
    if len(lines) > 6:
        print(indent + "       " + _c(f"   ... 共 {len(lines)} 行", _C.DIM))


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _print_node_update_summary(node_name: str, node_update: dict, elapsed: float) -> None:
    """Print structured progress for each orchestrator node (depth==0 only).

    Called every time a top-level node finishes and emits its state update.
    `node_update` is the full state dict returned by the node (via `{**state, ...}`).
    """
    issues: list = node_update.get("issues", [])
    issue_tag = _c(f"  ⚠ {len(issues)}", _C.YELLOW) if issues else ""

    # ── Helper: last artifact of a given type ─────────────────────────
    def _last_artifact(atype: str) -> dict | None:
        for a in reversed(node_update.get("artifacts", []) or []):
            if isinstance(a, dict) and a.get("type") == atype:
                return a.get("value", {}) or {}
            if hasattr(a, "type") and a.type == atype:
                return a.value if isinstance(a.value, dict) else {}
        return None

    # ─────────────────────────────────────────────────────────────────
    if node_name == "collect_raw_facts":
        fin = node_update.get("financial_facts")
        mkt = node_update.get("market_facts")
        n_snaps = 0
        symbol  = ""
        if fin is not None:
            snaps = getattr(fin, "snapshots", None) or fin.get("snapshots", []) if isinstance(fin, dict) else []
            n_snaps = len(snaps)
            symbol = getattr(fin, "symbol", None) or (fin.get("symbol", "") if isinstance(fin, dict) else "")
        has_mkt = mkt is not None
        print(
            f"  📊 已采集 {_c(symbol, _C.BOLD, _C.WHITE) if symbol else ''}  "
            f"财报快照 {_c(str(n_snaps), _C.BOLD)} 期  "
            f"市值数据 {'✓' if has_mkt else '✗'}"
            f"{issue_tag}"
        )

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "run_analysis_engines":
        signal_analysis = node_update.get("signal_analysis") or {}
        anomaly_report  = node_update.get("anomaly_report")  or {}
        derived_facts   = node_update.get("derived_facts")   or {}

        results = signal_analysis.get("results", {}) if isinstance(signal_analysis, dict) else {}
        # Count triggered signals by level
        level_counts: dict[str, int] = {}
        for sid, sval in results.items():
            if isinstance(sval, dict):
                lv = sval.get("level", "none")
                if lv not in ("none", "unknown"):
                    level_counts[lv] = level_counts.get(lv, 0) + 1

        anomaly_count  = anomaly_report.get("anomaly_count", 0) if isinstance(anomaly_report, dict) else 0
        pattern_count  = anomaly_report.get("pattern_count", 0) if isinstance(anomaly_report, dict) else 0
        derived_count  = len(derived_facts) if derived_facts else 0

        sig_parts = []
        for lv in ("high", "medium", "low", "blocked"):
            n = level_counts.get(lv, 0)
            if n:
                c = _C.RED if lv == "high" else _C.YELLOW if lv == "medium" else _C.DIM
                sig_parts.append(_c(f"{lv} {n}", c))
        sig_str = "  ".join(sig_parts) if sig_parts else _c("无触发信号", _C.DIM)

        print(
            f"  ⚙️  信号: {sig_str}"
            f"  │ 异常: {_c(str(anomaly_count), _C.YELLOW if anomaly_count else _C.DIM)} 项"
            f"  │ 模式: {pattern_count}"
            f"  │ 衍生指标: {derived_count}"
            f"{issue_tag}"
        )

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "explore_conflicts":
        cr = node_update.get("conflicts_result") or {}
        conflicts = cr.get("conflicts", []) if isinstance(cr, dict) else []
        if not conflicts:
            print(f"  🔬 未发现显著冲突{issue_tag}")
            return
        n_hyp = sum(len(c.get("hypotheses", [])) for c in conflicts)
        high_sev = [c for c in conflicts if c.get("severity") in ("critical", "high")]
        sev_tag = _c(f"  {len(high_sev)} 高危", _C.RED) if high_sev else ""
        print(
            f"  🔬 {_c(str(len(conflicts)), _C.BOLD, _C.MAGENTA)} 个冲突"
            f"  {_c(str(n_hyp), _C.BOLD)} 条假设"
            f"{sev_tag}{issue_tag}"
        )
        for c in conflicts[:6]:
            sev = c.get("severity", "")
            sc = _C.RED if sev in ("critical", "high") else _C.YELLOW if sev == "medium" else _C.GRAY
            n_h = len(c.get("hypotheses", []))
            print(f"      {_c(f'[{sev}]', sc)} {c.get('theme','')}"
                  f"  {_c(f'{n_h}条假设', _C.DIM)}")
        if len(conflicts) > 6:
            print(_c(f"      …共 {len(conflicts)} 个", _C.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "verify_hypotheses":
        vr = node_update.get("verification_results") or []
        if not vr:
            print(f"  🧪 无假设待验证{issue_tag}")
            return
        verified = sum(1 for r in vr if r.get("status") in ("verified", "partial"))
        rejected = sum(1 for r in vr if r.get("status") == "rejected")
        unknown  = len(vr) - verified - rejected
        parts = []
        if verified: parts.append(_c(f"✓ {verified} 条支持", _C.GREEN))
        if rejected: parts.append(_c(f"✗ {rejected} 条排除", _C.RED))
        if unknown:  parts.append(_c(f"? {unknown} 条待定", _C.GRAY))
        print(f"  🧪 假设验证完成 — " + "  ".join(parts) + issue_tag)
        verified_items = [r for r in vr if r.get("status") in ("verified", "partial")]
        for r in verified_items[:4]:
            tag = _c("[partial]", _C.YELLOW) if r.get("status") == "partial" else _c("[✓]", _C.GREEN)
            summary = r.get("summary", "")[:90]
            print(f"      {tag} {summary}")
        if len(verified_items) > 4:
            print(_c(f"      …共 {len(verified_items)} 条被支持", _C.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "run_thesis":
        av = _last_artifact("thesis_analysis")
        if not av:
            print(f"  🏛  论点未生成{issue_tag}")
            return
        thesis = av.get("thesis") or {}
        conf   = thesis.get("overall_confidence", "unknown")
        level  = thesis.get("overall_signal_level", "")
        dims   = thesis.get("dimensions", [])
        cc     = _C.GREEN if conf == "high" else _C.YELLOW if conf == "medium" else _C.RED
        # Conflict data
        cd         = av.get("conflict_data") or {}
        verified_n = cd.get("verified_count", 0)
        conflict_tag = (
            _c(f"  │ {verified_n} 条验证假设纳入论点", _C.GREEN) if verified_n else ""
        )
        print(
            f"  🏛  置信度: {_c(conf, _C.BOLD, cc)}"
            f"  │ 综合信号: {_c(level, _C.BOLD) if level else _c('—', _C.DIM)}"
            f"  │ 维度: {len(dims)}"
            f"{conflict_tag}{issue_tag}"
        )
        # Show triggered dimensions
        triggered = [d for d in dims if isinstance(d, dict) and d.get("level") not in ("none", "unknown", None)]
        for d in triggered[:5]:
            dlv = d.get("level", "")
            dc  = _C.RED if dlv == "high" else _C.YELLOW if dlv == "medium" else _C.DIM
            print(f"      {_c(f'[{dlv}]', dc)} {d.get('dimension_id','')}: {d.get('summary','')[:70]}")
        if len(triggered) > 5:
            print(_c(f"      …共 {len(triggered)} 个触发维度", _C.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "review_thesis":
        av = _last_artifact("thesis_review")
        if not av:
            print(f"  🔍 审查未执行{issue_tag}")
            return
        overall = av.get("overall_status", av.get("review_overall_status", ""))
        findings = av.get("findings", []) or []
        n_warn   = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") in ("high", "critical"))
        oc = _C.GREEN if "pass" in overall.lower() else _C.YELLOW if "warn" in overall.lower() else _C.RED
        print(
            f"  🔍 审查结果: {_c(overall, _C.BOLD, oc) if overall else _c('—', _C.DIM)}"
            f"  │ 发现 {len(findings)} 项"
            f"  │ 高危 {_c(str(n_warn), _C.RED) if n_warn else '0'}"
            f"{issue_tag}"
        )
        for f in findings[:3]:
            if isinstance(f, dict):
                fsev = f.get("severity", "")
                fc   = _C.RED if fsev in ("high","critical") else _C.YELLOW if fsev == "medium" else _C.DIM
                print(f"      {_c(f'[{fsev}]', fc)} {f.get('message','')[:80]}")
        if len(findings) > 3:
            print(_c(f"      …共 {len(findings)} 项审查发现", _C.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "generate_report":
        av = _last_artifact("final_report")
        if av:
            title = av.get("title", "")
            conf  = av.get("overall_confidence", "")
            cc    = _C.GREEN if conf == "high" else _C.YELLOW if conf == "medium" else _C.RED
            print(
                f"  📝 {_c(title, _C.BOLD, _C.WHITE) if title else '报告已生成'}"
                f"  置信度: {_c(conf, cc) if conf else ''}"
                f"{issue_tag}"
            )
        else:
            print(f"  📝 报告生成中{issue_tag}")

def _render_final_report(final_payload: dict) -> None:
    """Parse and render the final JSON report payload in a readable format."""
    report = final_payload.get("final_report", {})
    if not report or not isinstance(report, dict):
        return

    title = report.get("title", "财报质量体检报告")
    summary = report.get("summary", "")
    confidence = report.get("overall_confidence", "unknown")
    risk_count = report.get("risk_count", {})
    sections = report.get("sections", {})

    print()
    print(_hr("═", 70, _C.GREEN))
    print(_c(f"  📋 {title}", _C.BOLD, _C.GREEN))
    print(_hr("═", 70, _C.GREEN))

    # Confidence badge
    conf_colors = {"high": _C.GREEN, "medium": _C.YELLOW, "low": _C.RED}
    conf_c = conf_colors.get(confidence, _C.GRAY)
    print(_c(f"  整体置信度: {confidence}", conf_c))

    # Risk summary
    if risk_count:
        high_r = risk_count.get("high", 0)
        med_r = risk_count.get("medium", 0)
        low_r = risk_count.get("low", 0)
        blocked_r = risk_count.get("blocked", 0)
        parts = []
        if high_r:
            parts.append(_c(f"高风险 {high_r}", _C.RED))
        if med_r:
            parts.append(_c(f"中风险 {med_r}", _C.YELLOW))
        if low_r:
            parts.append(_c(f"低风险 {low_r}", _C.DIM))
        if blocked_r:
            parts.append(_c(f"阻塞 {blocked_r}", _C.GRAY))
        if parts:
            print(f"  风险分布: {'  '.join(parts)}")
    print()

    # Summary
    if summary:
        print(_c("  💡 摘要", _C.BOLD, _C.WHITE))
        print(f"  {summary}")
        print()

    # Section: executive_summary
    exec_summary = sections.get("executive_summary", "")
    if exec_summary:
        print(_hr("─", 60, _C.CYAN))
        print(_c("  📌 核心发现", _C.BOLD, _C.CYAN))
        print(_hr("─", 60, _C.CYAN))
        print(f"  {exec_summary}")
        print()

    # Section: key_metrics
    key_metrics = sections.get("key_metrics", "")
    if key_metrics:
        print(_hr("─", 60, _C.CYAN))
        print(_c("  📈 核心指标", _C.BOLD, _C.CYAN))
        print(_hr("─", 60, _C.CYAN))
        print(key_metrics)
        print()

    # Section: signal_analysis
    signal_analysis = sections.get("signal_analysis", "")
    if signal_analysis:
        print(_hr("─", 60, _C.CYAN))
        print(_c("  🚨 风险信号", _C.BOLD, _C.CYAN))
        print(_hr("─", 60, _C.CYAN))
        print(signal_analysis)
        print()

    # Section: investment_thesis (truncated)
    investment_thesis = sections.get("investment_thesis", "")
    if investment_thesis:
        thesis_display = investment_thesis
        if len(thesis_display) > 2000:
            thesis_display = thesis_display[:2000] + "\n\n  ...(已截断，完整内容见最终 JSON)"
        print(_hr("─", 60, _C.CYAN))
        print(_c("  🏛 投资论点", _C.BOLD, _C.CYAN))
        print(_hr("─", 60, _C.CYAN))
        print(thesis_display)
        print()

    # Section: review_findings
    review_findings = sections.get("review_findings", "")
    if review_findings and review_findings != "未执行审查":
        print(_hr("─", 60, _C.CYAN))
        print(_c("  🔎 审查发现", _C.BOLD, _C.CYAN))
        print(_hr("─", 60, _C.CYAN))
        print(review_findings)
        print()

    # Section: conflict_analysis — 冲突探索与假设验证
    conflict_analysis = final_payload.get("conflict_analysis", {})
    if conflict_analysis and conflict_analysis.get("conflict_count", 0) > 0:
        verified = conflict_analysis.get("verified_count", 0)
        rejected = conflict_analysis.get("rejected_count", 0)
        total_h  = conflict_analysis.get("hypothesis_count", 0)
        unknown  = total_h - verified - rejected

        print(_hr("─", 60, _C.MAGENTA))
        print(_c("  🔬 冲突探索 · 假设验证", _C.BOLD, _C.MAGENTA))
        print(_hr("─", 60, _C.MAGENTA))
        stats_parts = []
        stats_parts.append(_c(f"冲突 {conflict_analysis['conflict_count']} 个", _C.WHITE))
        stats_parts.append(_c(f"假设 {total_h} 条", _C.WHITE))
        if verified:
            stats_parts.append(_c(f"✓ 验证 {verified}", _C.GREEN))
        if rejected:
            stats_parts.append(_c(f"✗ 排除 {rejected}", _C.RED))
        if unknown:
            stats_parts.append(_c(f"? 待定 {unknown}", _C.GRAY))
        print("  " + "  ".join(stats_parts))
        print()

        # Conflicts summary
        for ci in conflict_analysis.get("conflicts_summary", []):
            sev = ci.get("severity", "")
            sev_color = _C.RED if sev in ("critical", "high") else _C.YELLOW if sev == "medium" else _C.GRAY
            print(f"  {_c(f'[{sev}]', sev_color)} {_c(ci.get('theme',''), _C.BOLD, _C.WHITE)}")
            desc = ci.get("description", "")
            if desc:
                print(f"        {_c(desc, _C.DIM)}")
        print()

        # Verified hypotheses
        verified_hyps = conflict_analysis.get("verified_hypotheses", [])
        if verified_hyps:
            print(_c("  ✅ 被证实假设：", _C.BOLD, _C.GREEN))
            for h in verified_hyps:
                status = h.get("status", "")
                status_tag = _c("[partial]", _C.YELLOW) if status == "partial" else _c("[verified]", _C.GREEN)
                print(f"    {status_tag} {h.get('description', '')}")
            print()

    # Section: risks
    risks = sections.get("risks", "")
    if risks:
        print(_hr("─", 60, _C.CYAN))
        print(_c("  ⚠️ 主要风险", _C.BOLD, _C.RED))
        print(_hr("─", 60, _C.CYAN))
        print(risks)
        print()

    # Issues from system
    issues = final_payload.get("issues", [])
    if issues:
        print(_hr("─", 60, _C.CYAN))
        print(_c("  🐞 系统问题", _C.BOLD, _C.YELLOW))
        print(_hr("─", 60, _C.CYAN))
        for i in issues:
            sev = i.get("severity", "?")
            cat = i.get("category", "?")
            msg = i.get("message", "")
            color = _C.RED if sev in ("critical", "high") else _C.YELLOW if sev == "medium" else _C.DIM
            print(_c(f"  [{sev}] {cat}: {msg}", color))
        # if len(issues) > 5:
        print(_c(f"  共 {len(issues)} 个问题", _C.DIM))
        print()

    # Disclaimer
    disclaimer = sections.get("disclaimer", "")
    if disclaimer:
        print(_hr("─", 60, _C.GRAY))
        print(_c(disclaimer, _C.DIM))
        print(_hr("─", 60, _C.GRAY))
        print()


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

def _print_footer(total_steps: int, total_time: float, enhance: bool, llm_review: bool) -> None:
    print(_hr("═", 70, _C.CYAN))
    print(
        _c("  ✔  完成", _C.BOLD, _C.GREEN)
        + _c(f"   共 {total_steps} 步", _C.GRAY)
        + _c(f"   耗时 {total_time:.1f}s", _C.GRAY)
    )
    flags = []
    if enhance:
        flags.append("增强层 ✅")
    if llm_review:
        flags.append("LLM审查 ✅")
    if flags:
        print(_c("      模式: " + "  ".join(flags), _C.YELLOW))
    print(_hr("═", 70, _C.CYAN))
    print()


def _print_error(msg: str) -> None:
    print(_hr("═", 70, _C.RED))
    print(_c("  ✖  发生错误", _C.BOLD, _C.RED))
    print(_hr("─", 70, _C.RED))
    print(_c(msg, _C.RED))
    print(_hr("═", 70, _C.RED))
    print()


def _print_chat_help() -> None:
    print()
    print(_c("  💬 多轮对话模式", _C.BOLD, _C.CYAN))
    print(_c("  直接输入问题继续追问；输入 /clear 清空上下文，/exit 结束会话。", _C.DIM))
    print()


# ---------------------------------------------------------------------------
# Message content extractor
# ---------------------------------------------------------------------------

def _extract_text(content: Any) -> str:
    """Extract plain text from a message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "thinking"):
                    parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _tool_name_from_call(tool_call: dict) -> str:
    return tool_call.get("name", "unknown_tool")


def _tool_args_from_call(tool_call: dict) -> dict:
    args = tool_call.get("args", {})
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return {"raw": args}
    return args if isinstance(args, dict) else {}


def _tool_label_from_call(tool_call: dict) -> str:
    tool_name = _tool_name_from_call(tool_call)
    args = _tool_args_from_call(tool_call)
    kind, display_name, _ = _classify_call(tool_name, args)
    return f"{kind}:{display_name}"


def _parse_namespace(namespace: tuple) -> tuple[str, int]:
    """Convert a LangGraph namespace tuple into a human-readable path and depth.

    Namespace format: ("AgentName:uuid", "ChildAgent:uuid", ...)
    Returns: ("Orchestrator > CrossAnalysisAgent > FundamentalAgent", depth)
    """
    if not namespace:
        return "Orchestrator", 0
    parts = []
    for seg in namespace:
        name = seg.split(":")[0] if ":" in seg else seg
        parts.append(name)
    return " > ".join(parts), len(parts)


def _parse_report_payload(content: Any) -> dict | None:
    """Try to parse the final AIMessage content as a JSON report payload."""
    text = _extract_text(content)
    if not text:
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and "final_report" in payload:
            return payload
    except (json.JSONDecodeError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Core streaming runner
# ---------------------------------------------------------------------------

def _append_turn_history(history: list[Any], query: str, answer: str) -> None:
    history.append(HumanMessage(content=query))
    if answer:
        history.append(AIMessage(content=answer))


async def run_query(
    query: str,
    history: list[Any] | None = None,
    *,
    enhance: bool = False,
    llm_review: bool = False,
) -> str:
    logger = get_logger("main")
    start_ts = time.monotonic()
    conversation = [*(history or []), HumanMessage(content=query)]

    _print_header(query, enhance, llm_review)

    # Track pipeline stages for progress reporting
    active_stage: str | None = None
    stage_entry_ts = start_ts
    step = 0
    final_answer = ""
    report_payload: dict | None = None

    # (namespace_tuple, tool_call_id) → display_name
    pending_calls: dict[tuple, str] = {}

    logger.info(
        "query_start",
        query=query,
        history_messages=len(conversation) - 1,
        enhance=enhance,
        llm_review=llm_review,
    )

    # Create a fresh Langfuse handler per request
    trace_handler = CallbackHandler()

    try:
        async for namespace, chunk in alphabee_agent.astream(
            {"messages": conversation, "enhance": enhance, "llm_review": llm_review},
            config={"callbacks": [trace_handler]},
            stream_mode="updates",
            subgraphs=True,
        ):
            elapsed = time.monotonic() - start_ts
            agent_path, depth = _parse_namespace(namespace)

            for node_name, node_update in chunk.items():
                if not node_update:
                    continue

                # ── Stage transition detection ──
                if node_name in _STAGE_MAP and depth == 0:
                    if active_stage is not None and active_stage != node_name:
                        _print_stage_done(
                            active_stage,
                            time.monotonic() - stage_entry_ts,
                        )
                    if active_stage != node_name:
                        _print_stage_start(node_name, elapsed)
                        active_stage = node_name
                        stage_entry_ts = time.monotonic()

                if depth == 0:
                    # Orchestrator node completed — print structured summary.
                    # finalize_message is special: it embeds the final JSON payload
                    # inside an AIMessage. Capture that before skipping message loop.
                    if node_name == "finalize_message":
                        for msg in node_update.get("messages", []):
                            if isinstance(msg, AIMessage):
                                text = _extract_text(msg.content)
                                if text:
                                    final_answer = text
                                    break
                    _print_node_update_summary(node_name, node_update, elapsed)
                    continue

                # depth > 0: messages from nested subagent graphs
                messages: list = node_update.get("messages", [])
                if not messages:
                    continue

                for msg in messages:
                    step += 1

                    # ── AIMessage: model thinking or tool dispatch ──
                    if isinstance(msg, AIMessage):
                        text = _extract_text(msg.content)
                        tool_calls: list = msg.tool_calls or []

                        logger.info(
                            "model_output",
                            step=step,
                            agent=agent_path,
                            node=node_name,
                            has_text=bool(text),
                            tool_calls=[_tool_label_from_call(tc) for tc in tool_calls],
                            text_length=len(text),
                            elapsed=round(elapsed, 2),
                        )

                        if text:
                            _print_step_model_thinking(text, step, elapsed, agent_path, depth)
                            final_answer = text

                        for tc in tool_calls:
                            step += 1
                            tname = _tool_name_from_call(tc)
                            targs = _tool_args_from_call(tc)
                            tc_id = tc.get("id", "")
                            if tc_id:
                                pending_calls[(namespace, tc_id)] = tname
                            _print_step_tool_call(tname, targs, step, elapsed, agent_path, depth)
                            logger.info(
                                "tool_call",
                                step=step,
                                agent=agent_path,
                                node=node_name,
                                tool=tname,
                                args=targs,
                                call_id=tc_id,
                                elapsed=round(elapsed, 2),
                            )

                    # ── ToolMessage: result from a tool/subagent ──
                    elif isinstance(msg, ToolMessage):
                        tc_id = getattr(msg, "tool_call_id", "")
                        tname = pending_calls.pop(
                            (namespace, tc_id),
                            getattr(msg, "name", None) or "tool",
                        )
                        status = getattr(msg, "status", "success") or "success"
                        content_text = _extract_text(msg.content)

                        logger.info(
                            "tool_result",
                            step=step,
                            agent=agent_path,
                            node=node_name,
                            tool=tname,
                            status=status,
                            result_length=len(content_text),
                            elapsed=round(elapsed, 2),
                        )
                        _print_step_tool_result(
                            tname, content_text, status, step, elapsed, agent_path, depth
                        )

    except KeyboardInterrupt:
        print()
        print(_c("  ⚠  已中断", _C.BOLD, _C.YELLOW))
        logger.warning("query_interrupted", elapsed=round(time.monotonic() - start_ts, 2))
        sys.exit(0)
    except Exception as exc:
        tb = traceback.format_exc()
        _print_error(f"{type(exc).__name__}: {exc}\n\n{tb}")
        logger.error(
            "query_failed",
            error=str(exc),
            traceback=tb,
            elapsed=round(time.monotonic() - start_ts, 2),
        )
        sys.exit(1)

    total_time = time.monotonic() - start_ts

    # ── Final stage done ──
    if active_stage:
        _print_stage_done(active_stage, time.monotonic() - stage_entry_ts)

    # ── Render report ──
    # Try to parse the final AIMessage as a JSON report payload
    report_payload = _parse_report_payload(final_answer)
    if report_payload:
        _render_final_report(report_payload)
    elif final_answer:
        # Fallback: raw answer display
        print()
        print(_hr("─", 70, _C.GREEN))
        print(_c("  💡 最终回答", _C.BOLD, _C.GREEN))
        print(_hr("─", 70, _C.GREEN))
        print()
        # Truncate very long raw answers
        if len(final_answer) > 3000:
            print(final_answer[:3000])
            print(_c("  ...(已截断，完整内容见日志)", _C.DIM))
        else:
            print(final_answer)
        print()

    # ── Record capture for task records ──
    if report_payload:
        try:
            from alphabee.task_records import TaskRecorder, TaskStore
            symbols = extract_symbols_from_query(query)
            symbol = list(symbols.values())[0] if symbols else None
            artifacts_list = report_payload.get("artifacts", [])
            recorder = TaskRecorder()
            record = recorder.capture(
                query=query,
                symbol=symbol,
                flags={"enhance": enhance, "llm_review": llm_review},
                payload=report_payload,
                artifacts=artifacts_list,
                start_ts=start_ts,
            )
            store = TaskStore()
            store.save(record)
            logger.info("task_record_saved", task_id=record.task_id, symbol=symbol)
        except Exception as exc:
            logger.warning("task_record_capture_failed", error=str(exc))

    _print_footer(step, total_time, enhance, llm_review)
    logger.info(
        "query_done",
        total_steps=step,
        total_time=round(total_time, 2),
        enhance=enhance,
        llm_review=llm_review,
    )
    return final_answer


async def run_chat_session(
    initial_query: str | None = None,
    *,
    enhance: bool = False,
    llm_review: bool = False,
) -> None:
    history: list[Any] = []
    turn = 1

    _print_chat_help()

    if initial_query:
        initial_query = _normalize_query(initial_query)
        answer = await run_query(initial_query, history, enhance=enhance, llm_review=llm_review)
        _append_turn_history(history, initial_query, answer)
        turn += 1

    while True:
        try:
            raw = input(_c(f"[{turn:02d}] 你> ", _C.BOLD, _C.CYAN))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        query = raw.strip()
        if not query:
            continue

        command = query.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            print()
            print(_c("  👋 会话结束", _C.DIM))
            print()
            break
        if command == "/clear":
            history.clear()
            turn = 1
            print()
            print(_c("  ♻ 上下文已清空", _C.DIM))
            print()
            continue

        query = _normalize_query(query)
        answer = await run_query(query, history, enhance=enhance, llm_review=llm_review)
        _append_turn_history(history, query, answer)
        turn += 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AlphaBee — AI 投资分析助手",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="分析问题；不传则进入多轮对话模式",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        default=False,
        help="进入多轮对话模式；可与初始问题一起使用",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="禁用终端颜色输出",
    )
    parser.add_argument(
        "--log-dir",
        default="./logs",
        help="日志文件目录（默认: ./logs）",
    )
    parser.add_argument(
        "--enhance",
        action="store_true",
        default=False,
        help="启用 LLM 增强层（跨信号模式识别 + 行业/生命周期语境化 + 用户意图适配）",
    )
    parser.add_argument(
        "--llm-review",
        action="store_true",
        default=False,
        dest="llm_review",
        help="启用 LLM 审查层（定性评估证据充分性 / 信号一致性 / 语境合理性）",
    )
    parser.add_argument(
        "--monitor-framework",
        default=None,
        help="观察框架 Markdown 路径。提供后将进入持续跟踪模式。",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="监控模式下的股票代码，例如 300760 或 300760.SZ",
    )
    parser.add_argument(
        "--monitor-periods",
        type=int,
        default=8,
        help="监控模式拉取的财报期数（默认: 8）",
    )
    parser.add_argument(
        "--task-stats",
        action="store_true",
        default=False,
        help="输出最近运行记录的统计摘要",
    )
    parser.add_argument(
        "--distill",
        action="store_true",
        default=False,
        help="基于运行记录产出规则蒸馏建议报告（需 LLM）",
    )
    parser.add_argument(
        "--task-history",
        default=None,
        help="查看指定标的的历史运行记录，如 600519.SH",
    )
    parser.add_argument(
        "--task-record",
        default=None,
        help="查看指定 task_id 的完整运行记录",
    )
    return parser.parse_args()


def _normalize_query(query: str) -> str:
    """Strip accidental 'key=value' prefix if user ran: python main.py query=..."""
    if "=" in query and query.index("=") < 20 and not query.startswith("http"):
        key, _, rest = query.partition("=")
        if key.strip().isidentifier():
            return rest.strip()
    return query


def _handle_task_cli(args: argparse.Namespace) -> None:
    """处理 task records 相关的 CLI 命令。"""
    from alphabee.task_records import TaskAnalyzer, TaskStore, distill

    store = TaskStore()
    count = store.count()

    if count == 0:
        print(_c("  ℹ 暂无运行记录。请先执行分析任务。", _C.DIM))
        return

    print()
    print(_hr("═", 70, _C.CYAN))
    print(_c("  📊 AlphaBee Task Records", _C.BOLD, _C.CYAN))
    print(_c(f"  共 {count} 条记录", _C.DIM))
    print(_hr("═", 70, _C.CYAN))
    print()

    if args.task_stats:
        analyzer = TaskAnalyzer(store)
        summary = analyzer.summary()

        print(_c("  📈 执行概况", _C.BOLD, _C.WHITE))
        print(f"  总运行: {summary['run_count']} 次, 平均耗时: {summary['avg_duration_s']}s")
        print()

        print(_c("  🚨 最高频问题类别", _C.BOLD, _C.WHITE))
        for cat, cnt in summary["top_issues"][:8]:
            print(f"  {cat:30s} {cnt:4d}")

        print()
        print(_c("  🏷 最高频问题模式", _C.BOLD, _C.WHITE))
        for kw, cnt in summary["top_message_clusters"][:8]:
            print(f"  {kw:30s} {cnt:4d}")

        print()
        print(_c("  📡 信号触发率", _C.BOLD, _C.WHITE))
        print(f"  {'信号':30s} {'触发率':>6s}  {'High%':>6s}  {'Med%':>6s}  {'Low%':>6s}  {'Block%':>6s}")
        for sid, stats in summary["signal_trigger_rates"].items():
            print(f"  {sid:30s} {stats['triggered_pct']:5.0f}%  "
                  f"{stats['high_pct']:5.0f}%  {stats['medium_pct']:5.0f}%  "
                  f"{stats['low_pct']:5.0f}%  {stats['blocked_pct']:5.0f}%")

        print()
        print(_c("  🎯 Flag 影响 (overall_confidence) ", _C.BOLD, _C.WHITE))
        fi = summary["flag_impact"]
        for group, data in fi.items():
            if data.get("count", 0) > 0:
                print(f"  {group:20s}: H={data['high_pct']:5.1f}% M={data['medium_pct']:5.1f}% L={data['low_pct']:5.1f}% ({data['count']}次)")

        print()
        print(_c("  ⚠ 单证据维度", _C.BOLD, _C.WHITE))
        for dim, cnt in summary["single_evidence_dims"][:5]:
            print(f"  {dim:30s} {cnt:4d}")
        if not summary["single_evidence_dims"]:
            print("  (无)")

        print()
        print(_c("  🏭 语境适配缺口行业", _C.BOLD, _C.WHITE))
        for ind, cnt in summary["context_gap_industries"][:5]:
            print(f"  {ind:30s} {cnt:4d}")
        if not summary["context_gap_industries"]:
            print("  (无)")

    if args.distill:
        print(_c("  🔬 正在生成蒸馏分析报告...", _C.BOLD, _C.YELLOW))
        print()
        try:
            report = distill()
            print(report)
        except Exception as exc:
            print(_c(f"  ❌ 蒸馏失败: {exc}", _C.RED))

    if args.task_history:
        target = args.task_history.strip()
        records = [r for r in store.load_all() if r.symbol == target]
        if not records:
            print(_c(f"  ℹ 未找到标的 {target} 的历史记录", _C.DIM))
            return
        print(_c(f"  📋 {target} 历史记录 ({len(records)} 条)", _C.BOLD, _C.WHITE))
        print()
        print(f"  {'时间':22s} {'置信度':8s} {'审查':14s} {'异常':4s} {'问题':4s} {'耗时'}")
        print(f"  {'-'*22} {'-'*8} {'-'*14} {'-'*4} {'-'*4} {'-'*6}")
        for r in records:
            print(f"  {r.timestamp[:19]:22s} {r.overall_confidence:8s} "
                  f"{r.review_overall_status:14s} {r.anomaly_triggered_count:4d} "
                  f"{len(r.issues):4d} {r.total_duration_s:5.0f}s")

    if args.task_record:
        tid = args.task_record.strip()
        record = store.load(tid)
        if record is None:
            print(_c(f"  ❌ 未找到记录: {tid}", _C.RED))
            return
        import json as _json
        print(_json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2))

    print()
    print(_hr("═", 70, _C.CYAN))


def main() -> None:
    global _USE_COLOR

    args = _parse_args()
    if args.monitor_framework and not args.symbol:
        raise SystemExit("--monitor-framework 模式下必须同时提供 --symbol")

    if args.query:
        args.query = _normalize_query(args.query)

    if args.no_color or not sys.stdout.isatty():
        _USE_COLOR = False

    import logging

    configure_logging(log_dir=Path(args.log_dir))

    # Keep file logging but suppress the console handler so it doesn't
    # mix with our pretty-printed output.
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setLevel(logging.WARNING)

    if args.monitor_framework:
        start_ts = time.monotonic()
        _print_header(
            f"监控框架：{args.monitor_framework} | 标的：{args.symbol}",
            enhance=args.enhance,
            llm_review=args.llm_review,
        )
        result = asyncio.run(
            run_framework_monitor(
                framework_path=args.monitor_framework,
                symbol=args.symbol,
                periods=args.monitor_periods,
            )
        )
        print(_c("  💡 最终回答", _C.BOLD, _C.GREEN))
        print(render_monitor_report(result))
        _print_footer(1, time.monotonic() - start_ts, enhance=False, llm_review=False)
        return

    # ── Task records CLI ──
    if args.task_stats or args.distill or args.task_history or args.task_record:
        _handle_task_cli(args)
        return

    if args.chat or not args.query:
        asyncio.run(run_chat_session(
            args.query,
            enhance=args.enhance,
            llm_review=args.llm_review,
        ))
        return

    asyncio.run(run_query(
        args.query,
        enhance=args.enhance,
        llm_review=args.llm_review,
    ))


if __name__ == "__main__":
    main()
