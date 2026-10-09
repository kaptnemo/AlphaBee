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
- `expect_empty: true` 的 case 视为“期望空组”：保留任何候选即 FP（precision=0）；
- 报告数字均为**逐 case 指标的算术平均**，渲染时保留 **3 位小数**（原始值不做舍入后再参与比较）。

**prompt 无字面量**：本脚本的生成器打分与 judge 两个阶段**一律调用生产侧入口**
（``alphabee.company_track.peer_judge`` 的 ``infer_peer_scoring`` / ``judge_peer_candidates_batched``），
prompt 构造与解析逻辑全部在 ``peer_judge`` 内，本文件不再复制任何 prompt 文本（设计 §5.2 消除双维护）。

**阈值权威（RC-QUALITY-DEFAULTS 收口）**：``gate()`` 的签名默认值**引用生产常量**
（``peer_judge.DEFAULT_MIN_OVERLAP`` / ``DEFAULT_PRODUCT_FLOOR`` / ``DEFAULT_CUSTOMER_FLOOR`` /
``DEFAULT_WEIGHTS``）——不存在"harness 默认"这第二份权威；扫参只是**显式传参**覆盖这些默认，
生产生效阈值 = ``company_track.peer_quality`` 配置（缺省即上述生产常量）。

**verdict 口径（RC-VERDICT-DIVERGENCE 收口）**：``gate()`` 对**任何**策略都执行规则①
「``verdict == "reject"`` 先否决」——与生产 ``gate_candidates`` 同源同序。C 类策略（``B_single`` /
``C_dims`` / ``C_E_taxo``）的 verdict 由 :func:`project_decisions` **显式清空**，因为"C"策略的定义
就是"没有 judge"（等价于生产 ``judge_enabled=false``），不是"忽略 verdict"。

**judge 降级口径（与生产 fail-open 同源）**：某 case 的候选池**未逐只取得可用判定**（verdict 非法
或缺 dims）时，该 case 的 D 类策略按生产口径**回退生成器 dims/overlap**（判定 C 口径）并在报告
登记 ``judge_degraded`` 案例数——**不允许**"漏判即静默按未判定剔除"这种与生产不一致的处置。

