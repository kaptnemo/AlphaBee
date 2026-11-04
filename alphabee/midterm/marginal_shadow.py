"""F 边际改善影子期观测（设计 ``MIDTERM_F_MARGINAL_DESIGN`` §5.2 / §7.1 G-8 / §9 PR-3）。

本模块只做**离线影子观测**：读取历史帧（artifact 落盘文件），聚合 S1–S4 指标并给出
G-8 门禁判读，供"是否可以把 ``f_marginal_mode`` 从 ``shadow`` 改为 ``on``"这一决策留痕。

纪律（``alphabee-schema-steward`` / 纯函数优先）：

- **只有 :func:`load_frames` 做 I/O**，且坏文件/坏行只计数不抛；:func:`frame_stats` /
  :func:`shadow_report` / :func:`dedupe_frames` 是纯函数（不读文件、不读配置、不联网、
  不修改入参）。
- **分类逻辑单一来源**：状态翻转不复制状态机公式，而是把帧内 ``variable_scores`` 的 F
  换成 ``f_shadow`` 后调用 :func:`alphabee.midterm.classifier_dispatch.classify_state`，
  并沿用帧内记录的 ``classifier_formula`` / ``classifier_order``（旧帧缺省 v1 / etf）。
- **缺失显式 None**：无 ``factor_snapshot.fundamental`` / 无 ``score_breakdown`` / 无
  ``f_shadow`` ⇒ 对应指标 ``None``，并在报告里同时给出分母（``n_*``），绝不静默回退 0。

指标口径（§5.2 / P0-3 Q1 降级）：

- **S1 ΔF 分布**：``delta_f = f_shadow − f``；报告取 ``|ΔF|`` 的中位数与 P90。
  分位用**线性插值**（与 ``numpy.percentile`` 默认口径一致；n=1 直接返回该值），
  分母 = 同时具备 ``f`` 与 ``f_shadow`` 的帧。**G-8 判读用 ex-Q1 中位数**
  （``median_abs_delta_ex_q1``）：Q1 的 ``Δ`` 结构性为 0（修复前还会是 ``−0.3L``），
  不应把结构性/污染值计入 S1；旧字段 ``abs_delta_f_median`` 保留作对照。
- **S2 状态翻转率**：``argmax(F_shadow) ≠ argmax(F)`` 的帧占比；
  分母 = 翻转**可判定**的帧（既有帧内 argmax，又能算出影子 argmax）。
- **S3 危险翻转率**：兜底口径 ``现判 S5 而影子判到 {S1,S2,S3}`` 的帧占比；
  分母与 S2 相同（口径并列可比），而非"现判 S5 的帧"——这样 S2/S3 的分母一致。
- **S4 加速度覆盖率**：``f_marginal_source > 0`` 的帧占比；
  分母 = **有 ``factor_snapshot.fundamental``** 的帧（数据可用性的现实检验）。
  **G-8 判读用 ex-Q1 覆盖率**（``coverage_ex_q1``）：Q1 帧由设计降级为无边际键，
  不应计入「加速度覆盖率」分母；旧字段 ``coverage`` 保留作对照。
- **S5–S8（P2-3 扩展，设计 §15.1）**：S5 OCF 命中率（按 ``ocf_penalty_basis`` /
  报告季度分组；行业字段不可得 ⇒ 显式不分组）、S6 beat/miss 同期率（分母 = 可判定帧）、
  S7 扣非差异分布（``np_term_recurring`` vs ``net_profit_term`` 的 n/median/P90/缺失）、
  S8 分部分布（bonus 触发率 / 期间错配率 / 占比门槛率）。均为 additived 新键，缺失/旧帧
  显式 ``None``，不改变 S1–S4 既有键与口径。

帧的 Q1 判定（P0-3）：``factor_snapshot.fundamental.marginal_period`` 优先，
缺失回落 ``report_period``；两者都缺失（旧帧/手工帧）⇒ ``is_q1=None``（无法判定，
保守计入 ex-Q1 分母，不获得 Q1 豁免）。

G-8 门槛（§7.1，全部满足才允许 PR-4 开 ``on``）：真实帧 ≥ 50、``median|ΔF|`` ≤ 0.10、
翻转率 ≤ 10%、危险翻转率 ≤ 3%、覆盖率 ≥ 85%。空语料/无影子键 ⇒ 各 ``*_ok`` 均为
``False``、``g8_pass`` 为 ``False``（保守，不放行）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from alphabee.midterm.classifier_dispatch import classify_state
from alphabee.midterm.models import VariableScores

# ─────────────────────────────────────────────────────────────────────────────
# G-8 门槛（设计 §7.1）与指标常量
# ─────────────────────────────────────────────────────────────────────────────

G8_MIN_FRAMES = 50  # 真实帧下限（影子期语料门槛）
G8_MAX_ABS_DELTA_F_MEDIAN = 0.10  # |F_shadow − F| 中位数上限
G8_MAX_FLIP_RATE = 0.10  # 状态翻转率上限
G8_MAX_DANGEROUS_FLIP_RATE = 0.03  # S5 → {S1,S2,S3} 危险翻转率上限
G8_MIN_COVERAGE = 0.85  # 加速度覆盖率下限

#: 危险翻转的落点（现行 S5 = 已证伪/退出，被影子判成仍在确认/加速 ⇒ 抹掉证伪信号）。
_DANGEROUS_SHADOW_STATES: frozenset[str] = frozenset({"S1", "S2", "S3"})

#: 帧去重口径（G-8 的「一帧」= 一个 symbol-day，与 tracking 帧「一日一帧」对齐）。
DEDUPE_SYMBOL_DAY = "symbol-day"
DEDUPE_NONE = "none"
KNOWN_DEDUPE_SCOPES: tuple[str, ...] = (DEDUPE_SYMBOL_DAY, DEDUPE_NONE)

# 帧内分类器口径缺省（旧帧无这两个字段 ⇒ 与产物落盘前的实际口径一致）。
_DEFAULT_CLASSIFIER_ORDER = "etf"
_DEFAULT_CLASSIFIER_FORMULA = "v1"

#: 可注入的分类函数签名（测试替身用；生产默认 :func:`classifier_dispatch.classify_state`）。
ClassifyFn = Callable[..., Any]


# ─────────────────────────────────────────────────────────────────────────────
# 值解析（缺失显式 None；不静默回退 0）
# ─────────────────────────────────────────────────────────────────────────────


def _as_float(value: Any) -> float | None:
    """int/float/数字字符串 → float；bool/NaN/空/非数值 → ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _as_int(value: Any) -> int | None:
    """同 :func:`_as_float`，整数化（用于 ``f_marginal_source`` 位掩码）。"""
    number = _as_float(value)
    return int(number) if number is not None else None


