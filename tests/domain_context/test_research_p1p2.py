"""P1/P2 修复的回归用例（DRIVER_PROFILE_P1_P2_FIX_PLAN §10.1，除 P2-8）。

覆盖范围与文档 §10.1 表格逐条对应（12 例）：

- **P1-3（3 例）**：candidates 闭包（目录外 id 移出 + ``research_meta["candidate_drops"]`` + LOW
  issue）、``proposed_frameworks`` 透传且不进 candidates、输入包携带 ``available_playbooks``；
- **P1-4（2 例）**：契约 ``novel_drivers`` 唯一来源 = C4 推导、LLM ``novel_concepts`` 不混入契约字段；
- **P1-5（4 例）**：药明在手订单对齐回归（共享「在手订单」⇒ ``project_delivery``）、双字共享
  不匹配、等长命中按 primitive id 升序决平、激活原语覆盖目录同 id（注入缝保留）；
- **P2-6（2 例）**：同一小句内多期别 + 多数字命中（LOW、不降权）、跨句各单期别不命中；
- **P2-7（1 例）**：``open_questions`` 与 ``unverified_drivers`` 分列。

**P2-8 刻意不落地**（文档 §7）：``_build_driver_profile_summary`` 的 hypotheses/agenda/
open_questions **有界摘要**（≤8KB）随 D2 下游接线同批实现与测试，本次只定形状——故本文件
不含任何 summary / 8KB / ``payload_builders`` 相关用例。新增 P2-8 用例时应随那次接线一起写。

纯函数范式：用例只构造 ``DriverResearchOutput`` / ``DriverProfile`` 后调 ``converge``
（P1-5 的决平/共享子串用例直接调受控匹配函数），不调 LLM、不读盘、不联网；唯一外部读取是
loader 对 ``domain_primitives/*.yaml``（``domain_playbooks/*.yaml``）的目录加载——收敛层自身
的目录读取，带 ``lru_cache``。helper 在本文件自建，不 import 其它测试模块的私有 helper。
"""

from __future__ import annotations

from typing import Any

from alphabee.agents.driver_profile.output import (
    DriverResearchOutput,
    ResearchEvidence,
    ResearchHypothesis,
)
from alphabee.config import DomainContextResearchSettings
from alphabee.domain_context import research
from alphabee.domain_context.context_router import RouterInput
from alphabee.domain_context.contracts import ActivatedPrimitive, DriverProfile
from alphabee.domain_context.loader import load_playbooks, load_primitives

# ══════════════════════════════════════════════════════════════════════════
# helpers（沿用 test_research.py 的构造思路，但不跨文件 import）
# ══════════════════════════════════════════════════════════════════════════


def _settings(**overrides: Any) -> DomainContextResearchSettings:
    base: dict[str, Any] = {"enabled": True, "max_hypotheses": 7, "max_agenda_items": 6}
    base.update(overrides)
    return DomainContextResearchSettings(**base)


def _profile(**overrides: Any) -> DriverProfile:
    base: dict[str, Any] = {
        "symbol": "603259.SH",
        "playbook": "generic_fundamental",
        "playbook_version": 1,
        "anchor_strength": "none",
        "fallback": True,
        "provenance": "rule",
        "primary_drivers": ["毛利率（Tier A 兜底）"],
    }
    base.update(overrides)
    return DriverProfile(**base)


def _hypothesis(
    variable: str,
    *,
    evidence: bool = True,
    ref: str = "2025 年报 第三节 管理层讨论与分析",
    confidence: float = 0.7,
    role: str = "primary",
    company_form: str = "",
    mechanism: str = "",
    observables: list[str] | None = None,
) -> ResearchHypothesis:
    return ResearchHypothesis(
        variable=variable,
        role=role,
        mechanism=mechanism,
        company_form=company_form,
        observables=list(observables or []),
        evidence=[ResearchEvidence(kind="annual_report", ref=ref, quote="原文片段")] if evidence else [],
        confidence=confidence,
    )


def _output(hypotheses: list[ResearchHypothesis] | None = None, **kwargs: Any) -> DriverResearchOutput:
    return DriverResearchOutput(driver_hypotheses=list(hypotheses or []), **kwargs)


