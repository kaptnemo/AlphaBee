"""State diff 引擎：``CompanyStateArtifact → CompanyStateDiff``（design MIDTERM_STATE_DIFF_DESIGN.md）。

把同一标的两个时间点的 :class:`~alphabee.midterm.models.CompanyStateArtifact` 差分成
五层分层变化（L1 数据 → L2 评分 → L3 状态/置信 → L4 赔率 → L5 仓位）+ 归因 +
边界 + 退出检查。

核心纪律（design §12）：

- **数值核心全纯规则，禁 LLM**：字段差 / rel_delta / TV 距离 / 熵差 / log-odds /
  EV 差 / 权重差 / 速度 全部确定性计算，本模块不调任何 LLM；
- **canonical 字段对齐**（alphabee-schema-steward）：只在同名同口径字段间求差，
  字段清单复用 ``factors.py`` 的 ``_*_FIELDS`` 元组；
- **缺失显式 ``None``**：``None→值`` = appeared、``值→None`` = disappeared，与
  up / down 严格区分，绝不静默回退 0；
- **引用不内嵌**：diff 只存 ``prev/curr/anchor`` 的 :class:`ArtifactRef` 引用，
  不复制整帧（反漂移）。

本文件按 D2 分步落地：当前实现步骤 0/1（校验 + 首帧基线登记）、步骤 2（L1 因子
差）、步骤 3（L2 评分差）、步骤 4（L3 状态漂移）、步骤 5（L3' 置信度差）、步骤 6
（L4 赔率差）；步骤 7–11（仓位 / 证据 / 归因 / 退出检查 / 边界）由后续 D2 步骤
补齐，未实现的层保持 typed contract 缺省值。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from alphabee.midterm.bayes import _logit
from alphabee.midterm.classifier import _BACKWARD, _FORWARD, _REOPEN, _STATES, _drift
from alphabee.midterm.factors import (
    _AUDIT_FIELDS,
    _CROWDING_FIELDS,
    _EXPECTATION_FIELDS,
    _FUNDAMENTAL_FIELDS,
    _MARKET_FIELDS,
    _RISK_NUMERIC_FIELDS,
    _TREND_FIELDS,
    _VALUATION_FIELDS,
)
from alphabee.midterm.models import (
    ArtifactRef,
    CompanyStateArtifact,
    CompanyStateDiff,
    ConfidenceDelta,
    EVDiff,
    FactorDelta,
    FactorSnapshot,
    FieldChange,
    FieldDelta,
    ScenarioOutcome,
    StateShift,
    VariableScores,
)

# ─────────────────────────────────────────────────────────────────────────────
# 常量（纯规则参数）
# ─────────────────────────────────────────────────────────────────────────────

_EPS = 1e-9  # 浮点相等 / 方向判定阈值

# 七因子 → FactorSnapshot 子对象属性名（canonical 因子标识 F/E/T/V/C/R/M）
_FACTOR_ATTRS: dict[str, str] = {
    "F": "fundamental",
    "E": "expectation",
    "T": "trend",
    "V": "valuation",
    "C": "crowding",
    "R": "risk",
    "M": "market",
}

# 七因子 → 该因子承载的 canonical 数据字段清单（复用 factors.py 的 _*_FIELDS 元组）
_FACTOR_FIELDS: dict[str, tuple[str, ...]] = {
    "F": _FUNDAMENTAL_FIELDS,
    "E": _EXPECTATION_FIELDS,
    "T": _TREND_FIELDS,
    "V": _VALUATION_FIELDS,
    "C": _CROWDING_FIELDS,
    # R：数值型直接字段（质押/杠杆/商誉）+ 审计子对象字段（audit_* 为文本，audit_fees 为数值）
    "R": _RISK_NUMERIC_FIELDS + _AUDIT_FIELDS,
    "M": _MARKET_FIELDS,
}

# L2 评分层：VariableScores 的六个数值方向分（m 为 dict 摘要，不参与数值求差）
_SCORE_FIELDS: tuple[str, ...] = (
    "f_fundamental_trend",
    "e_revision",
    "t_relative_strength",
    "v_valuation_percentile",
    "c_crowding",
    "r_risk",
)


# ─────────────────────────────────────────────────────────────────────────────
# 校验 / 快照引用（步骤 0）
# ─────────────────────────────────────────────────────────────────────────────


def _parse_date(date_str: str) -> _dt.date:
    """``YYYY-MM-DD`` → ``datetime.date``；非法格式抛 ``ValueError``。"""
    if not date_str:
        raise ValueError("as_of_date 为空，无法计算 elapsed_days")
    return _dt.date.fromisoformat(date_str)


def _validate(
    prev: CompanyStateArtifact | None, curr: CompanyStateArtifact, anchor: CompanyStateArtifact | None
) -> None:
    """步骤 0 校验：同 symbol / 同 schema_version；prev 存在时 curr.date > prev.date。"""
    if prev is not None:
        if prev.symbol != curr.symbol:
            raise ValueError(f"symbol 不一致：prev={prev.symbol!r} curr={curr.symbol!r}")
        if prev.schema_version != curr.schema_version:
            raise ValueError(f"schema_version 不一致：prev={prev.schema_version!r} curr={curr.schema_version!r}")
        if _parse_date(curr.as_of_date) <= _parse_date(prev.as_of_date):
            raise ValueError(f"curr.date 必须晚于 prev.date：prev={prev.as_of_date!r} curr={curr.as_of_date!r}")
    if anchor is not None and anchor.symbol != curr.symbol:
        raise ValueError(f"anchor symbol 不一致：anchor={anchor.symbol!r} curr={curr.symbol!r}")


def _ref(artifact: CompanyStateArtifact) -> ArtifactRef:
    """把内存中的 CompanyStateArtifact 折叠为快照引用（不内嵌整帧）。

    内存对象无持久化 id，用 ``symbol:as_of_date`` 作确定性合成 id；D3 persistence
    落盘后可用真实持久化 id 覆盖。
    """
    return ArtifactRef(
        id=f"{artifact.symbol}:{artifact.as_of_date}",
        date=artifact.as_of_date,
        symbol=artifact.symbol,
    )


def _elapsed_days(prev: CompanyStateArtifact | None, curr: CompanyStateArtifact) -> int:
    if prev is None:
        return 0
    return (_parse_date(curr.as_of_date) - _parse_date(prev.as_of_date)).days


# ─────────────────────────────────────────────────────────────────────────────
# L1 字段差（步骤 2）
# ─────────────────────────────────────────────────────────────────────────────


def _is_numeric(value: Any) -> bool:
    """数值（int/float，排除 bool）判定——FieldDelta 只承载数值字段。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _factor_object(snapshot: FactorSnapshot | None, factor: str) -> Any:
    """取某个因子的子对象；snapshot 缺失时返回 ``None``。"""
    if snapshot is None:
        return None
    return getattr(snapshot, _FACTOR_ATTRS[factor], None)


