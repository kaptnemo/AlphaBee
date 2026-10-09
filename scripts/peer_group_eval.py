"""对标组候选判定策略回归（REPORT_QUALITY_FIX_ROADMAP §11 P2-① 的 §8 标定脚本）。

目的：在**人工标注集**上横向比较几种“候选判定”策略的 precision/recall，并做**离线权重/阈值扫描**
（LLM 维度冻结后纯计算，不重复调 LLM），为生产实现（C 结构化维度 / D 独立 judge / E 分类学特征）
提供参数依据。仅评测，不接入生产链路。

策略来源（C/D/E 的定义见 ``docs/design/PEER_GROUP_JUDGMENT_DESIGN.md``）：
- **B_single**：生成器自评 `overlap` 阈值（现状口径）；
- **C_dims**：生成器同一次调用的**结构化维度**合成后闸门；
- **C_D_judge**：**独立 judge 调用**给 verdict+dims 后闸门；
- **C_E_taxo**：C + 分类学 `same_l3/same_l2` 特征（仅作提示，不硬剔）；
- **C_D_E**：D + E。

两段指标：
1. `taxonomy_recall`：真实对标里有多少落在标的的 same L3 / same L2 成分内（评 E 的召回价值，离线可算）；
2. `gate P/R/F1`：闸门后保留集相对 ground truth（评 C/D 的精度价值）。

**指标口径**：
- 按 `split` 分段输出（train / holdout / 全体）——阈值只能看 train，holdout 只用于验收（设计 §5.2）；
- 每个 split 同时给『排除 disputed』与『未排除 disputed』两套数字（标签边界争议候选，设计 §5.2）；
- `expect_empty: true` 的 case 视为“期望空组”：保留任何候选即 FP（precision=0）。

**prompt 无字面量**：本脚本的生成器打分与 judge 两个阶段**一律调用生产侧入口**
（``alphabee.company_track.peer_judge`` 的 ``infer_peer_scoring`` / ``judge_peer_candidates``），
prompt 构造与解析逻辑全部在 ``peer_judge`` 内，本文件不再复制任何 prompt 文本（设计 §5.2 消除双维护）。

可复现性：LLM 产物落 `data/peer_eval_cache/<symbol>.json`（gitignored，重复采集落
`data/peer_eval_cache/stable_run<N>/<symbol>.json`），默认读缓存；`--record` 才调 LLM。
**权重/阈值扫描永远不调 LLM**。

用法：
    # 首采：调 LLM 并写缓存（需 LLM_API_KEY，脚本内 load_dotenv 兜底读取 .env）
    poetry run python scripts/peer_group_eval.py --record
    # 复算/CI：读缓存，跑全部策略 + 扫参（无网络/无 key 可跑）
    poetry run python scripts/peer_group_eval.py --sweep
    # 稳定性：重复采集 N 次（需 --record），产出保留集 Jaccard
    poetry run python scripts/peer_group_eval.py --record --repeat 3
输出：outputs/peer_group_eval.md（控制台亦打印汇总）。
"""

from __future__ import annotations

import argparse
import csv
import importlib
import itertools
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from alphabee import PROJECT_ROOT


