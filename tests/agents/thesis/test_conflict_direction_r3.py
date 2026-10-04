"""P0-2R3（t22 补充证据门 needs_revision 修复）：conflict_direction 良性判定收紧。

覆盖 t22 五条 findings 的修复钉：

* F1（high）混合句漏判：分句后任一分句含扩充风险词 ⇒ 非 benign（构造样本 + 真实语料
  漏判样本回归钉，恶意子句判 benign 即红）；
* F2（medium）标点截断：「不构成。资金占用」按分句独立判定 ⇒ negative；
* F3（medium，captain 细化）拉丁混排：白名单外拉丁词命中拉丁恶性词表 ⇒ 直接 negative；
  白名单外拉丁词无法归类且有良性命中 ⇒ unknown；白名单缩写（AI/PCB/ROE…）按中文规则
  正常判向；纯英文/空串 ⇒ unknown；下游按既有负贡献（_conflict_votes 钉票值 -penalty/max）；
* F4（medium，选 a）症状词良性语境：恶性命中全为 {异常/恶化/占用/积压} 且存在良性标记、
  且每症状词句尾方向有「而非/并非/属/现象/主因/所致」语境 ⇒ benign；002916 五冲突
  真实回归钉（1/2/3/4 → benign、5 → unknown）；
* F5（low）标记出处披露：见 engine.py 注释与认证快照 README（本文件不承载文案）。
"""

from __future__ import annotations

import pytest

from alphabee.agents.schemas import ConflictAnalysisResult, ConflictItem, HypothesisItem
from alphabee.agents.thesis import reviewer as reviewer_module
from alphabee.agents.thesis.engine import ThesisEngine, conflict_direction

# ── F1：混合句漏判收紧（构造样本 + 真实语料漏判样本）───────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 构造样本（t22 对抗套件实测 benign 泄漏的 4 例）
        ("不构成资金占用，但涉嫌利益输送", "negative"),
        ("风险可控，但存在违规担保", "negative"),
        ("排除操纵嫌疑，但内幕交易频发", "negative"),
        ("正常现象，但收入确认激进", "negative"),
        # 真实语料 benign 桶内语义为负样本（t22 F1 漏判候选）
        (
            "利润调节与非现金收益驱动：净利润增长主要由非经常性损益、公允价值变动、资产处置收益或会计估计变更驱动",
            "negative",
        ),
        ("收入确认口径或一次性收入贡献营收，核心主营增长被高估", "negative"),
        ("毛利率持续承压，利润高增主要来自费用端压缩或一次性收益，而非主营盈利改善，利润弹性不可持续", "negative"),
    ],
)
def test_mixed_clause_with_risk_words_is_never_benign(text: str, expected: str):
    """恶意/风险子句判 benign 即红（F1 requiredFix ③）。"""
    assert conflict_direction(text) == expected


# ── F2：否定窗口跨标点/换行不生效 ──────────────────────────────────────────


def test_punctuation_truncated_negation_does_not_cross_clause_boundary():
    """「不构成。资金占用」：第二分句的「占用」不被首分句否定词吞掉 ⇒ negative。"""
    assert conflict_direction("不构成。资金占用") == "negative"


def test_negation_window_is_clause_local():
    """同一分句内否定窗口照常生效：「不构成资金占用」仍 benign（对照钉）。"""
    assert conflict_direction("不构成资金占用") == "benign"


def test_newline_separated_clauses_are_independent():
    """换行分句同样独立判定：「不构成\n资金占用」⇒ negative。"""
    assert conflict_direction("不构成\n资金占用") == "negative"


def test_negation_beyond_window_stays_conservative_negative():
    """否定词超出 6 字符窗口（t22 安全侧过度惩罚样本）：维持 negative（保守方向）。"""
    assert conflict_direction("公司不存在任何形式的资金占用问题") == "negative"


# ── F3：拉丁混排保守回落（captain 细化：白名单外拉丁词 + 拉丁恶性词表）─────


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "no evidence of fraud",  # 纯英文（无 CJK）⇒ unknown（t22 保守默认）
        "This is a financial statement",  # 纯英文无恶性词 ⇒ unknown
    ],
)
def test_empty_and_pure_english_fall_back_to_unknown(text: str):
    assert conflict_direction(text) == "unknown"