def _converge(
    output: DriverResearchOutput,
    *,
    profile: DriverProfile | None = None,
    research_input: dict[str, Any] | None = None,
) -> DriverProfile:
    return research.converge(
        profile or _profile(),
        research.DriverResearchResult(output=output, meta={"cached": False}),
        research_input=research_input,
        settings=_settings(),
    )


def _catalog_pool() -> dict[str, Any]:
    """**全原语目录**候选池（等价于 ``converge`` 在未激活任何原语时的池，P1-5）。"""
    return {primitive.id: primitive for primitive in load_primitives().values()}


def _issues(profile: DriverProfile, category: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in profile.research_meta["issues"]
        if isinstance(item, dict) and item.get("category") == category
    ]


# ══════════════════════════════════════════════════════════════════════════
# P1-3：candidates 闭包 + 提案拆分 + 输入包目录
# ══════════════════════════════════════════════════════════════════════════


def test_candidates_invalid_id_dropped_and_issue() -> None:
    """目录（playbook ⊕ 原语）外的 candidate 被移出、留痕，并落一条 LOW issue（不抛错）。"""
    converged = _converge(
        _output(
            [_hypothesis("产能利用率")],
            candidates=["capacity_ramp_cdmo", "generic_fundamental", "policy_regulatory_risk"],
            proposed_frameworks=["产能爬坡型CDMO"],
        )
    )

    assert converged.candidates == ["generic_fundamental"]
    assert converged.research_meta["candidate_drops"] == ["capacity_ramp_cdmo", "policy_regulatory_risk"]
    issues = _issues(converged, "driver_profile_research_invalid_candidate")
    assert len(issues) == 1
    assert issues[0]["severity"] == "low"
    # 措辞（captain revision 2）：目录（playbook 或原语）里不存在的 id
    assert "目录（playbook 或原语）中不存在的 id" in issues[0]["message"]
    assert "capacity_ramp_cdmo" in issues[0]["message"]
    # 本轮提案在 issue 文案里回显（信息不丢：被踢出的是候选，不是提案）
    assert "产能爬坡型CDMO" in issues[0]["message"]


def test_proposed_frameworks_passthrough() -> None:
    """提案原样落契约字段（去重/去空白），且**不进** candidates；合法 primitive id 也过闭包。"""
    converged = _converge(
        _output(
            [_hypothesis("产能利用率")],
            # 并集闭包口径：playbook id 与 primitive id 都是合法候选
            candidates=["capacity_cycle", "generic_fundamental"],
            proposed_frameworks=["产能爬坡型CDMO", "产能爬坡型CDMO", " 定点监管风险 "],
        )
    )

    assert converged.candidates == ["capacity_cycle", "generic_fundamental"]
    assert "candidate_drops" not in converged.research_meta
    assert _issues(converged, "driver_profile_research_invalid_candidate") == []
    assert converged.proposed_frameworks == ["产能爬坡型CDMO", "定点监管风险"]
    assert "产能爬坡型CDMO" not in converged.candidates
    assert "定点监管风险" not in converged.candidates


def test_build_research_input_carries_available_playbooks() -> None:
    """输入包把可用 playbook 目录（id + description ≤160）给到模型；提示面仍 playbooks-only。"""
    pack = research.build_research_input(
        "603259.SH",
        RouterInput(track_label="CXO"),
        report_fragments=[],  # 显式注入空片段 ⇒ 不读盘
    )

    books = pack["available_playbooks"]
    assert [item["id"] for item in books] == sorted(load_playbooks())
    ids = {item["id"] for item in books}
    assert {"hog_cycle", "generic_fundamental"} <= ids
    assert all(set(item) == {"id", "description"} and len(item["description"]) <= 160 for item in books)
    # 提示面窄（有意的不对称：闭包面宽，见 research.py C4.0 注释）——不额外给原语 id
    assert not (ids & set(load_primitives()))
    assert "candidates 只能引用 available_playbooks 中的 id；新框架提案走 proposed_frameworks。" in pack["notes"]


# ══════════════════════════════════════════════════════════════════════════
# P1-4：novel_drivers 唯一来源 = C4 推导
# ══════════════════════════════════════════════════════════════════════════