def _field_value(factor_obj: Any, field: str) -> Any:
    """取因子字段值；R 的 audit_* 字段嵌套在 ``audit`` 子对象下。"""
    if factor_obj is None:
        return None
    if field in _AUDIT_FIELDS:
        audit = getattr(factor_obj, "audit", None)
        return getattr(audit, field, None) if audit is not None else None
    return getattr(factor_obj, field, None)


def _rel_delta(prev: float, curr: float) -> float | None:
    """相对差 ``curr/prev - 1``；prev 为 0 时无法定义 → ``None``（不静默回退）。"""
    if prev == 0.0:
        return None
    return curr / prev - 1.0


def _field_delta(field: str, prev_val: Any, curr_val: Any) -> FieldDelta | None:
    """单字段变化（最小粒度，纯规则）。

    ``None→值`` = appeared、``值→None`` = disappeared、否则 up / down / unchanged。
    两侧均缺失（或均为非数值文本）→ ``None``（不产 FieldDelta）。
    """
    prev_num = prev_val if _is_numeric(prev_val) else None
    curr_num = curr_val if _is_numeric(curr_val) else None

    if prev_num is None and curr_num is None:
        return None
    if prev_num is None:
        return FieldDelta(
            field=field, prev=None, curr=curr_num, delta=None, rel_delta=None, change=FieldChange.APPEARED
        )
    if curr_num is None:
        return FieldDelta(
            field=field, prev=prev_num, curr=None, delta=None, rel_delta=None, change=FieldChange.DISAPPEARED
        )

    delta = curr_num - prev_num
    if abs(delta) <= _EPS:
        change = FieldChange.UNCHANGED
    elif delta > 0.0:
        change = FieldChange.UP
    else:
        change = FieldChange.DOWN
    return FieldDelta(
        field=field,
        prev=prev_num,
        curr=curr_num,
        delta=delta,
        rel_delta=_rel_delta(prev_num, curr_num),
        change=change,
    )