def _load_dotenv() -> None:
    """加载工作区 ``.env``（供 ``LLM_API_KEY``）；缺失 dotenv/文件则静默跳过。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env")


#: ``.env`` 必须早于 ``alphabee.config`` 首次解析（``Settings`` 在 import 期展开 ``${LLM_API_KEY}``）；
#: 缺失 dotenv 时静默跳过（离线复算不需要 key）。
_load_dotenv()


from alphabee.company_track.peer_judge import DIMS as PRODUCTION_DIMS  # noqa: E402
from alphabee.company_track.peer_judge import (  # noqa: E402
    MODEL_COMPONENT,  # noqa: E402
    infer_peer_scoring,
    judge_peer_candidates,
)

# ── 常量 ────────────────────────────────────────────────────────────
DEFAULT_CASES = PROJECT_ROOT / "tests" / "fixtures" / "peer_group_eval" / "labels.yaml"
CACHE_DIR = PROJECT_ROOT / "data" / "peer_eval_cache"
REPORT_PATH = PROJECT_ROOT / "outputs" / "peer_group_eval.md"
_ALL_STOCKS = PROJECT_ROOT / "alphabee" / "static" / "all_stocks.csv"

#: 与生产同组件（设计 §8 决策 1）。
SMALL_MODEL_COMPONENT = MODEL_COMPONENT

#: 缓存结构版本（新增字段时递增；缺省按 v1 兼容读）。
CACHE_SCHEMA_VERSION = 2

STRATEGIES = ("B_single", "C_dims", "C_D_judge", "C_E_taxo", "C_D_E")
WEIGHTS_DEFAULT: dict[str, float] = {"product": 0.40, "customer": 0.30, "business_model": 0.20, "material_tech": 0.10}
#: 结构化维度定义与生产侧保持一致（单一来源）。
DIMS = PRODUCTION_DIMS

SPLITS = ("train", "holdout", "all")
DEFAULT_SPLIT = "train"

# 扫参网格
GRID_OVERLAP = (0.40, 0.45, 0.50, 0.55, 0.60)
GRID_PRODUCT_FLOOR = (0.20, 0.30, 0.40)
GRID_CUSTOMER_FLOOR = (0.10, 0.20, 0.30)


# ── 数据模型 ────────────────────────────────────────────────────────
@dataclass
class Candidate:
    code: str
    name: str = ""
    label: str = ""  # keep / drop
    rationale: str = ""
    same_l3: bool | None = None
    same_l2: bool | None = None
    disputed: bool = False  # 标签边界争议（设计 §5.2）


@dataclass
class EvalCase:
    symbol: str
    name: str = ""
    industry: str = ""
    taxonomy_l3: str = ""
    taxonomy_l2: str = ""
    segment: str = ""  # 业态归类（同质 L3 / 分类冲突 / 残差桶 / 境内外混合 / 无对标空组 / 跨行业）
    split: str = DEFAULT_SPLIT
    pool_source: str = ""  # 冻结候选池来源（历史 LLM 产出 ∪ 同 L2/L3 成分）
    business_description_source: str = "local_report"  # local_report | inline
    business_description: str = ""
    expect_empty: bool = False
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class Decision:
    """单候选的判定中间量（可缓存、可离线复算）。"""

    code: str
    gen_overlap: float | None = None  # B：生成器/自评 overall
    dims: dict[str, float] | None = None  # C：同一生成调用的结构化维度
    judge_verdict: str | None = None  # D：独立 judge
    judge_dims: dict[str, float] | None = None
    judge_overlap: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "gen_overlap": self.gen_overlap,
            "dims": self.dims,
            "judge_verdict": self.judge_verdict,
            "judge_dims": self.judge_dims,
            "judge_overlap": self.judge_overlap,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Decision:
        return cls(
            code=str(raw.get("code") or ""),
            gen_overlap=raw.get("gen_overlap"),
            dims=raw.get("dims"),
            judge_verdict=raw.get("judge_verdict"),
            judge_dims=raw.get("judge_dims"),
            judge_overlap=raw.get("judge_overlap"),
            reason=str(raw.get("reason") or ""),
        )


# ── 分类学（E 特征）─────────────────────────────────────────────────
def _stock_taxonomy() -> dict[str, dict[str, str]]:
    """``stock_code → {l2_code,l2_name,l3_code,l3_name}``（读失败 → 空表）。"""
    if not _ALL_STOCKS.is_file():
        return {}
    out: dict[str, dict[str, str]] = {}
    with _ALL_STOCKS.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            code = str(row.get("stock_code") or "").strip().upper()
            if code:
                out[code] = {
                    "l2_code": str(row.get("sw_l2_code") or "").strip(),
                    "l2_name": str(row.get("sw_l2_name") or "").strip(),
                    "l3_code": str(row.get("sw_l3_code") or "").strip(),
                    "l3_name": str(row.get("sw_l3_name") or "").strip(),
                }
    return out


def _attach_taxonomy(case: EvalCase, taxonomy: dict[str, dict[str, str]]) -> None:
    """给每个候选标 ``same_l3/same_l2``（相对标的的 sw_l3/l2 代码）。"""
    target = taxonomy.get(case.symbol.upper(), {})
    t_l3 = case.taxonomy_l3 or target.get("l3_code", "")
    t_l2 = case.taxonomy_l2 or target.get("l2_code", "")
    for cand in case.candidates:
        info = taxonomy.get(cand.code.upper(), {})
        cand.same_l3 = bool(t_l3) and info.get("l3_code") == t_l3
        cand.same_l2 = bool(t_l2) and info.get("l2_code") == t_l2
    if not case.taxonomy_l3:
        case.taxonomy_l3 = t_l3
    if not case.taxonomy_l2:
        case.taxonomy_l2 = t_l2


def taxonomy_recall(case: EvalCase, level: str = "l3") -> float:
    """真实对标（label=keep）里落在标的 same L{level} 内的比例（E 的召回上限）。"""
    keep = [c for c in case.candidates if c.label == "keep"]
    if not keep:
        return 1.0
    attr = "same_l3" if level == "l3" else "same_l2"
    return sum(1 for c in keep if getattr(c, attr)) / len(keep)


# ── 加载 ────────────────────────────────────────────────────────────
def load_cases(path: Path) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases: list[EvalCase] = []
    for item in raw:
        candidates = [
            Candidate(
                code=str(c.get("code") or ""),
                name=str(c.get("name") or ""),
                label=str(c.get("label") or ""),
                rationale=str(c.get("rationale") or ""),
                disputed=bool(c.get("disputed", False)),
            )
            for c in item.get("candidates", [])
        ]
        cases.append(
            EvalCase(
                symbol=str(item["symbol"]),
                name=str(item.get("name") or ""),
                industry=str(item.get("industry") or ""),
                taxonomy_l3=str(item.get("taxonomy_l3") or ""),
                taxonomy_l2=str(item.get("taxonomy_l2") or ""),
                segment=str(item.get("segment") or ""),
                split=str(item.get("split") or DEFAULT_SPLIT),
                pool_source=str(item.get("pool_source") or ""),
                business_description_source=str(item.get("business_description_source") or "local_report"),
                business_description=str(item.get("business_description") or ""),
                expect_empty=bool(item.get("expect_empty", False)),
                candidates=candidates,
            )
        )
    return cases


def _resolve_business_description(case: EvalCase) -> str:
    if case.business_description:
        return case.business_description
    from alphabee.company_track.peer_report import fetch_local_report_fragments

    sections, _ = fetch_local_report_fragments(case.symbol)
    return sections[0] if sections else ""


def case_candidates_block(case: EvalCase) -> list[dict[str, Any]]:
    """标注集 case → 生产入口的候选池入参（含分类学特征）。"""
    return [{"code": c.code, "name": c.name, "same_l3": c.same_l3, "same_l2": c.same_l2} for c in case.candidates]


# ── LLM 采集 / 缓存（一律走生产侧入口） ─────────────────────────────
def collect_decisions(
    case: EvalCase, *, record: bool, model: Any = None, run_index: int | None = None
) -> dict[str, Decision]:
    """采集一个 case 的生成器分与 judge 分。

    两个 LLM 阶段都调用**生产侧函数**（``peer_judge``），本脚本不含任何 prompt 字面量。
    ``record=True`` 调 LLM 并写缓存；否则读缓存（缺失则报错提示 ``--record``）。
    ``run_index`` 非空时额外写 ``stable_run<N>/``（稳定性重复采集互不覆盖）；第 0 次仍更新主缓存。
    """
    path = cache_path(case.symbol, run_index)
    if record:
        active = model if model is not None else _resolve_model()
        business = _resolve_business_description(case)
        pool = case_candidates_block(case)
        gen_map = infer_peer_scoring(business, pool, industry=case.industry, model=active)
        judge_map = judge_peer_candidates(business, pool, model=active)
        decisions = {c.code: Decision(code=c.code) for c in case.candidates}
        for code, payload in gen_map.items():
            row = decisions.get(code)
            if row is None:
                continue
            row.gen_overlap = payload.get("overlap")
            row.dims = payload.get("dims")
        for code, payload in judge_map.items():
            row = decisions.get(code)
            if row is None:
                continue
            row.judge_verdict = payload.get("verdict")
            row.judge_dims = payload.get("dims")
            row.reason = str(payload.get("reason") or "")
        primary = cache_path(case.symbol, None if run_index == 0 else run_index)
        write_cache(primary, case, decisions, model_component=SMALL_MODEL_COMPONENT, run_index=run_index)
        if run_index == 0:
            # 第 0 次重复即基线：主缓存也同步更新，使报告基线策略与被测重复采集同一份产物
            write_cache(path, case, decisions, model_component=SMALL_MODEL_COMPONENT, run_index=run_index)
        return decisions
    if not path.is_file():
        raise FileNotFoundError(f"缺缓存 {path}；请先 `--record` 采集，或将该 case 从标注集移除。")
    raw = json.loads(path.read_text(encoding="utf-8"))
    # v1 兼容：旧缓存是**裸数组**；v2 起为含血缘的 dict（``decisions`` 字段）。
    rows = raw if isinstance(raw, list) else (raw.get("decisions") or [])
    return {d["code"]: Decision.from_dict(d) for d in rows}


def _resolve_model() -> Any:
    """生产同组件模型（``agent.peer_group``）。"""
    llm = importlib.import_module("alphabee.utils.llm")
    return llm.create_chat_model(SMALL_MODEL_COMPONENT)


def _available_stability_runs() -> int:
    """已有 ``stable_run<N>/`` 缓存的最大重复次数（无 → 0）；用于纯复算时自动带出稳定性章节。"""
    indices = [int(path.name.removeprefix("stable_run")) for path in CACHE_DIR.glob("stable_run*") if path.is_dir()]
    return max(indices) + 1 if indices else 0


def cache_path(symbol: str, run_index: int | None) -> Path:
    if run_index is None:
        return CACHE_DIR / f"{symbol.upper()}.json"
    return CACHE_DIR / f"stable_run{run_index}" / f"{symbol.upper()}.json"


def write_cache(
    path: Path,
    case: EvalCase,
    decisions: dict[str, Decision],
    *,
    model_component: str,
    run_index: int | None,
) -> None:
    """写缓存（含血缘：case 分割/业态/模型组件/采集时刻）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "symbol": case.symbol,
        "split": case.split,
        "segment": case.segment,
        "run_index": run_index,
        "model_component": model_component,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "decisions": [d.to_dict() for d in decisions.values()],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 闸门（确定性）──────────────────────────────────────────────────