**证据口径（F-6）**：``<cache-root>/**`` 是**采集时血缘**（``split`` / ``segment`` 为采集当时的
标注集快照），**权威一律取 ``tests/fixtures/peer_group_eval/labels.yaml``**；本脚本**只读不写**缓存，
也不改写任何历史缓存字节，报告按 :func:`cache_lineage_audit` 现算并列出「缓存血缘与标注集不一致」
的文件及其 ``sha256``（历史遗留：``002318.SZ`` 的 split，属于 Step 0 世代）。缓存本身
**不是不可变证据**——报告与 :func:`archive_run` 的 manifest 才是可复算留档（F-5）。

**缓存世代**：``CACHE_DIR`` = 现行世代 ``data/peer_eval_cache/gen2``（判定 D 接通后由
``judge_peer_candidates_batched`` 的生产 prompt 采集，评测默认根）；``LEGACY_CACHE_DIR`` =
``data/peer_eval_cache``（Step 0 的旧 judge prompt 采集，**只读血缘证据**，报告一并列出其审计结果）。

可复现性：LLM 产物落 ``<cache-root>/<symbol>.json``（gitignored；重复采集落 ``stable_run<N>/``），
默认读缓存；``--record`` 才调 LLM；``--cache-root`` 可切换**缓存世代**（新 prompt 世代重采时用新目录，
历史世代字节保持原样）。
**权重/阈值扫描永远不调 LLM**。

用法：
    # 首采：调 LLM 并写缓存（需 LLM_API_KEY，脚本内 load_dotenv 兜底读取 .env）
    poetry run python scripts/peer_group_eval.py --record
    # 复算/CI：读缓存，跑全部策略 + 扫参（无网络/无 key 可跑）
    poetry run python scripts/peer_group_eval.py --sweep
    # 稳定性：重复采集 N 次（需 --record），产出保留集 Jaccard
    poetry run python scripts/peer_group_eval.py --record --repeat 3
    # 新 prompt 世代（不触碰历史缓存字节）
    poetry run python scripts/peer_group_eval.py --record --repeat 3 --sweep \
        --cache-root data/peer_eval_cache/gen_judge_v2
输出：outputs/peer_group_eval.md（控制台亦打印汇总）+ outputs/peer_group_eval/baseline_run/<ts>/
（报告 + manifest 版本化留档，F-5）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import statistics
import subprocess
import sys
from dataclasses import dataclass, field, replace
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


from alphabee.company_track.peer_judge import (  # noqa: E402
    DEFAULT_CUSTOMER_FLOOR,  # noqa: E402
    DEFAULT_MIN_OVERLAP,  # noqa: E402
    DEFAULT_PRODUCT_FLOOR,  # noqa: E402
    JUDGE_BATCH_SIZE_DEFAULT,  # noqa: E402
    MODEL_COMPONENT,  # noqa: E402
    VALID_VERDICTS,  # noqa: E402
    infer_peer_scoring,  # noqa: E402
    judge_peer_candidates_batched,  # noqa: E402
)
from alphabee.company_track.peer_judge import (  # noqa: E402
    DEFAULT_WEIGHTS as PRODUCTION_WEIGHTS,
)
from alphabee.company_track.peer_judge import (  # noqa: E402
    DIMS as PRODUCTION_DIMS,
)

# ── 常量 ────────────────────────────────────────────────────────────
DEFAULT_CASES = PROJECT_ROOT / "tests" / "fixtures" / "peer_group_eval" / "labels.yaml"
#: **现行缓存世代**（默认评测根）：判定 D 接通后由 ``judge_peer_candidates_batched`` 的生产 prompt 采集。
CACHE_DIR = PROJECT_ROOT / "data" / "peer_eval_cache" / "gen2"
#: **历史缓存世代**（只读血缘证据）：Step 0 的旧 judge prompt 采集，`F-6` 明令**不得改写其字节**。
LEGACY_CACHE_DIR = PROJECT_ROOT / "data" / "peer_eval_cache"
REPORT_PATH = PROJECT_ROOT / "outputs" / "peer_group_eval.md"
ARCHIVE_DIR = PROJECT_ROOT / "outputs" / "peer_group_eval" / "baseline_run"

#: 与生产同组件（设计 §8 决策 1）。
SMALL_MODEL_COMPONENT = MODEL_COMPONENT

#: 缓存结构版本（新增**破坏性**字段时递增；``judge`` 血缘块与 ``recorded_at`` 等为 append 型字段，
#: 缺省按 v1/v2 兼容读——旧缓存（裸数组 / 无 judge 块）仍可离线复算）。
CACHE_SCHEMA_VERSION = 2

STRATEGIES = ("B_single", "C_dims", "C_D_judge", "C_E_taxo", "C_D_E")
#: 含 judge（判定 D）的策略——其余策略的 verdict 由 :func:`project_decisions` 显式清空。
D_STRATEGIES = ("C_D_judge", "C_D_E")
#: 结构化维度定义与生产侧保持一致（单一来源）。
DIMS = PRODUCTION_DIMS
#: 权重默认 = 生产权重（RC-QUALITY-DEFAULTS：harness 不再持有第二份权威）。
WEIGHTS_DEFAULT: dict[str, float] = dict(PRODUCTION_WEIGHTS)

SPLITS = ("train", "holdout", "all")
DEFAULT_SPLIT = "train"

# 扫参网格
GRID_OVERLAP = (0.40, 0.45, 0.50, 0.55, 0.60)
GRID_PRODUCT_FLOOR = (0.20, 0.30, 0.40)
GRID_CUSTOMER_FLOOR = (0.10, 0.20, 0.30)

#: **F-2 舍入口径声明**（t16 未达项）：稳定性差值由**未舍入**均值计算，
#: 故读者按印出的三位舍入均值相减可能相差 0.001。报告两处差值呈现共用本常量（单一来源）。
ROUNDING_SCOPE_NOTE = "- **舍入口径**：稳定性差值由**未舍入**均值计算，故可能与上方三位舍入均值之差**相差 0.001**"

#: DoD（设计 §6 Step 2）复核的两个阈值点：
#: 「标定点」由 train 段扫参给出；「占位默认」= RC-DOD-SENS 要求复核的 0.50/0.30/0.20。
PLACEHOLDER_DEFAULTS: dict[str, float] = {"min_overlap": 0.50, "product_floor": 0.30, "customer_floor": 0.20}

#: judge 采集与在线生产同批大小（生产默认 = ``peer_judge.JUDGE_BATCH_SIZE_DEFAULT``）。
JUDGE_BATCH_SIZE = JUDGE_BATCH_SIZE_DEFAULT


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


# ── 分类学（E 特征）——**单一来源**：复用生产函数，harness 不再自带一份 CSV 读取 ──────
def _stock_taxonomy() -> dict[str, dict[str, str]]:
    """``stock_code → {l2_code,l2_name,l3_code,l3_name}``（读失败 → 空表）。

    实现 = ``alphabee.company_track.peer_taxonomy.stock_taxonomy()``（生产**唯一**快照读取处）；
    本函数只做形状适配，避免 harness 与生产各读一份 CSV 而口径漂移。
    """
    from alphabee.company_track.peer_taxonomy import stock_taxonomy

    return {
        code: {
            "l2_code": entry.l2_code,
            "l2_name": entry.l2_name,
            "l3_code": entry.l3_code,
            "l3_name": entry.l3_name,
        }
        for code, entry in stock_taxonomy().items()
    }


def _attach_taxonomy(case: EvalCase, taxonomy: dict[str, dict[str, str]]) -> None:
    """给每个候选标 ``same_l3/same_l2``（**生产函数** ``peer_taxonomy.same_levels``，未知 ⇒ None）。"""
    from alphabee.company_track.peer_taxonomy import same_levels

    target = taxonomy.get(case.symbol.upper(), {})
    if not case.taxonomy_l3:
        case.taxonomy_l3 = target.get("l3_code", "")
    if not case.taxonomy_l2:
        case.taxonomy_l2 = target.get("l2_code", "")
    pairs = same_levels(case.symbol, [cand.code for cand in case.candidates])
    for cand in case.candidates:
        pair = pairs.get(cand.code.upper())
        if pair is None:
            cand.same_l3, cand.same_l2 = None, None
        else:
            cand.same_l3, cand.same_l2 = pair


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

    两个 LLM 阶段都调用**生产侧函数**（``peer_judge``）：生成器 = ``infer_peer_scoring``，
    judge = ``judge_peer_candidates_batched``（**生产入口**：批量切分 + 闭集校验 + 降级记账），
    本脚本不含任何 prompt 字面量。``record=True`` 调 LLM 并写缓存；否则读缓存
    （缺失/损坏一律**报错**并提示 ``--record``，不静默降级——F-1 的 fail-loud 要求）。
    ``run_index`` 非空时额外写 ``stable_run<N>/``（稳定性重复采集互不覆盖）；第 0 次仍更新主缓存。
    """
    path = cache_path(case.symbol, run_index)
    if record:
        active = model if model is not None else _resolve_model()
        business = _resolve_business_description(case)
        pool = case_candidates_block(case)
        gen_map = infer_peer_scoring(business, pool, industry=case.industry, model=active)
        judge_report = judge_peer_candidates_batched(business, pool, batch_size=JUDGE_BATCH_SIZE, model=active)
        decisions = {c.code: Decision(code=c.code) for c in case.candidates}
        for code, payload in gen_map.items():
            row = decisions.get(code)
            if row is None:
                continue
            row.gen_overlap = payload.get("overlap")
            row.dims = payload.get("dims")
        for code, payload in judge_report.results.items():
            row = decisions.get(code)
            if row is None:
                continue
            row.judge_verdict = payload.get("verdict")
            row.judge_dims = payload.get("dims")
            row.reason = str(payload.get("reason") or "")
        judge_lineage = {
            "batches": judge_report.batches,
            "failed_batches": judge_report.failed_batches,
            "missing_codes": list(judge_report.missing_codes),
            "out_of_set_codes": list(judge_report.out_of_set_codes),
            "errors": list(judge_report.errors),
            "ok": judge_report.ok,
        }
        primary = cache_path(case.symbol, None if run_index == 0 else run_index)
        write_cache(
            primary,
            case,
            decisions,
            model_component=SMALL_MODEL_COMPONENT,
            run_index=run_index,
            judge=judge_lineage,
        )
        if run_index == 0:
            # 第 0 次重复即基线：主缓存也同步更新，使报告基线策略与被测重复采集同一份产物
            write_cache(
                path,
                case,
                decisions,
                model_component=SMALL_MODEL_COMPONENT,
                run_index=run_index,
                judge=judge_lineage,
            )
        return decisions
    if not path.is_file():
        raise FileNotFoundError(f"缺缓存 {path}；请先 `--record` 采集，或将该 case 从标注集移除。")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"缓存损坏（{path}）：{exc}；请重新 `--record` 采集该 case。") from exc
    if not isinstance(raw, (list, dict)):
        raise ValueError(f"缓存结构非法（{path}）：顶层应为数组或对象，实际 {type(raw).__name__}。")
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
    judge: dict[str, Any] | None = None,
) -> None:
    """写缓存（含血缘：case 分割/业态/模型组件/采集时刻 + judge 批量与降级记账）。

    ``split`` / ``segment`` 为**采集当时**的标注集快照（血缘，非权威；权威取 ``labels.yaml``）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "symbol": case.symbol,
        "split": case.split,
        "segment": case.segment,
        "run_index": run_index,
        "model_component": model_component,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "judge": judge or {},
        "decisions": [d.to_dict() for d in decisions.values()],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 闸门（确定性）──────────────────────────────────────────────────
def _overlap(dims: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights[dim] * dims.get(dim, 0.0) for dim in weights)


def judge_coverage(case: EvalCase, decisions: dict[str, Decision]) -> bool:
    """该 case 的候选池是否**逐只**取得可用 judge 判定（verdict 合法三级 + dims 齐全）。

    与生产同源（``peer_judge.JudgeReport.ok`` 的全覆盖口径）：只要有一只候选漏判或判定非法，
    该 case 的 D 类策略即按生产 fail-open 口径**整体回退生成器分**（而不是"漏判者按未判定剔除"）。
    """
    rows = decisions.get(case.symbol) or {}
    if not case.candidates:
        return False
    for cand in case.candidates:
        row = rows.get(cand.code)
        if row is None or row.judge_verdict not in VALID_VERDICTS or not isinstance(row.judge_dims, dict):
            return False
    return True


def project_decisions(decisions: dict[str, Decision], strategy: str, *, judge_ok: bool) -> dict[str, Decision]:
    """策略投影：把「该策略实际消费的输入」**显式化**（RC-VERDICT-DIVERGENCE 的收口点）。

    - ``B_single`` / ``C_dims`` / ``C_E_taxo``：**清空** judge 字段 —— 这三个策略的定义就是
      "没有 judge"（等价于生产 ``judge_enabled=false`` 或 judge 未判定），故不带 verdict；
    - ``C_D_judge`` / ``C_D_E`` 且 ``judge_ok``：保留 judge 的 verdict + dims（与生产 judge 生效态同源）；
    - ``C_D_judge`` / ``C_D_E`` 且 ``judge_ok=False``（judge 降级）：按生产 fail-open 口径
      **回退生成器 dims/overlap**、不带 verdict（等价于 C 口径，`judge_degraded` 案例数在报告中登记）。
    """
    out: dict[str, Decision] = {}
    use_judge = strategy in D_STRATEGIES and judge_ok
    for code, decision in decisions.items():
        if use_judge:
            out[code] = decision
        elif strategy in D_STRATEGIES:
            # 降级：judge 面整体失效 ⇒ 生成器口径（dims/自评），且不携带 verdict
            out[code] = replace(
                decision,
                judge_verdict=None,
                judge_dims=dict(decision.dims) if isinstance(decision.dims, dict) else None,
                judge_overlap=decision.gen_overlap,
            )
        else:
            out[code] = replace(decision, judge_verdict=None, judge_dims=None, judge_overlap=None)
    return out


def gate(
    decisions: dict[str, Decision],
    strategy: str,
    *,
    weights: dict[str, float] = WEIGHTS_DEFAULT,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    product_floor: float = DEFAULT_PRODUCT_FLOOR,
    customer_floor: float = DEFAULT_CUSTOMER_FLOOR,
) -> set[str]:
    """策略 → 保留代码集合（纯确定性；E 仅作提示，不硬剔）。

    **阈值权威**（RC-QUALITY-DEFAULTS）：签名默认值直接**引用生产常量**
    （``peer_judge.DEFAULT_MIN_OVERLAP`` / ``DEFAULT_PRODUCT_FLOOR`` / ``DEFAULT_CUSTOMER_FLOOR`` /
    ``DEFAULT_WEIGHTS``），harness 不再持有第二份"看似权威"的默认；扫参是**显式传参**覆盖。

    **规则与生产 ``gate_candidates`` 同源同序**（设计 §3.3/§4）：
    ① ``verdict == "reject"`` 先否决（**任何策略**都先否决——verdict 是否存在由
    :func:`project_decisions` 决定）；
    ② 维度下限仅在候选**携带 dims** 时生效（旧存量/人工候选没有 dims ⇒ 只按 overlap 判定）；
    ③ 有 dims 时用合成 Σ w_i·dim_i，缺 dims 时回落自评 overlap；两者皆缺 ⇒ 不可评估 ⇒ 剔。
    """
    kept: set[str] = set()
    for code, decision in decisions.items():
        if strategy == "B_single":
            if (decision.gen_overlap or 0.0) >= min_overlap:
                kept.add(code)
            continue
        verdict = str(decision.judge_verdict or "").strip().lower()
        if verdict == "reject":
            continue  # 规则①：与生产同源先否决（RC-VERDICT-DIVERGENCE）
        raw_dims = decision.judge_dims if strategy in D_STRATEGIES else decision.dims
        has_dims = isinstance(raw_dims, dict) and any(dim in raw_dims for dim in DIMS)
        if has_dims:
            assert raw_dims is not None
            if raw_dims.get("product", 0.0) < product_floor:
                continue
            if raw_dims.get("customer", 0.0) < customer_floor:
                continue
            score = _overlap(raw_dims, weights)
        else:
            fallback = decision.judge_overlap if strategy in D_STRATEGIES else decision.gen_overlap
            score = float(fallback or 0.0)
        if score < min_overlap:
            continue
        kept.add(code)
    return kept


def gate_for_case(
    case: EvalCase, decisions: dict[str, Decision], strategy: str, **gate_kwargs: Any
) -> tuple[set[str], bool]:
    """按 case 取「策略投影 + 闸门」（评估与稳定性共用；返回 ``(保留集, judge_ok)``）。"""
    judge_ok = judge_coverage(case, decisions)
    projected = project_decisions(decisions.get(case.symbol) or {}, strategy, judge_ok=judge_ok)
    return gate(projected, strategy, **gate_kwargs), judge_ok


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
    """按 split 分段评估一个策略；每段同时给『排除 disputed』与『未排除 disputed』两套数字。

    judge 状态逐 case 登记（``judge_status``）：``applied``（D 策略且全覆盖）/ ``degraded``
    （D 策略但池内有漏判 ⇒ 按生产 fail-open 回退生成器分）/ ``n/a``（非 D 策略）。
    """
    out: dict[str, Any] = {"strategy": strategy, "segments": {}, "judge_status": {}}
    for split in SPLITS:
        subset = cases_for_split(cases, split)
        rows: list[dict[str, Any]] = []
        rows_no_disputed: list[dict[str, Any]] = []
        rows_with_disputed: list[dict[str, Any]] = []
        for case in subset:
            kept, judge_ok = gate_for_case(case, decisions, strategy, **gate_kwargs)
            out["judge_status"][case.symbol] = judge_status_of(strategy, judge_ok)
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


def judge_status_of(strategy: str, judge_ok: bool) -> str:
    """``applied``（D 策略且全覆盖）/ ``degraded``（D 策略但池内有漏判）/ ``n/a``（非 D 策略）。"""
    if strategy not in D_STRATEGIES:
        return "n/a"
    return "applied" if judge_ok else "degraded"


def judge_status_counts(result: dict[str, Any]) -> dict[str, int]:
    """``judge_status`` → 计数（报告登记 ``judge_degraded`` 案例数的唯一处）。"""
    counts = {"applied": 0, "degraded": 0, "n/a": 0}
    for status in result.get("judge_status", {}).values():
        counts[str(status)] = counts.get(str(status), 0) + 1
    return counts


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
                "strategy": strategy,
                "params": {
                    "min_overlap": min_overlap,
                    "product_floor": product_floor,
                    "customer_floor": customer_floor,
                },
                "calibrate_split": calibrate_split,
                "judge_status": dict(result.get("judge_status") or {}),
            }
    return best or {}


def calibration_points(
    cases: list[EvalCase],
    decisions: dict[str, dict[str, Decision]],
    strategies: tuple[str, ...] = ("B_single", "C_dims", "C_D_judge", "C_D_E"),
    *,
    calibrate_split: str = DEFAULT_SPLIT,
) -> dict[str, Any]:
    """**逐策略**在 train 段扫参取标定点（holdout 只随点输出、不参与选点）。

    每个策略各取自己的标定点，是 DoD「C+D ≥ C」比较的前提：两侧都在**各自的 train 标定点**上
    报 holdout 数字，另在 :data:`PLACEHOLDER_DEFAULTS`（0.50/0.30/0.20）复核一遍（RC-DOD-SENS）。
    """
    return {name: sweep(cases, decisions, name, calibrate_split=calibrate_split) for name in strategies}


# ── 稳定性 ──────────────────────────────────────────────────────────
def jaccard(left: set[str], right: set[str]) -> float:
    """两个保留集的 Jaccard；**双方皆空 → 1.0**（"都空"视为一致，不是缺失）。"""
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def pair_is_evaluable(left: set[str], right: set[str]) -> bool:
    """一对保留集是否**可评估**：至少一侧非空（双方皆空 = 无信息，不参与均值）。"""
    return bool(left or right)


def mean_pairwise_jaccard(sets: list[set[str]]) -> float | None:
    """同一 case 多次采集保留集的**两两 Jaccard 均值**（只统计可评估对）。

    F-1 收口：n 次保留集**全为空**（或 n<2）时返回 ``None`` = **不可评估**，
    既不再静默报 ``1.000`` 假通过，也不计入分段均值；有信息但抖动的对仍照实参与。
    """
    pairs = [pair for pair in itertools.combinations(sets, 2) if pair_is_evaluable(*pair)]
    if not pairs:
        return None
    return statistics.mean(jaccard(left, right) for left, right in pairs)


def empty_run_count(sets: list[set[str]]) -> int:
    """n 次采集里保留集为空的次数（报告「几次皆空」的唯一处）。"""
    return sum(1 for item in sets if not item)


def stability(
    cases: list[EvalCase],
    strategies: tuple[str, ...],
    repeat: int,
    *,
    record: bool,
    model: Any = None,
    **gate_kwargs: Any,
) -> list[dict[str, Any]]:
    """重复采集 N 次，测 per-case 保留集 Jaccard（需 ``--record`` 才有意义）。

    集合定义：第 r 次采集的 LLM 产物经该策略闸门后的**保留代码集合**；聚合方式：同一 case +
    同一策略的 n 次保留集取**两两 Jaccard 均值**（只统计可评估对；**n 次皆空 ⇒ 不可评估**，
    记为 ``None`` 并连同 ``empty_runs`` 一并登记，F-1）。一次采集的结果被所有策略复用
    （同一次 LLM 产物分别过闸），以便横向比较"judge 是否降低抖动"。
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
                kept, _judge_ok = gate_for_case(case, {case.symbol: decisions}, strategy, **gate_kwargs)
                runs[(case.symbol, strategy)].append(kept)
    out: list[dict[str, Any]] = []
    for case in cases:
        row: dict[str, Any] = {
            "case": case.symbol,
            "split": case.split,
            "segment": case.segment,
            "repeat": repeat,
            "results": {},
            "empty_runs": {},
        }
        for strategy in strategies:
            sets = runs[(case.symbol, strategy)]
            row["results"][strategy] = mean_pairwise_jaccard(sets)
            row["empty_runs"][strategy] = empty_run_count(sets)
        out.append(row)
    return out


def stability_by_split(rows: list[dict[str, Any]], strategy: str) -> dict[str, float]:
    """按 split（含 all）聚合某策略的稳定性 Jaccard 均值（**只计可评估行**；全不可评估 → 0.0）。"""
    pairs = [
        (str(row["split"]), cast(float, row["results"][strategy]))
        for row in rows
        if row["results"].get(strategy) is not None
    ]
    return _stability_split_mean(pairs)


def stability_unassessable_cases(rows: list[dict[str, Any]], strategy: str) -> list[str]:
    """该策略下**不可评估**（n 次皆空 / n<2）的 case 列表（F-1：报告须显式列出，不并入均值）。"""
    return [str(row["case"]) for row in rows if row["results"].get(strategy) is None]


def stability_empty_everywhere_cases(rows: list[dict[str, Any]], strategy: str) -> list[str]:
    """该策略下**每一次采集的保留集都为空**的 case（报告里的「n 次皆空案例」）。"""
    out: list[str] = []
    for row in rows:
        repeat = int(row.get("repeat") or 0)
        empty_runs = int((row.get("empty_runs") or {}).get(strategy, 0))
        if repeat and empty_runs == repeat:
            out.append(str(row["case"]))
    return out


def _stability_split_mean(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """``[(split, jaccard)] → {train/holdout/all: 均值}``（纯函数，便于判别力变异实验）。"""
    out: dict[str, float] = {}
    for split in SPLITS:
        values = [value for row_split, value in pairs if split == "all" or row_split == split]
        out[split] = statistics.mean(values) if values else 0.0
    return out


# ── 判定 E：召回口径审计 + 置信度复算（设计 §3.1/§3.7/§6 Step 3 的 DoD） ──
def taxonomy_recall_audit(cases: list[EvalCase], *, min_constituents: int = 15) -> dict[str, Any]:
    """E 的**召回两口径对照**（DoD ②）——三个口径，算法见下（数字与算法一并进报告）。

    记 ``keep`` = 该 case 的人工标注 keep 候选，``pool`` = 标注集冻结候选池（= 首采缓存 ∪ 同 L3 成分，
    Step 0 起冻结）。则：

    1. ``llm_only``（**未并入**分类学成分）：``|keep ∩ pool∖(same_l3∪same_l2)| / |keep|``——
       池内「既非 same_l3 亦非 same_l2」的候选只可能来自 LLM/研报路径，故该集合等价于
       「未并入同 L3/L2 成分」的反事实池；
    2. ``with_taxonomy``（**并入后**）：``|keep ∩ pool| / |keep|``（并入实现后池已含同 L3/L2 成分）；
    3. ``production_pool``（**真实召回头寸**）：直接调用生产 ``peer_taxonomy.recall_pool(symbol)``
       （残差桶自动降级 L2）得到的成分池对 keep 的覆盖率——与在线并入**同一实现**。

    判据：``with_taxonomy ≥ llm_only``（单位是「覆盖 keep 的比例」，越高越好）。
    """
    from alphabee.company_track.peer_taxonomy import recall_pool

    rows: list[dict[str, Any]] = []
    for case in cases:
        keeps = [cand for cand in case.candidates if cand.label == "keep"]
        if not keeps:
            continue
        pool_codes = {cand.code for cand in case.candidates}
        taxo_codes = {cand.code for cand in case.candidates if cand.same_l3 or cand.same_l2}
        llm_codes = pool_codes - taxo_codes
        production_codes, level = recall_pool(case.symbol, min_constituents=min_constituents)
        production = set(production_codes)
        keeps_codes = {cand.code for cand in keeps}
        keep_total = len(keeps_codes)
        rows.append(
            {
                "case": case.symbol,
                "split": case.split,
                "keep_total": keep_total,
                "kept_in_pool": len(keeps_codes & pool_codes),
                "kept_in_llm_only": len(keeps_codes & llm_codes),
                "kept_in_production_pool": len(keeps_codes & production),
                "production_level": level,
                "production_pool_size": len(production),
                "coverage_llm_only": len(keeps_codes & llm_codes) / keep_total,
                "coverage_with_taxonomy": len(keeps_codes & pool_codes) / keep_total,
                "coverage_production_pool": len(keeps_codes & production) / keep_total,
            }
        )
    means = {
        key: (statistics.mean([cast(float, row[key]) for row in rows]) if rows else 0.0)
        for key in ("coverage_llm_only", "coverage_with_taxonomy", "coverage_production_pool")
    }
    return {"rows": rows, "means": means, "cases": len(rows)}


def confidence_recompute(
    cases: list[EvalCase],
    decisions: dict[str, dict[str, Decision]],
    *,
    min_constituents: int = 15,
) -> dict[str, Any]:
    """对标组置信度的**离线复算**（DoD ③）：用生产合成函数 + 真实三信号逐 case 复算三档。

    信号口径（与设计 §3.7 一致）：

    - ``taxonomy_reliable``：生产 ``peer_taxonomy.assess_reliability``（残差桶/成分不足 ⇒ False）；
    - ``judge_direct_ratio``：``C_D_judge`` 策略**保留项**中 ``verdict == "direct"`` 的占比；
    - ``mean_overlap``：保留项的合成 overlap 均值。

    返回逐 case 结果 + 三档分布 + 确定性自检（复算两遍逐位一致）。
    """
    from alphabee.company_track.peer_group_build import synthesize_peer_confidence
    from alphabee.company_track.peer_taxonomy import assess_reliability

    def _project(case: EvalCase) -> dict[str, Any] | None:
        rows = decisions.get(case.symbol) or {}
        kept, judge_ok = gate_for_case(case, decisions, "C_D_judge")
        if not kept:
            return None
        direct = sum(1 for code in kept if (rows.get(code) or Decision(code=code)).judge_verdict == "direct")
        overlaps = [overlap_score(rows[code].judge_dims or {}) for code in kept if code in rows]
        confidence = synthesize_peer_confidence(
            taxonomy_reliable=assess_reliability(case.symbol, min_constituents=min_constituents).reliable,
            judge_direct_ratio=(direct / len(kept)) if judge_ok else None,
            mean_overlap=(statistics.mean(overlaps) if overlaps else None),
        )
        return {
            "case": case.symbol,
            "split": case.split,
            "level": confidence.level,
            "score": confidence.score,
            "signals": confidence.signals,
            "basis": confidence.basis(),
        }

    first = [_project(case) for case in cases]
    second = [_project(case) for case in cases]
    deterministic = first == second
    distribution = {level: 0 for level in ("低", "中", "高")}
    for row in first:
        if row is not None:
            distribution[str(row["level"])] += 1
    return {
        "rows": [row for row in first if row is not None],
        "distribution": distribution,
        "deterministic": deterministic,
        "cases_with_peers": sum(1 for row in first if row is not None),
    }


def overlap_score(dims: dict[str, float]) -> float:
    """四维 → 合成 overlap（**生产权重**单一来源，供离线复算复用）。"""
    from alphabee.company_track.peer_judge import overlap_score as production_overlap

    return production_overlap(dims)


# ── 留档 / 血缘审计 / DoD 复核（F-5、F-6、设计 §6 Step 2） ────────────
def sha256_of(path: Path) -> str:
    """文件 sha256（留档与血缘审计用）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path) -> str:
    """报告/留档里的路径显示：项目内用相对路径，项目外（tmp_path）用绝对路径（不抛）。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def git_fingerprint() -> dict[str, Any]:
    """**只读** git 指纹（HEAD + 工作树改动条数）；git 不可用 → 空 dict（不抛）。"""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15
        )
    except Exception:  # noqa: BLE001 —— 指纹是证据不是判据，取不到就不写
        return {}
    if head.returncode != 0 or status.returncode != 0:
        return {}
    return {
        "git_head": head.stdout.strip(),
        "git_status_lines": len([line for line in status.stdout.splitlines() if line.strip()]),
    }


def cache_lineage_audit(cases: list[EvalCase], *, root: Path | None = None, label: str = "") -> dict[str, Any]:
    """**现算**「缓存血缘 vs 标注集」不一致清单（F-6；只读，绝不改写缓存字节）。

    ``data/peer_eval_cache/**`` 的 ``split`` / ``segment`` 是**采集当时**的标注集快照（血缘），
    权威一律取 ``labels.yaml``。本函数把「哪些缓存文件与标注集不一致」连同**现行 sha256**
    现算出来，报告直接展示 —— 历史遗留（``002318.SZ`` 的 split）因此不再依赖人手维护的散文声明。
    """
    active_root = root or CACHE_DIR
    by_symbol = {case.symbol: case for case in cases}
    files: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(active_root.rglob("*.json")):
        case = by_symbol.get(path.stem.upper())
        if case is None:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue  # v1 裸数组缓存：无血缘字段可比
        scanned += 1
        mismatched = [
            field
            for field, expected in (("split", case.split), ("segment", case.segment))
            if str(raw.get(field) or "") != expected
        ]
        if not mismatched:
            continue
        files.append(
            {
                "path": _display_path(path),
                "sha256": sha256_of(path),
                "mismatched_fields": mismatched,
                "cache_split": str(raw.get("split") or ""),
                "cache_segment": str(raw.get("segment") or ""),
                "labels_split": case.split,
                "labels_segment": case.segment,
            }
        )
    return {"root": _display_path(active_root), "label": label, "scanned": scanned, "mismatches": files}


def archive_run(
    report_text: str,
    *,
    meta: dict[str, Any],
    out_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    """**版本化留档**（F-5）：把报告与 manifest（缓存/标注集 sha256 + git 指纹 + 参数）落到
    ``outputs/peer_group_eval/baseline_run/<ts>/``。

    明示口径：``data/peer_eval_cache/<symbol>.json`` 只是采集产物（会被 ``--record`` 覆盖），
    **不是不可变证据**；可复算留档 = 本目录的 ``report.md`` + ``manifest.json``（含逐文件 sha256）。
    """
    archived_at = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = (out_dir or ARCHIVE_DIR) / archived_at
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.md").write_text(report_text, encoding="utf-8")
    manifest = {
        **meta,
        "archived_at": archived_at,
        "report_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dir": str(target), "manifest": manifest}


def _point_metrics(
    cases: list[EvalCase], decisions: dict[str, dict[str, Decision]], strategy: str, params: dict[str, float]
) -> dict[str, float]:
    return segment_metrics(evaluate_strategy(cases, decisions, strategy, **params), "holdout", exclude_disputed=True)


def threshold_point_table(
    cases: list[EvalCase],
    decisions: dict[str, dict[str, Decision]],
    points: dict[str, dict[str, float]],
    *,
    strategies: tuple[str, ...] = ("B_single", "C_dims", "C_D_judge"),
) -> dict[str, dict[str, dict[str, float]]]:
    """阈值点对照表：``点 → 策略 → {train_f1, holdout_f1（排除 disputed）}``。

    用于把「train 段标定点 / 现行生产生效点 / 占位默认点」三者的取舍**机器可读地**摆在同一张表里
    （避免用散文只报有利方向；train 上的增益与 holdout 上的代价必须并读）。
    """
    out: dict[str, dict[str, dict[str, float]]] = {}
    for label, params in points.items():
        out[label] = {}
        for name in strategies:
            result = evaluate_strategy(cases, decisions, name, **params)
            out[label][name] = {
                "train_f1": segment_metrics(result, "train")["f1"],
                "holdout_f1": segment_metrics(result, "holdout", exclude_disputed=True)["f1"],
            }
    return out


def dod_check(
    cases: list[EvalCase],
    decisions: dict[str, dict[str, Decision]],
    *,
    calibrate_split: str = DEFAULT_SPLIT,
    stability_rows: list[dict[str, Any]] | None = None,
    baseline: str = "C_dims",
    candidates: tuple[str, ...] = ("C_D_judge", "C_D_E"),
) -> dict[str, Any]:
    """DoD（设计 §6 Step 2）复核：**holdout（排除 disputed）** 上 ``C+D ≥ C`` 且稳定性不下降。

    协议（RC-DOD-SENS 要求的两点复核）：

    1. **标定点**：各策略用 **train 段**扫参各自的标定点（holdout 只随点输出、不参与选点）；
    2. **占位默认点**：:data:`PLACEHOLDER_DEFAULTS`（0.50/0.30/0.20）——与标定点对照，
       若 DoD 只在标定点成立，报告须显式说明「生产生效阈值 = 标定点」；
    3. **稳定性**：judge 策略的保留集 Jaccard 相对生成器口径（``B_single`` 自评 / ``C_dims`` 维度）
       **均不得下降**（不可评估行不参与均值，F-1）。

    Returns: 结构化的复核结果（报告章节直接渲染；数字为逐 case 均值，渲染保留 3 位小数）。
    """
    calibration = calibration_points(cases, decisions, (baseline, *candidates), calibrate_split=calibrate_split)
    calibrated_metrics = {
        name: {
            "params": calibration[name]["params"],
            "holdout": _point_metrics(cases, decisions, name, calibration[name]["params"]),
        }
        for name in calibration
    }
    placeholder_metrics = {
        name: {
            "params": dict(PLACEHOLDER_DEFAULTS),
            "holdout": _point_metrics(cases, decisions, name, PLACEHOLDER_DEFAULTS),
        }
        for name in calibration
    }
    #: 「生产生效点」= 现行生产常量 / ``company_track.peer_quality`` 配置缺省值（线上真正生效的阈值）
    production_params = {
        "min_overlap": DEFAULT_MIN_OVERLAP,
        "product_floor": DEFAULT_PRODUCT_FLOOR,
        "customer_floor": DEFAULT_CUSTOMER_FLOOR,
    }
    production_metrics = {
        name: {"params": production_params, "holdout": _point_metrics(cases, decisions, name, production_params)}
        for name in calibration
    }
    checks: list[dict[str, Any]] = []
    for point_name, table in (
        ("calibrated", calibrated_metrics),
        ("production", production_metrics),
        ("placeholder", placeholder_metrics),
    ):
        base_f1 = table[baseline]["holdout"]["f1"]
        for name in candidates:
            f1 = table[name]["holdout"]["f1"]
            checks.append(
                {
                    "point": point_name,
                    "strategy": name,
                    "baseline": baseline,
                    "f1": f1,
                    "baseline_f1": base_f1,
                    "f1_ok": f1 >= base_f1 - 1e-12,
                    "params": table[name]["params"],
                    "baseline_params": table[baseline]["params"],
                }
            )
    stability: dict[str, Any] = {}
    stability_skipped: list[str] = []
    if stability_rows:
        for name in candidates:
            if all(row["results"].get(name) is None for row in stability_rows):
                # 该策略未纳入 --stability-strategies（无重复产物）⇒ 不计算、不当作 0、不参与判定
                stability_skipped.append(name)
                continue
            cand = stability_by_split(stability_rows, name)
            row = {}
            for ref in ("B_single", baseline):
                ref_split = stability_by_split(stability_rows, ref)
                row[ref] = {split: cand[split] - ref_split[split] for split in SPLITS}
            stability[name] = row
    stability_ok = (
        all(all(delta >= -1e-9 for delta in row[ref].values()) for row in stability.values() for ref in row)
        if stability
        else None
    )
    point_table = threshold_point_table(
        cases,
        decisions,
        {
            "calibrated": calibration[baseline]["params"],
            "production": production_params,
            "placeholder": dict(PLACEHOLDER_DEFAULTS),
        },
    )
    f1_ok_at_calibrated = any(c["f1_ok"] for c in checks if c["point"] == "calibrated")
    f1_ok_at_production = any(c["f1_ok"] for c in checks if c["point"] == "production")
    f1_ok_at_placeholder = any(c["f1_ok"] for c in checks if c["point"] == "placeholder")
    return {
        "calibrate_split": calibrate_split,
        "baseline": baseline,
        "candidates": list(candidates),
        "calibrated": calibrated_metrics,
        "production": production_metrics,
        "placeholder": placeholder_metrics,
        "checks": checks,
        "point_table": point_table,
        "stability_deltas": stability,
        "stability_skipped": stability_skipped,
        "stability_ok": stability_ok,
        #: 逐判据（DoD = F1 判据 **且** 稳定性判据；任一不成立 ⇒ 负结果路径）
        "f1_ok_at_calibrated": f1_ok_at_calibrated,
        "f1_ok_at_production": f1_ok_at_production,
        "f1_ok_at_placeholder": f1_ok_at_placeholder,
        "dod_satisfied": bool(f1_ok_at_calibrated and (stability_ok is not False)),
    }


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
    stability_strategies: tuple[str, ...] = ("B_single", "C_dims", "C_D_judge", "C_D_E"),
    repeat: int = 0,
    sweep_points: dict[str, Any] | None = None,
    dod: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
    generated_at: str = "",
    recall_audit: dict[str, Any] | None = None,
    confidence: dict[str, Any] | None = None,
) -> str:
    lines = ["# 对标组判定策略回归", ""]
    lines.append(f"- 测量时刻（UTC）: {generated_at or '—'}")
    lines.append(f"- cases: {len(cases)}（split 分布：{_split_counts(cases)}）")
    lines.append(f"- 业态分布：{_segment_counts(cases)}")
    lines.append(
        "- 指标口径：按 split 分段（train 用于标定 / holdout 仅验收 / all 全体），每段两套（排除·未排除 disputed）；"
        "数字为逐 case 指标的算术平均，渲染保留 3 位小数"
    )
    lines.append(
        "- 生成器打分与 judge 两阶段均调用生产侧入口 `alphabee.company_track.peer_judge`"
        "（`infer_peer_scoring` / `judge_peer_candidates_batched`），脚本内无 prompt 字面量"
    )
    lines.append(
        "- **阈值权威（RC-QUALITY-DEFAULTS）**：`gate()` 签名默认值引用生产常量"
        f"（min_overlap={DEFAULT_MIN_OVERLAP} / product_floor={DEFAULT_PRODUCT_FLOOR} / "
        f"customer_floor={DEFAULT_CUSTOMER_FLOOR} / weights={WEIGHTS_DEFAULT}）；扫参只作显式覆盖，"
        "生产生效阈值取 `company_track.peer_quality` 配置"
    )
    lines.append(
        "- **verdict 口径（RC-VERDICT-DIVERGENCE）**：`gate()` 对任何策略都执行规则①"
        "「verdict==reject 先否决」（与生产 `gate_candidates` 同源同序）；C 类策略的 verdict 由"
        " `project_decisions` 显式清空（C 策略 = 无 judge），不是“忽略 verdict”"
    )
    lines.append("")

    lines.append("## 证据口径与血缘（F-5 / F-6）")
    lines.append("")
    lines.append(
        "- `data/peer_eval_cache/**` 的 `split` / `segment` 是**采集当时**的标注集快照（血缘），"
        "**权威一律取 `tests/fixtures/peer_group_eval/labels.yaml`**"
    )
    lines.append(
        "- 缓存**不是不可变证据**：`--record` 会覆盖它；可复算留档 = "
        "`outputs/peer_group_eval/baseline_run/<ts>/`（报告 + manifest：逐文件 sha256 + git 指纹 + 参数）"
    )
    for audit in lineage or []:
        if not isinstance(audit, dict):
            continue
        mismatches = audit.get("mismatches") or []
        lines.append(
            f"- 缓存世代 `{audit.get('root')}`（{audit.get('label') or ''}；扫描 "
            f"{audit.get('scanned')} 个含血缘字段的缓存文件）"
        )
        if mismatches:
            lines.append(f"  - 血缘不一致文件（现算，共 {len(mismatches)} 个，**未改写其字节**）：")
            for item in mismatches:
                fields = item["mismatched_fields"]
                cache_side = ", ".join(f"{field}={item['cache_' + field]}" for field in fields)
                labels_side = ", ".join(f"{field}={item['labels_' + field]}" for field in fields)
                lines.append(
                    f"    - `{item['path']}`：字段 {fields} 缓存={{{cache_side}}} "
                    f"标注集={{{labels_side}}} sha256=`{item['sha256']}`"
                )
        else:
            lines.append("  - 血缘不一致文件：**无**（该世代缓存与标注集 split/segment 逐例一致）")
    lines.append("")

    lines.append("## 策略等价关系（F-2 披露）")
    lines.append("")
    lines.append(
        "- E（same_l3/same_l2）在 Gate 中**只作提示、不硬剔** ⇒ `C_E_taxo ≡ C_dims`、"
        "`C_D_E ≡ C_D_judge`（保留集逐 case 相同）；因此这两对策略的 P/R/F1 必然逐位相同，"
        "下表**不是**两套独立证据"
    )
    if "C_dims" in results and "C_E_taxo" in results:
        lines.extend(_equivalence_lines(results, "C_dims", "C_E_taxo"))
    if "C_D_judge" in results and "C_D_E" in results:
        lines.extend(_equivalence_lines(results, "C_D_judge", "C_D_E"))
    lines.append("")

    lines.append("## 策略汇总（gate 后）")
    lines.append("")
    lines.extend(_metric_table(cases, results, exclude_disputed=False))
    lines.append("## 策略汇总（gate 后，排除 disputed 口径）")
    lines.append("")
    lines.extend(_metric_table(cases, results, exclude_disputed=True))

    lines.append("## judge 生效/降级状态（与生产 fail-open 同源）")
    lines.append("")
    lines.append("| strategy | applied | degraded | n/a |")
    lines.append("|---|---|---|---|")
    for name, result in results.items():
        counts = judge_status_counts(result)
        lines.append(f"| {name} | {counts.get('applied', 0)} | {counts.get('degraded', 0)} | {counts.get('n/a', 0)} |")
    lines.append("")
    lines.append(
        "> `degraded` = 该 case 候选池内存在漏判/判定非法 ⇒ 按生产口径**整体回退生成器 dims/overlap**"
        "（判定 C 口径）并在此登记，不做「漏判者静默剔除」的替代处置。"
    )
    lines.append("")

    lines.append("## 分类学召回（E）")
    lines.append("")
    lines.append("| case | split | 业态 | same_l3 recall | same_l2 recall | 生产分类学 | 召回层级 |")
    lines.append("|---|---|---|---|---|---|---|")
    for case in cases:
        info = _taxonomy_row(case)
        lines.append(
            f"| {case.symbol} {case.name} | {case.split} | {case.segment or '—'} "
            f"| {taxonomy_recall(case, 'l3'):.2f} | {taxonomy_recall(case, 'l2'):.2f} "
            f"| {info['reliable']}{info['detail']} | {info['level']} |"
        )
    lines.append("")

    if recall_audit is not None:
        lines.extend(_recall_audit_lines(recall_audit, results))

    if confidence is not None:
        lines.extend(_confidence_lines(confidence))

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

    if sweep_points:
        lines.append("## 逐策略标定点（train 段选点，holdout 仅随点输出）")
        lines.append("")
        lines.append("| strategy | 标定点 params | train f1 | holdout f1（排除 disputed） |")
        lines.append("|---|---|---|---|")
        for name, point in sweep_points.items():
            if not point:
                continue
            lines.append(
                f"| {name} | {point['params']} | {point['score']:.3f} "
                f"| {segment_metrics(point['result'], 'holdout', exclude_disputed=True)['f1']:.3f} |"
            )
        lines.append("")

    if dod:
        lines.extend(_dod_lines(dod))

    if stability_rows:
        lines.append("## 稳定性（保留集 Jaccard）")
        lines.append("")
        lines.append(
            "- 集合定义：第 r 次采集的 LLM 产物经闸门后的**保留代码集合**；"
            "聚合：同一 case + 同一策略的 n 次保留集取**两两 Jaccard 均值**"
            "（只统计可评估对：至少一侧非空；**n 次皆空 ⇒ 不可评估**，F-1）"
        )
        lines.append(f"- 策略: {', '.join(stability_strategies)}")
        lines.append(f"- 重复采集次数: {repeat}（读数来自 `<cache-root>/stable_run*/`）")
        lines.append("")
        for strategy in stability_strategies:
            by_split = stability_by_split(stability_rows, strategy)
            unassessable = stability_unassessable_cases(stability_rows, strategy)
            lines.append(
                f"- `{strategy}` 均值（只计可评估行）：train={by_split['train']:.3f} "
                f"holdout={by_split['holdout']:.3f} all={by_split['all']:.3f}"
                f"；**不可评估 case 数={len(unassessable)}**（{repeat} 次皆空，不参与均值）"
                f"{('：' + '、'.join(unassessable)) if unassessable else ''}"
            )
        if len(stability_strategies) >= 2:
            candidate = stability_strategies[-1]
            cand_split = stability_by_split(stability_rows, candidate)
            lines.append("")
            lines.append(ROUNDING_SCOPE_NOTE)
            lines.append(f"- 对照：`{candidate}` vs 生成器口径（差值为正表示前者更稳）")
            for baseline_side in stability_strategies[:-1]:
                base_split = stability_by_split(stability_rows, baseline_side)
                for split in SPLITS:
                    delta = cand_split[split] - base_split[split]
                    verdict = "更稳" if delta > 1e-9 else ("更抖" if delta < -1e-9 else "持平")
                    lines.append(
                        f"  - [{baseline_side}] {split}: {cand_split[split]:.3f} - {base_split[split]:.3f} "
                        f"= {delta:+.3f} ⇒ {verdict}"
                    )
            for baseline_side in stability_strategies[:-1]:
                base_split = stability_by_split(stability_rows, baseline_side)
                worse = [split for split in SPLITS if cand_split[split] < base_split[split] - 1e-9]
                if worse:
                    lines.append(
                        f"- **负结果**：`{candidate}` 相对 `{baseline_side}` 在 {'/'.join(worse)} 段**更抖**，"
                        "即 judge 未在该段降低抖动（如实登记，不美化）。"
                    )
                else:
                    lines.append(f"- `{candidate}` 相对 `{baseline_side}` 在全部段均不劣于对照。")
        lines.append("")
        header = "| case | split | 业态 | " + " | ".join(f"{s} Jaccard" for s in stability_strategies) + " |"
        lines.append(header)
        lines.append("|" + "---|" * (3 + len(stability_strategies)))
        for row in stability_rows:
            cells = " | ".join(
                "n/a（皆空）" if row["results"].get(s) is None else f"{cast(float, row['results'][s]):.3f}"
                for s in stability_strategies
            )
            lines.append(f"| {row['case']} | {row['split']} | {row['segment'] or '—'} | {cells} |")
        lines.append("")
        lines.append("> 抖动判定：Jaccard < 1 即该 case 的保留集在重复采集间发生变化（有抖动）；")
        lines.append("> `n/a（皆空）` = n 次采集的保留集**全为空** ⇒ 该行无信息、不参与均值（F-1 收口）。")
        lines.append("> 若 judge 策略并未比对照策略更稳定，则如实登记为负结果（不美化、不只报有利方向）。")
        lines.append("")

    return "\n".join(lines)


def _taxonomy_row(case: EvalCase) -> dict[str, str]:
    """生产分类学口径（``peer_taxonomy``）逐 case 摘要：可信性、L3 名称/成分数、召回层级。"""
    from alphabee.company_track.peer_taxonomy import assess_reliability

    reliability = assess_reliability(case.symbol)
    entry = reliability.entry
    detail = ""
    if entry is not None:
        detail = f"（{entry.l3_name or '—'} / {reliability.l3_constituents} 只）"
    return {
        "reliable": "可信" if reliability.reliable else "不可信",
        "detail": detail,
        "level": reliability.recall_level,
        "reason": reliability.reason,
    }


def _recall_audit_lines(recall_audit: dict[str, Any], results: dict[str, dict[str, Any]]) -> list[str]:
    """E 召回两口径对照章节（DoD ②）：数字 + 算法说明 + 判据。"""
    means = recall_audit["means"]
    lines = ["## E 召回口径审计（DoD ②）", ""]
    lines.append(
        "- **算法**：`keep` = 人工标注 keep；`pool` = 标注集冻结候选池（首采缓存 ∪ 同 L3 成分）。"
        "`llm_only`（**未并入**）= `|keep ∩ pool∖(same_l3∪same_l2)| / |keep|`（池内既非同 L3 亦非同 L2 的"
        "候选只可能来自 LLM/研报路径，故等价于未并入的反事实池）；`with_taxonomy`（**并入后**）"
        "= `|keep ∩ pool| / |keep|`；`production_pool`（**真实召回头寸**）= 生产 "
        "`peer_taxonomy.recall_pool(symbol)`（残差桶自动降级 L2）对 keep 的覆盖率"
    )
    lines.append(
        f"- **均值**：llm_only={means['coverage_llm_only']:.3f}；with_taxonomy="
        f"{means['coverage_with_taxonomy']:.3f}；production_pool={means['coverage_production_pool']:.3f}"
        f"（{recall_audit['cases']} 例有 keep 的 case）"
    )
    verdict = means["coverage_with_taxonomy"] >= means["coverage_llm_only"] - 1e-12
    lines.append(f"- **判据（with_taxonomy ≥ llm_only）**：{'成立 ✓' if verdict else '不成立 ✗'}")
    lines.append(
        "- **诚实口径提示**：`with_taxonomy=1.000` 含**构造性**成分——Step 0 的冻结候选池本就按"
        "「首采缓存 ∪ 同 L3 成分」构建，故并入口径必然覆盖全部 keep；**真实召回头寸以 "
        f"`production_pool={means['coverage_production_pool']:.3f}` 为准**（生产 `recall_pool` 实际能覆盖的 "
        "keep 比例），其未覆盖部分 = **跨 L3/L2 的真对标**（E 的固有漏检，设计 §5.1 结论 3：E 只能作召回/特征、"
        "绝不硬闸）"
    )
    lines.append("")
    lines.append("| case | split | keep | llm_only | with_taxonomy | production_pool | 生产召回层级 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in recall_audit["rows"]:
        lines.append(
            f"| {row['case']} | {row['split']} | {row['keep_total']} | {row['coverage_llm_only']:.3f} "
            f"| {row['coverage_with_taxonomy']:.3f} | {row['coverage_production_pool']:.3f} "
            f"| {row['production_level']}（池 {row['production_pool_size']} 只） |"
        )
    lines.append("")
    if "C_dims" in results and "C_E_taxo" in results:
        lines.append(
            "- **精度对照（DoD ①）**：`C_E_taxo` 与 `C_dims` 保留集逐 case 相同（E 只作特征/召回池、"
            "不进 Gate 条件）⇒ 精度不放宽；逐 split 数字见上方策略汇总"
        )
        lines.append("")
    return lines


def _confidence_lines(confidence: dict[str, Any]) -> list[str]:
    """对标组置信度复算章节（DoD ③）：逐 case 三档 + 分布 + 确定性自检。"""
    lines = ["## 对标组置信度复算（DoD ③）", ""]
    lines.append(
        "- **口径**：`confidence_score = w_taxonomy·taxonomy_reliable + w_judge_direct·judge_direct_ratio "
        "+ w_overlap·mean_overlap`（生产合成函数，缺失信号按 0）；"
        "档位 `< low → 低`、`< medium → 中`、否则 `高`"
    )
    lines.append(
        "- **D 项口径**：`judge_enabled=false`（当前默认）或 judge 不可用时，D 项（`judge_direct_ratio`）"
        "按 **0** 计入 ⇒ 档位只反映 E（`taxonomy_reliable`）与 C（`mean_overlap`）两路信号"
        "（**不得**读作「judge 判过但都不是 direct」）；本报告为**离线复算**，传入录制缓存的"
        "**真实 ratio** ⇒ 三路齐备（公式本体被覆盖，而非只覆盖 0 分支）"
    )
    lines.append(
        f"- **三档分布**：{confidence['distribution']}（有对标组的 case {confidence['cases_with_peers']} 个）；"
        f"**确定性自检（复算两遍逐位一致）**：{'通过 ✓' if confidence['deterministic'] else '失败 ✗'}"
    )
    lines.append("- 边界用例（score 恰等于 low/medium）由单测钉住：`tests/company_track/test_peer_quality.py`")
    lines.append("")
    lines.append("| case | split | 档位 | score | 信号（taxonomy/判分占比/overlap） |")
    lines.append("|---|---|---|---|---|")
    for row in confidence["rows"]:
        signals = row["signals"]
        parts = ", ".join(
            f"{name}={'缺失' if signals.get(name) is None else f'{signals[name]:.2f}'}"
            for name in ("taxonomy_reliable", "judge_direct_ratio", "mean_overlap")
        )
        lines.append(f"| {row['case']} | {row['split']} | {row['level']} | {row['score']:.3f} | {parts} |")
    lines.append("")
    return lines