def test_latin_malignant_word_is_directly_negative():
    """白名单外拉丁词命中拉丁恶性词表 ⇒ 直接 negative（不得 benign）。"""
    assert conflict_direction("manipulation 正常") == "negative"
    assert conflict_direction("经营正常, financial fraud confirmed") == "negative"
    assert conflict_direction("存货水平 overstated 属正常") == "negative"


def test_latin_acronym_whitelist_keeps_benign():
    """白名单缩写（AI/PCB/ROE/PE…）不触发混排回落，按中文规则正常判向。"""
    assert conflict_direction("AI 需求驱动的良性备货") == "benign"
    assert conflict_direction("PCB 行业订单驱动，主动备货属正常") == "benign"
    assert conflict_direction("PE 处于合理区间，有支撑") == "benign"
    assert conflict_direction("CoWoS 需求驱动的备货属正常") == "benign"
    assert conflict_direction("Q1 季节性波动属正常") == "benign"


def test_unclassifiable_latin_word_with_benign_hit_falls_back_to_unknown():
    """白名单外拉丁词无法归类且存在良性命中 ⇒ unknown（保守）。"""
    assert conflict_direction("正常现象, accruals timing") == "unknown"


def test_mixed_text_downstream_keeps_negative_contribution():
    """混排判 unknown/negative ⇒ 下游按既有负贡献：_conflict_votes 钉票值 = -penalty/max。"""
    conflicts = ConflictAnalysisResult(
        conflicts=[
            ConflictItem(
                id="c1",
                theme="冲突",
                description="描述",
                related_dimensions=["earnings_quality"],
                severity="high",
                confidence=0.9,
                hypotheses=[
                    HypothesisItem(
                        id="h1",
                        conflict_id="c1",
                        explanation="正常现象, accruals timing",
                        predictions=[],
                        required_evidence=[],
                        score=0.8,
                        status="verified",
                    )
                ],
            )
        ]
    )
    votes, _ = reviewer_module._conflict_votes(conflicts)
    assert votes == pytest.approx([-0.55 / 0.8])


# ── F1/F2 补充：转折连词（但/而/然而/不过）切分分句 ────────────────────────


def test_conjunction_split_makes_risk_clause_visible():
    """无标点的「良性分句但恶性分句」按转折连词切分 ⇒ negative。"""
    assert conflict_direction("正常现象但收入确认激进") == "negative"
    assert conflict_direction("排除操纵嫌疑而内幕交易频发") == "negative"
    assert conflict_direction("风险可控然而资金占用明显") == "negative"


def test_conjunction_split_keeps_symptom_context_rule():
    """「而」切分不破坏症状词良性语境：「属…现象而非操纵」仍 benign。"""
    assert conflict_direction("扩张期正常现象而非操纵") == "benign"
    assert conflict_direction("现金流恶化主因在存货而非应收，备货属正常") == "benign"


# ── F4（选 a）：症状词良性语境 + 002916 五冲突真实回归钉 ────────────────────

# 002916.SZ task-02c518ebf03f 的真实五冲突 explanation（severity 如实）：
#   c1 勾稽关系断裂与异常聚集（critical）、c2 盈利高增但现金流质量偏弱（high）、
#   c3 存货增速远超营收但毛利率稳定（high）、c4 大存大贷（high）、
#   c5 高估值溢价与中等ROE及盈利质量背离（high）。
_REAL_002916_CONFLICTS = [
    (
        "c1",
        "critical",
        "高增长期营运资本与资本开支同步扩张，导致多项勾稽指标相对静态历史基线异常，属扩张期正常现象而非操纵",
        "benign",
    ),
    (
        "c2",
        "high",
        "存货大幅扩张占用现金（备货/扩产），现金流出先行而利润确认滞后，现金流恶化主因在存货而非应收",
        "benign",
    ),
    ("c3", "high", "在手订单充足，主动备货+原材料储备支撑后续交付，属良性扩张", "benign"),
    ("c4", "high", "扩张期储备现金以备资本开支，同时利用低息负债锁定资金，属主动财务安排", "benign"),
    (
        "c5",
        "high",
        "市场给予的高估值由AI/高多层板景气与产能稀缺性驱动，反映对未来成长而非当期盈利质量，需持续兑现高增速",
        "unknown",
    ),
]


