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

``MarketScoreEngine`` / ``load_rules`` 采用**惰性加载**：``score_engine`` 会级联导入
``derived_facts → facts → collectors.tushare``（``ts.set_token`` 写 ``~/tk.csv``），
仅在真正用到评分引擎时才触发，避免「只 import 契约（``models`` / ``position``）」的
调用方被迫付 tushare 初始化代价。
"""

from typing import Any

from alphabee.market_regime.models import (
    CollectorOutput,
    MarketIndicatorSnapshot,
    MarketScore,
    MarketScoreResult,
    PositionAdvice,
    RegimeSnapshot,
    RegimeTransition,
    SimilarityHit,
    SimilarityResult,
)
from alphabee.market_regime.position import advise_position, load_position_rules

__all__ = [
    "CollectorOutput",
    "MarketIndicatorSnapshot",
    "MarketScore",
    "MarketScoreResult",
    "PositionAdvice",
    "RegimeSnapshot",
    "RegimeTransition",
    "SimilarityHit",
    "SimilarityResult",
    "MarketScoreEngine",
    "load_rules",
    "advise_position",
    "load_position_rules",
]


def __getattr__(name: str) -> Any:
    if name in ("MarketScoreEngine", "load_rules"):
        from alphabee.market_regime.score_engine import MarketScoreEngine, load_rules

        return {"MarketScoreEngine": MarketScoreEngine, "load_rules": load_rules}[name]
    raise AttributeError(f"module 'alphabee.market_regime' has no attribute {name!r}")