def _factor_direction(field_deltas: list[FieldDelta]) -> str:
    """因子方向聚合：对变化字段的 ``rel_delta`` 求和取符号。

    结构性示意（design §4 步骤 2「聚合关键字段 rel_delta 方向」）：rel_delta 跨量纲
    可比；不同字段的「利好/利空」符号语义与关键字段加权留待方向分口径细化，此处
    只给确定性净方向。仅 appeared/disappeared（无 rel_delta）时 → ``neutral``。
    """
    rels = [f.rel_delta for f in field_deltas if f.rel_delta is not None]
    if not rels:
        return "neutral"
    net = sum(rels)
    if net > _EPS:
        return "improving"
    if net < -_EPS:
        return "deteriorating"
    return "neutral"


def _factor_consistency(direction: str, other_directions: list[str]) -> str:
    """跨因子一致性（§4 步骤 2）：resonant / divergent / independent。

    纯规则：本因子非 neutral 时，与其它非 neutral 因子方向同向 → resonant、
    存在反向 → divergent、无其它非 neutral 因子 → independent。
    """
    others = [d for d in other_directions if d != "neutral"]
    if direction == "neutral" or not others:
        return "independent"
    if all(o == direction for o in others):
        return "resonant"
    if any(o != direction for o in others):
        return "divergent"
    return "independent"


def _factor_deltas(prev_snap: FactorSnapshot | None, curr_snap: FactorSnapshot | None) -> list[FactorDelta]:
    """L1 因子差：逐因子逐字段计算 ``FieldDelta``，聚合为 ``FactorDelta``。

    ``fields`` 仅含发生变化（appeared / disappeared / up / down）的字段——unchanged
    与两侧均缺失不进入字段清单（design：``fields`` 仅含发生变化的字段）。
    """
    factor_deltas: list[FactorDelta] = []
    directions: dict[str, str] = {}

    # 先逐因子聚合字段变化与方向
    for factor in _FACTOR_FIELDS:
        prev_obj = _factor_object(prev_snap, factor)
        curr_obj = _factor_object(curr_snap, factor)
        changed: list[FieldDelta] = []
        for field in _FACTOR_FIELDS[factor]:
            fd = _field_delta(field, _field_value(prev_obj, field), _field_value(curr_obj, field))
            if fd is not None and fd.change != FieldChange.UNCHANGED:
                changed.append(fd)
        directions[factor] = _factor_direction(changed)
        factor_deltas.append(FactorDelta(factor=factor, direction=directions[factor], fields=changed, consistency=""))

    # 回填跨因子一致性（需要所有因子方向先算完）
    for i, factor in enumerate(_FACTOR_FIELDS):
        others = [directions[f] for j, f in enumerate(_FACTOR_FIELDS) if j != i]
        factor_deltas[i].consistency = _factor_consistency(directions[factor], others)

    return factor_deltas