@pytest.mark.parametrize(("conflict_id", "severity", "explanation", "expected"), _REAL_002916_CONFLICTS)
def test_002916_five_real_conflicts_direction(conflict_id: str, severity: str, explanation: str, expected: str):
    """五冲突真实回归钉：症状词被良性语境包围 ⇒ benign；无良性标记 ⇒ unknown（保守负贡献）。"""
    assert conflict_direction(explanation) == expected, f"{conflict_id} 方向钉失守"


def test_symptom_word_without_benign_context_stays_negative():
    """症状词无良性语境（句尾无「而非/属/现象/主因/所致」）⇒ negative。"""
    assert conflict_direction("正常范围内的积压") == "negative"


def test_bare_context_marker_without_benign_hit_stays_negative():
    """收紧口径钉（captain 裁定）：良性语境必须与良性标记命中共现——
    裸「主因在…而非…」不单独判良性（否则负面归因被误放行）。"""
    assert conflict_direction("现金流恶化主因在存货而非应收") == "negative"
    assert conflict_direction("存货积压，主因在需求下滑") == "negative"
    assert conflict_direction("经营异常，系管理层动荡所致") == "negative"
    assert conflict_direction("存货积压，主因在需求下滑但备货充足") == "benign"


def test_002916_c6_c7_real_conflicts_stay_conservative():
    """002916 记录中另 2 条已结算冲突（medium，D3 issue 明文列出 7 条冲突票）：
    c6 含『不可持续』、c7 为裸『主因在…而非…』（无良性标记）⇒ 均维持负贡献。"""
    assert conflict_direction("PEG 基于不可持续高增速") == "negative"
    assert conflict_direction("现金流恶化主因在存货而非应收") == "negative"


def test_engine_002916_five_conflicts_projection():
    """engine 侧投影：五冲突中仅 c5（unknown）维持负贡献（1 次 high 扣分），其余不投负票。"""
    signal_results = {
        "quality_signal": {
            "level": "high",
            "interpretation": "盈利质量信号。",
            "thesis_impact": {"earnings_quality": "positive"},
        }
    }
    conflict_analysis = {
        "conflicts": [
            {
                "id": cid,
                "theme": f"冲突{cid}",
                "description": "描述",
                "related_dimensions": ["earnings_quality"],
                "severity": severity,
                "confidence": 0.9,
                "hypotheses": [{"id": f"h{cid}", "explanation": explanation, "status": "pending"}],
            }
            for (cid, severity, explanation, _expected) in _REAL_002916_CONFLICTS
        ]
    }
    verification_results = [
        {"hypothesis_id": f"h{cid}", "status": "verified", "summary": explanation, "gaps": []}
        for (cid, _severity, explanation, _expected) in _REAL_002916_CONFLICTS
    ]
    thesis = ThesisEngine().run(
        symbol="002916.SZ",
        period="2026Q2",
        signal_results=signal_results,
        conflict_analysis=conflict_analysis,
        verification_results=verification_results,
    )
    dim = thesis.dimensions["earnings_quality"]
    conflict_evidence = [item for item in dim.evidence if item.source_type == "conflict"]
    # 仅 c5（unknown）计票一次（high 档 -0.55）；c1/c2/c3/c4 良性跳过（不投负票）
    assert len(conflict_evidence) == 1
    assert conflict_evidence[0].signal_id == "conflict:c5"
    assert dim.score == pytest.approx(1.0 - 0.55)


def test_reviewer_002916_five_conflicts_votes():
    """reviewer 侧口径一致：五冲突中仅 c5（unknown）投负票，票值 = -0.55/0.8。"""
    conflicts = ConflictAnalysisResult(
        conflicts=[
            ConflictItem(
                id=cid,
                theme=f"冲突{cid}",
                description="描述",
                related_dimensions=["earnings_quality"],
                severity=severity,
                confidence=0.9,
                hypotheses=[
                    HypothesisItem(
                        id=f"h{cid}",
                        conflict_id=cid,
                        explanation=explanation,
                        predictions=[],
                        required_evidence=[],
                        score=0.8,
                        status="verified",
                    )
                ],
            )
            for (cid, severity, explanation, _expected) in _REAL_002916_CONFLICTS
        ]
    )
    votes, notes = reviewer_module._conflict_votes(conflicts)
    assert votes == pytest.approx([-0.55 / 0.8])
    # _conflict_votes 的真实契约：每冲突一条 note——4 条良性跳过 + 1 条 c5 扣分
    assert len(notes) == 5
    assert sum("不计负票" in note for note in notes) == 4
    assert any("c5" in note and "扣分" in note for note in notes)
