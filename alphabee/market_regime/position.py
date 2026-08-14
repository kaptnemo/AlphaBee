"""Position-band mapping + single-week delta limit (rules/position.yaml).

Phase 1.3: maps ``total_score`` → band (regime + raw position range), then applies
``weekly_delta_limit`` so the recommended position can move at most ±10% per week
versus the previous week's recommended position (prevents chasing rallies /
panic-selling).

The previous week's recommended position is passed as ``prev_week_score`` (a
fraction in [0, 1]) per the roadmap wording; the suppressed difference is recorded
in ``rationale`` so score jumps are documented rather than masked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from alphabee.market_regime.models import PositionAdvice

DEFAULT_POSITION_YAML = Path(__file__).resolve().parent / "rules" / "position.yaml"


@dataclass
class PositionBand:
    min_score: float
    max_score: float
    regime: str
    position_lo: float
    position_hi: float


@dataclass
class PositionRules:
    bands: list[PositionBand] = field(default_factory=list)
    weekly_delta_limit: float = 0.10


def load_position_rules(path: str | Path | None = None) -> PositionRules:
    """Load band definitions and the weekly delta limit from ``rules/position.yaml``."""
    yaml_path = Path(path) if path else DEFAULT_POSITION_YAML
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    bands: list[PositionBand] = []
    for item in data.get("bands", []):
        bands.append(
            PositionBand(
                min_score=float(item["min"]),
                max_score=float(item["max"]),
                regime=str(item["regime"]),
                position_lo=float(item["position_lo"]),
                position_hi=float(item["position_hi"]),
            )
        )
    bands.sort(key=lambda b: b.min_score)
    return PositionRules(
        bands=bands,
        weekly_delta_limit=float(data.get("weekly_delta_limit", 0.10)),
    )


def find_band(score: float, rules: PositionRules) -> PositionBand | None:
    """Return the band matching ``score`` (``min <= score < max``; top band inclusive)."""
    # 区间约定：左闭右开 [min, max)。分数恰落在档位分界线上时归入较低档，
    # 避免相邻档位双重命中（如 score=70 归"震荡阶段"，70 以下才归"趋势健康"）。
    for band in rules.bands:
        if band.min_score <= score < band.max_score:
            return band
    # 兜底：最高档（max=100）含上限，因此 score=100 也能命中；分数超出上限
    # （浮点误差/未来规则改动）也直接归入最高档，保证总有档位可用。
    if rules.bands and score >= rules.bands[-1].max_score:
        return rules.bands[-1]
    return None


def advise_position(
    score: float,
    prev_week_score: float | None = None,
    path: str | Path | None = None,
    rules: PositionRules | None = None,
) -> PositionAdvice:
    """Map a total score to a position band and apply the weekly delta limit.

    Args:
        score:           0-100 market total score (deterministic engine output).
        prev_week_score: previous week's recommended position (fraction in [0, 1]).
                         ``None`` on first evaluation → no weekly restriction.
        path:            override the position YAML path.
        rules:           preloaded ``PositionRules`` (avoids re-reading YAML).

    Returns:
        ``PositionAdvice`` with the raw band range, the weekly-limited advised
        range, whether the limit was binding, and the rationale.
    """
    loaded = rules or load_position_rules(path)
    band = find_band(score, loaded)
    if band is None:
        return PositionAdvice(
            regime="未知",
            band_low=0.0,
            band_high=0.0,
            position_low=None,
            position_high=None,
            restricted=False,
            rationale=["评分未命中任何仓位档位"],
        )

    raw_lo, raw_hi = band.position_lo, band.position_hi
    lo, hi = raw_lo, raw_hi
    rationale: list[str] = []
    weekly_change: float | None = None
    restricted = False

    if prev_week_score is not None:
        delta = loaded.weekly_delta_limit
        # 单周调整约束：建议区间被限制为
        #   [max(档位下限, 上周 ± delta 的下沿), min(档位上限, 上周 ± delta 的上沿)]
        # 即本周仓位只能相对上周建议仓位移动 ±delta，防止：
        #   - 分数单周大涨 → 追涨一次性满仓；
        #   - 分数单周大跌 → 恐慌性清仓。
        # 这实现了"分批建仓/分批减仓"的风控意图。
        lo = round(max(raw_lo, prev_week_score - delta), 4)
        hi = round(min(raw_hi, prev_week_score + delta), 4)
        if lo > hi:
            # 情形：目标档位整体位于上周建议仓位的一侧（如上周 90%、本周档位 20-40%），
            # 直接裁剪会得到空区间。此时收敛为一个"本周末可达到的最近点"：
            #   从上周仓位只移动 delta，且不越过目标档位边界，尊重原始档位意图。
            if prev_week_score > raw_hi:
                point = round(max(prev_week_score - delta, raw_hi), 4)
            else:
                point = round(min(prev_week_score + delta, raw_lo), 4)
            lo = hi = point
        weekly_change = round(score - prev_week_score, 2)
        if lo != raw_lo or hi != raw_hi:
            restricted = True
            rationale.append(
                f"单周 ±{delta:.0%} 限制：原始区间 [{raw_lo:.0%}, {raw_hi:.0%}] → 建议 [{lo:.0%}, {hi:.0%}]"
            )

    if not rationale:
        rationale.append("本周建议仓位区间未受单周调整限制")

    return PositionAdvice(
        regime=band.regime,
        band_low=round(raw_lo, 4),
        band_high=round(raw_hi, 4),
        position_low=round(lo, 4),
        position_high=round(hi, 4),
        weekly_change=weekly_change,
        restricted=restricted,
        rationale=rationale,
    )
