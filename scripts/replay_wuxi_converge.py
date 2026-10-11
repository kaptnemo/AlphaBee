#!/usr/bin/env python
"""药明康德（603259.SH）Tier C 收敛层真机重放（DRIVER_PROFILE_P0_FIX_PLAN §10.4，步骤四）。

只读 Tier B 研究缓存 ``data/driver_profiles/603259.SH.json``（真实 LLM 产物），把其中的
``output.driver_hypotheses`` 原样喂给 :func:`alphabee.domain_context.research.converge`
重放收敛层，断言方案文档 §10.4 的两条预期：

1. **P0-1A/P0-1B**：H1（化学业务）与 H3（美国客户）两条假设的 confidence 双双被压到 ≤ 0.3，
   并产出一条 ``driver_profile_research_internal_inconsistency``（隐含集团收入 289 亿 vs 208 亿）；
   实现是按"同一 (基数, 期别) 的 finding 同时覆盖双方"发一条 issue，故同时断言该 finding 的
   ``hypothesis_indices`` 覆盖这两条假设。
2. **P0-2**：FX（生物学业务毛利率 34.92%）**不再**出现 ``driver_profile_research_fact_conflict``
   误报（旧实现拿分部值与集团 53.9% 比，是药明康德那轮的唯一误报）。

输入包说明（缓存只有 LLM 原始输出，不含 ``financials`` / ``latest_segments``）：
按方案文档 §8.3 记录的实测值重建——集团 ``gross_margin=53.9``、分部 ``生物学业务=34.9`` /
``化学业务=56.55``（见下方 ``RESEARCH_INPUT`` 注释）。若缓存不可得，脚本会显式回退到文档
§8.1/§8.3 的回归 fixture 并在输出里标明来源（等效替代，断言 1 仍成立）。

约束：无 LLM 调用、无网络、不写任何文件（含缓存）；重复运行输出与结论一致（幂等）。
（``langchain_*`` 模块会随 ``alphabee.utils`` 的包级导入链路被加载，这是 ``research.py`` 自身的
既有 import 链、与本脚本无关：本脚本不构造 agent、不调用任何模型——可把 ``create_chat_model`` /
``_build_research_agent`` / ``run_driver_research`` 全部替换成抛异常后实测**仍然全绿**。）
运行：``HOME=/tmp .venv/bin/python scripts/replay_wuxi_converge.py``
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from alphabee import PROJECT_ROOT
from alphabee.agents.driver_profile.output import (
    DriverResearchOutput,
    ResearchEvidence,
    ResearchHypothesis,
)
from alphabee.config import DomainContextResearchSettings
from alphabee.domain_context import research
from alphabee.domain_context.contracts import DriverProfile

SYMBOL = "603259.SH"
CACHE_PATH = PROJECT_ROOT / "data" / "driver_profiles" / f"{SYMBOL}.json"

#: 期望上界：C3 的矛盾降权上界（research._CONTRADICTION_MAX_CONFIDENCE）。
MAX_CONFIDENCE = research._CONTRADICTION_MAX_CONFIDENCE

# ── 输入包（缓存不含 financials/latest_segments，按方案文档 §8.3 的实测值重建）──────────
#: 药明康德 2026H1 集团毛利率（方案文档 §8.3 回归 fixture 的集团基准：``financials.gross_margin``）。
GROUP_MARGIN = 53.9
#: 2026H1 分部毛利率：生物学业务 34.92% 的**同名分部基准**（FX 假设口径分流的对照值）；
#: 化学业务基准用于让 H1 里的「对比 2025 年化学业务毛利率 51.89%」走同名分部比对而非集团值。
RESEARCH_INPUT: dict[str, Any] = {
    "financials": {"gross_margin": GROUP_MARGIN},
    "latest_segments": [
        {"segment_name": "化学业务", "gross_margin": 56.55},
        {"segment_name": "生物学业务", "gross_margin": 34.9},
    ],
}

#: 三个被断言的对象（真实缓存里的 variable 前缀）。
H1_PREFIX = "化学业务 D&M"
H3_PREFIX = "美国客户收入占比"
FX_PREFIX = "美元兑人民币汇率变动"

#: 回退 fixture（仅在缓存不可得时使用；文本取自方案文档 §8.1/§8.3）。
FIXTURE_HYPOTHESES: tuple[tuple[str, str, float], ...] = (
    (
        "化学业务 D&M 管线（§8.1 fixture）",
        "2026H1 化学业务收入249.9亿（占集团86.46%）、毛利率56.55%（同比+7.48pp）",
        0.85,
    ),
    (
        "美国客户收入占比（§8.1 fixture）",
        "2026H1 美国客户收入140.3亿、占集团收入67.45%（同比+31.0%）",
        0.8,
    ),
    (
        "美元兑人民币汇率变动（§8.3 fixture）",
        "2026H1 生物学业务毛利率34.92%，同比-0.12pp",
        0.55,
    ),
)


def _fixture_hypotheses() -> list[ResearchHypothesis]:
    """§8.1/§8.3 fixture 等效替代（缓存不可得时使用；显式在输出里标注）。"""
    return [
        ResearchHypothesis(
            variable=variable,
            company_form=company_form,
            confidence=confidence,
            evidence=[
                ResearchEvidence(
                    kind="annual_report",
                    ref="2025 年报 第三节 管理层讨论与分析",
                    quote="报告期内主要业务收入同比增长。",
                )
            ],
        )
        for variable, company_form, confidence in FIXTURE_HYPOTHESES
    ]


def _load_hypotheses() -> tuple[list[ResearchHypothesis], str]:
    """读缓存（只读）重建假设列表；缓存不可得 ⇒ 回退 fixture 并返回来源说明。"""
    if not CACHE_PATH.exists():
        return _fixture_hypotheses(), "fixture §8.1/§8.3（缓存不可得，显式等效替代）"
    raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    output = DriverResearchOutput.model_validate(raw.get("output") or {})
    return list(output.driver_hypotheses), f"cache {CACHE_PATH.relative_to(PROJECT_ROOT)}"


def _cache_signature() -> tuple[int, int, str] | None:
    """缓存文件的 (大小, mtime_ns, sha256)；不存在返回 None（只读、不创建）。"""
    if not CACHE_PATH.exists():
        return None
    stat = CACHE_PATH.stat()
    digest = hashlib.sha256(CACHE_PATH.read_bytes()).hexdigest()
    return stat.st_size, stat.st_mtime_ns, digest


def _pick(hypotheses: list[Any], prefix: str) -> Any | None:
    for hypothesis in hypotheses:
        if hypothesis.variable.startswith(prefix):
            return hypothesis
    return None


def _index_of(hypotheses: list[Any], prefix: str) -> int:
    """假设在输入列表中的序号（-1 = 未找到）。"""
    for index, hypothesis in enumerate(hypotheses):
        if hypothesis.variable.startswith(prefix):
            return index
    return -1


def _categories(converged: DriverProfile) -> list[str]:
    return [str(issue.get("category")) for issue in converged.research_meta["issues"]]


def _messages(converged: DriverProfile, category: str) -> list[str]:
    return [
        str(issue.get("message")) for issue in converged.research_meta["issues"] if issue.get("category") == category
    ]


def main() -> int:
    before = _cache_signature()
    hypotheses, source = _load_hypotheses()

    profile = DriverProfile(
        symbol=SYMBOL,
        # 缓存里只有 track_label（"化学业务(WuXiChemistry)"），playbook 仅用于 provenance 判定。
        playbook="generic_fundamental",
        playbook_version=1,
        anchor_strength="none",  # 仅影响 provenance 标签（hybrid/llm），不影响本脚本断言
        fallback=True,
        provenance="rule",
        primary_drivers=["（重放基线占位：Tier A 规则驱动）"],
    )
    result = research.DriverResearchResult(
        output=DriverResearchOutput(driver_hypotheses=hypotheses),
        meta={"cached": True, "cache_key": f"replay:{SYMBOL}", "trigger": "replay:t7"},
    )
    converged = research.converge(
        profile,
        result,
        settings=DomainContextResearchSettings(enabled=True, max_hypotheses=8),
        research_input=RESEARCH_INPUT,
        trigger="replay:t7",
    )
    after = _cache_signature()

    print("=" * 78)
    print(f"药明康德 {SYMBOL} 收敛层真机重放（DRIVER_PROFILE_P0_FIX_PLAN §10.4）")
    print("=" * 78)
    print(f"数据来源     : {source}（假设 {len(hypotheses)} 条）")
    print(f"输入包（重建）: financials={RESEARCH_INPUT['financials']} segments={RESEARCH_INPUT['latest_segments']}")

    checked: list[tuple[str, float, float]] = []
    for hypothesis in converged.driver_hypotheses:
        original = _pick(hypotheses, hypothesis.variable)
        before_conf = float(original.confidence) if original is not None else float("nan")
        checked.append((hypothesis.variable, before_conf, float(hypothesis.confidence)))
    print("\n收敛后假设（降权前 → 降权后）：")
    for variable, before_conf, after_conf in checked:
        flag = "  ↓降权" if after_conf < before_conf else ""
        print(f"  - {variable[:42]:<44} {before_conf:.2f} → {after_conf:.2f}{flag}")

    issues = converged.research_meta["issues"]
    print("\nissues（category × 条数）：")
    for category in sorted({str(issue.get("category")) for issue in issues}):
        print(f"  - {category}: {_categories(converged).count(category)}")

    # ── 预期 ①：H1/H3 双方降权 + 内部不一致 issue（同一 finding 覆盖两条假设）────────
    h1 = _pick(converged.driver_hypotheses, H1_PREFIX)
    h3 = _pick(converged.driver_hypotheses, H3_PREFIX)
    fx = _pick(converged.driver_hypotheses, FX_PREFIX)
    expectations: list[tuple[str, bool, str]] = []

    if h1 is None or h3 is None:
        expectations.append(("① H1/H3 存在于收敛结果", False, "未找到 H1/H3（variable 前缀变了？）"))
    else:
        ok_conf = h1.confidence <= MAX_CONFIDENCE and h3.confidence <= MAX_CONFIDENCE
        expectations.append(
            (
                "① H1（化学业务）与 H3（美国客户）conf ≤ 0.3",
                ok_conf,
                f"H1={h1.confidence:.2f}, H3={h3.confidence:.2f}, 上界={MAX_CONFIDENCE}",
            )
        )

        findings = research._implied_total_contradictions(hypotheses)
        index_h1 = _index_of(hypotheses, H1_PREFIX)
        index_h3 = _index_of(hypotheses, H3_PREFIX)
        covered = [
            finding
            for finding in findings
            if finding["base"] == "集团" and set(finding["hypothesis_indices"]) == {index_h1, index_h3}
        ]
        expectations.append(
            (
                "① 同一 (基数=集团, 期别) 的 finding 同时覆盖 H1/H3",
                bool(covered) and covered[0]["implied_totals"] == [289.0, 208.0],
                f"hypothesis_indices={sorted({index_h1, index_h3})}；findings={findings}",
            )
        )

        messages = _messages(converged, "driver_profile_research_internal_inconsistency")
        ok_issue = (
            len(messages) == 1
            and "289" in messages[0]
            and "208" in messages[0]
            and "249.9" in messages[0]
            and "140.3" in messages[0]
        )
        expectations.append(
            (
                "① internal_inconsistency issue 含 289/208 与双方声明",
                ok_issue,
                f"issue 条数={len(messages)}；message={messages[0][:160] if messages else '（无）'}…",
            )
        )

    # ── 预期 ②：FX 不再出现 fact_conflict 误报（降权若发生，须来自 P0-1B 而非 P0-2）──
    if fx is None:
        expectations.append(("② FX 假设存在（生物学业务毛利率 34.92%）", False, "未找到 FX 假设"))
    else:
        fact_conflicts = _messages(converged, "driver_profile_research_fact_conflict")
        fx_conflicts = [m for m in fact_conflicts if "生物学" in m or "34.9" in m]
        expectations.append(
            (
                "② FX 无 fact_conflict 误报（生物学业务 34.92% vs 集团 53.9%）",
                not fact_conflicts,
                f"fact_conflict 条数={len(fact_conflicts)}；生物学相关={fx_conflicts}",
            )
        )
        # FX 可能仍被 P0-1B（「占比83.5%」这类占比从句内的无出处数字）降权——那是另一条校验的
        # 合法结论，与 P0-2 的口径误报无关；这里要求"若降权则必须能归因到 P0-1B 的占比从句数字"。
        original_fx = _pick(hypotheses, FX_PREFIX)
        escalated = [
            claim for claim in research._uncovered_claims(original_fx, RESEARCH_INPUT) if claim.in_share_clause
        ]
        expectations.append(
            (
                "② FX 若被降权，来源是 P0-1B 占比从句无出处数字（非 P0-2 口径误报）",
                fx.confidence >= float(original_fx.confidence) or bool(escalated),
                f"conf {float(original_fx.confidence):.2f} → {fx.confidence:.2f}；"
                f"占比从句无出处数字={[(claim.value, claim.unit) for claim in escalated]}",
            )
        )

    print("\n预期判定：")
    for name, passed, detail in expectations:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        print(f"         {detail}")

    # ── 只读证明：缓存 (大小, mtime, sha256) 前后必须一致 ─────────────────────────
    readonly_ok = before == after
    print("\n只读校验（缓存文件未被修改）：")
    if after is None:
        print("  [PASS] 缓存文件不存在（本次走 fixture 回退，未创建任何文件）")
    else:
        print(
            f"  [PASS] size={after[0]} mtime_ns={after[1]} sha256={after[2][:16]}…（前后一致）"
            if readonly_ok
            else "  [FAIL] 缓存文件被修改"
        )
        expectations.append(("③ 只读：缓存文件前后 (size, mtime, sha256) 一致", readonly_ok, f"before={before}"))

    failed = [name for name, passed, _ in expectations if not passed]
    print("\n" + "=" * 78)
    if failed:
        print(f"结论：{len(failed)} 项预期未满足 → {failed}")
        return 1
    print(
        f"结论：全部 {len(expectations)} 项预期成立（① 药明双方降权 + internal_inconsistency；② FX 无 fact_conflict 误报）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