def test_novel_drivers_only_c4_derived() -> None:
    """契约 ``novel_drivers`` 只收 C4 未对齐变量；LLM 自述概念只进 ``research_meta``。"""
    converged = _converge(_output([_hypothesis("铜箔-树脂价差", confidence=0.7)], novel_concepts=["概念X"]))

    assert converged.novel_drivers == ["铜箔-树脂价差"]
    assert converged.research_meta["novel_concepts"] == ["概念X"]
    assert "概念X" not in converged.novel_drivers


def test_novel_concepts_not_duplicated_into_drivers() -> None:
    """LLM 把假设变量逐字复述进 ``novel_concepts`` 时，契约字段仍只有 C4 推导值、不重复。

    药明康德 run 的病根：11 条 ``novel_drivers`` 里 7 条是 ``driver_hypotheses[].variable``
    的逐字副本（两路语义混进同一字段）。本用例把两个变量名都塞进 ``novel_concepts``，
    断言只有**未对齐**的那条进契约字段。
    """
    converged = _converge(
        _output(
            [_hypothesis("产能利用率", confidence=0.8), _hypothesis("铜箔-树脂价差", confidence=0.7)],
            novel_concepts=["产能利用率", "铜箔-树脂价差"],
        )
    )

    assert converged.driver_hypotheses[0].matched_primitive == "capacity_cycle"
    assert converged.novel_drivers == ["铜箔-树脂价差"]
    assert len(converged.novel_drivers) == len(set(converged.novel_drivers))
    assert converged.research_meta["novel_concepts"] == ["产能利用率", "铜箔-树脂价差"]


# ══════════════════════════════════════════════════════════════════════════
# P1-5：最长公共子串对齐 + 全目录池 + id 升序决平 + 激活覆盖
# ══════════════════════════════════════════════════════════════════════════


def test_shared_substring_aligns_order_book_to_project_delivery() -> None:
    """药明回归：变量与 key_variable 共享「在手订单」（4 字）⇒ 对齐 ``project_delivery``。"""
    variable = "持续经营业务在手订单余额及其同比"

    matched, key_variable, shared = research._match_primitive(variable, _catalog_pool())
    assert matched is not None and matched.id == "project_delivery"
    assert key_variable == "新签订单与在手订单"
    assert shared == "在手订单"

    llm_mechanism = "在手订单转化率回升 → 收入确认提速"
    converged = _converge(_output([_hypothesis(variable, confidence=0.8, mechanism=llm_mechanism)]))
    item = converged.driver_hypotheses[0]

    assert item.matched_primitive == "project_delivery"
    causal_paths = load_primitives()["project_delivery"].causal_paths
    assert causal_paths[0] == "订单增长 -> 收入确认滞后 -> 利润逐步兑现"
    # 「对齐即无条件继承原语因果链」的实现形如 llm 机制 + "；" + "；".join(causal_paths)；
    # project_delivery 有两条因果链，故这里不写 endswith（否则第二条链会被误判为回归）。
    assert item.mechanism == "；".join([llm_mechanism, *causal_paths])
    assert "订单增长 -> 收入确认滞后 -> 利润逐步兑现" in item.mechanism
    assert converged.research_meta["match_details"] == [
        {
            "variable": variable,
            "primitive": "project_delivery",
            "key_variable": "新签订单与在手订单",
            "shared": "在手订单",
        }
    ]


def test_two_char_shared_substring_does_not_match() -> None:
    """双字共享（「成本」）视为偶合 ⇒ 不对齐（判据下界 ``_MATCH_MIN_CHARS``）。"""
    pool = _catalog_pool()
    assert "cost_curve" in pool  # 池里确实有它，只是判据不成立
    assert research._MATCH_MIN_CHARS == 4
    # 「成本压力」×「与同行的成本差」只共享「成本」（2 字）⇒ 不回报任何共享子串
    assert research._longest_shared_substring("成本压力", "与同行的成本差") == ""
    assert research._match_primitive("成本压力", pool) == (None, "", "")

    converged = _converge(_output([_hypothesis("成本压力")]))
    assert converged.driver_hypotheses[0].matched_primitive == ""
    assert converged.novel_drivers == ["成本压力"]