def _overlap(dims: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights[dim] * dims.get(dim, 0.0) for dim in weights)


def gate(
    decisions: dict[str, Decision],
    strategy: str,
    *,
    weights: dict[str, float] = WEIGHTS_DEFAULT,
    min_overlap: float = 0.5,
    product_floor: float = 0.30,
    customer_floor: float = 0.20,
) -> set[str]:
    """策略 → 保留代码集合（纯确定性；E 仅作提示，不硬剔）。"""
    kept: set[str] = set()
    for code, decision in decisions.items():
        if strategy == "B_single":
            if (decision.gen_overlap or 0.0) >= min_overlap:
                kept.add(code)
            continue
        if strategy in ("C_D_judge", "C_D_E"):
            dims = decision.judge_dims
            if dims is None or decision.judge_verdict is None:
                continue
            if decision.judge_verdict == "reject":
                continue
        else:  # C_dims / C_E_taxo
            dims = decision.dims
            if dims is None:
                continue
        if dims.get("product", 0.0) < product_floor:
            continue
        if dims.get("customer", 0.0) < customer_floor:
            continue
        if _overlap(dims, weights) < min_overlap:
            continue
        kept.add(code)
    return kept


# ── 指标 ────────────────────────────────────────────────────────────
def prf(kept: set[str], labels: dict[str, str]) -> tuple[float, float, float]:
    pos = {code for code, label in labels.items() if label == "keep"}
    tp = len(kept & pos)
    fp = len(kept - pos)
    fn = len(pos - kept)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def cases_for_split(cases: list[EvalCase], split: str) -> list[EvalCase]:
    """按 split 取 case 子集；``all`` 返回全体。"""
    if split == "all":
        return list(cases)
    return [case for case in cases if case.split == split]


