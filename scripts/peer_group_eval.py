"""对标组候选判定策略回归（REPORT_QUALITY_FIX_ROADMAP §11 P2-① 的 §8 标定脚本）。

目的：在**人工标注集**上横向比较几种"候选判定"策略的 precision/recall，并做**离线权重/阈值扫描**
（LLM 维度冻结后纯计算，不重复调 LLM），为生产实现（C 结构化维度 / D 独立 judge / E 分类学特征）
提供参数依据。仅评测，不接入生产链路。

三种策略来源（C/D/E 的定义见与 captain 的设计讨论）：
- **B_single**：生成器自评 `overlap` 阈值（现状口径）；
- **C_dims**：同一生成调用的**结构化维度**（product/customer/material_tech/business_model）合成后闸门；
- **C_D_judge**：**独立 judge 调用**给 verdict+dims 后闸门；
- **C_E_taxo**：C + 分类学 `same_l3/same_l2` 特征（仅作提示，不硬剔）；
- **C_D_E**：D + E。

评测两段指标：
1. `taxonomy_recall`：真实对标里有多少落在标的的 same L3 / same L2 成分内（评 E 的召回价值，离线可算）；
2. `gate P/R/F1`：闸门后保留集相对 ground truth（评 C/D 的精度价值）。

可复现性：LLM 产物落 `data/peer_eval_cache/<symbol>.json`（gitignored），默认 `--replay` 读缓存；
`--record` 才调 LLM。**权重/阈值扫描永远不调 LLM**。

用法：
    # 首采：调 LLM 并写缓存
    poetry run python scripts/peer_group_eval.py --record
    # 复算/CI：读缓存，跑全部策略 + 扫参
    poetry run python scripts/peer_group_eval.py --sweep
    # 稳定性：重复采集 N 次（需 --record）
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from alphabee import PROJECT_ROOT

# ── 常量 ────────────────────────────────────────────────────────────
DEFAULT_CASES = PROJECT_ROOT / "tests" / "fixtures" / "peer_group_eval" / "labels.yaml"
CACHE_DIR = PROJECT_ROOT / "data" / "peer_eval_cache"
REPORT_PATH = PROJECT_ROOT / "outputs" / "peer_group_eval.md"
_ALL_STOCKS = PROJECT_ROOT / "alphabee" / "static" / "all_stocks.csv"

SMALL_MODEL_COMPONENT = "agent.peer_group"  # 与生产同组件

STRATEGIES = ("B_single", "C_dims", "C_D_judge", "C_E_taxo", "C_D_E")
WEIGHTS_DEFAULT: dict[str, float] = {"product": 0.40, "customer": 0.30, "business_model": 0.20, "material_tech": 0.10}
DIMS = ("product", "customer", "material_tech", "business_model")

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


@dataclass
class EvalCase:
    symbol: str
    name: str = ""
    industry: str = ""
    taxonomy_l3: str = ""
    taxonomy_l2: str = ""
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
            Candidate(**{k: c.get(k, "") for k in ("code", "name", "label", "rationale")})
            for c in item.get("candidates", [])
        ]
        cases.append(
            EvalCase(
                symbol=str(item["symbol"]),
                name=str(item.get("name") or ""),
                industry=str(item.get("industry") or ""),
                taxonomy_l3=str(item.get("taxonomy_l3") or ""),
                taxonomy_l2=str(item.get("taxonomy_l2") or ""),
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


# ── LLM 采集 / 缓存 ─────────────────────────────────────────────────
def _candidates_block(case: EvalCase) -> str:
    lines = []
    for cand in case.candidates:
        tags = []
        if cand.same_l3 is not None:
            tags.append(f"same_l3={cand.same_l3}")
        if cand.same_l2 is not None:
            tags.append(f"same_l2={cand.same_l2}")
        suffix = f"（{'，'.join(tags)}）" if tags else ""
        lines.append(f"- {cand.code} {cand.name}{suffix}")
    return "\n".join(lines)


def _generator_prompt(case: EvalCase, business_description: str) -> str:
    """单次生成调用：对**给定候选**逐条给 overall overlap + 结构化维度（B 与 C 共用）。"""
    return (
        "你是买方研究员。给定标的主要业务与候选公司清单，对**每个候选**判断其与标的在"
        "以下维度的重叠度（0–1）：product(产品/服务)、customer(客户/终端应用)、"
        "material_tech(材料/技术路线)、business_model(盈利模式/业态)，并给出 overall overlap（0–1）。\n"
        "候选清单已给定，不要新增或改动代码。\n"
        "只输出 JSON 数组（无候选则 []），每条："
        '{"code": "...", "overlap": 0.0-1.0, "dims": {"product":0,"customer":0,"material_tech":0,"business_model":0}, "reason": "一句理由"}。\n'
        f"行业: {case.industry or case.taxonomy_l3 or '未标注'}\n"
        f"候选清单:\n{_candidates_block(case)}\n\n"
        f"标的主要业务描述:\n{business_description[:8000]}"
    )


def _judge_prompt(case: EvalCase, business_description: str) -> str:
    """独立 judge 调用：给 verdict + 结构化维度（D）。E 的 same_* 作为特征注入。"""
    return (
        "你是**独立评审**（与生成者无关）。给定标的主要业务、候选公司清单及其与标的的申万分类关系，"
        "判断每个候选是否为**同产业链环节的直接对标**：\n"
        '- verdict: "direct"（同环节、可互为替代）| "adjacent"（相邻/部分重叠）| "reject"（不同环节或材料/终端实质不同）；\n'
        "- dims: product/customer/material_tech/business_model（0–1）；\n"
        "- reason: 先列「匹配点 / 不匹配点」，再据此给 verdict 与 dims。\n"
        "规则：同 L3 是「同环节」的强证据，但**不能替代业务判断**——L3 不同但产品/客户高度重叠仍应 direct；"
        "同 L3 但材料/终端/盈利模式不同应 reject。\n"
        "只输出 JSON 数组，每条："
        '{"code": "...", "verdict": "direct|adjacent|reject", "dims": {"product":0,"customer":0,"material_tech":0,"business_model":0}, "reason": "..."}。\n'
        f"候选清单（含申万分类关系）:\n{_candidates_block(case)}\n\n"
        f"标的主要业务描述:\n{business_description[:8000]}"
    )


def _coerce_dims(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for dim in DIMS:
        value = raw.get(dim)
        if value is None:
            out[dim] = 0.0
            continue
        try:
            out[dim] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            out[dim] = 0.0
    return out


def _coerce_overlap(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def _call_llm(case: EvalCase) -> dict[str, Decision]:
    """真调 LLM 两次（生成器 + 独立 judge），合成 Decision（按 code）。"""
    llm = importlib.import_module("alphabee.utils.llm")
    parse_json = importlib.import_module("alphabee.utils.pipeline").parse_json
    model = llm.create_chat_model(SMALL_MODEL_COMPONENT)
    business = _resolve_business_description(case)

    decisions: dict[str, Decision] = {c.code: Decision(code=c.code) for c in case.candidates}

    for name, prompt, kind in (
        ("gen", _generator_prompt(case, business), "gen"),
        ("judge", _judge_prompt(case, business), "judge"),
    ):
        parsed = parse_json(str(model.invoke(prompt).content))
        if not isinstance(parsed, list):
            continue
        for item in parsed:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().upper()
            decision = decisions.get(code)
            if decision is None:
                continue
            if kind == "gen":
                decision.gen_overlap = _coerce_overlap(item.get("overlap"))
                decision.dims = _coerce_dims(item.get("dims"))
            else:
                decision.judge_verdict = str(item.get("verdict") or "").strip().lower() or None
                decision.judge_dims = _coerce_dims(item.get("dims"))
                decision.judge_overlap = _coerce_overlap(item.get("overlap"))
                decision.reason = str(item.get("reason") or "").strip()
    return decisions


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}.json"


def collect_decisions(case: EvalCase, *, record: bool) -> dict[str, Decision]:
    """record=True 调 LLM 并写缓存；否则读缓存（缺失则报错提示 --record）。"""
    path = _cache_path(case.symbol)
    if record:
        decisions = _call_llm(case)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([d.to_dict() for d in decisions.values()], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return decisions
    if not path.is_file():
        raise FileNotFoundError(f"缺缓存 {path}；请先 `--record` 采集，或将该 case 从标注集移除。")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {d["code"]: Decision.from_dict(d) for d in raw}


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


def evaluate_strategy(
    cases: list[EvalCase],
    decisions: dict[str, dict[str, Decision]],
    strategy: str,
    **gate_kwargs: Any,
) -> dict[str, Any]:
    rows = []
    for case in cases:
        kept = gate(decisions[case.symbol], strategy, **gate_kwargs)
        labels = {c.code: c.label for c in case.candidates}
        if case.expect_empty:
            # 期望空：保留即误报
            precision = 1.0 if not kept else 0.0
            rows.append(
                {"case": case.symbol, "p": precision, "r": 1.0, "f": 0.0, "kept": sorted(kept), "labels": labels}
            )
            continue
        p, r, f = prf(kept, labels)
        rows.append({"case": case.symbol, "p": p, "r": r, "f": f, "kept": sorted(kept), "labels": labels})
    return {
        "strategy": strategy,
        "precision": statistics.mean([cast(float, row["p"]) for row in rows]),
        "recall": statistics.mean([cast(float, row["r"]) for row in rows]),
        "f1": statistics.mean([cast(float, row["f"]) for row in rows]),
        "per_case": rows,
    }


def failures(result: dict[str, Any]) -> list[str]:
    """列出每 case 的 FP（保留但 drop）/ FN（剔除但 keep）。"""
    out: list[str] = []
    for row in result["per_case"]:
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
def sweep(cases: list[EvalCase], decisions: dict[str, dict[str, Decision]], strategy: str = "C_D_E") -> dict[str, Any]:
    """在冻结 dims 上扫描 (min_overlap, product_floor, customer_floor)，取 F1 最大。"""
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
        if best is None or result["f1"] > best["result"]["f1"]:
            best = {
                "result": result,
                "params": {
                    "min_overlap": min_overlap,
                    "product_floor": product_floor,
                    "customer_floor": customer_floor,
                },
            }
    return best or {}


# ── 稳定性 ──────────────────────────────────────────────────────────
def stability(cases: list[EvalCase], strategy: str, repeat: int, *, record: bool) -> list[dict[str, Any]]:
    """重复采集 N 次，测 per-case 保留集的 Jaccard（需 --record 才有意义）。"""
    if repeat <= 1:
        return []
    runs: dict[str, list[set[str]]] = {case.symbol: [] for case in cases}
    for _ in range(repeat):
        for case in cases:
            decisions = collect_decisions(case, record=record)
            runs[case.symbol].append(gate(decisions, strategy))
    out = []
    for case in cases:
        sets = runs[case.symbol]
        jaccards = []
        for left, right in itertools.combinations(sets, 2):
            union = left | right
            jaccards.append(len(left & right) / len(union) if union else 1.0)
        out.append({"case": case.symbol, "jaccard": statistics.mean(jaccards) if jaccards else 1.0})
    return out


# ── 报告 ────────────────────────────────────────────────────────────
def render_report(
    cases: list[EvalCase],
    results: dict[str, dict[str, Any]],
    taxonomy: dict[str, dict[str, str]],
    sweep_best: dict[str, Any] | None,
    stability_rows: list[dict[str, Any]],
) -> str:
    lines = ["# 对标组判定策略回归", "", f"- cases: {len(cases)}", ""]

    lines.append("## 策略汇总（gate 后）")
    lines.append("")
    lines.append("| strategy | precision | recall | f1 |")
    lines.append("|---|---|---|---|")
    for name, result in results.items():
        lines.append(f"| {name} | {result['precision']:.3f} | {result['recall']:.3f} | {result['f1']:.3f} |")
    lines.append("")

    lines.append("## 分类学召回（E）")
    lines.append("")
    lines.append("| case | same_l3 recall | same_l2 recall |")
    lines.append("|---|---|---|")
    for case in cases:
        lines.append(
            f"| {case.symbol} {case.name} | {taxonomy_recall(case, 'l3'):.2f} | {taxonomy_recall(case, 'l2'):.2f} |"
        )
    lines.append("")

    lines.append("## 逐 case（按策略保留集）")
    lines.append("")
    for name, result in results.items():
        lines.append(f"### {name}")
        for row in result["per_case"]:
            lines.append(f"- {row['case']}: P={row['p']:.2f} R={row['r']:.2f} F1={row['f']:.2f} kept={row['kept']}")
        fails = failures(result)
        if fails:
            lines.append("  - 失败: " + "；".join(fails))
        lines.append("")

    if sweep_best:
        lines.append("## 扫参推荐（C_D_E）")
        lines.append("")
        lines.append(f"- params: {sweep_best['params']}")
        lines.append(f"- f1: {sweep_best['result']['f1']:.3f}")
        lines.append("")

    if stability_rows:
        lines.append("## 稳定性（保留集 Jaccard 均值）")
        lines.append("")
        for row in stability_rows:
            lines.append(f"- {row['case']}: {row['jaccard']:.2f}")
        lines.append("")

    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="对标组判定策略回归（§8）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--record", action="store_true", help="调 LLM 并写缓存（否则读缓存）")
    parser.add_argument("--repeat", type=int, default=1, help="重复采集次数（稳定性，需 --record）")
    parser.add_argument("--sweep", action="store_true", help="扫描 min_overlap/floors")
    parser.add_argument("--strategies", nargs="*", default=list(STRATEGIES))
    parser.add_argument("--out", default=str(REPORT_PATH))
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    taxonomy = _stock_taxonomy()
    for case in cases:
        _attach_taxonomy(case, taxonomy)

    decisions = {case.symbol: collect_decisions(case, record=args.record) for case in cases}

    results = {name: evaluate_strategy(cases, decisions, name) for name in args.strategies}
    sweep_best = sweep(cases, decisions) if args.sweep else None
    stability_rows = stability(cases, "C_D_E", args.repeat, record=args.record) if args.repeat > 1 else []

    report = render_report(cases, results, taxonomy, sweep_best, stability_rows)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    for name, result in results.items():
        print(f"[{name}] precision={result['precision']:.3f} recall={result['recall']:.3f} f1={result['f1']:.3f}")
    if sweep_best:
        print(f"[sweep] best={sweep_best['params']} f1={sweep_best['result']['f1']:.3f}")
    print(f"report → {out_path}")


if __name__ == "__main__":
    main()
