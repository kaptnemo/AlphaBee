"""Layer 4 仓位引擎：三轴（State × Confidence × RiskAdjustedEV）× M × 组合 → ``PositionDecision``（设计文档 §6 / §7）。

把「个股决策」（三轴正交）与「市场暴露 / 组合约束」（Layer 4）分层相乘，产出最终仓位。
核心纪律：

- **确定性纯函数**：不调 LLM、不读外部状态；同一输入必得同一输出。
- **三轴正交，永不合并**（§6.2）：State 决定动作**类型**（``argmax_state`` 粗粒度意图锚点），
  状态分布决定动作**力度**（对分布求期望，§2b.3），Confidence 缩放力度，
  RiskAdjustedEV 作赔率**门槛**（EV 不足 → 压 0）。三者相乘，不求和。
  最终 ``position_band`` 标签由**折减后实际仓位**（``actual_weight``）映射，
  避免「argmax=核心」标签与 0.6% 实际仓位的背离。
- **禁止 `if state == S2: buy()` 反模式**（§41）：动作类型与力度由数据驱动映射 +
  软状态期望得出，不写硬编码分支。
- **软状态期望**（§2b.3）：``Σ_k P(S_k) · weight_band(S_k)``，熵高（分布宽）时期望
  仓位自动更保守——不确定性本身在压低仓位，无需额外规则。
- **组合独立于个股 State**（§7.2）：``actual_weight = portfolio_exposure × stock_weight``，
  单股上限 / 集中度置位 ``restricted``。

仓位带为结构性示意（§6.3 原话：应回测，不是固定数字），集中在模块顶部常量。
"""

from __future__ import annotations

from alphabee.midterm.models import PositionDecision, StateBelief

# ─────────────────────────────────────────────────────────────────────────────
# 仓位带映射（§6.3，结构性示意常量）
# ─────────────────────────────────────────────────────────────────────────────

# 动作类型（argmax_state → 粗粒度意图锚点），仅作动作类型语义，不再直接充当 position_band
_ACTION_BAND: dict[str, str] = {
    "S0": "观察",  # 研究候选（无仓位）
    "S1": "试探",  # 5–10%
    "S2": "加仓",  # 10–15%
    "S3": "核心",  # 15–20%
    "S4": "减仓",  # 降至试探/退出
    "S5": "清仓",  # 0
}

# 折减后实际仓位（actual_weight，portfolio 层）→ position_band 标签。
# 阈值与 _WEIGHT_BAND 权益仓参考带对齐（S1 5% / S2 10% / S3 15%）。
# 低于 5% 的微小仓位统一标「观察」（观望，未成气候），0 标「清仓」。
_ACTUAL_BAND_UP: tuple[tuple[float, str], ...] = (
    (0.15, "核心"),
    (0.10, "加仓"),
    (0.05, "试探"),
)

# 权益仓内参考带（lower, upper），§6.3 示意
_WEIGHT_BAND: dict[str, tuple[float, float]] = {
    "S0": (0.00, 0.00),
    "S1": (0.05, 0.10),
    "S2": (0.10, 0.15),
    "S3": (0.15, 0.20),
    "S4": (0.00, 0.05),
    "S5": (0.00, 0.00),
}

# 每个状态的代表权重（band 中点），用于 Σ P(S_k)·weight_band(S_k)
_WEIGHT_MIDPOINT: dict[str, float] = {s: (lo + hi) / 2.0 for s, (lo, hi) in _WEIGHT_BAND.items()}

_STATES = ("S0", "S1", "S2", "S3", "S4", "S5")

# ─────────────────────────────────────────────────────────────────────────────
# 结构性示意参数
# ─────────────────────────────────────────────────────────────────────────────