def _metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "precision": statistics.mean([cast(float, row["p"]) for row in rows]) if rows else 0.0,
        "recall": statistics.mean([cast(float, row["r"]) for row in rows]) if rows else 0.0,
        "f1": statistics.mean([cast(float, row["f"]) for row in rows]) if rows else 0.0,
        "cases": float(len(rows)),
    }


def _visible_labels(case: EvalCase, *, exclude_disputed: bool) -> dict[str, str]:
    labels: dict[str, str] = {}
    for cand in case.candidates:
        if exclude_disputed and cand.disputed:
            continue
        labels[cand.code] = cand.label
    return labels


def evaluate_strategy(
    cases: list[EvalCase],
    decisions: dict[str, dict[str, Decision]],
    strategy: str,
    **gate_kwargs: Any,
) -> dict[str, Any]:
    """按 split 分段评估一个策略；每段同时给『排除 disputed』与『未排除 disputed』两套数字。"""
    out: dict[str, Any] = {"strategy": strategy, "segments": {}}
    for split in SPLITS:
        subset = cases_for_split(cases, split)
        rows: list[dict[str, Any]] = []
        rows_no_disputed: list[dict[str, Any]] = []
        rows_with_disputed: list[dict[str, Any]] = []
        for case in subset:
            kept = gate(decisions[case.symbol], strategy, **gate_kwargs)
            labels = _visible_labels(case, exclude_disputed=False)
            if case.expect_empty:
                # 期望空：保留即误报（precision=0）；recall 约定为 1.0（无正样本）
                p = 1.0 if not kept else 0.0
                r, f = 1.0, 0.0
            else:
                p, r, f = prf(kept, labels)
            row = {"case": case.symbol, "p": p, "r": r, "f": f, "kept": sorted(kept), "labels": labels}
            rows.append(row)
            # 排除 disputed：争议候选从保留集与标签中一并剔除（该候选不参与任何口径）
            labels_nd = _visible_labels(case, exclude_disputed=True)
            disputed_codes = {c.code for c in case.candidates if c.disputed}
            kept_nd = kept - disputed_codes
            if case.expect_empty and not disputed_codes:
                pd, rd, fd = (1.0, 1.0, 0.0) if not kept_nd else (0.0, 1.0, 0.0)
            elif not labels_nd:
                # 该 case 全部候选均为 disputed：排除后无候选，保留集必须为空才算一致
                pd, rd, fd = (1.0, 1.0, 0.0) if not kept_nd else (0.0, 1.0, 0.0)
            else:
                pd, rd, fd = prf(kept_nd, labels_nd)
            rows_no_disputed.append({**row, "p": pd, "r": rd, "f": fd, "kept": sorted(kept_nd), "labels": labels_nd})
            rows_with_disputed.append(row)
        out["segments"][split] = {
            "include_disputed": {**_metrics(rows_with_disputed), "per_case": rows_with_disputed},
            "exclude_disputed": {**_metrics(rows_no_disputed), "per_case": rows_no_disputed},
        }
    return out


