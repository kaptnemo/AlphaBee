#!/usr/bin/env python
"""药明康德（603259.SH）P1/P2 修复的真机重放（DRIVER_PROFILE_P1_P2_FIX_PLAN §11.5，步骤五）。

只读 Tier B 研究缓存 ``data/driver_profiles/603259.SH.json``（真实 LLM 产物，**不修改**），把其中
``output`` 原样喂给 :func:`alphabee.domain_context.research.converge` 重放收敛层，断言本文档
§11.5 的四条预期（以**当前缓存**的 ground truth 为准，见下「快照差异」）：

1. **P1-5 对齐**：``match_details`` 4 条（TIDES→``capacity_cycle``/「产能利用率」、
   小分子 D&M→``project_delivery``/「在手订单」、新增产能投放→``capacity_cycle``/「capex」、
   存货与应收账款→``working_capital_stress``/「应收账款增速」）；``matched_primitives`` 是
   ``_dedup(matched_ids)``，故为 **3 个去重 id**（capacity_cycle / project_delivery /
   working_capital_stress）而非 4；其余 3 条假设（美元净头寸 / 非经常性投资收益 / 美国政策）进
   novel（``novel_drivers``）。
2. **P1-3 闭包**：当前缓存的 4 个 candidates 都是并集目录（``load_playbooks() ∪ load_primitives()``）
   内的合法 id ⇒ **闭包 0 移除、原样保留**；``proposed_frameworks == []``（旧版输出没有该字段）。
3. **P1-4 语义拆分**：旧字段 ``novel_drivers``（4 条**概念**）必须**显式映射**为
   ``novel_concepts`` —— ``DriverResearchOutput`` 是 ``extra="ignore"``，不映射就会被**静默丢弃**
   （脚本内做了对照：不映射时 ``novel_concepts == []``）。契约 ``novel_drivers`` 是 3 条 C4 推导的
   假设变量，与 ``meta["novel_concepts"]`` 的 4 条概念**不相交**。
4. **P2-7 拆分**：契约 ``open_questions`` == 缓存里的 6 条；``unverified_drivers == []``（7 条假设
   均带非空 ``evidence.ref``，故没有假设被 C2 隔离）。

**快照差异（与本文档 §11.5② 的示例不一致，脚本内显式断言当前事实）**：文档 §11.5② 的
「candidates 不再含不存在的 id，``proposed_frameworks`` 承接两个提案」对应**更早一次 run** 的快照；
当前缓存（``created_at=2026-10-11T04:33:25+00:00``）已被重新生成：candidates 已是 4 个**全部合法**的
id（含 3 个 primitive 级积木：capacity_cycle / cost_curve / working_capital_stress），且**没有**
``proposed_frameworks`` 字段。故本脚本的断言②按当前 ground truth 写成「0 移除 + 全保留 +
``proposed_frameworks == []``」，并在运行时打印缓存 ``created_at``/sha256 供核对。

**输入包说明**：该缓存只存 LLM 原始输出，不含 ``financials`` / ``latest_segments``。本脚本断言的
四条预期（对齐 / 闭包 / 语义拆分 / 开放问题）都与 C3 数值校验无关，故 ``research_input=None``；
需要复核 P0 数值校验（隐含总量、数字出处、分部毛利率口径）时见 ``scripts/replay_wuxi_converge.py``
（那一支按文档 §8.3 实测值重建输入包）。

约束：**无 LLM 调用、无网络、不写任何文件（含缓存）**、重复运行结论一致（幂等）。脚本把研究层的
LLM / 缓存写入入口（``_build_research_agent`` / ``run_driver_research`` / ``save_cached_research``）
在本进程内替换成**抛异常**后再重放：若能跑完，即证明重放路径完全不触碰 LLM 与写盘（进程退出前
原样还原，不落盘）。

运行：``HOME=/tmp .venv/bin/python scripts/replay_wuxi_p1p2.py``
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from typing import Any

from alphabee import PROJECT_ROOT
from alphabee.agents.driver_profile.output import DriverResearchOutput
from alphabee.config import DomainContextResearchSettings
from alphabee.domain_context import research
from alphabee.domain_context.contracts import DriverProfile
from alphabee.domain_context.loader import load_playbooks, load_primitives

SYMBOL = "603259.SH"
CACHE_PATH = PROJECT_ROOT / "data" / "driver_profiles" / f"{SYMBOL}.json"

#: 当前缓存的创建时间（快照差异的显式凭据；仅用于打印与断言提示，不参与筛选）。
EXPECTED_CACHE_CREATED_AT = "2026-10-11T04:33:25+00:00"

#: 期望的候选（当前缓存快照；见模块 docstring「快照差异」）。
EXPECTED_CANDIDATES = ["generic_fundamental", "capacity_cycle", "cost_curve", "working_capital_stress"]
#: 缓存里的 6 条开放问题（P2-7 断言用；条数在此显式钉住）。
EXPECTED_OPEN_QUESTIONS = 6
#: 旧字段 ``novel_drivers`` 里的概念条数（P1-4 断言用）。
EXPECTED_LEGACY_CONCEPTS = 4
#: 期望的「未对齐驱动变量」条数（P1-5 / P1-4 断言用）。
EXPECTED_NOVEL_DRIVERS = 3

#: 假设定位前缀（真实缓存里 7 条假设的 variable 前缀；用前缀而非整串，避免文本微调即失效）。
TIDES_PREFIX = "TIDES"
DM_PREFIX = "小分子 D&M"
CAPEX_PREFIX = "新增产能投放"
WORKING_CAPITAL_PREFIX = "存货与应收账款"
NOVEL_PREFIXES = ("美元资产负债净头寸", "非经常性投资收益", "美国政策与地缘")

#: ① 期望的对齐结果（前缀 → primitive id / 共享子串）。
#: 注：capex 那条的**原始** shared 是 "capex "（变量「（capex 上行」与目录 key「公司自身 capex
#: 与折旧」的公共子串含尾随空格），故断言时统一 ``.strip()`` 比较并单独打印原始值。
EXPECTED_MATCHES: tuple[tuple[str, str, str], ...] = (
    (TIDES_PREFIX, "capacity_cycle", "产能利用率"),
    (DM_PREFIX, "project_delivery", "在手订单"),
    (CAPEX_PREFIX, "capacity_cycle", "capex"),
    (WORKING_CAPITAL_PREFIX, "working_capital_stress", "应收账款增速"),
)
#: ① 期望的去重后 matched_primitives（``_dedup(matched_ids)``）。
EXPECTED_MATCHED_PRIMITIVES = ["capacity_cycle", "project_delivery", "working_capital_stress"]

SETTINGS = DomainContextResearchSettings(enabled=True, max_hypotheses=7, max_agenda_items=6)


# ── 缓存读取与只读签名 ─────────────────────────────────────────────────────


def _cache_signature() -> tuple[int, int, str] | None:
    """缓存文件的 ``(size, mtime_ns, sha256)``；不存在 ⇒ ``None``（只读、不创建）。"""
    if not CACHE_PATH.exists():
        return None
    stat = CACHE_PATH.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(CACHE_PATH.read_bytes()).hexdigest()


def _load_raw() -> dict[str, Any]:
    raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"缓存结构异常（非对象）：{CACHE_PATH}")
    return raw


# ── P1-4：旧字段显式映射（extra="ignore" 会静默丢弃）──────────────────────


def _build_output(raw_output: Mapping[str, Any]) -> tuple[DriverResearchOutput, list[str], list[str]]:
    """``raw_output`` → ``DriverResearchOutput``，并做旧字段 ``novel_drivers`` → ``novel_concepts`` 映射。

    Returns:
        ``(映射后的 output, 旧字段里的概念列表, **不映射时**的 novel_concepts)``。第三个返回值是
        对照实验：``DriverResearchOutput.model_config`` 是 ``extra="ignore"``，直接
        ``model_validate(raw_output)`` 会把旧键静默丢掉 ⇒ 必须显式映射（本脚本断言之）。
    """
    legacy_concepts = [str(item).strip() for item in (raw_output.get("novel_drivers") or []) if str(item).strip()]
    unmapped = DriverResearchOutput.model_validate(dict(raw_output))
    mapped_input = dict(raw_output)
    mapped_input.pop("novel_drivers", None)
    mapped_input["novel_concepts"] = legacy_concepts
    return DriverResearchOutput.model_validate(mapped_input), legacy_concepts, list(unmapped.novel_concepts)


# ── 重放（纯函数路径：converge）────────────────────────────────────────────


def _profile() -> DriverProfile:
    """重放基线画像（缓存里没有画像，只有 LLM 原始输出）。

    ``activated_primitives`` 留空：C4 候选池是**全原语目录 ⊕ 激活覆盖**，真实 run 激活的通用三原语
    （cost_curve/capacity_cycle/working_capital_stress）与目录同 id 同内容（由 playbook 展开而来，不
    带额外信息），故留空不改变任何对齐结果；``anchor_strength`` 只影响 ``provenance`` 标签。
    """
    return DriverProfile(
        symbol=SYMBOL,
        playbook="generic_fundamental",
        playbook_version=1,
        anchor_strength="none",
        fallback=True,
        provenance="rule",
        primary_drivers=["（重放基线占位：Tier A 规则驱动）"],
    )


def _replay(output: DriverResearchOutput) -> DriverProfile:
    """把缓存输出原样喂给收敛层（``research_input=None``，见模块 docstring）。"""
    return research.converge(
        _profile(),
        research.DriverResearchResult(
            output=output,
            meta={"cached": True, "cache_key": f"replay:{SYMBOL}", "trigger": "replay:p1p2"},
        ),
        settings=SETTINGS,
        research_input=None,
        trigger="replay:p1p2",
    )


def _fingerprint(converged: DriverProfile) -> str:
    """收敛结果的规范化指纹（幂等对比用）。"""
    return json.dumps(
        {
            "matched_primitives": converged.research_meta["matched_primitives"],
            "match_details": converged.research_meta["match_details"],
            "novel_drivers": converged.novel_drivers,
            "novel_concepts": converged.research_meta["novel_concepts"],
            "candidates": converged.candidates,
            "proposed_frameworks": converged.proposed_frameworks,
            "open_questions": converged.open_questions,
            "unverified_drivers": converged.unverified_drivers,
            "confidence": converged.research_confidence,
            "categories": sorted(str(issue.get("category")) for issue in converged.research_meta["issues"]),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# ── LLM / 写盘入口守卫（进程内替换，证明重放不触碰它们）────────────────────


def _install_guards() -> list[tuple[str, Any]]:
    """把研究层的 LLM 与缓存写入入口换成抛异常，返回原函数以便还原。"""

    def _forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("重放不应触发 LLM 调用或缓存写入")

    originals: list[tuple[str, Any]] = []
    for name in ("_build_research_agent", "run_driver_research", "save_cached_research"):
        originals.append((name, getattr(research, name)))
        setattr(research, name, _forbid)
    return originals


def _restore_guards(originals: list[tuple[str, Any]]) -> None:
    for name, function in originals:
        setattr(research, name, function)


# ── 小工具 ────────────────────────────────────────────────────────────────


def _prefix_of(variable: str) -> str:
    for prefix in (*NOVEL_PREFIXES, *(item[0] for item in EXPECTED_MATCHES)):
        if variable.startswith(prefix):
            return prefix
    return variable


def _detail_of(details: list[dict[str, str]], prefix: str) -> dict[str, str] | None:
    for detail in details:
        if str(detail.get("variable", "")).startswith(prefix):
            return detail
    return None


def _candidates_of(raw_output: Mapping[str, Any]) -> list[str]:
    return [str(item).strip() for item in (raw_output.get("candidates") or []) if str(item).strip()]


def _open_questions_of(raw_output: Mapping[str, Any]) -> list[str]:
    return [str(item).strip() for item in (raw_output.get("open_questions") or []) if str(item).strip()]


def main() -> int:
    before = _cache_signature()
    if before is None:
        print(f"[FAIL] 缓存不存在：{CACHE_PATH}（本脚本只读真实缓存，不做 fixture 回退）")
        return 1

    raw = _load_raw()
    raw_output = raw.get("output") if isinstance(raw.get("output"), Mapping) else {}
    created_at = str(raw.get("created_at") or "")

    guards = _install_guards()
    guard_note = "入口已替换为抛异常（_build_research_agent / run_driver_research / save_cached_research）"
    try:
        output, legacy_concepts, unmapped_concepts = _build_output(raw_output)
        converged = _replay(output)
        converged_again = _replay(output)  # 幂等：同输入第二次重放必须同结果
    finally:
        _restore_guards(guards)

    hypotheses = list(output.driver_hypotheses)
    details = [dict(item) for item in converged.research_meta["match_details"]]
    categories = [str(issue.get("category")) for issue in converged.research_meta["issues"]]
    cached_candidates = _candidates_of(raw_output)
    cached_open_questions = _open_questions_of(raw_output)
    catalog = {*load_playbooks(), *load_primitives()}

    print("=" * 92)
    print(f"药明康德 {SYMBOL} P1/P2 真机重放（DRIVER_PROFILE_P1_P2_FIX_PLAN §11.5）")
    print("=" * 92)
    print(f"缓存         : {CACHE_PATH.relative_to(PROJECT_ROOT)}")
    print(f"              created_at={created_at} sha256={before[2][:16]}… size={before[0]}B")
    print("输入包       : research_input=None（缓存不含 financials/latest_segments；四条预期与 C3 数值校验无关）")
    print(f"假设 {len(hypotheses)} 条 / 议程 {len(output.research_agenda)} 条 / candidates {len(cached_candidates)} 个")
    print(f"LLM 守卫     : {guard_note}")

    print("\n① P1-5 对齐明细（variable 前缀 → primitive / key_variable / 共享子串）：")
    for hypothesis in hypotheses:
        detail = _detail_of(details, _prefix_of(hypothesis.variable))
        if detail is None:
            print(f"  - {hypothesis.variable[:46]:<48} → novel（未对齐）")
        else:
            print(
                f"  - {hypothesis.variable[:46]:<48} → {detail['primitive']}"
                f" / {detail['key_variable']} / shared={detail['shared']!r}"
            )
    print(f"  matched_primitives（去重后）= {converged.research_meta['matched_primitives']}")

    print("\n② P1-3 候选闭包：")
    for candidate in cached_candidates:
        print(f"  - {candidate:<24} {'目录内 ✓' if candidate in catalog else '目录外 ✗'}")
    print(
        f"  契约 candidates={converged.candidates} | candidate_drops={converged.research_meta.get('candidate_drops')}"
    )
    print(f"  契约 proposed_frameworks={converged.proposed_frameworks}")

    print("\n③ P1-4 语义拆分：")
    print(f"  旧字段 novel_drivers → novel_concepts 显式映射：{len(legacy_concepts)} 条概念已映射")
    print(f"  不映射的对照（extra=ignore 静默丢弃）：novel_concepts={unmapped_concepts}")
    print(f"  meta.novel_concepts（概念）={len(converged.research_meta['novel_concepts'])} 条")
    for concept in converged.research_meta["novel_concepts"]:
        print(f"    * {concept[:88]}")
    print(f"  契约 novel_drivers（C4 推导的未对齐变量）={len(converged.novel_drivers)} 条")
    for variable in converged.novel_drivers:
        print(f"    * {variable[:88]}")

    print("\n④ P2-7 拆分：")
    print(f"  契约 open_questions={len(converged.open_questions)} 条（缓存 {len(cached_open_questions)} 条）")
    print(f"  契约 unverified_drivers={converged.unverified_drivers}")
    print(
        f"  7 条假设的 evidence.ref 均非空：{all(h.evidence and all((e.ref or '').strip() for e in h.evidence) for h in hypotheses)}"
    )

    print("\nissues（category × 条数）：")
    for category in sorted(set(categories)):
        print(f"  - {category}: {categories.count(category)}")

    # ── 四条预期 + 幂等 + 只读 ───────────────────────────────────────────────
    expectations: list[tuple[str, bool, str]] = []

    # ① P1-5
    expected_novel_variables = [h.variable for h in hypotheses if h.variable.startswith(NOVEL_PREFIXES)]
    bad_matches: list[str] = []
    if len(details) != 4:
        bad_matches.append(f"match_details 条数={len(details)}（期望 4）")
    for prefix, primitive, shared in EXPECTED_MATCHES:
        detail = _detail_of(details, prefix)
        if detail is None:
            bad_matches.append(f"{prefix}: 无对齐明细")
        elif detail["primitive"] != primitive or str(detail["shared"]).strip() != shared:
            bad_matches.append(f"{prefix}: {detail['primitive']}/{detail['shared']!r} != {primitive}/{shared}")
    expectations.append(
        ("① P1-5 对齐 4/7 且 shared 与实测一致", not bad_matches, "；".join(bad_matches) or "4 条明细全部命中")
    )
    expectations.append(
        (
            "① matched_primitives 为去重后 3 个 id（_dedup(matched_ids)）",
            converged.research_meta["matched_primitives"] == EXPECTED_MATCHED_PRIMITIVES,
            f"matched_primitives={converged.research_meta['matched_primitives']}",
        )
    )
    expectations.append(
        (
            "① 其余 3 条假设（美元净头寸/非经常性投资收益/美国政策）进 novel_drivers",
            converged.novel_drivers == expected_novel_variables
            and len(expected_novel_variables) == EXPECTED_NOVEL_DRIVERS,
            f"novel_drivers={[item[:24] for item in converged.novel_drivers]}",
        )
    )

    # ② P1-3
    candidates_ok = (
        cached_candidates == EXPECTED_CANDIDATES
        and converged.candidates == cached_candidates
        and all(candidate in catalog for candidate in cached_candidates)
        and "candidate_drops" not in converged.research_meta
        and "driver_profile_research_invalid_candidate" not in categories
    )
    expectations.append(
        (
            "② P1-3 闭包 0 移除、4 个合法 candidate 全保留",
            candidates_ok,
            f"cached={cached_candidates} | 契约={converged.candidates} | drops={converged.research_meta.get('candidate_drops')}",
        )
    )
    expectations.append(
        (
            "② proposed_frameworks == []（本快照的旧版输出无该字段）",
            converged.proposed_frameworks == [],
            f"proposed_frameworks={converged.proposed_frameworks}",
        )
    )

    # ③ P1-4
    legacy_ok = (
        len(legacy_concepts) == EXPECTED_LEGACY_CONCEPTS
        and unmapped_concepts == []
        and converged.research_meta["novel_concepts"] == legacy_concepts
        and len(converged.novel_drivers) == EXPECTED_NOVEL_DRIVERS
        and not (set(converged.novel_drivers) & set(converged.research_meta["novel_concepts"]))
    )
    expectations.append(
        (
            "③ P1-4 旧键显式映射 novel_concepts、契约 novel_drivers 3 条且两者不相交",
            legacy_ok,
            f"概念 {len(converged.research_meta['novel_concepts'])} 条 / 驱动 {len(converged.novel_drivers)} 条 / "
            f"不相交={not (set(converged.novel_drivers) & set(converged.research_meta['novel_concepts']))}",
        )
    )

    # ④ P2-7
    expectations.append(
        (
            "④ P2-7 open_questions 6 条独立、unverified_drivers 为空",
            converged.open_questions == cached_open_questions
            and len(cached_open_questions) == EXPECTED_OPEN_QUESTIONS
            and converged.unverified_drivers == [],
            f"open_questions={len(converged.open_questions)} | unverified_drivers={converged.unverified_drivers}",
        )
    )

    # ⑤ 幂等 / 只读 / 无 LLM
    expectations.append(
        ("⑤ 幂等：同输入两次重放指纹一致", _fingerprint(converged) == _fingerprint(converged_again), "fingerprint 相同")
    )
    after = _cache_signature()
    expectations.append(
        (
            "⑤ 只读：缓存 (size, mtime_ns, sha256) 前后一致",
            after == before,
            f"before={before[0]}/{before[1]}/{before[2][:16]}… after={after[0] if after else None}",
        )
    )
    expectations.append(("⑤ 无 LLM / 无缓存写入（入口替换为抛异常后仍跑完）", True, guard_note))

    print("\n预期判定：")
    for name, passed, detail in expectations:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        print(f"           {detail}")

    failed = [name for name, passed, _ in expectations if not passed]
    print("\n" + "=" * 92)
    if failed:
        print(f"结论：{len(failed)} 项预期未满足 → {failed}")
        return 1
    print("结论：全部" + f"{len(expectations)} 项预期成立（① P1-5 4/7；② P1-3 0 移除；③ P1-4 语义分道；④ P2-7 拆分）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
