"""Market regime subsystem — market-level analysis track (3–6 month horizon).

This package is deliberately independent from the per-symbol orchestrator
pipeline: it consumes market-level data (index valuation, liquidity, breadth,
risk preference) and shares the ``alphabee/core`` models and typed-artifact
conventions so it can later be consumed by stock-level analysis as risk-exposure
context.

Pipeline phases:
- Phase 0 (done): data base — collectors + normalized daily indicator CSV.
- Phase 1 (this): deterministic scoring engine — YAML rules + topological sort +
  safe-AST formulas (reusing ``agents/derived_facts`` Engine semantics) producing
  ``MarketScore`` / ``RegimeSnapshot`` / ``PositionAdvice``.
- Phase 2+: regime state machine, similarity search, LLM explainer, graph/CLI.
"""

from alphabee.market_regime.models import (
    CollectorOutput,
    MarketIndicatorSnapshot,
    MarketScore,
    MarketScoreResult,
    PositionAdvice,
    RegimeSnapshot,
)
from alphabee.market_regime.position import advise_position, load_position_rules
from alphabee.market_regime.score_engine import MarketScoreEngine, load_rules

__all__ = [
    "CollectorOutput",
    "MarketIndicatorSnapshot",
    "MarketScore",
    "MarketScoreResult",
    "PositionAdvice",
    "RegimeSnapshot",
    "MarketScoreEngine",
    "load_rules",
    "advise_position",
    "load_position_rules",
]