def test_tie_break_by_primitive_id_ascending() -> None:
    """等长命中按 primitive id 升序决平（确定性，与传入字典序无关）。"""
    pool = {
        "bbb_tie": ActivatedPrimitive(id="bbb_tie", description="乙", key_variables=["ZZ产能利用率WW"]),
        "aaa_tie": ActivatedPrimitive(id="aaa_tie", description="甲", key_variables=["XX产能利用率YY"]),
    }
    # 两个 key 与变量共享同一 5 字子串 ⇒ 真·等长（决平才被真正触发）
    assert research._longest_shared_substring("产能利用率", "XX产能利用率YY") == "产能利用率"
    assert research._longest_shared_substring("产能利用率", "ZZ产能利用率WW") == "产能利用率"

    matched, key_variable, shared = research._match_primitive("产能利用率", pool)
    assert matched is not None and matched.id == "aaa_tie"
    assert key_variable == "XX产能利用率YY"
    assert shared == "产能利用率"


def test_activated_primitive_overrides_catalog_same_id() -> None:
    """激活原语按 id 覆盖目录条目（弱锚时「锚定知识优先」），继承注入版因果链/证伪信号。"""
    activated = ActivatedPrimitive(
        id="capacity_cycle",
        description="激活版产能周期",
        key_variables=["产能利用率", "行业新增产能"],
        causal_paths=["激活版因果链"],
        disconfirming_signals=["激活版证伪信号"],
    )
    converged = _converge(
        _output([_hypothesis("产能利用率", confidence=0.8, mechanism="自写机制")]),
        profile=_profile(anchor_strength="weak", activated_primitives=[activated]),
    )
    item = converged.driver_hypotheses[0]

    assert item.matched_primitive == "capacity_cycle"
    assert item.mechanism == "自写机制；激活版因果链"
    assert item.falsifiers == ["激活版证伪信号"]
    # 覆盖生效的反证：目录版 capacity_cycle 的因果链没有参与
    assert load_primitives()["capacity_cycle"].causal_paths[0] not in item.mechanism


# ══════════════════════════════════════════════════════════════════════════
# P2-6：同句期别混用轻检测
# ══════════════════════════════════════════════════════════════════════════


def test_period_mixing_same_sentence_flags() -> None:
    """同一小句内 ≥2 个期别标签 + ≥2 处数字 ⇒ LOW issue（只留痕，不降权）。

    注意 fixture 必须是**不含逗号/顿号**的同一小句：``_SENTENCE_SPLIT_RE`` 把 ``,``/``、``
    也算句界，写成「2026H1 毛利率 56.55%，2025 年为 51.89%」会被切成两句而不命中
    （§5.2 判据 + 复用 P0 正则的必然形态）。
    """
    converged = _converge(
        _output([_hypothesis("毛利率结构", company_form="2026H1 毛利率 56.55%（2025 年 51.89%）", confidence=0.9)])
    )

    issues = _issues(converged, "driver_profile_research_period_mixing")
    assert len(issues) == 1
    assert issues[0]["severity"] == "low"
    assert "「毛利率结构」" in issues[0]["message"]
    assert "2026H1" in issues[0]["message"]
    # 轻检测：不降权（conf 保持原值）
    assert converged.driver_hypotheses[0].confidence == 0.9


def test_period_mixing_cross_sentence_not_flagged() -> None:
    """跨句各含单一期别是正常的趋势对照 ⇒ 不命中（不做机器裁决）。"""
    converged = _converge(
        _output([_hypothesis("甲驱动", company_form="2026H1 甲业务收入100亿。2025年 乙业务收入300亿。")])
    )

    assert _issues(converged, "driver_profile_research_period_mixing") == []


# ══════════════════════════════════════════════════════════════════════════
# P2-7：open_questions 与 unverified_drivers 分列
# ══════════════════════════════════════════════════════════════════════════


def test_open_questions_split_from_unverified() -> None:
    """认知缺口（``open_questions``）与被 C2 剥夺证据资格的驱动变量（``unverified_drivers``）分列。"""
    converged = _converge(_output([_hypothesis("变量Y", evidence=False, confidence=0.9)], open_questions=["缺口X"]))

    assert converged.unverified_drivers == ["变量Y"]
    assert converged.open_questions == ["缺口X"]
    assert "缺口X" not in converged.unverified_drivers
    assert "变量Y" not in converged.open_questions
