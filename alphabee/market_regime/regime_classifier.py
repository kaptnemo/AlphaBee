"""Six-phase regime state machine + Markov-style transition constraints (Phase 2.1).

The Phase 1 scoring engine produces a 0-100 ``market_score`` plus per-engine scores.
The classifier maps it to one of six phases:

    吸筹期 → 趋势启动 → 趋势加速 → 高位分歧 → 风险释放 → 底部修复 →（回吸筹期）

Design follows the roadmap:

- **Rule layer**: candidate phase comes from ``total_score`` + trend/breadth +
  risk-preference delta (all deterministic, no LLM).
- **Constraint layer**: ``rules/transition_matrix.yaml`` declares legal transitions
  (Markov style). An illegal jump keeps the candidate but is flagged
  ``suspicious`` (transition_valid=False) for manual/LLM review, never silently
  accepted.

Determinism: same inputs → same phase/confidence/rationale, fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from alphabee.market_regime.models import MarketScoreResult, RegimeTransition

DEFAULT_TRANSITION_YAML = Path(__file__).resolve().parent / "rules" / "transition_matrix.yaml"

PHASES = ("吸筹期", "趋势启动", "趋势加速", "高位分歧", "风险释放", "底部修复")


@dataclass
class TransitionRules:
    """Loaded transition matrix: phase order + per-phase allowed destinations."""

    phases: list[str] = field(default_factory=lambda: list(PHASES))
    transitions: dict[str, list[str]] = field(default_factory=dict)

    def allows(self, from_phase: str | None, to_phase: str) -> bool:
        """Whether a move ``from_phase → to_phase`` is legal.

        First evaluation (``from_phase`` is None/unknown) accepts any declared phase.
        """
        if not from_phase or from_phase == "未知":
            return to_phase in self.phases
        return to_phase in self.transitions.get(from_phase, [])


def load_transition_rules(path: str | Path | None = None) -> TransitionRules:
    """Load ``transition_matrix.yaml`` into a ``TransitionRules``."""
    yaml_path = Path(path) if path else DEFAULT_TRANSITION_YAML
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    phases = [str(p) for p in data.get("phases", list(PHASES))]
    transitions = {str(k): [str(v) for v in vals] for k, vals in data.get("transitions", {}).items()}
    return TransitionRules(phases=phases, transitions=transitions)


def _candidate_phase(
    total: float,
    risk_delta: float,
    breadth: float | None,
) -> tuple[str, float, list[str]]:
    """Rule-layer phase determination (deterministic).

    Score bands mirror ``market_score.yaml`` thresholds; breadth/risk deltas refine
    the phase inside a band:
    - ``>= 70``  : 趋势加速, but overheated (risk_delta >= 1 or breadth < 55) → 高位分歧
    - ``50-70``  : 风险释放 if risk_delta <= -1; 趋势启动 if breadth >= 55; else 吸筹期
    - ``30-50``  : 风险释放 if risk_delta <= -1 or breadth < 40; else 吸筹期
    - ``< 30``   : 底部修复
    """
    if total >= 70:
        overheated = risk_delta >= 1.0 or (breadth is not None and breadth < 55)
        if overheated:
            return "高位分歧", 0.65, ["总分≥70 但情绪/宽度过热（risk_delta≥1 或宽度<55），判定高位分歧"]
        return "趋势加速", 0.70, ["总分≥70 且趋势与宽度共振，趋势加速阶段"]
    if total >= 50:
        if risk_delta <= -1.0:
            return "风险释放", 0.60, ["总分50-70 但风险偏好回落，市场进入风险释放"]
        if breadth is not None and breadth >= 55:
            return "趋势启动", 0.60, ["总分50-70 且宽度回暖（≥55），趋势启动"]
        return "吸筹期", 0.50, ["总分50-70 且宽度偏弱（<55），处于吸筹期"]
    if total >= 30:
        if risk_delta <= -1.0 or (breadth is not None and breadth < 40):
            return "风险释放", 0.60, ["总分30-50 且情绪/宽度走弱，风险释放"]
        return "吸筹期", 0.45, ["总分30-50 且未进一步恶化，吸筹期"]
    return "底部修复", 0.70, ["总分<30，处于底部修复阶段"]


def classify_regime(
    result: MarketScoreResult,
    prev_phase: str | None = None,
    path: str | Path | None = None,
    rules: TransitionRules | None = None,
) -> RegimeTransition:
    """Classify one scored week into a six-phase regime under transition constraints.

    Args:
        result:       ``MarketScoreResult`` from the Phase 1 scoring engine.
        prev_phase:   previous week's phase (``None`` on first evaluation).
        path:         override the transition-matrix YAML path.
        rules:        preloaded ``TransitionRules`` (avoids re-reading YAML).

    Returns:
        ``RegimeTransition`` carrying the candidate phase, confidence, and whether
        the move from ``prev_phase`` is legal (illegal → ``suspicious``).
    """
    scores = result.scores
    total = scores.total_score
    if total is None:
        return RegimeTransition(
            date=result.date,
            phase="未知",
            confidence=0.0,
            transition_from=prev_phase,
            transition_valid=True,
            suspicious=True,
            rationale=["评分缺失，无法判定阶段"],
        )

    # 宽度取 breadth_score 规则结果（0-100）；缺失时按 None 处理（规则层会用
    # risk_delta 兜底，不会因此抛错）。
    breadth_result = result.rule_results.get("breadth_score", {})
    breadth_raw = breadth_result.get("breadth_score")
    breadth: float | None = float(breadth_raw) if breadth_raw is not None else None

    phase, confidence, rationale = _candidate_phase(total, scores.risk_preference_delta, breadth)
    loaded = rules or load_transition_rules(path)
    valid = loaded.allows(prev_phase, phase)
    return RegimeTransition(
        date=result.date,
        phase=phase,
        confidence=round(confidence, 2),
        transition_from=prev_phase,
        transition_valid=valid,
        suspicious=not valid,
        rationale=rationale,
    )