def segment_metrics(result: dict[str, Any], split: str = "all", *, exclude_disputed: bool = False) -> dict[str, float]:
    key = "exclude_disputed" if exclude_disputed else "include_disputed"
    return cast(dict[str, float], result["segments"][split][key])


def discipline_metrics(
    results: dict[str, dict[str, Any]], *, exclude_disputed: bool = False
) -> dict[str, dict[str, float]]:
    """DoD 口径速览：``strategy → {split → {precision, recall, f1}}``。"""
    return {
        name: {split: segment_metrics(result, split, exclude_disputed=exclude_disputed) for split in SPLITS}
        for name, result in results.items()
    }


def failures(result: dict[str, Any], split: str = "all", *, exclude_disputed: bool = False) -> list[str]:
    """列出每 case 的 FP（保留但 drop）/ FN（剔除但 keep）。"""
    key = "exclude_disputed" if exclude_disputed else "include_disputed"
    out: list[str] = []
    for row in result["segments"][split][key]["per_case"]:
        labels = row["labels"]
        kept = set(row["kept"])
        fp = sorted(kept - {c for c, lab in labels.items() if lab == "keep"})
        fn = sorted({c for c, lab in labels.items() if lab == "keep"} - kept)
        if fp:
            out.append(f"{row['case']}: FP {fp}")
        if fn:
            out.append(f"{row['case']}: FN {fn}")
    return out


# ── 扫参 ────────────────────────────────────────────────────────────
def sweep(
    cases: list[EvalCase],
    decisions: dict[str, dict[str, Decision]],
    strategy: str = "C_D_E",
    *,
    calibrate_split: str = DEFAULT_SPLIT,
) -> dict[str, Any]:
    """在冻结 dims 上扫描 ``(min_overlap, product_floor, customer_floor)``，取校准段 F1 最大。

    设计 §5.2 / §8 决策 3：阈值必须以 **train 段**（校准段）标定，holdout 只用于验收，
    故默认只按 ``train`` 段的 f1 选点；holdout 段数字随选点一并输出（不改选点）。
    """
    best: dict[str, Any] | None = None
    for min_overlap, product_floor, customer_floor in itertools.product(
        GRID_OVERLAP, GRID_PRODUCT_FLOOR, GRID_CUSTOMER_FLOOR
    ):
        result = evaluate_strategy(
            cases,
            decisions,
            strategy,
            min_overlap=min_overlap,
            product_floor=product_floor,
            customer_floor=customer_floor,
        )
        score = segment_metrics(result, calibrate_split)["f1"]
        if best is None or score > best["score"]:
            best = {
                "result": result,
                "score": score,
                "params": {
                    "min_overlap": min_overlap,
                    "product_floor": product_floor,
                    "customer_floor": customer_floor,
                },
                "calibrate_split": calibrate_split,
            }
    return best or {}


