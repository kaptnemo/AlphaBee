"""evidence_extractor Stage A（客观事实抽取）单测。

覆盖设计 ``MIDTERM_EVIDENCE_EXTRACTION.md`` §4.1/§4.2：

- 非结构化文本 → FactEvent[]（客观、只问发生了什么）；
- prompt 强制 quotes/date/kind/数值只抄原文/不相关输出空列表（防幻觉）；
- 严格 JSON FactEvent[] + Pydantic 校验；
- LLM 失败 / 坏结构 / 坏 JSON → 降级 []（不中断）；
- id 由确定性规则回填（§7 事件签名哈希）。
"""

import json

from langchain_core.messages import AIMessage

from alphabee.midterm.evidence_extractor import (
    _build_messages,
    _fact_id,
    _normalize_texts,
    extract_facts,
)


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