def _equivalence_lines(results: dict[str, dict[str, Any]], left: str, right: str) -> list[str]:
    """现算两策略逐 case 保留集是否相同（等价关系**可证伪**：不同即判红字面披露）。"""
    out: list[str] = []
    same, diff = 0, []
    for split in ("train", "holdout"):
        for row_left in results[left]["segments"][split]["include_disputed"]["per_case"]:
            row_right = next(
                (
                    entry
                    for entry in results[right]["segments"][split]["include_disputed"]["per_case"]
                    if entry["case"] == row_left["case"]
                ),
                None,
            )
            if row_right is None:
                continue
            if set(row_left["kept"]) == set(row_right["kept"]):
                same += 1
            else:
                diff.append(f"{row_left['case']}(train/holdout)")
    out.append(
        f"  - 实测：`{left}` vs `{right}` 保留集相同 {same} 例，不同 {len(diff)} 例" + (f"：{diff}" if diff else "")
    )
    return out


def _dod_lines(dod: dict[str, Any]) -> list[str]:
    """DoD 复核章节（设计 §6 Step 2 + RC-DOD-SENS 三点复核 + 逐判据 + 负结果路径）。

    判据（DoD = F1 判据 **且** 稳定性判据）：

    1. **F1 判据**：holdout（排除 disputed）上 judge 策略 ≥ 基线（各在 train 标定点）；
    2. **稳定性判据**：judge 策略的保留集 Jaccard 相对**生成器口径**（``B_single`` 自评 / ``C_dims``
       维度）在 train/holdout/all 三段均**不得下降**；
    3. 两点复核（RC-DOD-SENS）：**生产生效点**（现行常量/配置）与**占位默认点**（0.50/0.30/0.20）
       一并报数；若只在标定点成立，须声明「生产生效阈值 = 标定点」。

    任一判据不成立 ⇒ **负结果路径**：不得默认启用 judge（``judge_enabled=false``），
    接线与配置保留、Gate 走判定 C 口径，并如实登记依据（不改口径凑数、不只报有利方向）。
    """
    lines = ["## DoD 复核（holdout，排除 disputed）", ""]
    lines.append(
        f"- 协议：基线 `{dod['baseline']}`，候选 {dod['candidates']}；各策略在 **{dod['calibrate_split']} 段**"
        f"各自的标定点上比较，并在**生产生效点** {dod['production'][dod['baseline']]['params']} 与"
        f"**占位默认点** {PLACEHOLDER_DEFAULTS} 复核（holdout 从不参与选点）"
    )
    lines.append("")
    lines.append("| 点 | strategy | params | holdout f1 | 基线 f1 | f1 ≥ 基线 |")
    lines.append("|---|---|---|---|---|---|")
    for check in dod["checks"]:
        lines.append(
            f"| {check['point']} | {check['strategy']} | {check['params']} | {check['f1']:.3f} "
            f"| {check['baseline_f1']:.3f} | {'✓' if check['f1_ok'] else '✗'} |"
        )
    lines.append("")
    lines.append(
        "- **F1 判据**：标定点 "
        + ("成立 ✓" if dod["f1_ok_at_calibrated"] else "不成立 ✗")
        + "；生产生效点 "
        + ("成立 ✓" if dod.get("f1_ok_at_production") else "不成立 ✗")
        + "；占位默认点 "
        + ("成立 ✓" if dod["f1_ok_at_placeholder"] else "不成立 ✗")
    )
    if dod.get("stability_deltas"):
        lines.append(ROUNDING_SCOPE_NOTE)
        lines.append("- **稳定性判据**（judge 策略相对生成器口径的 Jaccard 差值，正=更稳）：")
        for strategy, row in dod["stability_deltas"].items():
            for ref, deltas in row.items():
                cells = "，".join(f"{split}={delta:+.3f}" for split, delta in deltas.items())
                lines.append(f"  - `{strategy}` vs `{ref}`：{cells}")
        lines.append(f"- 稳定性判据总体：{'不下降 ✓' if dod.get('stability_ok') else '存在下降段 ✗'}")
        if dod.get("stability_skipped"):
            lines.append(
                f"- 未计算稳定性的候选：{dod['stability_skipped']}（未纳入 `--stability-strategies`；"
                "按等价关系可用其恒等策略的稳定性代表，见「策略等价关系」章节）"
            )
    else:
        lines.append("- **稳定性判据**：未采集重复产物（``--repeat``≤1）⇒ 不可评估，本报告不作判定")
    lines.append("")
    if dod.get("point_table"):
        lines.append(
            f"- **阈值点对照**（train 段增益与 holdout 代价并读；`calibrated` = **基线 `{dod['baseline']}`** "
            "的 train 段 F1 最大点，`production` = 现行生产常量/配置缺省，`placeholder` = 占位默认）："
        )
        lines.append("")
        lines.append("| 点 | strategy | train f1 | holdout f1（排除 disputed） |")
        lines.append("|---|---|---|---|")
        for label, table in dod["point_table"].items():
            for name, row in table.items():
                lines.append(f"| {label} | {name} | {row['train_f1']:.3f} | {row['holdout_f1']:.3f} |")
        lines.append("")
    satisfied = bool(dod.get("dod_satisfied"))
    unmet: list[str] = []
    if not dod["f1_ok_at_calibrated"]:
        unmet.append("F1 判据（标定点上 judge 策略未 ≥ 基线）")
    if dod.get("stability_ok") is False:
        unmet.append("稳定性判据（judge 策略的保留集 Jaccard 相对生成器口径存在下降段）")
    if satisfied:
        lines.append("- **DoD 成立 ✓**（F1 判据 且 稳定性判据 均满足）")
        if not dod["f1_ok_at_placeholder"]:
            lines.append(
                "- **口径声明（RC-DOD-SENS）**：F1 判据只在**标定点/生产生效点**成立、占位默认点不成立 ⇒ "
                "**生产生效阈值 = 标定点**（写入 `company_track.peer_quality` 默认值与 `config.yaml.example`），"
                "并保留本敏感性表作为证据；不得表述为「DoD 已稳健成立」"
            )
    else:
        lines.append("- **负结果（诚实登记）**：DoD **不成立** —— 未满足判据：" + "；".join(unmet) + "。")
        lines.append(
            "  依据设计 §6 Step 2 的负结果路径：**不得默认启用 judge**（`company_track.peer_quality.judge_enabled` "
            "默认 false；接线与配置保留，Gate 走判定 C 口径）。本结论对样本量（train 13 / holdout 6）与阈值敏感，"
            "禁止改口径凑数、禁止只报有利方向"
        )
        if dod["f1_ok_at_calibrated"] and not dod["f1_ok_at_placeholder"]:
            lines.append(
                "  **阈值口径（RC-DOD-SENS）**：F1 判据只在**标定点 / 生产生效点**成立、占位默认点不成立 ⇒ "
                "**生产生效阈值 = 标定点**（现行 `company_track.peer_quality` 取值即该点），占位默认 "
                f"{PLACEHOLDER_DEFAULTS} 仅作 sweep 起点；不得表述为「DoD 已稳健成立」"
            )
    lines.append("")
    return lines


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> None:
    global CACHE_DIR
    parser = argparse.ArgumentParser(description="对标组判定策略回归（设计 §6 Step 0/Step 2）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--record", action="store_true", help="调 LLM 并写缓存（否则读缓存）")
    parser.add_argument("--repeat", type=int, default=1, help="重复采集次数（稳定性，需 --record）")
    parser.add_argument(
        "--cache-root",
        default=str(CACHE_DIR),
        help="缓存世代根目录（默认 data/peer_eval_cache；新 prompt 世代重采时用新目录，历史世代文件字节保持原样）",
    )
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
        default=["B_single", "C_dims", "C_D_judge", "C_D_E"],
        help="稳定性对照策略（默认 B_single=自评口径、C_dims=生成器维度口径，"
        "C_D_judge/C_D_E=judge 策略（二者保留集恒等））",
    )
    parser.add_argument("--out", default=str(REPORT_PATH))
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="不写 outputs/peer_group_eval/baseline_run/<ts>/ 版本化留档（默认写）",
    )
    args = parser.parse_args()

    CACHE_DIR = Path(args.cache_root)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
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
    sweep_points = (
        calibration_points(cases, decisions, tuple(args.strategies), calibrate_split=args.calibrate_split)
        if args.sweep
        else {}
    )
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
    dod = (
        dod_check(
            cases,
            decisions,
            calibrate_split=args.calibrate_split,
            stability_rows=stability_rows,
        )
        if args.sweep
        else None
    )
    recall_audit = taxonomy_recall_audit(cases)
    confidence = confidence_recompute(cases, decisions)
    lineage = [cache_lineage_audit(cases, root=CACHE_DIR, label="现行世代（评测用）")]
    if LEGACY_CACHE_DIR != CACHE_DIR and LEGACY_CACHE_DIR.is_dir():
        lineage.append(
            cache_lineage_audit(cases, root=LEGACY_CACHE_DIR, label="历史世代（Step 0 旧 judge prompt，只读血缘证据）")
        )

    report = render_report(
        cases,
        results,
        taxonomy,
        sweep_best,
        stability_rows,
        stability_strategies=tuple(args.stability_strategies),
        repeat=repeat,
        sweep_points=sweep_points,
        dod=dod,
        lineage=lineage,
        generated_at=generated_at,
        recall_audit=recall_audit,
        confidence=confidence,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    if not args.no_archive:
        archive = archive_run(
            report,
            meta={
                "generated_at": generated_at,
                "cache_root": str(CACHE_DIR),
                "labels": {
                    "path": str(Path(args.cases).relative_to(PROJECT_ROOT)),
                    "sha256": sha256_of(Path(args.cases)),
                },
                "caches": _cache_hashes(cases),
                "params": {
                    "sweep": bool(args.sweep),
                    "calibrate_split": args.calibrate_split,
                    "repeat": repeat,
                    "strategies": list(args.strategies),
                    "stability_strategies": list(args.stability_strategies),
                    "record": bool(args.record),
                },
                "sweep_points": {
                    name: (point.get("params") if point else None) for name, point in sweep_points.items()
                },
                "dod": {
                    "f1_ok_at_calibrated": None if dod is None else dod["f1_ok_at_calibrated"],
                    "f1_ok_at_placeholder": None if dod is None else dod["f1_ok_at_placeholder"],
                    "stability_ok": None if dod is None else dod["stability_ok"],
                },
                "lineage_mismatches": [item for audit in lineage for item in (audit.get("mismatches") or [])],
                "e_recall": {
                    "means": recall_audit["means"],
                    "cases": recall_audit["cases"],
                },
                "peer_confidence": {
                    "distribution": confidence["distribution"],
                    "deterministic": confidence["deterministic"],
                    "cases_with_peers": confidence["cases_with_peers"],
                },
                **git_fingerprint(),
            },
        )
        print(f"archive → {archive['dir']}")

    for name, result in results.items():
        row = segment_metrics(result, "all")
        hold = segment_metrics(result, "holdout", exclude_disputed=True)
        counts = judge_status_counts(result)
        print(
            f"[{name}] all: precision={row['precision']:.3f} recall={row['recall']:.3f} f1={row['f1']:.3f}"
            f" | holdout(excl disputed): f1={hold['f1']:.3f} | judge applied/degraded={counts['applied']}/{counts['degraded']}"
        )
    if sweep_best:
        row = segment_metrics(sweep_best["result"], "holdout", exclude_disputed=True)
        print(
            f"[sweep] best={sweep_best['params']} {args.calibrate_split}_f1={sweep_best['score']:.3f} "
            f"holdout_f1={row['f1']:.3f}"
        )
    if dod:
        print(
            f"[DoD] f1_ok@calibrated={dod['f1_ok_at_calibrated']} "
            f"f1_ok@placeholder={dod['f1_ok_at_placeholder']} stability_ok={dod['stability_ok']}"
        )
    for strategy in args.stability_strategies:
        by_split = stability_by_split(stability_rows, strategy)
        if stability_rows:
            unassessable = stability_unassessable_cases(stability_rows, strategy)
            print(
                f"[stability:{strategy}] train={by_split['train']:.3f} "
                f"holdout={by_split['holdout']:.3f} all={by_split['all']:.3f} "
                f"unassessable={len(unassessable)}"
            )
    means = recall_audit["means"]
    print(
        f"[E 召回] llm_only={means['coverage_llm_only']:.3f} with_taxonomy={means['coverage_with_taxonomy']:.3f} "
        f"production_pool={means['coverage_production_pool']:.3f}"
    )
    print(
        f"[置信度] 分布={confidence['distribution']} 确定性={confidence['deterministic']} "
        f"cases={confidence['cases_with_peers']}"
    )
    print(f"report → {out_path}")


def _cache_hashes(cases: list[EvalCase]) -> dict[str, Any]:
    """缓存世代逐文件 sha256 + 采集血缘字段（留档用；只读，不改写任何缓存字节）。"""
    out: dict[str, Any] = {}
    for path in sorted(CACHE_DIR.rglob("*.json")):
        if path.stem.upper() not in {case.symbol for case in cases}:
            continue
        entry: dict[str, Any] = {"sha256": sha256_of(path)}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out[str(path)] = {**entry, "unreadable": True}
            continue
        if isinstance(raw, dict):
            entry.update(
                {"split": raw.get("split"), "segment": raw.get("segment"), "recorded_at": raw.get("recorded_at")}
            )
        out[_display_path(path)] = entry
    return out


if __name__ == "__main__":
    main()
    sys.exit(0)
