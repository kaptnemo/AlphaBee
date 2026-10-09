"""生产侧对标组判定入口（``alphabee/company_track/peer_judge.py``）的解析与契约测试。

覆盖判定 C（生成器打分）/ D（独立 judge）的**纯逻辑契约**（fake model，不调 LLM）：
- verdict 三级语义（``direct|adjacent|reject``；非法值 → ``None``）；
- dims 四维归一化（缺字段 / 非法值 / 越界 / 非 dict）；
- ``overlap`` 归一化（百分数容错）；
- **fail-open**：缺字段 / 非法值 / 非数组 / 异常 → 不抛异常，返回 ``{}``；
- prompt 契约：候选池（含 ``same_l3/same_l2`` 特征）注入、reasoning-first、闭环外 code 丢弃。
"""

from __future__ import annotations

from typing import Any

import pytest

from alphabee.company_track.peer_judge import (
    DIMS,
    MAX_DESCRIPTION_CHARS,
    MODEL_COMPONENT,
    VALID_VERDICTS,
    build_judge_prompt,
    build_scoring_prompt,
    coerce_dims,
    coerce_overlap,
    infer_peer_scoring,
    judge_peer_candidates,
)

DESCRIPTION = "深南电路主营印制电路板（通信/数据中心高多层板）与封装基板。"


class FakeModel:
    """最小 ChatModel：返回给定 content，并记录收到的 prompt。"""

    def __init__(self, content: Any) -> None:
        self.content = content
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        return type("R", (), {"content": self.content})()


class BoomModel:
    def invoke(self, prompt: str) -> Any:
        raise RuntimeError("llm down")


def _pool() -> list[dict[str, Any]]:
    return [
        {"code": "002463.SZ", "name": "沪电股份", "same_l3": True, "same_l2": True},
        {"code": "600584.SH", "name": "长电科技", "same_l3": False, "same_l2": True},
    ]


# ── 归一化 ──────────────────────────────────────────────────────────────────


def test_dims_contract_is_four_dimensions():
    assert DIMS == ("product", "customer", "material_tech", "business_model")
    assert VALID_VERDICTS == {"direct", "adjacent", "reject"}
    assert MODEL_COMPONENT == "agent.peer_group"


def test_coerce_dims_fills_missing_and_clamps():
    assert coerce_dims({"product": 0.9, "customer": "0.5"}) == {
        "product": 0.9,
        "customer": 0.5,
        "material_tech": 0.0,
        "business_model": 0.0,
    }
    assert coerce_dims({"product": 1.8, "customer": -0.3}) == {
        "product": 1.0,
        "customer": 0.0,
        "material_tech": 0.0,
        "business_model": 0.0,
    }
    # 非法值 → 该维 0.0（不抛）
    assert coerce_dims({"product": "高", "customer": None}) == {
        "product": 0.0,
        "customer": 0.0,
        "material_tech": 0.0,
        "business_model": 0.0,
    }
    assert coerce_dims(None) is None
    assert coerce_dims([1, 2]) is None


def test_coerce_overlap_tolerates_percentage_and_garbage():
    assert coerce_overlap(0.7) == pytest.approx(0.7)
    assert coerce_overlap("85") == pytest.approx(0.85)
    # >1 视为百分数（沿用生产既有口径）：1.4 不是 1.4 分而是 1.4%（本步不改变该行为）
    assert coerce_overlap(1.4) == pytest.approx(0.014)
    assert coerce_overlap(120) == pytest.approx(1.0)
    assert coerce_overlap(-1) == pytest.approx(0.0)
    assert coerce_overlap(float("nan")) is None
    assert coerce_overlap("高") is None
    assert coerce_overlap(None) is None


# ── verdict 三级语义 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("direct", "direct"), ("ADJACENT ", "adjacent"), ("Reject", "reject")])
def test_judge_verdict_three_levels(raw, expected):
    model = FakeModel(f'[{{"code": "002463.SZ", "verdict": "{raw}", "dims": {{}}, "reason": "r"}}]')
    out = judge_peer_candidates(DESCRIPTION, _pool(), model=model)
    assert out["002463.SZ"]["verdict"] == expected


@pytest.mark.parametrize("raw", ["maybe", "", "  ", None, 3, "direct-ish"])
def test_judge_illegal_verdict_becomes_none(raw):
    payload = [{"code": "002463.SZ", "verdict": raw, "dims": {"product": 0.9}, "reason": "r"}]
    out = judge_peer_candidates(DESCRIPTION, _pool(), model=FakeModel(payload))
    assert out["002463.SZ"]["verdict"] is None  # 未判定 → 由闸门丢弃（不猜测）