def _first_frame_factors(curr_snap: FactorSnapshot | None) -> list[FactorDelta]:
    """首帧基线登记（步骤 1）：所有有值字段 ``change=appeared``。

    ``None`` 字段不登记 appeared（它们是缺失，不是「出现」）；方向为 ``neutral``
    （无 rel_delta），consistency 为 ``independent``（无上一帧可比）。
    """
    factor_deltas: list[FactorDelta] = []
    for factor in _FACTOR_FIELDS:
        obj = _factor_object(curr_snap, factor)
        appeared: list[FieldDelta] = []
        for field in _FACTOR_FIELDS[factor]:
            value = _field_value(obj, field)
            if _is_numeric(value):
                appeared.append(
                    FieldDelta(
                        field=field, prev=None, curr=value, delta=None, rel_delta=None, change=FieldChange.APPEARED
                    )
                )
        factor_deltas.append(
            FactorDelta(factor=factor, direction="neutral", fields=appeared, consistency="independent")
        )
    return factor_deltas


# ─────────────────────────────────────────────────────────────────────────────
# L2 评分差（步骤 3）
# ─────────────────────────────────────────────────────────────────────────────


def _score_deltas(prev_scores: VariableScores, curr_scores: VariableScores) -> dict[str, float | None]:
    """L2 评分差：``scores[name] = curr − prev``（仅数值方向分）。

    ``m``（dict 摘要）不参与数值求差；两侧任一为 ``None`` 时对应项显式 ``None``
    （不静默回退 0）。
    """
    deltas: dict[str, float | None] = {}
    for name in _SCORE_FIELDS:
        p = getattr(prev_scores, name, None)
        c = getattr(curr_scores, name, None)
        if p is None or c is None:
            deltas[name] = None
        else:
            deltas[name] = c - p
    return deltas


# ─────────────────────────────────────────────────────────────────────────────
# L3 状态漂移（步骤 4，纯规则）
# ─────────────────────────────────────────────────────────────────────────────

_STATE_ORDER: dict[str, int] = {s: i for i, s in enumerate(_STATES)}


def _shift_legal_kind(from_state: str, to_state: str) -> tuple[bool, str]:
    """迁移合法性与 kind（复用 classifier 的 _FORWARD/_BACKWARD/_REOPEN 判定口径）。

    ``kind`` 映射到 design §3 取值域 upgrade / downgrade / same / reopen；
    非法跳级（如 S1→S3）``legal=False``，kind 按名义方向（argmax 序号升/降）给出。
    """
    if from_state == to_state:
        return True, "same"
    if (from_state, to_state) in _REOPEN:
        return True, "reopen"
    if (from_state, to_state) in _FORWARD:
        return True, "upgrade"
    if (from_state, to_state) in _BACKWARD:
        return True, "downgrade"
    # 非法跳级：legal=False，kind 按名义方向
    kind = "upgrade" if _STATE_ORDER.get(to_state, 99) > _STATE_ORDER.get(from_state, -1) else "downgrade"
    return False, kind


def _full_distribution(distribution: dict[str, float]) -> dict[str, float]:
    """把（可能部分稀疏的）软状态分布补齐为全 6 态（缺省 0.0），对齐 classifier._STATES 口径。"""
    return {s: distribution.get(s, 0.0) for s in _STATES}


