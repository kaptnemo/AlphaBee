"""Compatibility facade for orchestrator nodes and services.

The active implementation now lives under:

- ``alphabee.orchestrator.nodes``
- ``alphabee.orchestrator.services``
"""

from alphabee.orchestrator.nodes.analyze import run_analysis_engines
from alphabee.orchestrator.nodes.conflicts import explore_conflicts
from alphabee.orchestrator.nodes.thesis import run_thesis
from alphabee.orchestrator.nodes.verification import verify_hypotheses
from alphabee.orchestrator.services.company_context import (
    build_company_context as _build_company_context,
)

__all__ = [
    "_build_company_context",
    "run_analysis_engines",
    "explore_conflicts",
    "verify_hypotheses",
    "run_thesis",
]
