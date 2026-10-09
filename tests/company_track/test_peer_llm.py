"""判定 D（独立 batch judge 调用层）的密闭测试：批量切分 / 闭集校验 / fail-open / prompt 契约。

被测实现位于 ``alphabee/company_track/peer_judge.py``（判定 C/D 的**单一实现处**：prompt 构造、
归一化、权重阈值、judge 批量入口都在那里）。本文件名沿用任务契约 inScope 的路径名，
以免为凑路径另建一个只做 re-export 的空壳模块。

纪律：

- **不调真 LLM**：一律注入假模型（``model=`` 参数），断言 prompt 内容与解析结果；
- **判别力自证**：M4（批次数超限不切分、丢候选）、M5（闭集外代码未丢弃）对实现字节实测必红。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from alphabee import PROJECT_ROOT

_JUDGE_MODULE = PROJECT_ROOT / "alphabee" / "company_track" / "peer_judge.py"


def _dims(product: float = 0.9, customer: float = 0.9, material: float = 0.9, model: float = 0.9) -> dict[str, float]:
    return {"product": product, "customer": customer, "material_tech": material, "business_model": model}


def _pool(codes: list[str] | None = None) -> list[dict[str, Any]]:
    codes = codes or ["002463.SZ", "300476.SZ", "600183.SH"]
    return [{"code": code, "name": f"名{index}"} for index, code in enumerate(codes)]


def _rows_json(rows: list[tuple[str, str, dict[str, float]]]) -> str:
    import json

    return json.dumps(
        [
            {"code": code, "verdict": verdict, "dims": dims, "reason": f"匹配点：{code}；不匹配点：无"}
            for code, verdict, dims in rows
        ],
        ensure_ascii=False,
    )


class FakeJudge:
    """按调用序号返回预设 content 的假模型（记录 prompts）。"""

    def __init__(self, contents: list[str] | str, *, raises: Exception | None = None) -> None:
        self.contents = contents if isinstance(contents, list) else [contents]
        self.raises = raises
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        index = min(len(self.prompts) - 1, len(self.contents) - 1)
        return type("R", (), {"content": self.contents[index]})()


# ── 成功路径 / 批量切分 / 闭集校验 ──────────────────────────────────────────


def test_judge_batch_returns_rows_for_whole_pool():
    from alphabee.company_track import peer_judge

    pool = _pool()
    model = FakeJudge(
        _rows_json(
            [
                ("002463.SZ", "direct", _dims(0.9, 0.85)),
                ("300476.SZ", "adjacent", _dims(0.6, 0.5)),
                ("600183.SH", "reject", _dims(0.1, 0.1)),
            ]
        )
    )
    report = peer_judge.judge_peer_candidates_batched("印制电路板", pool, model=model)
    assert len(model.prompts) == 1
    assert report.called is True and report.ok is True and report.degraded is False
    assert report.batches == 1 and report.failed_batches == 0
    assert report.missing_codes == () and report.out_of_set_codes == ()
    assert set(report.results) == {"002463.SZ", "300476.SZ", "600183.SH"}
    assert report.results["002463.SZ"]["verdict"] == "direct"
    assert report.results["002463.SZ"]["dims"]["product"] == pytest.approx(0.9)
    # dims 恒为四维（缺字段补 0）
    assert set(report.results["002463.SZ"]["dims"]) == set(peer_judge.DIMS)
    assert report.note() == ""


def test_judge_splits_pool_into_batches_and_covers_all():
    from alphabee.company_track import peer_judge

    pool = _pool(["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"])
    model = FakeJudge(
        [
            _rows_json([("A.SZ", "direct", _dims()), ("B.SZ", "direct", _dims())]),
            _rows_json([("C.SZ", "direct", _dims()), ("D.SZ", "direct", _dims())]),
            _rows_json([("E.SZ", "adjacent", _dims())]),
        ]
    )
    report = peer_judge.judge_peer_candidates_batched("PCB", pool, batch_size=2, model=model)
    assert report.batches == 3 and len(model.prompts) == 3
    assert report.ok is True and report.missing_codes == ()
    assert set(report.results) == {"A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"}
    # 每批 prompt 只含该批候选（闭集入参），不是全池
    assert "A.SZ" in model.prompts[0] and "C.SZ" not in model.prompts[0]
    assert "E.SZ" in model.prompts[2] and "A.SZ" not in model.prompts[2]


def test_judge_drops_out_of_set_codes():
    """闭集校验：模型给出池外代码 ⇒ 一律丢弃（不计入结果），并记账于 out_of_set_codes。"""
    from alphabee.company_track import peer_judge

    pool = _pool(["002463.SZ"])
    model = FakeJudge(_rows_json([("002463.SZ", "direct", _dims()), ("999999.SZ", "direct", _dims())]))
    report = peer_judge.judge_peer_candidates_batched("PCB", pool, model=model)
    assert set(report.results) == {"002463.SZ"}
    assert report.out_of_set_codes == ("999999.SZ",)
    assert report.ok is True  # 池内全覆盖 ⇒ 可采纳（池外代码不影响池内判定）


def test_judge_duplicate_code_first_wins():
    from alphabee.company_track import peer_judge

    model = FakeJudge(_rows_json([("002463.SZ", "direct", _dims(0.9)), ("002463.SZ", "reject", _dims(0.1))]))
    report = peer_judge.judge_peer_candidates_batched("PCB", _pool(["002463.SZ"]), model=model)
    assert report.results["002463.SZ"]["verdict"] == "direct"
    assert report.results["002463.SZ"]["dims"]["product"] == pytest.approx(0.9)


# ── fail-open：四类失败 + 未调用 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "contents", "raises"),
    [
        ("调用异常", [""], RuntimeError("llm down")),
        ("输出非 JSON 数组", ['{"code": "002463.SZ"}'], None),
        ("输出不可解析", ["not json at all"], None),
        ("空数组（整批漏判）", ["[]"], None),
    ],
)
def test_judge_fail_open_four_classes(label, contents, raises):
    """四类失败（异常/非数组/不可解析/整批空）⇒ ``ok=False``/``degraded=True``，绝不抛异常。"""
    from alphabee.company_track import peer_judge

    model = FakeJudge(contents, raises=raises)
    report = peer_judge.judge_peer_candidates_batched("PCB", _pool(["002463.SZ", "300476.SZ"]), model=model)
    assert report.called is True, label
    assert report.ok is False and report.degraded is True, label
    assert report.results == {}, label
    assert set(report.missing_codes) == {"002463.SZ", "300476.SZ"}, label
    note = report.note()
    assert "judge 降级" in note and "回退生成器分" in note and "不置 no_peers" in note, f"{label}: {note}"


def test_judge_partial_coverage_is_degraded():
    """池内漏判（部分覆盖）⇒ 降级（不混用：调用方须整体回退生成器分）。"""
    from alphabee.company_track import peer_judge

    model = FakeJudge(_rows_json([("002463.SZ", "direct", _dims())]))
    report = peer_judge.judge_peer_candidates_batched("PCB", _pool(["002463.SZ", "300476.SZ"]), model=model)
    assert report.ok is False and report.degraded is True
    assert report.missing_codes == ("300476.SZ",)


@pytest.mark.parametrize(
    "bad_row",
    [
        '{"code": "002463.SZ", "verdict": "maybe", "dims": {"product": 0.9}}',  # 非法三级
        '{"code": "002463.SZ", "verdict": "direct"}',  # 缺 dims
        '{"code": "002463.SZ", "verdict": "direct", "dims": "高"}',  # dims 非法类型
    ],
)
def test_judge_invalid_row_counts_as_missing(bad_row):
    """判定非法（verdict 不在三级内 / dims 非法）＝未取得可用判定 ⇒ 降级，且结果表不含该行。"""
    from alphabee.company_track import peer_judge

    report = peer_judge.judge_peer_candidates_batched("PCB", _pool(["002463.SZ"]), model=FakeJudge(f"[{bad_row}]"))
    assert report.ok is False and report.missing_codes == ("002463.SZ",)
    assert report.results == {}


def test_judge_empty_pool_is_not_called_and_not_degraded():
    """空池 / 空描述 ⇒ ``called=False``（既未判定也不算降级，不污染 judge_degraded 统计）。"""
    from alphabee.company_track import peer_judge

    model = FakeJudge("[]")
    for pool, description in (([], "PCB"), (_pool(), "   ")):
        report = peer_judge.judge_peer_candidates_batched(description, pool, model=model)
        assert report.called is False and report.ok is False and report.degraded is False
    assert model.prompts == []


def test_judge_batch_size_is_clamped_and_configurable():
    from alphabee.company_track import peer_judge

    model = FakeJudge(_rows_json([("A.SZ", "direct", _dims())]))
    report = peer_judge.judge_peer_candidates_batched("PCB", _pool(["A.SZ"]), batch_size=0, model=model)
    assert report.batches == 1 and len(model.prompts) == 1  # 0 ⇒ 钳到 1，不产生空批次


# ── prompt 契约（reasoning-first / verdict 语义 / 同 L3 声明 / 闭集） ────────


def test_judge_prompt_contract():
    from alphabee.company_track import peer_judge

    prompt = peer_judge.build_judge_prompt("印制电路板与封装基板", _pool(["002463.SZ", "600183.SH"]))
    # ① reasoning-first：先写匹配点/不匹配点，再给 verdict 与 dims
    assert "先列「匹配点」与「不匹配点」" in prompt
    assert "不得先给分数再补理由" in prompt
    # ② verdict 三级语义明确
    for token in ('"direct"', '"adjacent"', '"reject"'):
        assert token in prompt
    # ③ 同 L3 是强证据但不能替代业务判断（两侧都写）
    assert (
        "不能替代业务判断" in prompt
        and "L3 不同但产品/客户高度重叠" in prompt
        and "同 L3 但材料/终端/盈利模式不同" in prompt
    )
    # ④ 闭集：只能对清单内代码给结论
    assert "只能对清单内的代码给出结论" in prompt and "不得新增、改动或猜测代码" in prompt
    # ⑤ 不要求 LLM 做采纳决定（决定权交 Gate）
    assert "不负责决定是否采纳" in prompt
    # 候选清单与标的描述都在 prompt 内，且 job 不注入生成器分数（防自评自利）
    assert "002463.SZ 名0" in prompt and "印制电路板与封装基板" in prompt
    assert "overlap" not in prompt


def test_judge_prompt_has_no_generator_scores():
    """judge 入参**不含生成器分数**（生成/判定解耦，设计 D-2）：候选只需 code/name（+可选 E 特征）。"""
    from alphabee.company_track import peer_judge

    pool_with_scores = [
        {"code": "002463.SZ", "name": "沪电股份", "overlap": 0.97, "dims": _dims()},
    ]
    prompt = peer_judge.build_judge_prompt("PCB", pool_with_scores)
    assert "0.97" not in prompt


# ── 判别力自证：变异必红 ────────────────────────────────────────────────────


def _mutant(old: str, new: str, name: str) -> Any:
    source = _JUDGE_MODULE.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"变异锚点不唯一：{old!r}"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.py"
        path.write_text(source.replace(old, new), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def test_m4_batch_size_ignored_is_killed():
    """M4：批量切分失效（不切分 ⇒ 只调一次、且池内候选被漏判）。"""
    from alphabee.company_track import peer_judge

    mutant = _mutant(
        "    size = max(1, int(batch_size))\n"
        "    chunks = [ordered[index : index + size] for index in range(0, len(ordered), size)]",
        "    chunks = [ordered]  # M4 忽略 batch_size（不切分）",
        "pj_judge_mutant_m4",
    )
    pool = _pool(["A.SZ", "B.SZ", "C.SZ"])
    model = FakeJudge(
        [
            _rows_json([("A.SZ", "direct", _dims()), ("B.SZ", "direct", _dims())]),
            _rows_json([("C.SZ", "direct", _dims())]),
        ]
    )
    report = mutant.judge_peer_candidates_batched("PCB", pool, batch_size=2, model=model)
    # 正确实现：2 批、C 由第二批覆盖 ⇒ ok=True；变异：1 批 ⇒ C 漏判 ⇒ ok=False
    assert report.batches == 1 and report.ok is False and report.missing_codes == ("C.SZ",)
    assert (
        peer_judge.judge_peer_candidates_batched(
            "PCB",
            pool,
            batch_size=2,
            model=FakeJudge(
                [
                    _rows_json([("A.SZ", "direct", _dims()), ("B.SZ", "direct", _dims())]),
                    _rows_json([("C.SZ", "direct", _dims())]),
                ]
            ),
        ).ok
        is True
    ), "未变异的实现必须 ok=True（否则判据不灵敏）"


def test_m5_out_of_set_filter_removed_is_killed():
    """M5：闭集校验失效（池外代码被当成结果采纳）。"""
    from alphabee.company_track import peer_judge

    mutant = _mutant(
        "        if code not in allowed:\n            out_of_set.append(code)\n            continue",
        "        if False:  # M5 不校验闭集\n            out_of_set.append(code)\n            continue",
        "pj_judge_mutant_m5",
    )
    model_content = _rows_json([("002463.SZ", "direct", _dims()), ("999999.SZ", "reject", _dims())])
    mutant_report = mutant.judge_peer_candidates_batched("PCB", _pool(["002463.SZ"]), model=FakeJudge(model_content))
    assert "999999.SZ" in mutant_report.results, "M5 未被杀死：池外代码仍被丢弃"
    assert mutant_report.out_of_set_codes == ()
    reference = peer_judge.judge_peer_candidates_batched("PCB", _pool(["002463.SZ"]), model=FakeJudge(model_content))
    assert set(reference.results) == {"002463.SZ"} and reference.out_of_set_codes == ("999999.SZ",)
