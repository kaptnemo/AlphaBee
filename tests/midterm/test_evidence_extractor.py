"""evidence_extractor 两阶段抽取（Stage A 事实抽取 + Stage B 方向判定/组装）单测。

覆盖设计 ``MIDTERM_EVIDENCE_EXTRACTION.md`` §4.1/§4.2/§4.3：

- Stage A：非结构化文本 → FactEvent[]（客观、只问发生了什么）；
- prompt 强制 quotes/date/kind/数值只抄原文/不相关输出空列表（防幻觉）；
- 严格 JSON FactEvent[] + Pydantic 校验；
- LLM 失败 / 坏结构 / 坏 JSON → 降级 []（不中断）；
- id 由确定性规则回填（§7 事件签名哈希）；
- Stage B：FactEvent + Thesis H → EvidenceJudgment（方向 + 离散强度），thesis 空 / LLM
  失败退化（数值按符号、定性 neutral），组装 EvidenceEvent（strength→delta 离散映射）。
"""

import json

from langchain_core.messages import AIMessage

from alphabee.midterm.evidence_extractor import (
    _build_messages,
    _build_stage_b_messages,
    _fact_id,
    _normalize_texts,
    assemble_events,
    extract_facts,
    judge_facts,
)
from alphabee.midterm.models import EffectOnThesis, EvidenceJudgment, FactEvent, Strength


class FakeModel:
    """注入式 mock LLM：返回指定 AIMessage 内容，或抛出异常。"""

    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return AIMessage(content=self.content)


