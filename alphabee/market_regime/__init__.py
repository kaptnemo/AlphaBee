"""Market regime subsystem — market-level analysis track (3–6 month horizon).

This package is deliberately independent from the per-symbol orchestrator
pipeline: it consumes market-level data (index valuation, liquidity, breadth,
risk preference) and shares the ``alphabee/core`` models and typed-artifact
conventions so it can later be consumed by stock-level analysis as risk-exposure
context.
"""

from alphabee.market_regime.models import CollectorOutput, MarketIndicatorSnapshot

__all__ = ["CollectorOutput", "MarketIndicatorSnapshot"]