def _state_shift(prev_state: Any, curr_state: Any) -> StateShift | None:
    """L3 软状态漂移：质量流动 + TV 距离 + 熵差（argmax 只是投影之一）。

    ``mass_delta`` 承载逐状态质量流动；``tv_distance=0.5·Σ|ΔP|`` 为信念位移总量；
    ``entropy_delta``（负=变确定 / 正=变模糊）与「整体平移」分开表达；``drift`` 复用
    ``classifier._drift`` 口径；``legal/kind`` 复用 classifier 迁移判定。
    """
    if curr_state is None:
        return None
    if prev_state is None:
        # 状态层首次出现（无上一帧软状态）
        return StateShift(
            argmax_from=None,
            argmax_to=curr_state.argmax_state,
            mass_delta={},
            tv_distance=0.0,
            entropy_from=None,
            entropy_to=curr_state.entropy,
            entropy_delta=None,
            drift=None,
            legal=True,
            kind="same",
        )

    curr_full = _full_distribution(curr_state.distribution)
    prev_full = _full_distribution(prev_state.distribution)
    mass_delta = {s: curr_full[s] - prev_full[s] for s in _STATES}
    tv_distance = 0.5 * sum(abs(v) for v in mass_delta.values())
    entropy_delta = curr_state.entropy - prev_state.entropy
    legal, kind = _shift_legal_kind(prev_state.argmax_state, curr_state.argmax_state)
    # _drift 要求 curr distribution 为全 6 态（内部直接下标）；prev 用 .get 兼容稀疏
    drift = _drift(curr_full, prev_state)

    return StateShift(
        argmax_from=prev_state.argmax_state,
        argmax_to=curr_state.argmax_state,
        mass_delta=mass_delta,
        tv_distance=tv_distance,
        entropy_from=prev_state.entropy,
        entropy_to=curr_state.entropy,
        entropy_delta=entropy_delta,
        drift=drift,
        legal=legal,
        kind=kind,
    )


# ─────────────────────────────────────────────────────────────────────────────
# L3' 置信度差（步骤 5，纯规则）
# ─────────────────────────────────────────────────────────────────────────────


def _new_evidence_ids(prev: CompanyStateArtifact, curr: CompanyStateArtifact) -> list[str]:
    """本窗口新增 EvidenceEvent 的 id（按 id 差集，纯规则）。"""
    prev_ids = {e.id for e in prev.evidence_log}
    return [e.id for e in curr.evidence_log if e.id not in prev_ids]


def _confidence_delta(prev: CompanyStateArtifact | None, curr: CompanyStateArtifact) -> ConfidenceDelta:
    """L3' 置信度差：Δ = posterior − prior，log_odds_delta = logit 差。

    ``prior`` 取上一帧 ``thesis_confidence``（t-1 的后验即 t 的先验）；首帧
    ``prior=None``、``delta/log_odds_delta=None``。``evidence_ids`` = 本窗口新增证据
    的 id（其原料 EvidenceEvent 由上游抽取，数值差本身纯规则）。
    """
    if prev is None:
        return ConfidenceDelta(
            prior=None,
            posterior=curr.thesis_confidence,
            delta=None,
            log_odds_delta=None,
            evidence_ids=[],
        )
    prior = prev.thesis_confidence
    posterior = curr.thesis_confidence
    delta = posterior - prior
    log_odds_delta = _logit(posterior) - _logit(prior)
    return ConfidenceDelta(
        prior=prior,
        posterior=posterior,
        delta=delta,
        log_odds_delta=log_odds_delta,
        evidence_ids=_new_evidence_ids(prev, curr),
    )


# ─────────────────────────────────────────────────────────────────────────────
# L4 赔率差（步骤 6，纯规则）
# ─────────────────────────────────────────────────────────────────────────────


def _scenario_map(scenarios: list[ScenarioOutcome]) -> dict[str, ScenarioOutcome]:
    return {s.scenario: s for s in scenarios}