def test_judge_normalizes_dims_and_keeps_codes_in_pool():
    payload = [
        {"code": "002463.SZ", "verdict": "direct", "dims": {"product": 0.9, "customer": "0.8"}, "reason": "匹配"},
        {"code": "999999.XX", "verdict": "direct", "dims": {"product": 1.0}, "reason": "池外"},  # 必须丢弃
        {"code": "600584.SH", "verdict": "reject", "dims": {"product": 0.2}, "reason": "不匹配"},
    ]
    out = judge_peer_candidates(DESCRIPTION, _pool(), model=FakeModel(payload))
    assert set(out) == {"002463.SZ", "600584.SH"}
    assert out["002463.SZ"]["dims"] == {"product": 0.9, "customer": 0.8, "material_tech": 0.0, "business_model": 0.0}
    assert out["600584.SH"]["verdict"] == "reject"


def test_judge_scoring_generator_shape():
    payload = [
        {"code": "002463.SZ", "overlap": 86, "dims": {"product": 0.85}, "reason": "同环节"},
        {"code": "600584.SH", "overlap": "低", "dims": "not-a-dict", "reason": "不同环节"},
    ]
    out = infer_peer_scoring(DESCRIPTION, _pool(), industry="印制电路板", model=FakeModel(payload))
    assert out["002463.SZ"]["overlap"] == pytest.approx(0.86)
    assert out["600584.SH"]["overlap"] is None  # 非法 overlap 不因此剔除
    assert out["600584.SH"]["dims"] is None


# ── fail-open ───────────────────────────────────────────────────────────────


def test_judge_fail_open_on_missing_fields():
    payload = [{"code": "002463.SZ"}]
    out = judge_peer_candidates(DESCRIPTION, _pool(), model=FakeModel(payload))
    assert out == {"002463.SZ": {"verdict": None, "dims": None, "reason": ""}}


@pytest.mark.parametrize(
    "content",
    [
        '{"code": "002463.SZ"}',  # JSON 对象而非数组
        "不是 JSON",
        "",
        None,
        [1, 2, 3],  # 数组但元素非对象
        "[not json",
    ],
)
def test_judge_fail_open_non_array_or_garbage(content):
    out = judge_peer_candidates(DESCRIPTION, _pool(), model=FakeModel(content))
    assert out == {}


def test_judge_fail_open_on_exception():
    assert judge_peer_candidates(DESCRIPTION, _pool(), model=BoomModel()) == {}
    assert infer_peer_scoring(DESCRIPTION, _pool(), model=BoomModel()) == {}


@pytest.mark.parametrize("description", ["", "   "])
def test_judge_skips_without_description(description):
    model = FakeModel('[{"code": "002463.SZ", "verdict": "direct"}]')
    assert judge_peer_candidates(description, _pool(), model=model) == {}
    assert infer_peer_scoring(description, _pool(), model=model) == {}
    assert model.prompts == []  # 无描述不调模型（不编造）


def test_judge_skips_empty_candidate_pool():
    model = FakeModel('[{"code": "002463.SZ", "verdict": "direct"}]')
    assert judge_peer_candidates(DESCRIPTION, [], model=model) == {}
    assert model.prompts == []


# ── prompt 契约（生产侧唯一处，harness 不再另写） ───────────────────────────


def test_judge_prompt_contract():
    prompt = build_judge_prompt(DESCRIPTION, _pool())
    # reasoning-first：要求先列匹配点/不匹配点
    assert "匹配点" in prompt and "不匹配点" in prompt
    # verdict 三级语义
    for verdict in VALID_VERDICTS:
        assert verdict in prompt
    # 不要求 LLM 做采纳决定（决定权交 Gate）
    assert "采纳" not in prompt
    # E 特征注入 + 「同 L3 不能替代业务判断」
    assert "same_l3=True" in prompt and "same_l2=True" in prompt
    assert "不能替代业务判断" in prompt
    # 候选池代码与名称都在 prompt 内（闭集）
    assert "002463.SZ" in prompt and "沪电股份" in prompt and "600584.SH" in prompt


def test_scoring_prompt_contract_has_all_dims_and_no_new_codes():
    prompt = build_scoring_prompt(DESCRIPTION, _pool(), industry="印制电路板")
    for dim in DIMS:
        assert dim in prompt
    assert "不要新增或改动代码" in prompt
    assert "印制电路板" in prompt


def test_prompt_truncates_long_description():
    long_desc = "业" * (MAX_DESCRIPTION_CHARS + 500)
    prompt = build_judge_prompt(long_desc, _pool())
    assert long_desc[:MAX_DESCRIPTION_CHARS] in prompt
    assert long_desc not in prompt


def test_judge_prompt_omits_absent_taxonomy_hints():
    prompt = build_judge_prompt(DESCRIPTION, [{"code": "601138.SH", "name": "工业富联"}])
    assert "same_l3" not in prompt