_NEUTRAL_CONFIDENCE = 0.5  # confidence 缺失时的中性力度（无证据 → 不放大也不退缩）
_RISK_SENSITIVITY = 0.3  # r_risk 每单位方向分对权重的调整幅度（风险升 → 权重降）
_EV_THRESHOLD = 1.0  # RiskAdjustedEV < 阈值 → 赔率不足（改造 C：软阈值，减仓而非清仓）
_EV_SOFT_REDUCTION = 0.3  # 赔率不足时的减仓乘数（stock_weight × 0.3，减仓而非压 0）
_EV_HARD_ZERO = 0.0  # RiskAdjustedEV < 0 才可能硬压 0（且需低熵确认）
# 熵阈值（nats）：entropy <= 该值 = 低熵（高置信确认负面，可硬压 0）；
# entropy > 该值 = 不确定（高熵），赔率为负也只减仓不清仓（不确定性降低动作力度）。
_ENTROPY_UNCERTAIN = 1.0


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _band_from_weight(weight: float | None, fallback: str) -> str:
    """按折减后实际仓位映射回仓位标签（清仓/观察/试探/加仓/核心）。

    - ``weight <= 0`` → 清仓；
    - ``(0, 0.05)`` → 观察（微小仓位，观望）；
    - ``[0.05, 0.10)`` → 试探；``[0.10, 0.15)`` → 加仓；``>= 0.15`` → 核心。
    - ``weight`` 为 ``None``（无市场暴露且 stock_weight 缺失，防御分支）→ 回退到
      argmax 动作类型标签（粗粒度意图）。
    """
    if weight is None:
        return fallback
    if weight <= 0.0:
        return "清仓"
    for threshold, label in _ACTUAL_BAND_UP:
        if weight >= threshold:
            return label
    return "观察"


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def build_position(
    state: StateBelief,
    *,
    confidence: float | None = None,
    risk_adjusted_ev: float | None = None,
    market_exposure: float | None = None,
    risk_adjustment: float | None = None,
    portfolio_adjustment: float = 1.0,
    base_risk_budget: float = 1.0,
    single_stock_cap: float | None = None,
) -> PositionDecision:
    """把三轴 × MarketExposure × PortfolioAdjustment 分层相乘合成仓位。

    Args:
        state: 软状态（``classifier.classify_state`` 产物），含 distribution /
            argmax_state / entropy。
        confidence: 后验 P(H|E)（0-1，``bayes.update_confidence`` 产物）；``None``
            时用中性力度 0.5。
        risk_adjusted_ev: RiskAdjustedEV（RATIO，``ExpectedValue.risk_adjusted_ev``）；
            低于 ``_EV_THRESHOLD`` 时赔率不足 → 减仓（×``_EV_SOFT_REDUCTION``），仅当
            赔率为负且熵 <= ``_ENTROPY_UNCERTAIN``（低熵=高置信确认负面）时才压 0；
            ``None`` 时不设赔率门槛（理由中注明）。
        market_exposure: M 的建议暴露（``MarketFactor.position_high`` / PositionAdvice），
            作为 ``portfolio_exposure`` 透传。
        risk_adjustment: r_risk 方向分（[-1,1]，风险升 → 越负）；``None`` 时不调整。
        portfolio_adjustment: 相关性 / 集中度 / 风格暴露调整乘数（默认 1.0 无调整）。
        base_risk_budget: BaseRiskBudget（示意，默认 1.0）。
        single_stock_cap: 单股上限（RATIO）；``stock_weight`` 超过时置 ``restricted``
            并封顶。

    Returns:
        :class:`PositionDecision`：``stock_weight`` = BaseRiskBudget × 期望仓位带 ×
        Confidence × RiskAdjustment × PortfolioAdjustment；``actual_weight`` =
        ``portfolio_exposure × stock_weight``；``position_band`` 由折减后实际仓位
        （``actual_weight``，缺失时回退 ``stock_weight``）映射，而非裸 ``argmax_state``。
        ``restricted`` 由单股上限置位。
    """
    rationale: list[str] = []

    # 1. 动作类型：argmax_state → 粗粒度意图锚点（仅语义，不直接充当 position_band）
    argmax = state.argmax_state
    action_type = _ACTION_BAND.get(argmax, "观察")
    rationale.append(f"动作类型={action_type}（argmax_state={argmax}，粗粒度意图锚点）")

    # 2. 动作力度：对状态分布求期望 Σ P(S_k)·weight_band(S_k)（软状态，§2b.3）
    distribution = state.distribution
    expected_weight = sum(distribution.get(s, 0.0) * _WEIGHT_MIDPOINT[s] for s in _STATES)
    rationale.append(f"期望仓位带=ΣP·weight_band={expected_weight:.4f}（entropy={state.entropy:.3f}）")

    # 3. Confidence 力度缩放（后验越高越敢下注）
    conf_mult = _clip(confidence, 0.0, 1.0) if confidence is not None else _NEUTRAL_CONFIDENCE
    if confidence is None:
        rationale.append("confidence 缺失 → 中性力度 0.5")

    # 4. RiskAdjustment（r_risk 三层风险合成：风险升 → 权重降）
    if risk_adjustment is not None:
        risk_mult = _clip(1.0 + _RISK_SENSITIVITY * risk_adjustment, 0.0, 1.5)
        rationale.append(f"风险调整乘数={risk_mult:.3f}（r_risk={risk_adjustment}）")
    else:
        risk_mult = 1.0
        rationale.append("r_risk 缺失 → 风险调整中性 1.0")

    # 5. 分层相乘（不求和）——个股层 stock_weight
    stock_weight = base_risk_budget * expected_weight * conf_mult * risk_mult * portfolio_adjustment

    # 6. 赔率门槛（改造 C：软阈值，减仓而非清仓）——RiskAdjustedEV 不足时
    #    stock_weight × _EV_SOFT_REDUCTION（减仓）；仅当赔率为负且低熵「高置信确认
    #    负面」时才硬压 0（高熵/不确定 → 减仓而非清仓，不确定性真正降低动作力度）。
    if risk_adjusted_ev is not None and risk_adjusted_ev < _EV_THRESHOLD:
        if risk_adjusted_ev < _EV_HARD_ZERO and state.entropy <= _ENTROPY_UNCERTAIN:
            rationale.append(
                f"RiskAdjustedEV={risk_adjusted_ev} < {_EV_HARD_ZERO} 且 entropy={state.entropy:.3f} "
                f"<= {_ENTROPY_UNCERTAIN}（低熵=高置信确认负面）→ 赔率不足且确定，压 0"
            )
            stock_weight = 0.0
        else:
            rationale.append(
                f"RiskAdjustedEV={risk_adjusted_ev} < {_EV_THRESHOLD} → 赔率不足，"
                f"减仓 ×{_EV_SOFT_REDUCTION}（高熵/不确定不清仓）"
            )
            stock_weight *= _EV_SOFT_REDUCTION
    elif risk_adjusted_ev is None:
        rationale.append("RiskAdjustedEV 缺失 → 不设赔率门槛")

    # 7. 组合约束：单股上限 / 集中度 → restricted（§7.2）
    restricted = False
    if single_stock_cap is not None and stock_weight > single_stock_cap:
        restricted = True
        rationale.append(f"stock_weight={stock_weight:.4f} 超过单股上限 {single_stock_cap} → 封顶")
        stock_weight = single_stock_cap

    # 8. 组合层：actual_weight = portfolio_exposure × stock_weight
    portfolio_exposure = market_exposure
    actual_weight = (
        portfolio_exposure * stock_weight if portfolio_exposure is not None and stock_weight is not None else None
    )

    # 9. position_band：由折减后实际仓位映射（而非裸 argmax_state），消除「核心」标签与
    #    0.6% 实际仓位的背离。市场暴露缺失时回退到 stock_weight（置信度/风险折减后权益仓内权重）。
    effective_weight = actual_weight if actual_weight is not None else stock_weight
    position_band = _band_from_weight(effective_weight, action_type)
    if actual_weight is None:
        rationale.append(f"position_band={position_band}（市场暴露缺失，按 stock_weight={effective_weight:.4f} 映射）")
    else:
        rationale.append(f"position_band={position_band}（按 actual_weight={actual_weight:.4f} 映射）")

    return PositionDecision(
        portfolio_exposure=portfolio_exposure,
        stock_weight=stock_weight,
        actual_weight=actual_weight,
        position_band=position_band,
        rationale=rationale,
        restricted=restricted,
    )
