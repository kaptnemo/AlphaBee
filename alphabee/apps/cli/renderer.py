"""终端渲染 / 打印（从根 main.py 拆出）。

包含：各 pipeline 阶段的进度打印（``print_node_update_summary``）+ 最终报告渲染
（``render_final_report``）+ 头部/页脚/错误/帮助等辅助。所有函数只做「展示」，不碰业务逻辑。

依赖：colors（颜色）、parsing（``classify_call`` / ``truncate_json``）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alphabee.apps.cli.colors import Color, color, hr
from alphabee.apps.cli.parsing import classify_call, truncate_json

# ---------------------------------------------------------------------------
# Stage definitions (for progress tracking)
# ---------------------------------------------------------------------------

STAGE_MAP: dict[str, tuple[str, str, str]] = {
    "collect_raw_facts": ("📊", "事实采集", Color.CYAN),
    "resolve_industry_context": ("🏭", "行业语境解析", Color.CYAN),
    "resolve_company_track": ("🏷", "公司赛道解析", Color.CYAN),
    "resolve_driver_profile": ("🧭", "驱动画像解析", Color.CYAN),
    "run_analysis_engines": ("⚙️ ", "规则引擎计算", Color.CYAN),
    "explore_conflicts": ("🔬", "冲突探索", Color.MAGENTA),
    "verify_hypotheses": ("🧪", "假设验证", Color.MAGENTA),
    "run_thesis": ("🏛 ", "投资论点生成", Color.BLUE),
    "review_thesis": ("🔍", "论点审查", Color.MAGENTA),
    "generate_report": ("📝", "报告生成", Color.BLUE),
    "review_report": ("🛡️", "报告质量门控", Color.MAGENTA),
    "finalize_message": ("✅", "完成", Color.GREEN),
}


# ---------------------------------------------------------------------------
# Pretty console helpers
# ---------------------------------------------------------------------------


def print_header(query: str, enhance: bool, llm_review: bool) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    flags = []
    if enhance:
        flags.append("增强层")
    if llm_review:
        flags.append("LLM审查")
    flag_str = f"  [{' + '.join(flags)}]" if flags else ""

    print()
    print(hr("═", 70, Color.CYAN))
    print(
        color("  🐝  AlphaBee  ", Color.BOLD, Color.CYAN)
        + color(f"  {now}", Color.GRAY)
        + color(flag_str, Color.YELLOW)
    )
    print(hr("═", 70, Color.CYAN))
    print(color("  📝 问题：", Color.BOLD, Color.WHITE) + query)
    print(hr())
    print()


def print_stage_start(node_name: str, elapsed: float) -> None:
    """Print a pipeline stage transition indicator."""
    info = STAGE_MAP.get(node_name)
    if info is None:
        return
    icon, label, stage_color = info
    print()
    print(hr("─", 60, Color.GRAY))
    print(f"  {icon}  {color(label, Color.BOLD, stage_color)}  {color(f'+{elapsed:.1f}s', Color.GRAY)}")
    print(hr("─", 60, Color.GRAY))
    print()


def print_stage_done(node_name: str, elapsed: float) -> None:
    """Print stage completion line."""
    info = STAGE_MAP.get(node_name)
    if info is None:
        return
    icon, label, stage_color = info
    print(f"  {color('─' * 48, Color.GRAY)}")
    print(f"  {icon}  {color(label, Color.BOLD, stage_color)} 完成  {color(f'+{elapsed:.1f}s', Color.GRAY)}")
    print()


def print_step_model_thinking(text: str, step: int, elapsed: float, agent_path: str = "", depth: int = 0) -> None:
    """LLM 正在推理 / 生成文字。"""
    indent = "  " * depth
    agent_tag = color(f" [{agent_path}]", Color.MAGENTA if depth > 0 else Color.CYAN) if agent_path else ""
    prefix = (
        indent + color(f"[{step:02d}]", Color.GRAY) + " " + color("🤔 模型推理", Color.BOLD, Color.BLUE) + agent_tag
    )
    print(f"{prefix}  {color(f'+{elapsed:.1f}s', Color.GRAY)}")
    display = text.strip()
    if len(display) > 500:
        display = display[:500] + color("  ...(已截断)", Color.DIM)
    for line in display.splitlines():
        print(indent + "       " + color(line, Color.BLUE))
    print()


def print_step_tool_call(
    tool_name: str, args: dict[str, Any], step: int, elapsed: float, agent_path: str = "", depth: int = 0
) -> None:
    """LLM 决定调用某个工具/子代理。"""
    indent = "  " * depth
    kind, display_name, display_args = classify_call(tool_name, args)
    title = "🤖 调用子代理" if kind == "subagent" else "🔧 调用工具"
    tool_color = Color.MAGENTA if kind == "subagent" else Color.YELLOW
    agent_tag = color(f" [{agent_path}]", Color.MAGENTA if depth > 0 else Color.CYAN) if agent_path else ""
    prefix = indent + color(f"[{step:02d}]", Color.GRAY) + " " + color(title, Color.BOLD, tool_color) + agent_tag
    print(f"{prefix}  {color(f'+{elapsed:.1f}s', Color.GRAY)}")
    print(indent + "       " + color(f"▶  {display_name}", Color.BOLD, tool_color))
    if display_args:
        print(indent + "       " + color(f"   入参: {truncate_json(display_args)}", Color.DIM))
    print()


def print_step_tool_result(
    tool_name: str, content: str, status: str, step: int, elapsed: float, agent_path: str = "", depth: int = 0
) -> None:
    """工具调用结果返回（精简输出）。"""
    indent = "  " * depth
    is_subagent = tool_name.endswith("Agent")
    if is_subagent:
        icon = "✅" if status != "error" else "❌"
        title = f"{icon} 子代理结果"
        result_color = Color.MAGENTA if status != "error" else Color.RED
    else:
        icon = "✅" if status != "error" else "❌"
        title = f"{icon} 工具结果"
        result_color = Color.GREEN if status != "error" else Color.RED
    agent_tag = color(f" [{agent_path}]", Color.MAGENTA if depth > 0 else Color.CYAN) if agent_path else ""
    prefix = indent + color(f"[{step:02d}]", Color.GRAY) + " " + color(title, Color.BOLD, result_color) + agent_tag
    print(f"{prefix}  {color(f'+{elapsed:.1f}s', Color.GRAY)}")
    print(indent + "       " + color(f"◀  {tool_name}", Color.BOLD, result_color))
    display = content.strip() if content else "(空)"
    if len(display) > 300:
        display = display[:300] + color("  ...(已截断)", Color.DIM)
    lines = display.splitlines()
    for line in lines[:6]:
        print(indent + "       " + color(line, result_color if status != "error" else Color.RED))
    if len(lines) > 6:
        print(indent + "       " + color(f"   ... 共 {len(lines)} 行", Color.DIM))


# ---------------------------------------------------------------------------
# Per-node progress summary
# ---------------------------------------------------------------------------


def print_node_update_summary(node_name: str, node_update: dict, elapsed: float) -> None:
    """Print structured progress for each orchestrator node from its incremental update."""
    issues: list = node_update.get("issues", [])
    issue_tag = color(f"  ⚠ {len(issues)}", Color.YELLOW) if issues else ""

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
        symbol = ""
        if fin is not None:
            snaps = getattr(fin, "snapshots", None) or fin.get("snapshots", []) if isinstance(fin, dict) else []
            n_snaps = len(snaps)
            symbol = getattr(fin, "symbol", None) or (fin.get("symbol", "") if isinstance(fin, dict) else "")
        has_mkt = mkt is not None
        print(
            f"  📊 已采集 {color(symbol, Color.BOLD, Color.WHITE) if symbol else ''}  "
            f"财报快照 {color(str(n_snaps), Color.BOLD)} 期  "
            f"市值数据 {'✓' if has_mkt else '✗'}"
            f"{issue_tag}"
        )

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "resolve_industry_context":
        ic = _last_artifact("industry_context")
        if not ic:
            print(f"  🏭 行业语境未生成{issue_tag}")
            return

        industry = ic.get("industry", "") if isinstance(ic, dict) else getattr(ic, "industry", "")
        standard = (
            ic.get("classification_standard", "")
            if isinstance(ic, dict)
            else getattr(ic, "classification_standard", "")
        )
        degraded = ic.get("degraded", False) if isinstance(ic, dict) else getattr(ic, "degraded", False)
        peer_count = ic.get("peer_count") if isinstance(ic, dict) else getattr(ic, "peer_count", None)
        if not peer_count:
            peer_count = 0

        def _count_non_null(group) -> int:
            if isinstance(group, dict):
                return sum(1 for v in group.values() if v is not None)
            if hasattr(group, "values"):
                return sum(1 for v in group.values() if v is not None)
            return 0

        def _group_get(group, key, default=None):
            if isinstance(group, dict):
                return group.get(key, default)
            if hasattr(group, "get"):
                return group.get(key, default)
            return default

        valuation = (
            ic.get("valuation_benchmarks", {}) if isinstance(ic, dict) else getattr(ic, "valuation_benchmarks", {})
        )
        financial = (
            ic.get("financial_benchmarks", {}) if isinstance(ic, dict) else getattr(ic, "financial_benchmarks", {})
        )
        growth = ic.get("growth_benchmarks", {}) if isinstance(ic, dict) else getattr(ic, "growth_benchmarks", {})

        pe = _group_get(valuation, "industry_pe_ttm")
        pb = _group_get(valuation, "industry_pb")
        roe = _group_get(financial, "industry_avg_roe")
        debt = _group_get(financial, "industry_avg_debt_ratio")
        margin = _group_get(financial, "industry_avg_gross_margin")
        rev_yoy = _group_get(growth, "industry_revenue_yoy")

        def _fmt(v, suffix="") -> str:
            if v is None:
                return color("—", Color.DIM)
            return f"{v:g}{suffix}"

        def _fmt_ratio(v) -> str:
            """RATIO 口径基准（ROE/负债率/毛利）→ 显示为百分比。"""
            if v is None:
                return color("—", Color.DIM)
            return f"{v * 100:g}%"

        deg_tag = color("  ⚠ 部分降级", Color.YELLOW) if degraded else ""
        peer_tag = color(f"{peer_count} 只成分股", Color.BOLD) if peer_count else color("无成分股", Color.DIM)
        print(
            f"  🏭 行业: {color(industry, Color.BOLD, Color.WHITE)}"
            f"  │ 标准: {color(standard, Color.GRAY) if standard else color('—', Color.DIM)}"
            f"  │ {peer_tag}"
            f"{deg_tag}{issue_tag}"
        )
        print(
            f"       估值  PE {_fmt(pe)}  PB {_fmt(pb)}"
            f"  │ 财务  ROE {_fmt_ratio(roe)}  负债率 {_fmt_ratio(debt)}  毛利 {_fmt_ratio(margin)}"
            f"  │ 成长  营收增速 {_fmt(rev_yoy, '%')}"
        )
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "resolve_company_track":
        track = _last_artifact("company_track")
        if not track:
            print(f"  🏷 公司赛道未生成{issue_tag}")
            return
        label = track.get("track_label", "") if isinstance(track, dict) else getattr(track, "track_label", "")
        bm = track.get("business_model", "") if isinstance(track, dict) else getattr(track, "business_model", "")
        peers = track.get("peer_group", []) if isinstance(track, dict) else getattr(track, "peer_group", [])
        stale = track.get("stale", False) if isinstance(track, dict) else getattr(track, "stale", False)
        bm_label = color(f"｜{bm}", Color.GRAY) if bm else ""
        peer_tag = color(f"{len(peers)} 只对标", Color.BOLD) if peers else color("无对标组", Color.DIM)
        stale_tag = color("  ⚠ 已过期", Color.YELLOW) if stale else ""
        print(
            f"  🏷 赛道: {color(label, Color.BOLD, Color.WHITE) if label else color('—', Color.DIM)}"
            f"{bm_label}  │ {peer_tag}{stale_tag}{issue_tag}"
        )
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "resolve_driver_profile":
        dp = _last_artifact("driver_profile")
        if not dp:
            print(f"  🧭 驱动画像未生成{issue_tag}")
            return
        playbook = dp.get("playbook", "") if isinstance(dp, dict) else getattr(dp, "playbook", "")
        drivers = dp.get("primary_drivers", []) if isinstance(dp, dict) else getattr(dp, "primary_drivers", [])
        secondary = dp.get("secondary_drivers", []) if isinstance(dp, dict) else getattr(dp, "secondary_drivers", [])
        primitives = (
            dp.get("activated_primitives", []) if isinstance(dp, dict) else getattr(dp, "activated_primitives", [])
        )
        fallback = dp.get("fallback", False) if isinstance(dp, dict) else getattr(dp, "fallback", False)
        degraded = dp.get("degraded", False) if isinstance(dp, dict) else getattr(dp, "degraded", False)
        fb_tag = color("（兜底）", Color.DIM) if fallback else ""
        deg_tag = color("  ⚠ 降级", Color.YELLOW) if degraded else ""
        driver_str = "、".join(drivers[:5]) if drivers else color("—", Color.DIM)
        print(
            f"  🧭 框架: {color(playbook, Color.BOLD, Color.WHITE) if playbook else color('—', Color.DIM)}"
            f"{fb_tag}  │ 驱动: {driver_str}{deg_tag}{issue_tag}"
        )
        # 激活原语：框架展开出的分析积木，让用户看到「为什么用这套框架」
        prim_ids = [p.get("id", "") if isinstance(p, dict) else getattr(p, "id", "") for p in primitives]
        if prim_ids:
            print(f"       原语: {color(' · '.join(prim_ids), Color.CYAN)}")
        # 每个原语的首个核心问题（报告要围绕什么展开）
        for p in primitives[:3]:
            pid = p.get("id", "") if isinstance(p, dict) else getattr(p, "id", "")
            questions = p.get("priority_questions", []) if isinstance(p, dict) else getattr(p, "priority_questions", [])
            if questions:
                print(f"         {color('▪', Color.DIM)} {color(pid, Color.CYAN)}: {questions[0]}")
        if secondary:
            print(f"       次驱动: {color('、'.join(secondary[:4]), Color.DIM)}")
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "run_analysis_engines":
        signal_analysis = _last_artifact("signal_analysis") or {}
        anomaly_report = _last_artifact("anomaly_report") or {}
        derived_facts = _last_artifact("derived_facts") or {}

        if hasattr(signal_analysis, "results"):
            results = signal_analysis.results
        else:
            results = signal_analysis.get("results", {}) if isinstance(signal_analysis, dict) else {}
        # Count triggered signals by level
        level_counts: dict[str, int] = {}
        for sid, sval in results.items():
            if isinstance(sval, dict):
                lv = sval.get("level", "none")
                if lv not in ("none", "unknown"):
                    level_counts[lv] = level_counts.get(lv, 0) + 1

        if hasattr(anomaly_report, "anomaly_count"):
            anomaly_count = anomaly_report.anomaly_count
            pattern_count = anomaly_report.pattern_count
        else:
            anomaly_count = anomaly_report.get("anomaly_count", 0) if isinstance(anomaly_report, dict) else 0
            pattern_count = anomaly_report.get("pattern_count", 0) if isinstance(anomaly_report, dict) else 0
        if hasattr(derived_facts, "results"):
            derived_count = len(derived_facts.results)
        else:
            derived_count = len(derived_facts) if derived_facts else 0

        sig_parts = []
        for lv in ("high", "medium", "low", "blocked"):
            n = level_counts.get(lv, 0)
            if n:
                c = Color.RED if lv == "high" else Color.YELLOW if lv == "medium" else Color.DIM
                sig_parts.append(color(f"{lv} {n}", c))
        sig_str = "  ".join(sig_parts) if sig_parts else color("无触发信号", Color.DIM)

        print(
            f"  ⚙️  信号: {sig_str}"
            f"  │ 异常: {color(str(anomaly_count), Color.YELLOW if anomaly_count else Color.DIM)} 项"
            f"  │ 模式: {pattern_count}"
            f"  │ 衍生指标: {derived_count}"
            f"{issue_tag}"
        )

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "explore_conflicts":
        cr = _last_artifact("conflicts_result") or {}
        conflicts = (
            cr.conflicts if hasattr(cr, "conflicts") else (cr.get("conflicts", []) if isinstance(cr, dict) else [])
        )
        if not conflicts:
            print(f"  🔬 未发现显著冲突{issue_tag}")
            return
        n_hyp = sum(len(c.hypotheses) if hasattr(c, "hypotheses") else len(c.get("hypotheses", [])) for c in conflicts)
        high_sev = [
            c
            for c in conflicts
            if (c.severity if hasattr(c, "severity") else c.get("severity")) in ("critical", "high")
        ]
        sev_tag = color(f"  {len(high_sev)} 高危", Color.RED) if high_sev else ""
        print(
            f"  🔬 {color(str(len(conflicts)), Color.BOLD, Color.MAGENTA)} 个冲突"
            f"  {color(str(n_hyp), Color.BOLD)} 条假设"
            f"{sev_tag}{issue_tag}"
        )
        for c in conflicts[:6]:
            sev = c.severity if hasattr(c, "severity") else c.get("severity", "")
            sc = Color.RED if sev in ("critical", "high") else Color.YELLOW if sev == "medium" else Color.GRAY
            n_h = len(c.hypotheses) if hasattr(c, "hypotheses") else len(c.get("hypotheses", []))
            theme = c.theme if hasattr(c, "theme") else c.get("theme", "")
            print(f"      {color(f'[{sev}]', sc)} {theme}  {color(f'{n_h}条假设', Color.DIM)}")
        if len(conflicts) > 6:
            print(color(f"      …共 {len(conflicts)} 个", Color.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "verify_hypotheses":
        vr = _last_artifact("verification_results") or []
        if hasattr(vr, "results"):
            vr_items = [item.model_dump(mode="json") for item in vr.results]
        else:
            vr_items = vr.get("results", []) if isinstance(vr, dict) else vr
        if not vr_items:
            print(f"  🧪 无假设待验证{issue_tag}")
            return
        verified = sum(1 for r in vr_items if r.get("status") in ("verified", "partial"))
        rejected = sum(1 for r in vr_items if r.get("status") == "rejected")
        unknown = len(vr_items) - verified - rejected
        parts = []
        if verified:
            parts.append(color(f"✓ {verified} 条支持", Color.GREEN))
        if rejected:
            parts.append(color(f"✗ {rejected} 条排除", Color.RED))
        if unknown:
            parts.append(color(f"? {unknown} 条待定", Color.GRAY))
        print("  🧪 假设验证完成 — " + "  ".join(parts) + issue_tag)
        verified_items = [r for r in vr_items if r.get("status") in ("verified", "partial")]
        for r in verified_items[:4]:
            tag = color("[partial]", Color.YELLOW) if r.get("status") == "partial" else color("[✓]", Color.GREEN)
            summary = r.get("summary", "")[:90]
            print(f"      {tag} {summary}")
        if len(verified_items) > 4:
            print(color(f"      …共 {len(verified_items)} 条被支持", Color.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "run_thesis":
        av = _last_artifact("thesis_analysis")
        if not av:
            print(f"  🏛  论点未生成{issue_tag}")
            return
        thesis = av.get("thesis") or {}
        conf = thesis.get("overall_confidence", "unknown")
        level = thesis.get("overall_signal_level", "")
        dims = thesis.get("dimensions", [])
        cc = Color.GREEN if conf == "high" else Color.YELLOW if conf == "medium" else Color.RED
        # Conflict data
        cd = av.get("conflict_data") or {}
        verified_n = cd.get("verified_count", 0)
        conflict_tag = color(f"  │ {verified_n} 条验证假设纳入论点", Color.GREEN) if verified_n else ""
        print(
            f"  🏛  置信度: {color(conf, Color.BOLD, cc)}"
            f"  │ 综合信号: {color(level, Color.BOLD) if level else color('—', Color.DIM)}"
            f"  │ 维度: {len(dims)}"
            f"{conflict_tag}{issue_tag}"
        )
        # Show triggered dimensions
        triggered = [d for d in dims if isinstance(d, dict) and d.get("level") not in ("none", "unknown", None)]
        for d in triggered[:5]:
            dlv = d.get("level", "")
            dc = Color.RED if dlv == "high" else Color.YELLOW if dlv == "medium" else Color.DIM
            print(f"      {color(f'[{dlv}]', dc)} {d.get('dimension_id', '')}: {d.get('summary', '')[:70]}")
        if len(triggered) > 5:
            print(color(f"      …共 {len(triggered)} 个触发维度", Color.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "review_thesis":
        av = _last_artifact("thesis_review")
        if not av:
            print(f"  🔍 审查未执行{issue_tag}")
            return
        overall = av.get("overall_status", av.get("review_overall_status", ""))
        oc = Color.GREEN if "pass" in overall.lower() else Color.YELLOW if "warn" in overall.lower() else Color.RED

        # ── 审查发现明细 ──
        # thesis_review artifact 的字段是 blocking_issues / warning_issues
        # （不是 findings），每条已带 [维度名] 前缀，直接打印即可。
        # 额外再合并 node_update 里的 Issue 记录（含 thesis_conflict 等
        # 未写入 artifact 列表的高危项），保证 ⚠ 总数与明细一致。
        blocking_msgs = list(av.get("blocking_issues", []) or [])
        warning_msgs = list(av.get("warning_issues", []) or [])
        seen = set(blocking_msgs) | set(warning_msgs)
        for item in node_update.get("issues", []) or []:
            if isinstance(item, dict):
                sev, cat, msg = item.get("severity", ""), item.get("category", ""), item.get("message", "")
            else:
                sev, cat, msg = (
                    getattr(item, "severity", ""),
                    getattr(item, "category", ""),
                    getattr(item, "message", ""),
                )
            sev, cat, msg = str(sev), str(cat), str(msg)
            if not msg or msg in seen:
                continue
            seen.add(msg)
            if cat == "thesis_conflict" or sev in ("high", "critical"):
                blocking_msgs.append(msg)
            else:
                warning_msgs.append(msg)

        n_block = len(blocking_msgs)
        n_warn = len(warning_msgs)
        print(
            f"  🔍 审查结果: {color(overall, Color.BOLD, oc) if overall else color('—', Color.DIM)}"
            f"  │ 高危 {color(str(n_block), Color.RED, Color.BOLD) if n_block else color('0', Color.DIM)}"
            f"  │ 警告 {color(str(n_warn), Color.YELLOW, Color.BOLD) if n_warn else color('0', Color.DIM)}"
            f"{issue_tag}"
        )
        for msg in blocking_msgs:
            print(f"      {color('[高危]', Color.RED, Color.BOLD)} {msg}")
        for msg in warning_msgs:
            print(f"      {color('[警告]', Color.YELLOW)} {msg}")
        if not blocking_msgs and not warning_msgs:
            print(color("      （未发现审查问题）", Color.DIM))
        print()

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "generate_report":
        av = _last_artifact("report")
        if av:
            title = av.get("title", "")
            conf = av.get("overall_confidence", "")
            cc = Color.GREEN if conf == "high" else Color.YELLOW if conf == "medium" else Color.RED
            print(
                f"  📝 {color(title, Color.BOLD, Color.WHITE) if title else '报告已生成'}"
                f"  置信度: {color(conf, cc) if conf else ''}"
                f"{issue_tag}"
            )
            # ── 报告明细 ──
            risk_count = av.get("risk_count", {}) or {}
            if isinstance(risk_count, dict) and risk_count:
                parts = []
                if risk_count.get("high"):
                    parts.append(color(f"高危 {risk_count['high']}", Color.RED, Color.BOLD))
                if risk_count.get("medium"):
                    parts.append(color(f"中危 {risk_count['medium']}", Color.YELLOW))
                if risk_count.get("low"):
                    parts.append(f"低危 {risk_count['low']}")
                if parts:
                    print("      风险: " + "  ".join(parts))
            sections = av.get("sections", {}) or {}
            if isinstance(sections, dict) and sections:
                present = sum(1 for v in sections.values() if v not in (None, "", [], {}))
                print(color(f"      章节覆盖: {present}/{len(sections)}", Color.DIM))
            summary = av.get("summary", "")
            if summary:
                print(f"      {color('摘要', Color.DIM)}: {str(summary)[:120]}")
            print()
        else:
            print(f"  📝 报告生成中{issue_tag}")

    # ─────────────────────────────────────────────────────────────────
    elif node_name == "review_report":
        ev = _last_artifact("evaluation_report")
        if not ev:
            print(f"  🛡️  门控未执行{issue_tag}")
            return
        passed = bool(ev.get("passed", False))
        blocking = list(ev.get("blocking_issues", []) or [])
        weaknesses = list(ev.get("weaknesses", []) or [])
        strengths = list(ev.get("strengths", []) or [])
        summary = ev.get("summary", "") or ""
        recommendation = ev.get("recommendation", "") or ""
        round_n = node_update.get("report_review_round", 0)
        rewrite_needed = bool(node_update.get("report_rewrite_needed", not passed))
        oc = Color.GREEN if passed else Color.RED
        status_word = "通过" if passed else ("需重写" if rewrite_needed else "未通过")
        print(
            f"  🛡️  门控结果: {color(status_word, Color.BOLD, oc)}"
            f"  │ 轮次 {round_n}"
            f"  │ 阻断 {color(str(len(blocking)), Color.RED, Color.BOLD) if blocking else color('0', Color.DIM)}"
            f"  │ 弱点 {color(str(len(weaknesses)), Color.YELLOW) if weaknesses else '0'}"
            f"  │ 亮点 {len(strengths)}"
            f"{issue_tag}"
        )
        # 定量指标速览（EvaluateMetrics 关键项）
        metrics = ev.get("metrics", {}) or {}
        if isinstance(metrics, dict):
            m_parts = []
            schema_ok = metrics.get("schema_validity")
            if schema_ok is not None:
                m_parts.append(f"结构{'✓' if schema_ok else '✗'}")
            cov = metrics.get("artifact_coverage")
            if isinstance(cov, (int, float)):
                m_parts.append(f"章节覆盖 {cov:.0%}")
            ev_cov = metrics.get("evidence_coverage")
            if isinstance(ev_cov, (int, float)):
                m_parts.append(f"证据引用 {ev_cov:.0%}")
            gr = metrics.get("grounding_score")
            if isinstance(gr, (int, float)):
                m_parts.append(f"可追溯 {gr:.0%}")
            handling = metrics.get("issue_handling")
            if handling is not None:
                m_parts.append(f"风险披露{'✓' if handling else '✗'}")
            cross = metrics.get("cross_source_consistency")
            if cross is not None:
                m_parts.append(f"冲突一致{'✓' if cross else '✗'}")
            if m_parts:
                print(color("      定量: " + "  ".join(m_parts), Color.DIM))
        if summary:
            print(f"      {color('摘要', Color.DIM)}: {summary}")
        for msg in blocking:
            print(f"      {color('[阻断]', Color.RED, Color.BOLD)} {msg}")
        for w in weaknesses:
            print(f"      {color('[弱点]', Color.YELLOW)} {w}")
        for s in strengths[:3]:
            print(f"      {color('[亮点]', Color.GREEN)} {s}")
        if len(strengths) > 3:
            print(color(f"      …共 {len(strengths)} 条亮点", Color.DIM))
        if recommendation:
            print(f"      {color('建议', Color.CYAN)}: {recommendation}")
        print()


def render_final_report(final_payload: dict) -> None:
    """Parse and render the final JSON report payload in a readable format."""
    report = final_payload.get("final_report", {})
    if not report or not isinstance(report, dict):
        return

    title = report.get("title", "投资分析报告")
    summary = report.get("summary", "")
    confidence = report.get("overall_confidence", "unknown")
    risk_count = report.get("risk_count", {})
    sections = report.get("sections", {})

    print()
    print(hr("═", 70, Color.GREEN))
    print(color(f"  📋 {title}", Color.BOLD, Color.GREEN))
    print(hr("═", 70, Color.GREEN))

    conf_colors = {"high": Color.GREEN, "medium": Color.YELLOW, "low": Color.RED}
    conf_c = conf_colors.get(confidence, Color.GRAY)
    print(color(f"  整体置信度: {confidence}", conf_c))

    if risk_count:
        high_r = risk_count.get("high", 0)
        med_r = risk_count.get("medium", 0)
        low_r = risk_count.get("low", 0)
        blocked_r = risk_count.get("blocked", 0)
        parts = []
        if high_r:
            parts.append(color(f"高风险 {high_r}", Color.RED))
        if med_r:
            parts.append(color(f"中风险 {med_r}", Color.YELLOW))
        if low_r:
            parts.append(color(f"低风险 {low_r}", Color.DIM))
        if blocked_r:
            parts.append(color(f"阻塞 {blocked_r}", Color.GRAY))
        if parts:
            print(f"  风险分布: {'  '.join(parts)}")
    print()

    if summary:
        print(color("  💡 摘要", Color.BOLD, Color.WHITE))
        print(f"  {summary}")
        print()

    exec_summary = sections.get("executive_summary", "")
    if exec_summary:
        print(hr("─", 60, Color.CYAN))
        print(color("  📌 核心观点摘要", Color.BOLD, Color.CYAN))
        print(hr("─", 60, Color.CYAN))
        print(f"  {exec_summary}")
        print()

    investment_viewpoint = sections.get("investment_viewpoint", "")
    if investment_viewpoint and investment_viewpoint != "未生成独立投资观点，以下为结构化数据分析。":
        print(hr("─", 60, Color.MAGENTA))
        print(color("  🎯 投资观点展开", Color.BOLD, Color.MAGENTA))
        print(hr("─", 60, Color.MAGENTA))
        vp_display = investment_viewpoint
        if len(vp_display) > 3000:
            vp_display = vp_display[:3000] + "\n\n  ...(已截断，完整内容见最终 JSON)"
        print(vp_display)
        print()

    scenario_analysis = sections.get("scenario_analysis", "")
    if scenario_analysis and scenario_analysis != "未生成情景分析。":
        print(hr("─", 60, Color.MAGENTA))
        print(color("  🔮 情景分析", Color.BOLD, Color.MAGENTA))
        print(hr("─", 60, Color.MAGENTA))
        print(scenario_analysis)
        print()

    key_metrics = sections.get("key_metrics", "")
    if key_metrics:
        print(hr("─", 60, Color.CYAN))
        print(color("  📈 核心指标", Color.BOLD, Color.CYAN))
        print(hr("─", 60, Color.CYAN))
        print(key_metrics)
        print()

    signal_analysis = sections.get("signal_analysis", "")
    if signal_analysis:
        print(hr("─", 60, Color.CYAN))
        print(color("  🚨 风险信号", Color.BOLD, Color.CYAN))
        print(hr("─", 60, Color.CYAN))
        print(signal_analysis)
        print()

    anomaly_detection = sections.get("anomaly_detection", "")
    if anomaly_detection:
        print(hr("─", 60, Color.CYAN))
        print(color("  🔍 勾稽关系异常", Color.BOLD, Color.CYAN))
        print(hr("─", 60, Color.CYAN))
        print(anomaly_detection)
        print()

    conflict_analysis = sections.get("conflict_analysis", "")
    if conflict_analysis and conflict_analysis != "未检测到显著数据矛盾，多维度指标之间逻辑自洽。":
        print(hr("─", 60, Color.MAGENTA))
        print(color("  🔬 数据矛盾", Color.BOLD, Color.MAGENTA))
        print(hr("─", 60, Color.MAGENTA))
        print(conflict_analysis)
        print()

    dimension_analysis = sections.get("dimension_analysis", "")
    if dimension_analysis:
        dim_display = dimension_analysis
        if len(dim_display) > 2000:
            dim_display = dim_display[:2000] + "\n\n  ...(已截断，完整内容见最终 JSON)"
        print(hr("─", 60, Color.CYAN))
        print(color("  🏛 维度分析", Color.BOLD, Color.CYAN))
        print(hr("─", 60, Color.CYAN))
        print(dim_display)
        print()

    review_findings = sections.get("review_findings", "")
    if review_findings and review_findings != "未执行审查":
        print(hr("─", 60, Color.CYAN))
        print(color("  🔎 审查发现", Color.BOLD, Color.CYAN))
        print(hr("─", 60, Color.CYAN))
        print(review_findings)
        print()

    falsification_conditions = sections.get("falsification_conditions", "")
    if falsification_conditions and falsification_conditions != "无可证伪条件。":
        print(hr("─", 60, Color.YELLOW))
        print(color("  ⚖️ 可证伪条件", Color.BOLD, Color.YELLOW))
        print(hr("─", 60, Color.YELLOW))
        print(falsification_conditions)
        print()

    risks = sections.get("risks", "")
    if risks:
        print(hr("─", 60, Color.CYAN))
        print(color("  ⚠️ 主要风险", Color.BOLD, Color.RED))
        print(hr("─", 60, Color.CYAN))
        print(risks)
        print()

    issues = final_payload.get("issues", [])
    if issues:
        print(hr("─", 60, Color.CYAN))
        print(color("  🐞 系统问题", Color.BOLD, Color.YELLOW))
        print(hr("─", 60, Color.CYAN))
        for i in issues:
            sev = i.get("severity", "?")
            cat = i.get("category", "?")
            msg = i.get("message", "")
            issue_color = Color.RED if sev in ("critical", "high") else Color.YELLOW if sev == "medium" else Color.DIM
            print(color(f"  [{sev}] {cat}: {msg}", issue_color))
        print(color(f"  共 {len(issues)} 个问题", Color.DIM))
        print()

    disclaimer = sections.get("disclaimer", "")
    if disclaimer:
        print(hr("─", 60, Color.GRAY))
        print(color(disclaimer, Color.DIM))
        print(hr("─", 60, Color.GRAY))
        print()


# ---------------------------------------------------------------------------
# Footer / error / help
# ---------------------------------------------------------------------------


def print_footer(total_steps: int, total_time: float, enhance: bool, llm_review: bool) -> None:
    print(hr("═", 70, Color.CYAN))
    print(
        color("  ✔  完成", Color.BOLD, Color.GREEN)
        + color(f"   共 {total_steps} 步", Color.GRAY)
        + color(f"   耗时 {total_time:.1f}s", Color.GRAY)
    )
    flags = []
    if enhance:
        flags.append("增强层 ✅")
    if llm_review:
        flags.append("LLM审查 ✅")
    if flags:
        print(color("      模式: " + "  ".join(flags), Color.YELLOW))
    print(hr("═", 70, Color.CYAN))
    print()


def print_error(msg: str) -> None:
    print(hr("═", 70, Color.RED))
    print(color("  ✖  发生错误", Color.BOLD, Color.RED))
    print(hr("─", 70, Color.RED))
    print(color(msg, Color.RED))
    print(hr("═", 70, Color.RED))
    print()


def print_chat_help() -> None:
    print()
    print(color("  💬 多轮对话模式", Color.BOLD, Color.CYAN))
    print(color("  直接输入问题继续追问；输入 /clear 清空上下文，/exit 结束会话。", Color.DIM))
    print()
