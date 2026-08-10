"""Core data models for the market regime (market-state) subsystem.

Market regime is a *market-level* analysis track, separate from the per-symbol
orchestrator pipeline. These models carry the normalized daily snapshot and the
per-collector output contract used across ``alphabee/collectors/market_regime/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CollectorOutput:
    """Result of a single market-regime collector.

    Attributes:
        values:   normalized ``{canonical_field: value}`` map (units already canonical).
        source:   provenance label, e.g. ``"akshare:stock_index_pe_lg"``.
        warnings: non-fatal notes (missing fields, fallback dates, etc.).
    """

    values: dict[str, float] = field(default_factory=dict)
    source: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class MarketIndicatorSnapshot:
    """One dated snapshot of normalized market indicators.

    ``values`` only contains canonical fields (see ``schemas/market_regime.yaml``).
    ``sources`` keeps per-field provenance for lineage tracing.
    """

    date: str  # YYYY-MM-DD
    values: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fetched_at: str = ""

    def merge(self, output: CollectorOutput) -> MarketIndicatorSnapshot:
        """Merge a collector output into this snapshot (later wins)."""
        for field_name, value in output.values.items():
            self.values[field_name] = value
            if output.source:
                self.sources[field_name] = output.source
        self.warnings.extend(output.warnings)
        return self