def _ev_diff(prev: CompanyStateArtifact | None, curr: CompanyStateArtifact) -> EVDiff | None:
    """L4 赔率差：ΔEV / ΔRAEV / 各情景 ΔP / ΔR、probability_source 变化。

    任一帧无 ``expected_value`` 或对应值为 ``None`` 时 delta 显式 ``None``；
    ``scenario_return_delta`` 允许 ``None`` 值（R 缺失/无法求差）。首帧按基线登记
    （``ev_from=None``、各情景 ΔP=当前值、ΔR=None）。
    """
    curr_ev = curr.expected_value
    if curr_ev is None:
        return None
    prev_ev = prev.expected_value if prev is not None else None

    ev_from = prev_ev.ev if prev_ev is not None else None
    ev_to = curr_ev.ev
    ev_delta = (ev_to - ev_from) if (ev_from is not None and ev_to is not None) else None
    rae_from = prev_ev.risk_adjusted_ev if prev_ev is not None else None
    rae_to = curr_ev.risk_adjusted_ev
    rae_delta = (rae_to - rae_from) if (rae_from is not None and rae_to is not None) else None

    prev_scen = _scenario_map(prev_ev.scenarios) if prev_ev is not None else {}
    curr_scen = _scenario_map(curr_ev.scenarios)
    prob_delta: dict[str, float] = {}
    ret_delta: dict[str, float | None] = {}
    for name in ("bull", "base", "bear"):
        pc = curr_scen.get(name)
        pp = prev_scen.get(name)
        p_curr = pc.probability if pc is not None else None
        p_prev = pp.probability if pp is not None else None
        if p_curr is None and p_prev is None:
            continue
        prob_delta[name] = (p_curr if p_curr is not None else 0.0) - (p_prev if p_prev is not None else 0.0)
        r_curr = pc.expected_return if pc is not None else None
        r_prev = pp.expected_return if pp is not None else None
        ret_delta[name] = (r_curr - r_prev) if (r_curr is not None and r_prev is not None) else None

    prev_source = prev_ev.probability_source if prev_ev is not None else None
    curr_source = curr_ev.probability_source
    source_change = ""
    if prev_source is not None and prev_source != curr_source:
        source_change = f"{prev_source} → {curr_source}"

    return EVDiff(
        ev_from=ev_from,
        ev_to=ev_to,
        ev_delta=ev_delta,
        risk_adjusted_ev_delta=rae_delta,
        scenario_probability_delta=prob_delta,
        scenario_return_delta=ret_delta,
        probability_source_change=source_change,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def diff(
    prev: CompanyStateArtifact | None,
    curr: CompanyStateArtifact,
    anchor: CompanyStateArtifact | None = None,
) -> CompanyStateDiff:
    """差分两个 CompanyStateArtifact（纯函数，禁 LLM）。

    步骤 0 校验 → 步骤 1 首帧基线登记 → 步骤 2 L1 因子差 → 步骤 3 L2 评分差 →
    步骤 4 L3 状态漂移 → 步骤 5 L3' 置信度差 → 步骤 6 L4 赔率差。
    步骤 7–11（仓位 / 证据 / 归因 / 退出 / 边界）由后续 D2 步骤补齐，当前以 typed
    contract 缺省值承载（首帧时各层按基线登记处理）。

    Args:
        prev: 上一帧（首帧为 ``None``）。
        curr: 当前帧。
        anchor: 建仓锚点帧（可选，§44）。

    Returns:
        :class:`CompanyStateDiff`：五层分层差分。
    """
    _validate(prev, curr, anchor)

    prev_ref = _ref(prev) if prev is not None else None
    curr_ref = _ref(curr)
    anchor_ref = _ref(anchor) if anchor is not None else None
    is_first = prev is None

    if is_first:
        factors = _first_frame_factors(curr.factor_snapshot)
        scores: dict[str, float | None] = {}
    else:
        factors = _factor_deltas(prev.factor_snapshot, curr.factor_snapshot)
        scores = _score_deltas(prev.variable_scores, curr.variable_scores)

    state_shift = _state_shift(prev.state if prev is not None else None, curr.state)
    confidence = _confidence_delta(prev, curr)
    ev = _ev_diff(prev, curr)

    return CompanyStateDiff(
        symbol=curr.symbol,
        prev=prev_ref,
        curr=curr_ref,
        anchor=anchor_ref,
        is_first=is_first,
        elapsed_days=_elapsed_days(prev, curr),
        factors=factors,
        scores=scores,
        state_shift=state_shift,
        confidence=confidence,
        ev=ev,
        # L5 及归因/退出/边界（步骤 7–11）由后续 D2 步骤补齐，暂为缺省值。
        position=None,
        new_evidence=[],
        attribution=[],
        thesis_delta="",
        exit_conditions_met=[],
        degraded_flip="",
        missing_appeared=[],
        missing_disappeared=[],
    )