def _valid_json():
    return json.dumps(
        {
            "events": [
                {
                    "id": "",
                    "date": "2026-06-30",
                    "kind": "expectation",
                    "description": "公司发布业绩预告，预计净利润同比增长 50%",
                    "numbers": {"profit_forecast_min_change": 50.0},
                    "quotes": ["预计净利润同比增长 50%"],
                    "source_refs": [],
                    "source_type": "",
                }
            ]
        },
        ensure_ascii=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 基础纯函数
# ─────────────────────────────────────────────────────────────────────────────
def test_normalize_texts():
    assert _normalize_texts("abc") == "abc"
    assert "---" in _normalize_texts(["a", "b"])  # 多窗口拼接
    assert _normalize_texts([]) == ""


def test_prompt_enforces_objective_and_anti_hallucination():
    joined = "\n".join(str(m.content) for m in _build_messages("某公司 Q2 收入恢复"))
    # §4.2：客观 + 引用 + 数值只抄原文 + 不相关空列表
    assert "只报告" in joined or "发生了什么" in joined
    assert "quotes" in joined
    assert "禁止预测" in joined
    assert "补全" in joined
    assert "events" in joined  # FactEventList 字段定义


def test_fact_id_deterministic():
    a = _fact_id("2026-06-30", "expectation", "600519", "业绩预告", {"x": 1.0})
    b = _fact_id("2026-06-30", "expectation", "600519", "业绩预告", {"x": 1.0})
    c = _fact_id("2026-06-30", "expectation", "600519", "业绩预告", {"x": 2.0})
    assert a == b
    assert a != c  # 数值不同 → 不同 id


# ─────────────────────────────────────────────────────────────────────────────
# 正常抽取
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_facts_valid_json():
    facts = extract_facts("原文文本", symbol="600519", source_type="forecast", model=FakeModel(_valid_json()))
    assert len(facts) == 1
    f = facts[0]
    assert f.kind == "expectation"
    assert f.date == "2026-06-30"
    assert f.source_type == "forecast"  # 回填
    assert f.id == _fact_id(f.date, f.kind, "600519", f.description, f.numbers)  # id 由规则回填
    assert f.quotes == ["预计净利润同比增长 50%"]


def test_extract_facts_bare_list_compat():
    bare = json.dumps(
        [
            {
                "id": "",
                "date": "2026-06-30",
                "kind": "fundamental",
                "description": "营收同比增长 10%",
                "numbers": {"revenue_yoy": 10.0},
                "quotes": ["营收同比增长 10%"],
                "source_refs": [],
                "source_type": "",
            }
        ],
        ensure_ascii=False,
    )
    facts = extract_facts("t", model=FakeModel(bare))
    assert len(facts) == 1
    assert facts[0].kind == "fundamental"


def test_extract_facts_dedup():
    dup_json = json.dumps(
        {
            "events": [
                {
                    "id": "",
                    "date": "2026-06-30",
                    "kind": "expectation",
                    "description": "预计净利润同比增长 50%",
                    "numbers": {"profit_forecast_min_change": 50.0},
                    "quotes": ["预计净利润同比增长 50%"],
                    "source_refs": [],
                    "source_type": "",
                },
                {
                    "id": "",
                    "date": "2026-06-30",
                    "kind": "expectation",
                    "description": "预计净利润同比增长 50%",
                    "numbers": {"profit_forecast_min_change": 50.0},
                    "quotes": ["预计净利润同比增长 50%"],
                    "source_refs": ["https://example.com"],
                    "source_type": "",
                },
            ]
        },
        ensure_ascii=False,
    )
    facts = extract_facts("t", symbol="600519", model=FakeModel(dup_json))
    assert len(facts) == 1  # 同主题去重
    assert facts[0].source_refs == ["https://example.com"]


def test_extract_facts_empty_description_dropped():
    bad = json.dumps(
        {
            "events": [
                {
                    "id": "",
                    "date": "",
                    "kind": "expectation",
                    "description": "",
                    "numbers": {},
                    "quotes": [],
                    "source_refs": [],
                    "source_type": "",
                }
            ]
        },
        ensure_ascii=False,
    )
    assert extract_facts("t", model=FakeModel(bad)) == []


# ─────────────────────────────────────────────────────────────────────────────
# 失败降级（§11）：LLM 挂掉 / 坏结构 / 坏 JSON → []
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_facts_llm_raises_returns_empty():
    assert extract_facts("t", model=FakeModel(exc=RuntimeError("boom"))) == []


def test_extract_facts_invalid_json_returns_empty():
    assert extract_facts("t", model=FakeModel("这不是 JSON")) == []


def test_extract_facts_invalid_schema_returns_empty():
    # 缺必填字段（kind 不存在于 schema）→ Pydantic ValidationError → []
    bad = json.dumps({"events": [{"foo": "bar"}]}, ensure_ascii=False)
    assert extract_facts("t", model=FakeModel(bad)) == []


def test_extract_facts_empty_text_no_llm_call():
    m = FakeModel(_valid_json())
    assert extract_facts("", model=m) == []
    assert extract_facts("   ", model=m) == []
    assert m.calls == 0  # 空文本不触发 LLM


# ─────────────────────────────────────────────────────────────────────────────
# Stage B：方向判定（judge_facts）
# ─────────────────────────────────────────────────────────────────────────────
def _fact(fid="f1", kind="expectation", desc="营收同比增长 10%", numbers=None, quotes=None):
    return FactEvent(
        id=fid,
        date="2026-06-30",
        kind=kind,
        description=desc,
        numbers=numbers if numbers is not None else {},
        quotes=quotes if quotes is not None else ["营收同比增长 10%"],
        source_refs=["https://example.com"],
        source_type="forecast",
    )


def test_stage_b_prompt_requires_reasoning_cite_quotes():
    joined = "\n".join(str(m.content) for m in _build_stage_b_messages([_fact()], "H: 营收高增长"))
    assert "reasoning" in joined  # reasoning 字段定义
    assert "quotes" in joined  # 强制引用原文
    assert "weak" in joined and "medium" in joined and "strong" in joined  # 离散强度三档


def test_judge_facts_with_llm():
    fact = _fact(numbers={"revenue_yoy": 10.0})
    jjson = json.dumps(
        {
            "judgments": [
                {
                    "fact_id": "f1",
                    "effect_on_thesis": "confirming",
                    "strength": "medium",
                    "reasoning": "营收同比增长与 H 一致（引用原文「营收同比增长 10%」）",
                }
            ]
        },
        ensure_ascii=False,
    )
    judgments = judge_facts([fact], thesis="H: 营收高增长", model=FakeModel(jjson))
    assert len(judgments) == 1
    assert judgments[0].effect_on_thesis == EffectOnThesis.CONFIRMING
    assert judgments[0].strength == Strength.MEDIUM


def test_judge_facts_thesis_empty_degraded():
    numeric = _fact(fid="n", numbers={"revenue_yoy": 5.0})
    qualitative = _fact(fid="q", numbers={}, desc="公司发布新产品")
    judgments = judge_facts([numeric, qualitative], thesis="")
    by_id = {j.fact_id: j for j in judgments}
    assert by_id["n"].effect_on_thesis == EffectOnThesis.CONFIRMING  # 数值正 → confirming
    assert by_id["q"].effect_on_thesis == EffectOnThesis.NEUTRAL  # 定性 → neutral


def test_judge_facts_negative_numeric_degraded():
    numeric = _fact(fid="n", numbers={"revenue_yoy": -5.0})
    j = judge_facts([numeric], thesis="")[0]
    assert j.effect_on_thesis == EffectOnThesis.REFUTING  # 数值负 → refuting


def test_judge_facts_llm_failure_degraded():
    numeric = _fact(fid="n", numbers={"revenue_yoy": 5.0})
    qualitative = _fact(fid="q", numbers={})
    judgments = judge_facts([numeric, qualitative], thesis="H", model=FakeModel(exc=RuntimeError("boom")))
    by_id = {j.fact_id: j for j in judgments}
    assert by_id["n"].effect_on_thesis == EffectOnThesis.CONFIRMING
    assert by_id["q"].effect_on_thesis == EffectOnThesis.NEUTRAL


def test_judge_facts_rejects_continuous_strength():
    # LLM 输出 strength="0.42" → Pydantic ValidationError → 整批退化（禁连续值）
    numeric = _fact(fid="n", numbers={"revenue_yoy": 5.0})
    bad = json.dumps(
        {"judgments": [{"fact_id": "n", "effect_on_thesis": "confirming", "strength": "0.42", "reasoning": "r"}]},
        ensure_ascii=False,
    )
    judgments = judge_facts([numeric], thesis="H", model=FakeModel(bad))
    assert judgments[0].effect_on_thesis == EffectOnThesis.CONFIRMING  # 退化按符号
    assert judgments[0].strength == Strength.WEAK


# ─────────────────────────────────────────────────────────────────────────────
# 组装（assemble_events）
# ─────────────────────────────────────────────────────────────────────────────
def test_assemble_events_discrete_delta_and_source_refs():
    facts = [_fact(fid="f1", numbers={"revenue_yoy": 10.0})]
    judgments = [
        EvidenceJudgment(
            fact_id="f1", effect_on_thesis=EffectOnThesis.CONFIRMING, strength=Strength.MEDIUM, reasoning="r"
        )
    ]
    events = assemble_events(facts, judgments)
    assert len(events) == 1
    e = events[0]
    assert e.id == "f1"  # fact_id → id
    assert e.confidence_delta == 0.3  # medium
    assert e.effect_on_thesis == "confirming"
    assert e.source_refs == ["营收同比增长 10%", "https://example.com"]  # quotes + source_refs


def test_assemble_events_neutral_zero_delta():
    facts = [_fact(fid="f1")]
    judgments = [
        EvidenceJudgment(fact_id="f1", effect_on_thesis=EffectOnThesis.NEUTRAL, strength=Strength.WEAK, reasoning="r")
    ]
    events = assemble_events(facts, judgments)
    assert events[0].confidence_delta == 0.0  # neutral → bayes no-op
    assert events[0].effect_on_thesis == "neutral"


def test_assemble_events_all_discrete_deltas():
    facts = [_fact(fid="f1"), _fact(fid="f2"), _fact(fid="f3")]
    judgments = [
        EvidenceJudgment(
            fact_id="f1", effect_on_thesis=EffectOnThesis.CONFIRMING, strength=Strength.WEAK, reasoning="r"
        ),
        EvidenceJudgment(
            fact_id="f2", effect_on_thesis=EffectOnThesis.CONFIRMING, strength=Strength.MEDIUM, reasoning="r"
        ),
        EvidenceJudgment(
            fact_id="f3", effect_on_thesis=EffectOnThesis.REFUTING, strength=Strength.STRONG, reasoning="r"
        ),
    ]
    events = assemble_events(facts, judgments)
    assert {e.confidence_delta for e in events} == {0.1, 0.3, 0.5}  # 只取离散等级


def test_assemble_events_drops_orphan_judgment():
    facts = [_fact(fid="f1")]
    judgments = [
        EvidenceJudgment(
            fact_id="missing", effect_on_thesis=EffectOnThesis.CONFIRMING, strength=Strength.STRONG, reasoning="r"
        )
    ]
    assert assemble_events(facts, judgments) == []
