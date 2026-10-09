"""标注集元数据自洽校验（tests/fixtures/peer_group_eval/labels.yaml）。

目的：让「散文注释 / 分账行与机器可读字段不一致」这类缺陷今后**必然判红**，而不是靠人眼复核。

判据（全部**从文件内容现算**，测试内不硬编码任何例数/百分比/业态名计数）：
1. 头部 ``split_counts:`` 分账行 ↔ 逐 case ``split`` 字段现算值（total/train/holdout/百分比，百分比按现算值四舍五入到 0.1）；
2. 头部 ``segment_counts:`` 分账行 ↔ 逐 case ``segment`` 字段现算计数（业态名本身也从文件现收集，不写死）；
3. 头部 ``holdout_segments:`` 行 ↔ holdout 子集现算的业态集合；
4. 段头注释 ``# ── <业态>（<split>）`` ↔ 其下每个 case 的 ``segment``/``split`` 字段一致（防止 case 被挪动后段头撒谎）；
5. 契约阈值：例数 15–20、holdout 占比 25%–35%、holdout 含空组/跨行业/分类冲突各 ≥1、holdout 覆盖 ≥5 类业态且六类整体各 ≥1。

判别力变异（必红实测见 tests/company_track/test_peer_group_eval.py::test_m5_*）：
把分账行数字改错、或把某例 split 改了却不动分账行 ⇒ 本模块断言失败。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from alphabee import PROJECT_ROOT

LABELS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "peer_group_eval" / "labels.yaml"

REQUIRED_SEGMENTS = {"同质 L3", "分类冲突", "残差桶", "境内外混合", "无对标空组", "跨行业"}
MIN_CASES, MAX_CASES = 15, 20
MIN_HOLDOUT_RATIO, MAX_HOLDOUT_RATIO = 0.25, 0.35

_SPLIT_LINE = re.compile(
    r"^#\s+split_counts:\s*total=(\d+),\s*train=(\d+),\s*holdout=(\d+),\s*holdout_ratio=([\d.]+)%\s*$", re.M
)
_SEGMENT_LINE = re.compile(r"^#\s+segment_counts:\s*(.+)$", re.M)
_HOLDOUT_LINE = re.compile(r"^#\s+holdout_segments:\s*(.+)$", re.M)
_SECTION_HEADER = re.compile(r"^#\s*──\s*(.+?)（(train|holdout)）")


@pytest.fixture(scope="module")
def payload() -> dict[str, Any]:
    """现算：case 列表 + 头部注释分账 + 段头清单（全部来自文件内容）。"""
    return inspect_labels(LABELS_PATH)


def inspect_labels(path: Path) -> dict[str, Any]:
    """解析标注集并**现算**元数据；供测试与变异实验共用（不依赖 pytest fixture）。"""
    text = path.read_text(encoding="utf-8")
    cases = _load_cases(path)
    splits = [str(c.get("split") or "train") for c in cases]
    segments = [str(c.get("segment") or "") for c in cases]
    holdout_cases = [c for c in cases if (c.get("split") or "train") == "holdout"]

    split_line = _SPLIT_LINE.search(text)
    segment_line = _SEGMENT_LINE.search(text)
    holdout_line = _HOLDOUT_LINE.search(text)

    declared_segments: dict[str, int] = {}
    if segment_line:
        for part in segment_line.group(1).split(","):
            name, _, count = part.rpartition("=")
            if name.strip():
                declared_segments[name.strip()] = int(count.strip())

    return {
        "text": text,
        "cases": cases,
        "segments": segments,
        "splits": splits,
        "computed": {
            "total": len(cases),
            "train": splits.count("train"),
            "holdout": splits.count("holdout"),
            "holdout_ratio": round(100.0 * splits.count("holdout") / len(cases), 1) if cases else 0.0,
            "segment_counts": {seg: segments.count(seg) for seg in sorted(set(segments))},
            "holdout_segments": sorted({str(c.get("segment") or "") for c in holdout_cases}),
            "expect_empty_in_holdout": sum(1 for c in holdout_cases if c.get("expect_empty")),
        },
        "declared": {
            "split_counts": (
                [int(g) for g in split_line.groups()[:3]] + [float(split_line.group(4))] if split_line else None
            ),
            "segment_counts": declared_segments,
            "holdout_segments": (
                sorted({s.strip() for s in holdout_line.group(1).split(",") if s.strip()}) if holdout_line else []
            ),
        },
        "sections": _section_headers(text),
    }


def _load_cases(path: Path) -> list[dict[str, Any]]:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    assert isinstance(raw, list)
    return [c for c in raw if isinstance(c, dict)]


def _section_headers(text: str) -> list[dict[str, Any]]:
    """段头（业态, split）→ 其下 case 的 (segment, split)，按出现顺序。"""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    pending_segment = ""
    for line in text.split("\n"):
        match = _SECTION_HEADER.match(line)
        if match:
            current = {"segment": match.group(1), "split": match.group(2), "cases": []}
            out.append(current)
            continue
        if line.startswith("  segment: "):
            pending_segment = line.split(": ", 1)[1].strip()
        elif line.startswith("  split: ") and current is not None:
            current["cases"].append((pending_segment, line.split(": ", 1)[1].strip()))
    return out


# ── 1. 头部注释 ↔ 机器可读字段 ──────────────────────────────────────────────


def test_split_counts_line_matches_fields(payload):
    declared = payload["declared"]["split_counts"]
    assert declared is not None, "头部缺少 split_counts: 分账行"
    computed = payload["computed"]
    assert declared == [computed["total"], computed["train"], computed["holdout"], computed["holdout_ratio"]]


def test_segment_counts_line_matches_fields(payload):
    declared = payload["declared"]["segment_counts"]
    computed = payload["computed"]["segment_counts"]
    assert declared == computed, f"segment_counts 分账行与字段不一致：{declared} vs {computed}"


def test_holdout_segments_line_matches_fields(payload):
    assert payload["declared"]["holdout_segments"] == payload["computed"]["holdout_segments"]


def test_header_comment_has_no_stale_totals(payload):
    """头部不得残留任何写法不一致的「合计 N 例」自述（现算总数为唯一真值）。"""
    total = payload["computed"]["total"]
    others = [n for n in re.findall(r"合计\s*(\d+)\s*例", payload["text"]) if int(n) != total]
    assert others == [], f"头部出现与现算总数({total})不符的『合计 N 例』：{others}"


# ── 2. 段头 ↔ 各 case 字段 ──────────────────────────────────────────────────


def test_section_headers_match_case_fields(payload):
    sections = payload["sections"]
    assert sections, "标注集缺少段头注释"
    offenders = [
        (section["segment"], section["split"], wrong)
        for section in sections
        for wrong in section["cases"]
        if wrong != (section["segment"], section["split"])
    ]
    assert offenders == [], f"段头与其下 case 字段不一致：{offenders}"


def test_every_case_covered_by_a_section(payload):
    covered = sum(len(section["cases"]) for section in payload["sections"])
    assert covered == payload["computed"]["total"]


# ── 3. 契约阈值（holdout 覆盖 / 占比 / 六类业态） ───────────────────────────


def test_case_count_within_contract(payload):
    assert MIN_CASES <= payload["computed"]["total"] <= MAX_CASES


def test_holdout_ratio_within_contract(payload):
    ratio = payload["computed"]["holdout"] / payload["computed"]["total"]
    assert MIN_HOLDOUT_RATIO <= ratio <= MAX_HOLDOUT_RATIO, ratio


def test_six_segments_present(payload):
    assert REQUIRED_SEGMENTS <= set(payload["computed"]["segment_counts"])


def test_holdout_covers_at_least_five_segments_including_residual(payload):
    """holdout 必须覆盖 ≥5 类业态且**含残差桶**（否则 E 标定在 holdout 段无残差桶证据）。"""
    holdout_segments = set(payload["computed"]["holdout_segments"])
    assert len(holdout_segments) >= 5, holdout_segments
    residual = next(seg for seg in REQUIRED_SEGMENTS if "残差桶" in seg)
    assert residual in holdout_segments


def test_holdout_contains_required_archetypes(payload):
    """holdout 至少含 1 例空组（expect_empty）+ 1 例跨行业 + 1 例分类冲突。"""
    segments = set(payload["computed"]["holdout_segments"])
    assert payload["computed"]["expect_empty_in_holdout"] >= 1
    assert any("跨行业" in seg for seg in segments)
    assert any("分类冲突" in seg for seg in segments)


# ── 4. 每个 case 字段完整性（防止挪动/编辑时掉字段） ────────────────────────


def test_every_case_has_required_fields(payload):
    for case in payload["cases"]:
        for field in ("symbol", "name", "industry", "taxonomy_l3", "taxonomy_l2", "segment", "split", "pool_source"):
            assert case.get(field), f"{case.get('symbol')} 缺字段 {field}"
        assert case["split"] in {"train", "holdout"}
        assert case.get("candidates"), f"{case['symbol']} 无候选"


# ── 5. 判别力：把分账行改错 / 改字段不改注释 ⇒ 必红 ─────────────────────────


def _inspect_mutant(tmp_path: Path, text: str) -> dict[str, Any]:
    path = tmp_path / "labels_mutant.yaml"
    path.write_text(text, encoding="utf-8")
    return inspect_labels(path)


def test_m5a_wrong_total_in_comment_is_killed(payload, tmp_path):
    """变异 A：把分账行的 total 改错（注释撒谎）⇒ 断言必红。"""
    mutated = payload["text"].replace("split_counts: total=19,", "split_counts: total=18,", 1)
    assert mutated != payload["text"]
    mutant = _inspect_mutant(tmp_path, mutated)
    declared = mutant["declared"]["split_counts"]
    computed = mutant["computed"]
    assert declared != [computed["total"], computed["train"], computed["holdout"], computed["holdout_ratio"]]
    assert declared[0] == 18  # 变异体确实写进了错数字（说明判据不是靠巧合通过的）


def test_m5b_wrong_holdout_ratio_in_comment_is_killed(payload, tmp_path):
    """变异 B：百分比改错（27.8% <-> 31.6%）⇒ 必红。"""
    mutated = payload["text"].replace("holdout_ratio=31.6%", "holdout_ratio=27.8%", 1)
    mutant = _inspect_mutant(tmp_path, mutated)
    assert mutant["declared"]["split_counts"][3] != pytest.approx(mutant["computed"]["holdout_ratio"])


def test_m5c_changing_split_without_updating_comment_is_killed(payload, tmp_path):
    """变异 C：把 1 例 holdout 改成 train 却不动分账行 ⇒ 两条判据同时必红。"""
    mutated = payload["text"].replace("  split: holdout\n", "  split: train\n", 1)
    assert mutated != payload["text"]
    mutant = _inspect_mutant(tmp_path, mutated)
    computed = mutant["computed"]
    assert computed["holdout"] != 6 or computed["holdout_ratio"] != 31.6
    assert mutant["declared"]["split_counts"] != [
        computed["total"],
        computed["train"],
        computed["holdout"],
        computed["holdout_ratio"],
    ]


def test_m5d_wrong_segment_counts_line_is_killed(payload, tmp_path):
    """变异 D：业态分账行改错 ⇒ 必红。"""
    mutated = payload["text"].replace("残差桶=4", "残差桶=3", 1)
    mutant = _inspect_mutant(tmp_path, mutated)
    assert mutant["declared"]["segment_counts"] != mutant["computed"]["segment_counts"]


def test_m5e_changed_segment_without_header_update_is_killed(payload, tmp_path):
    """变异 E：只改 1 例 segment 字段而不动段头/分账行 ⇒ 段头判据与分账判据必红。"""
    mutated = payload["text"].replace("  segment: 残差桶\n", "  segment: 跨行业\n", 1)
    assert mutated != payload["text"]
    mutant = _inspect_mutant(tmp_path, mutated)
    offenders = [
        wrong
        for section in mutant["sections"]
        for wrong in section["cases"]
        if wrong != (section["segment"], section["split"])
    ]
    assert offenders, "段头判据未发现变异 ⇒ 判据不灵敏"
    assert mutant["declared"]["segment_counts"] != mutant["computed"]["segment_counts"]


def test_m5f_stale_total_sentence_is_killed(payload, tmp_path):
    """变异 F：插回旧的『合计 18 例』自述句 ⇒ 陈旧合计判据必红。"""
    mutated = payload["text"].replace(
        "# 元数据自洽（", "#   合计 18 例 = train 13 + holdout 5（27.8%）。\n# 元数据自洽（", 1
    )
    assert mutated != payload["text"]
    mutant = _inspect_mutant(tmp_path, mutated)
    total = mutant["computed"]["total"]
    others = [int(n) for n in re.findall(r"合计\s*(\d+)\s*例", mutant["text"]) if int(n) != total]
    assert others == [18]