# ── 稳定性 ──────────────────────────────────────────────────────────
def jaccard(left: set[str], right: set[str]) -> float:
    """两个保留集的 Jaccard；**双方皆空 → 1.0**（“都空”视为一致，不是缺失）。"""
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def mean_pairwise_jaccard(sets: list[set[str]]) -> float:
    """同一 case 多次采集保留集的**两两 Jaccard 均值**（n<2 → 1.0）。"""
    pairs = list(itertools.combinations(sets, 2))
    if not pairs:
        return 1.0
    return statistics.mean(jaccard(left, right) for left, right in pairs)


def stability(
    cases: list[EvalCase],
    strategies: tuple[str, ...],
    repeat: int,
    *,
    record: bool,
    model: Any = None,
) -> list[dict[str, Any]]:
    """重复采集 N 次，测 per-case 保留集 Jaccard（需 ``--record`` 才有意义）。

    集合定义：第 r 次采集的 LLM 产物经该策略闸门后的**保留代码集合**；聚合方式：同一 case +
    同一策略的 n 次保留集取**两两 Jaccard 均值**（n<2 → 1.0）。一次采集的结果被所有策略复用
    （同一次 LLM 产物分别过闸），以便横向比较“judge 是否降低抖动”。
    """
    if repeat <= 1:
        return []
    runs: dict[tuple[str, str], list[set[str]]] = {
        (case.symbol, strategy): [] for case in cases for strategy in strategies
    }
    for index in range(repeat):
        for case in cases:
            decisions = collect_decisions(case, record=record, model=model, run_index=index)
            for strategy in strategies:
                runs[(case.symbol, strategy)].append(gate(decisions, strategy))
    out: list[dict[str, Any]] = []
    for case in cases:
        row: dict[str, Any] = {"case": case.symbol, "split": case.split, "segment": case.segment, "results": {}}
        for strategy in strategies:
            row["results"][strategy] = mean_pairwise_jaccard(runs[(case.symbol, strategy)])
        out.append(row)
    return out


def stability_by_split(rows: list[dict[str, Any]], strategy: str) -> dict[str, float]:
    """按 split（含 all）聚合某策略的稳定性 Jaccard 均值（行含 ``results[strategy]``）。"""
    return _stability_split_mean(
        [(str(row["split"]), cast(float, row["results"][strategy])) for row in rows if strategy in row["results"]]
    )