def _as_period(value: Any) -> str | None:
    """报告期归一为 ``YYYYMMDD``（只取数字前 8 位）；缺失/非法 → ``None``。

    与 ``score_engine._normalize_ymd`` / ``factors._normalize_period`` 同口径的轻量版
    （本模块保持只依赖 models，不反向导入评分层/数据层）：``"20260331"`` /
    ``"2026-03-31"`` 都可判定；``None`` / 空串 / 非 8 位数字 → ``None``（无法判定）。
    """
    if value is None or isinstance(value, bool):
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else None


def _quantile(values: list[float], q: float) -> float | None:
    """线性插值分位（``q ∈ [0, 1]``；与 ``numpy.percentile`` 默认口径一致）。

    空列表 → ``None``；n=1 → 该值；其余在 ``q·(n−1)`` 位置做相邻两点线性插值。
    选择线性插值而非 nearest-rank：``q=0.5`` 与标准中位数（偶数个取中间两值均值）
    完全一致，这样 S1 的「中位数」与「P90」同源，报告内部不会出现两套分位口径。
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


# ─────────────────────────────────────────────────────────────────────────────
# 帧加载（唯一的 I/O；坏文件/坏行只计数不抛）
# ─────────────────────────────────────────────────────────────────────────────


def load_frames(paths: str | Path | Iterable[str | Path]) -> tuple[list[dict[str, Any]], int]:
    """读取历史帧文件，返回 ``(frames, skipped)``；坏文件/坏行**跳过并计数**，绝不抛。

    兼容三种落盘形态（本仓两种历史形态并存）：

    1. **整文件为单个 JSON 对象**（``data/midterm/state/603259.SH.jsonl`` 就是 pretty-print
       的整文件对象，290 行仍是一帧）；
    2. **逐行 JSONL**（每行一个紧凑 JSON 对象，如 ``300760.SZ.jsonl``）；
    3. JSON 数组（附加容忍，数组内非对象元素计入 ``skipped``）。

    目录入参 ⇒ 按文件名排序展开其中的 ``*.jsonl`` / ``*.json``（确定性输出）。
    整文件解析失败时**不会丢弃整份文件**：继续逐行解析，能读出的行照常保留，
    读不出的行计入 ``skipped``；文件读取失败按 1 次 ``skipped`` 计。
    """
    frames: list[dict[str, Any]] = []
    skipped = 0
    for path in _expand_paths(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1  # 文件级失败：计 1 次，继续读下一个（不中断整批）
            continue
        file_frames, file_skipped = _parse_text(text)
        frames.extend(file_frames)
        skipped += file_skipped
    return frames, skipped


def dedupe_frames(
    frames: Iterable[dict[str, Any]],
    *,
    scope: str = DEDUPE_SYMBOL_DAY,
) -> tuple[list[dict[str, Any]], int]:
    """按口径去重帧（纯函数，保持首次出现顺序）；返回 ``(frames, dropped)``。

    - ``scope="symbol-day"``（默认）：同一 ``(symbol, as_of_date)`` 只保留**首帧**。
      与 tracking 帧「一日一帧」口径对齐，避免同一标的同日多跑几次查询把
      G-8 的分母放大成"跑了几次"。
    - ``scope="none"``：原样返回（``dropped=0``）。
    - 缺 ``symbol`` 或 ``as_of_date`` 的帧**不参与去重**（无法判定同日，保留而不静默丢）。
    - 非法 ``scope`` → ``ValueError``（调用方参数错误，不静默降级）。

    跨源合并时把**更可信/字段更全的源排在前面**（如 state 帧先于 task_records 帧），
    "保留首帧"即等价于"优先保留该源"。
    """
    if scope not in KNOWN_DEDUPE_SCOPES:
        raise ValueError(f"未知去重口径: {scope!r}（可选 {KNOWN_DEDUPE_SCOPES}）")
    if scope == DEDUPE_NONE:
        return list(frames), 0

    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for frame in frames:
        symbol = str(frame.get("symbol") or "").strip()
        as_of = str(frame.get("as_of_date") or "").strip()
        if not symbol or not as_of:
            kept.append(frame)  # 无法判定同日 → 不去重
            continue
        key = (symbol, as_of)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(frame)
    return kept, dropped


def _expand_paths(paths: str | Path | Iterable[str | Path]) -> list[Path]:
    """把文件/目录/列表归一为有序文件列表（目录展开为 ``*.jsonl`` + ``*.json``）。"""
    if isinstance(paths, (str, Path)):
        candidates: list[str | Path] = [paths]
    else:
        candidates = list(paths)
    expanded: list[Path] = []
    for candidate in candidates:
        path = Path(candidate)
        if path.is_dir():
            expanded.extend(sorted(p for p in path.glob("*.jsonl") if p.is_file()))
            expanded.extend(sorted(p for p in path.glob("*.json") if p.is_file()))
        else:
            expanded.append(path)
    return expanded


_UNPARSED = object()


def _parse_text(text: str) -> tuple[list[dict[str, Any]], int]:
    """整文件优先、逐行回退的解析（返回 ``(frames, skipped)``）。"""
    stripped = text.strip()
    if not stripped:
        return [], 0

    payload: Any = _UNPARSED
    try:
        payload = json.loads(stripped)
    except ValueError:
        payload = _UNPARSED

    if payload is not _UNPARSED:
        if isinstance(payload, dict):
            return [payload], 0
        if isinstance(payload, list):
            array_frames = [item for item in payload if isinstance(item, dict)]
            return array_frames, len(payload) - len(array_frames)
        return [], 1  # 标量 JSON（不是帧）

    frames: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(item, dict):
            frames.append(item)
        else:
            skipped += 1
    return frames, skipped


# ─────────────────────────────────────────────────────────────────────────────
# 单帧统计与报告聚合（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def _fundamental_of(frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """取 ``factor_snapshot.fundamental``（缺失/类型不对 → ``None``）。"""
    snapshot = frame.get("factor_snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    fundamental = snapshot.get("fundamental")
    return fundamental if isinstance(fundamental, Mapping) else None


def _breakdown_of(fundamental: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """取 ``fundamental.score_breakdown``（旧帧无该键 → 空 dict）。"""
    if fundamental is None:
        return {}
    breakdown = fundamental.get("score_breakdown")
    return breakdown if isinstance(breakdown, Mapping) else {}


def _frame_state(frame: Mapping[str, Any]) -> str | None:
    """帧内记录的当前认知状态 argmax（旧帧/缺字段 → ``None``）。"""
    state = frame.get("state")
    if not isinstance(state, Mapping):
        return None
    value = state.get("argmax_state")
    return str(value) if value else None


def _frame_classifier_order(frame: Mapping[str, Any]) -> str:
    value = str(frame.get("classifier_order") or "").strip().lower()
    return value or _DEFAULT_CLASSIFIER_ORDER


def _frame_classifier_formula(frame: Mapping[str, Any]) -> str:
    value = str(frame.get("classifier_formula") or "").strip().lower()
    return value or _DEFAULT_CLASSIFIER_FORMULA


def _shadow_state(
    frame: Mapping[str, Any],
    f_shadow: float,
    classify: ClassifyFn,
) -> str | None:
    """把帧内 F 换成 ``f_shadow`` 后重算 argmax（分类逻辑复用 ``classify_state``）。

    单帧分类失败（坏 ``variable_scores`` / 数据形状不认识）⇒ ``None``（"无法判定"），
    不让一帧坏数据中断整批观测。
    """
    raw_scores = frame.get("variable_scores")
    if not isinstance(raw_scores, Mapping):
        return None
    try:
        scores = VariableScores.model_validate(raw_scores)
        shadow_scores = scores.model_copy(update={"f_fundamental_trend": f_shadow})
        result = classify(
            shadow_scores,
            order=_frame_classifier_order(frame),
            formula=_frame_classifier_formula(frame),
        )
    except Exception:  # noqa: BLE001 - 单帧失败只降级为「无法判定」，不影响整批
        return None
    state = getattr(getattr(result, "state", None), "argmax_state", None)
    return str(state) if state else None


def frame_stats(frame: Mapping[str, Any], *, classify: ClassifyFn | None = None) -> dict[str, Any]:
    """单帧影子观测：``ΔF`` / 状态翻转 / 危险翻转（纯函数）。

    Args:
        frame: 一帧 artifact（``CompanyStateArtifact.model_dump()`` 的 dict）。
        classify: 分类函数（默认 :func:`classifier_dispatch.classify_state`）；
            仅测试注入用，生产路径必须走分类器单一来源。

    Returns:
        dict（缺失显式 ``None``）：

        - ``has_fundamental``：帧内是否有 ``factor_snapshot.fundamental``。
        - ``report_period``：``fundamental.report_period`` 归一后的 ``YYYYMMDD``
          （旧帧/缺失 → ``None``；P0-3 起随帧落盘）。
        - ``is_q1``：是否 Q1 帧（``marginal_period`` 优先、缺失回落 ``report_period``；
          两者都缺失 → ``None``，不猜测、不获得 Q1 豁免）。
        - ``f`` / ``f_shadow`` / ``delta_f``：分解里的 ``f`` / ``f_shadow`` 及其差。
        - ``f_marginal_source``：可用腿位掩码（0 = 降级；旧帧无键 → ``None``）。
        - ``current_state``：帧内 ``state.argmax_state``。
        - ``shadow_state``：把 F 换成 ``f_shadow`` 后重算的 argmax（无法判定 → ``None``）。
        - ``flipped``：``shadow_state != current_state``（无法判定 → ``None``）。
        - ``dangerous_flip``：现判 S5 而影子判到 {S1,S2,S3}（无法判定 → ``None``）。
    """
    stats: dict[str, Any] = {
        "has_fundamental": False,
        "report_period": None,
        "is_q1": None,
        "f": None,
        "f_shadow": None,
        "delta_f": None,
        "f_marginal_source": None,
        "current_state": None,
        "shadow_state": None,
        "flipped": None,
        "dangerous_flip": None,
    }
    if not isinstance(frame, Mapping):
        return stats

    fundamental = _fundamental_of(frame)
    breakdown = _breakdown_of(fundamental)
    f = _as_float(breakdown.get("f"))
    f_shadow = _as_float(breakdown.get("f_shadow"))

    stats["has_fundamental"] = fundamental is not None
    if fundamental is not None:
        report_period = _as_period(fundamental.get("report_period"))
        # P0-3：单季还原目标期优先（marginal_period），缺失回落 report_period；都缺 ⇒ 无法判定。
        marginal_period = _as_period(fundamental.get("marginal_period")) or report_period
        stats["report_period"] = report_period
        stats["is_q1"] = marginal_period.endswith("0331") if marginal_period else None
    stats["f"] = f
    stats["f_shadow"] = f_shadow
    stats["delta_f"] = (f_shadow - f) if (f is not None and f_shadow is not None) else None
    stats["f_marginal_source"] = _as_int(breakdown.get("f_marginal_source"))
    stats["current_state"] = _frame_state(frame)

    if f_shadow is not None:
        stats["shadow_state"] = _shadow_state(frame, f_shadow, classify or classify_state)
    if stats["current_state"] is not None and stats["shadow_state"] is not None:
        stats["flipped"] = stats["shadow_state"] != stats["current_state"]
        stats["dangerous_flip"] = stats["current_state"] == "S5" and stats["shadow_state"] in _DANGEROUS_SHADOW_STATES
    return stats


def _ratio(numerator: int, denominator: int) -> float | None:
    """比例（分母 0 → ``None``，不制造 0/0 假象）。"""
    return (numerator / denominator) if denominator else None


def _quarter_label(period: str | None) -> str | None:
    """报告期后缀 → ``YYYYQn``；缺失/未知后缀 → ``None``（不猜测季度）。"""
    if period is None or len(period) < 8:
        return None
    suffix = period[4:8]
    quarter = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}.get(suffix)
    return f"{period[:4]}Q{quarter}" if quarter else None


def s5_ocf_hit_rate(frames: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """S5 OCF 惩罚命中率（设计 §15.1）：分母 = 可判定帧（分解含 ``ocf_penalty``）。

    - ``overall``：全部可判定帧的命中率；命中 = ``|ocf_penalty| > 0``；
    - ``by_basis``：按 ``ocf_penalty_basis`` 分组（1 = ``ttm`` / 0 = ``sign``·回退）；
    - ``by_quarter``：按 ``report_period`` 后缀分季度（Q1–Q4）；
    - ``by_industry``：**artifact 无行业字段 ⇒ 恒 ``None``**（不编造分组）。
    """
    n_frames = 0
    with_ocf = 0
    hits = 0
    basis_counts: dict[str, list[int]] = {"ttm": [0, 0], "sign": [0, 0]}
    basis_missing = 0
    quarter_counts: dict[str, list[int]] = {}
    for frame in frames:
        n_frames += 1
        fundamental = _fundamental_of(frame)
        breakdown = _breakdown_of(fundamental)
        penalty = _as_float(breakdown.get("ocf_penalty"))
        if penalty is None:
            continue
        with_ocf += 1
        hit = abs(penalty) > 1e-12
        hits += 1 if hit else 0
        basis = _as_int(breakdown.get("ocf_penalty_basis"))
        if basis == 1:
            basis_counts["ttm"][0] += 1
            basis_counts["ttm"][1] += 1 if hit else 0
        elif basis == 0:
            basis_counts["sign"][0] += 1
            basis_counts["sign"][1] += 1 if hit else 0
        else:
            basis_missing += 1
        if fundamental is not None:
            quarter = _quarter_label(_as_period(fundamental.get("report_period")))
            if quarter:
                bucket = quarter_counts.setdefault(quarter, [0, 0])
                bucket[0] += 1
                bucket[1] += 1 if hit else 0
    by_basis = {
        key: {"n": counts[0], "hits": counts[1], "rate": _ratio(counts[1], counts[0])}
        for key, counts in basis_counts.items()
    }
    by_quarter = {
        key: {"n": counts[0], "hits": counts[1], "rate": _ratio(counts[1], counts[0])}
        for key, counts in sorted(quarter_counts.items())
    }
    return {
        "n_frames": n_frames,
        "n_judgeable": with_ocf,
        "n_hits": hits,
        "rate": _ratio(hits, with_ocf),
        "by_basis": by_basis,
        "n_basis_missing": basis_missing,
        "by_quarter": by_quarter,
        "by_industry": None,  # artifact 无行业字段：显式不分组（不得编造）
        "industry_note": "artifact 无行业字段，未做行业分组",
    }


def s6_beat_miss_match(frames: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """S6 beat/miss 同期率（设计 §15.1）：

    - 分母 ``n_judgeable`` = 分解含 ``forecast_period_matched`` 的帧（可判定）；
    - 分子 ``n_matched`` = 其中 ``forecast_period_matched == 1`` 的帧；
    - ``n_missing`` = 有分解但缺该键的帧（旧帧，显式缺失、不进分母）。
    """
    n_judgeable = 0
    n_matched = 0
    n_missing = 0
    for frame in frames:
        breakdown = _breakdown_of(_fundamental_of(frame))
        if not breakdown:
            continue
        value = _as_int(breakdown.get("forecast_period_matched"))
        if value is None:
            n_missing += 1
            continue
        n_judgeable += 1
        n_matched += 1 if value == 1 else 0
    return {
        "n_judgeable": n_judgeable,
        "n_matched": n_matched,
        "n_missing": n_missing,
        "rate": _ratio(n_matched, n_judgeable),
    }


def _distribution(values: list[float], missing: int) -> dict[str, Any]:
    """分布摘要：n / median / P90 / 缺失计数（空值 ⇒ median/p90 为 None）。"""
    return {
        "n": len(values),
        "median": _quantile(values, 0.5),
        "p90": _quantile(values, 0.9),
        "missing": missing,
    }


def s7_recurring_gap(frames: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """S7 扣非差异（设计 §15.1）：``np_term_recurring`` / 归母净利项 / 两者差的分布。

    分母 = 有 ``score_breakdown`` 的帧；各自缺失计数单列（旧帧无 recurring 键 ⇒
    ``np_term_recurring.missing == n_frames``，不把它当 0）。``f_recurring_mode=on``
    的前置是 S7 可解释（设计 §15.2），故同时给出两者之差 ``diff``。
    """
    frame_list = list(frames)
    np_terms: list[float] = []
    parent_terms: list[float] = []
    diffs: list[float] = []
    n_breakdown = 0
    for frame in frame_list:
        breakdown = _breakdown_of(_fundamental_of(frame))
        if not breakdown:
            continue
        n_breakdown += 1
        np_term = _as_float(breakdown.get("np_term_recurring"))
        parent_term = _as_float(breakdown.get("net_profit_term"))
        if np_term is not None:
            np_terms.append(np_term)
        if parent_term is not None:
            parent_terms.append(parent_term)
        if np_term is not None and parent_term is not None:
            diffs.append(np_term - parent_term)
    return {
        "n_frames": len(frame_list),
        "n_with_breakdown": n_breakdown,
        "np_term_recurring": _distribution(np_terms, n_breakdown - len(np_terms)),
        "net_profit_term": _distribution(parent_terms, n_breakdown - len(parent_terms)),
        "diff": _distribution(diffs, n_breakdown - len(diffs)),
    }


def s8_segment(frames: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """S8 分部分布（设计 §15.1）：触发率 / 期间错配率 / 占比门槛率。

    每个比率的分母 = 分解含对应键的帧（可判定）；缺失计数单列。命中定义：
    ``segment_bonus > 0``（触发）、``segment_period_mismatch == 1``、``segment_share_below_min == 1``。
    """
    frame_list = list(frames)
    bonus_n = bonus_hits = 0
    mismatch_n = mismatch_hits = 0
    share_n = share_hits = 0
    for frame in frame_list:
        breakdown = _breakdown_of(_fundamental_of(frame))
        if not breakdown:
            continue
        bonus = _as_float(breakdown.get("segment_bonus"))
        if bonus is not None:
            bonus_n += 1
            bonus_hits += 1 if bonus > 0 else 0
        mismatch = _as_int(breakdown.get("segment_period_mismatch"))
        if mismatch is not None:
            mismatch_n += 1
            mismatch_hits += 1 if mismatch == 1 else 0
        share = _as_int(breakdown.get("segment_share_below_min"))
        if share is not None:
            share_n += 1
            share_hits += 1 if share == 1 else 0
    return {
        "n_frames": len(frame_list),
        "segment_bonus": {
            "n_judgeable": bonus_n,
            "n_triggered": bonus_hits,
            "rate": _ratio(bonus_hits, bonus_n),
            "missing": len(frame_list) - bonus_n,
        },
        "segment_period_mismatch": {
            "n_judgeable": mismatch_n,
            "n_mismatched": mismatch_hits,
            "rate": _ratio(mismatch_hits, mismatch_n),
            "missing": len(frame_list) - mismatch_n,
        },
        "segment_share_below_min": {
            "n_judgeable": share_n,
            "n_below_min": share_hits,
            "rate": _ratio(share_hits, share_n),
            "missing": len(frame_list) - share_n,
        },
    }


def shadow_report(frames: Iterable[Mapping[str, Any]], *, classify: ClassifyFn | None = None) -> dict[str, Any]:
    """聚合影子期指标 + G-8 判读（纯函数；空输入返回全 0/None，``g8_pass=False``）。

    ``ex-Q1`` 口径（P0-3）：Q1 帧的加速度结构性为 0（修复前还有 ``−0.3L`` 污染），
    因此 G-8 的 S1（``median_ok``）与 S4（``coverage_ok``）使用排除 Q1 后的指标；
    旧口径字段（``coverage`` / ``abs_delta_f_median`` / ``abs_delta_f_p90`` 等）保留作对照。

    Returns:
        dict：``n_frames`` / ``n_fundamental_frames`` / ``n_with_marginal`` / ``coverage`` /
        ``abs_delta_f_median`` / ``abs_delta_f_p90`` / ``flip_rate`` / ``dangerous_flip_rate`` /
        各分母计数；P0-3 追加 ``n_q1_frames`` / ``n_fundamental_frames_ex_q1`` /
        ``n_with_marginal_ex_q1`` / ``n_delta_frames_ex_q1`` / ``coverage_ex_q1`` /
        ``median_abs_delta_ex_q1``；以及 G-8 判读（``n_frames_ok`` / ``median_ok`` /
        ``flip_ok`` / ``dangerous_ok`` / ``coverage_ok`` / ``g8_pass``）。
    """
    frame_list = list(frames)
    stats = [frame_stats(frame, classify=classify) for frame in frame_list]

    n_frames = len(stats)
    n_fundamental_frames = sum(1 for item in stats if item["has_fundamental"])
    n_q1_frames = sum(1 for item in stats if item["is_q1"] is True)
    fundamental_ex_q1 = [item for item in stats if item["has_fundamental"] and item["is_q1"] is not True]
    n_with_marginal = sum(1 for item in stats if (item["f_marginal_source"] or 0) > 0)
    n_with_marginal_ex_q1 = sum(1 for item in fundamental_ex_q1 if (item["f_marginal_source"] or 0) > 0)
    abs_deltas = [abs(item["delta_f"]) for item in stats if item["delta_f"] is not None]
    abs_deltas_ex_q1 = [
        abs(item["delta_f"]) for item in stats if item["delta_f"] is not None and item["is_q1"] is not True
    ]
    flip_frames = [item for item in stats if item["flipped"] is not None]
    n_flips = sum(1 for item in flip_frames if item["flipped"])
    n_dangerous_flips = sum(1 for item in flip_frames if item["dangerous_flip"])

    coverage = (n_with_marginal / n_fundamental_frames) if n_fundamental_frames else None
    coverage_ex_q1 = (n_with_marginal_ex_q1 / len(fundamental_ex_q1)) if fundamental_ex_q1 else None
    flip_rate = (n_flips / len(flip_frames)) if flip_frames else None
    dangerous_flip_rate = (n_dangerous_flips / len(flip_frames)) if flip_frames else None
    abs_delta_f_median = _quantile(abs_deltas, 0.5)
    abs_delta_f_p90 = _quantile(abs_deltas, 0.9)
    median_abs_delta_ex_q1 = _quantile(abs_deltas_ex_q1, 0.5)

    n_frames_ok = n_frames >= G8_MIN_FRAMES
    # G-8 的 S1/S4 用 ex-Q1 口径（Q1 的 Δ 结构性为 0，不参与加速度质量门）。
    median_ok = median_abs_delta_ex_q1 is not None and median_abs_delta_ex_q1 <= G8_MAX_ABS_DELTA_F_MEDIAN
    flip_ok = flip_rate is not None and flip_rate <= G8_MAX_FLIP_RATE
    dangerous_ok = dangerous_flip_rate is not None and dangerous_flip_rate <= G8_MAX_DANGEROUS_FLIP_RATE
    coverage_ok = coverage_ex_q1 is not None and coverage_ex_q1 >= G8_MIN_COVERAGE

    return {
        "n_frames": n_frames,
        "n_fundamental_frames": n_fundamental_frames,
        "n_q1_frames": n_q1_frames,
        "n_fundamental_frames_ex_q1": len(fundamental_ex_q1),
        "n_with_marginal": n_with_marginal,
        "n_with_marginal_ex_q1": n_with_marginal_ex_q1,
        "n_delta_frames": len(abs_deltas),
        "n_delta_frames_ex_q1": len(abs_deltas_ex_q1),
        "n_flip_frames": len(flip_frames),
        "n_flips": n_flips,
        "n_dangerous_flips": n_dangerous_flips,
        "coverage": coverage,
        "coverage_ex_q1": coverage_ex_q1,
        "abs_delta_f_median": abs_delta_f_median,
        "abs_delta_f_p90": abs_delta_f_p90,
        "median_abs_delta_ex_q1": median_abs_delta_ex_q1,
        "flip_rate": flip_rate,
        "dangerous_flip_rate": dangerous_flip_rate,
        "n_frames_ok": n_frames_ok,
        "median_ok": median_ok,
        "flip_ok": flip_ok,
        "dangerous_ok": dangerous_ok,
        "coverage_ok": coverage_ok,
        "g8_pass": bool(n_frames_ok and median_ok and flip_ok and dangerous_ok and coverage_ok),
        # P2-3（设计 §15.1）：S5–S8 扩展观测（additive 新键；缺失/旧帧显式 None）。
        "s5_ocf_hit_rate": s5_ocf_hit_rate(frame_list),
        "s6_beat_miss_match": s6_beat_miss_match(frame_list),
        "s7_recurring_gap": s7_recurring_gap(frame_list),
        "s8_segment": s8_segment(frame_list),
    }