def _stability_split_mean(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """``[(split, jaccard)] → {train/holdout/all: 均值}``（纯函数，便于判别力变异实验）。"""
    out: dict[str, float] = {}
    for split in SPLITS:
        values = [value for row_split, value in pairs if split == "all" or row_split == split]
        out[split] = statistics.mean(values) if values else 0.0
    return out


# ── 报告 ────────────────────────────────────────────────────────────
def _split_counts(cases: list[EvalCase]) -> dict[str, int]:
    return {split: len(cases_for_split(cases, split)) for split in SPLITS}


def _segment_counts(cases: list[EvalCase]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.segment or "未标注"] = counts.get(case.segment or "未标注", 0) + 1
    return counts


def _metric_table(cases: list[EvalCase], results: dict[str, dict[str, Any]], *, exclude_disputed: bool) -> list[str]:
    lines: list[str] = []
    kind = "排除 disputed" if exclude_disputed else "未排除 disputed"
    for split in SPLITS:
        lines.append(f"### split={split}（{kind}；{len(cases_for_split(cases, split))} 例）")
        lines.append("")
        lines.append("| strategy | precision | recall | f1 |")
        lines.append("|---|---|---|---|")
        for name, result in results.items():
            row = segment_metrics(result, split, exclude_disputed=exclude_disputed)
            lines.append(f"| {name} | {row['precision']:.3f} | {row['recall']:.3f} | {row['f1']:.3f} |")
        lines.append("")
    return lines


def render_report(
    cases: list[EvalCase],
    results: dict[str, dict[str, Any]],
    taxonomy: dict[str, dict[str, str]],
    sweep_best: dict[str, Any] | None,
    stability_rows: list[dict[str, Any]],
    *,
    stability_strategies: tuple[str, ...] = ("C_D_E",),
    repeat: int = 0,
) -> str:
    lines = ["# 对标组判定策略回归", ""]
    lines.append(f"- cases: {len(cases)}（split 分布：{_split_counts(cases)}）")
    lines.append(f"- 业态分布：{_segment_counts(cases)}")
    lines.append(
        "- 指标口径：按 split 分段（train 用于标定 / holdout 仅验收 / all 全体），每段两套（排除·未排除 disputed）"
    )
    lines.append(
        "- 生成器打分与 judge 两阶段均调用生产侧入口 `alphabee.company_track.peer_judge`，脚本内无 prompt 字面量"
    )
    lines.append("")

    lines.append("## 策略汇总（gate 后）")
    lines.append("")
    lines.extend(_metric_table(cases, results, exclude_disputed=False))
    lines.append("## 策略汇总（gate 后，排除 disputed 口径）")
    lines.append("")
    lines.extend(_metric_table(cases, results, exclude_disputed=True))

    lines.append("## 分类学召回（E）")
    lines.append("")
    lines.append("| case | split | 业态 | same_l3 recall | same_l2 recall |")
    lines.append("|---|---|---|---|---|")
    for case in cases:
        lines.append(
            f"| {case.symbol} {case.name} | {case.split} | {case.segment or '—'} "
            f"| {taxonomy_recall(case, 'l3'):.2f} | {taxonomy_recall(case, 'l2'):.2f} |"
        )
    lines.append("")

    lines.append("## 逐 case（按策略保留集）")
    lines.append("")
    for name, result in results.items():
        lines.append(f"### {name}")
        for split in SPLITS:
            row = segment_metrics(result, split)
            lines.append(f"- [{split}] P={row['precision']:.3f} R={row['recall']:.3f} F1={row['f1']:.3f}")
        for split in ("train", "holdout"):
            for entry in result["segments"][split]["include_disputed"]["per_case"]:
                lines.append(
                    f"  - {entry['case']} ({split}): P={entry['p']:.2f} R={entry['r']:.2f} F1={entry['f']:.2f} "
                    f"kept={entry['kept']}"
                )
        fails = failures(result, "all")
        if fails:
            lines.append("  - 失败（all，含 disputed）: " + "；".join(fails))
        fails_nd = failures(result, "all", exclude_disputed=True)
        if fails_nd:
            lines.append("  - 失败（all，排除 disputed）: " + "；".join(fails_nd))
        lines.append("")

    if sweep_best:
        lines.append("## 扫参推荐")
        lines.append("")
        lines.append(f"- 校准段: {sweep_best['calibrate_split']}（阈值只能看校准段，holdout 仅验收）")
        lines.append(f"- params: {sweep_best['params']}")
        lines.append(f"- {sweep_best['calibrate_split']} f1: {sweep_best['score']:.3f}")
        for split in SPLITS:
            row = segment_metrics(sweep_best["result"], split)
            lines.append(f"- {split}（该选点）: P={row['precision']:.3f} R={row['recall']:.3f} F1={row['f1']:.3f}")
        lines.append("")

    if stability_rows:
        lines.append("## 稳定性（保留集 Jaccard）")
        lines.append("")
        lines.append(
            "- 集合定义：第 r 次采集的 LLM 产物经闸门后的**保留代码集合**；"
            "聚合：同一 case + 同一策略的 n 次保留集取**两两 Jaccard 均值**（双方皆空 → 1.0）"
        )
        lines.append(f"- 策略: {', '.join(stability_strategies)}")
        lines.append(f"- 重复采集次数: {repeat}（读数来自 data/peer_eval_cache/stable_run*/）")
        lines.append("")
        for strategy in stability_strategies:
            by_split = stability_by_split(stability_rows, strategy)
            lines.append(
                f"- `{strategy}` 均值：train={by_split['train']:.3f} holdout={by_split['holdout']:.3f} "
                f"all={by_split['all']:.3f}"
            )
        if len(stability_strategies) >= 2:
            baseline, candidate = stability_strategies[0], stability_strategies[-1]
            base_split = stability_by_split(stability_rows, baseline)
            cand_split = stability_by_split(stability_rows, candidate)
            lines.append("")
            lines.append(f"- 对照：`{candidate}` vs `{baseline}`（差值为正表示前者更稳）")
            for split in SPLITS:
                delta = cand_split[split] - base_split[split]
                verdict = "更稳" if delta > 1e-9 else ("更抖" if delta < -1e-9 else "持平")
                lines.append(
                    f"  - {split}: {cand_split[split]:.3f} - {base_split[split]:.3f} = {delta:+.3f} ⇒ {verdict}"
                )
            worse = [split for split in SPLITS if cand_split[split] < base_split[split] - 1e-9]
            if worse:
                lines.append(
                    f"- **负结果**：`{candidate}` 在 {'/'.join(worse)} 段**比对照更抖**，"
                    "即 judge 未在该段降低抖动（如实登记，不美化）。"
                )
            else:
                lines.append(f"- `{candidate}` 在全部段均不劣于对照（本批数据下 judge 未增加抖动）。")
        lines.append("")
        header = "| case | split | 业态 | " + " | ".join(f"{s} Jaccard" for s in stability_strategies) + " |"
        lines.append(header)
        lines.append("|" + "---|" * (3 + len(stability_strategies)))
        for row in stability_rows:
            cells = " | ".join(f"{row['results'].get(s, 1.0):.3f}" for s in stability_strategies)
            lines.append(f"| {row['case']} | {row['split']} | {row['segment'] or '—'} | {cells} |")
        lines.append("")
        lines.append("> 抖动判定：Jaccard < 1 即该 case 的保留集在重复采集间发生变化（有抖动）；")
        lines.append("> 若 judge 策略并未比对照策略更稳定，则如实登记为负结果（不美化、不只报有利方向）。")
        lines.append("")

    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="对标组判定策略回归（设计 §6 Step 0）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--record", action="store_true", help="调 LLM 并写缓存（否则读缓存）")
    parser.add_argument("--repeat", type=int, default=1, help="重复采集次数（稳定性，需 --record）")
    parser.add_argument(
        "--stability-only",
        action="store_true",
        help="只跑稳定性重复采集（复用已有主缓存，不重采主样本）",
    )
    parser.add_argument(
        "--reuse-stability",
        action="store_true",
        help="稳定性读已有 stable_run<N>/ 缓存（不重采 LLM），用于纯复算报告",
    )
    parser.add_argument("--sweep", action="store_true", help="扫描 min_overlap/floors（按 train 段选点）")
    parser.add_argument(
        "--calibrate-split", default=DEFAULT_SPLIT, choices=list(SPLITS), help="扫参校准段（默认 train）"
    )
    parser.add_argument("--strategies", nargs="*", default=list(STRATEGIES))
    parser.add_argument(
        "--stability-strategies",
        nargs="*",
        default=["B_single", "C_D_E"],
        help="稳定性对照策略（默认 B_single 为对照、C_D_E 为 judge 策略）",
    )
    parser.add_argument("--out", default=str(REPORT_PATH))
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    taxonomy = _stock_taxonomy()
    for case in cases:
        _attach_taxonomy(case, taxonomy)

    model = _resolve_model() if args.record else None
    repeat = args.repeat
    if repeat <= 1 and not args.stability_only:
        # 报告完整性：已有稳定重复缓存时自动按缓存复算（不调 LLM），避免 `--sweep` 丢掉稳定性章节
        repeat = _available_stability_runs()
    if args.stability_only and repeat > 1:
        # 稳定性专用：主样本读缓存（不重采），只补 `stable_run<N>/` 重复采集
        decisions = {case.symbol: collect_decisions(case, record=False) for case in cases}
    else:
        decisions = {case.symbol: collect_decisions(case, record=args.record, model=model) for case in cases}

    results = {name: evaluate_strategy(cases, decisions, name) for name in args.strategies}
    sweep_best = sweep(cases, decisions, calibrate_split=args.calibrate_split) if args.sweep else None
    stability_rows = (
        stability(
            cases,
            tuple(args.stability_strategies),
            repeat,
            record=args.record and not args.reuse_stability,
            model=model,
        )
        if repeat > 1
        else []
    )

    report = render_report(
        cases,
        results,
        taxonomy,
        sweep_best,
        stability_rows,
        stability_strategies=tuple(args.stability_strategies),
        repeat=repeat,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    for name, result in results.items():
        row = segment_metrics(result, "all")
        hold = segment_metrics(result, "holdout")
        print(
            f"[{name}] all: precision={row['precision']:.3f} recall={row['recall']:.3f} f1={row['f1']:.3f}"
            f" | holdout: f1={hold['f1']:.3f}"
        )
    if sweep_best:
        row = segment_metrics(sweep_best["result"], "holdout")
        print(
            f"[sweep] best={sweep_best['params']} {args.calibrate_split}_f1={sweep_best['score']:.3f} "
            f"holdout_f1={row['f1']:.3f}"
        )
    for strategy in args.stability_strategies:
        by_split = stability_by_split(stability_rows, strategy)
        if stability_rows:
            print(
                f"[stability:{strategy}] train={by_split['train']:.3f} "
                f"holdout={by_split['holdout']:.3f} all={by_split['all']:.3f}"
            )
    print(f"report → {out_path}")


if __name__ == "__main__":
    main()
    sys.exit(0)
