"""Helpers for recording signal data gaps into the failure database."""

from __future__ import annotations

from alphabee.agents.signal.registry import SIGNAL_RULES


def record_signal_data_gaps(
    signal_analysis: dict[str, dict],
    fact_values: dict[str, float],
    symbol: str | None,
) -> None:
    """Record blocked / missing_fact / invalid signals as failure events."""
    data_unavailable_levels = {"blocked", "missing_fact", "invalid"}

    for signal_id, result in signal_analysis.items():
        level = result.get("level", "")
        if level not in data_unavailable_levels:
            continue

        error_msg = result.get("error", "")
        rule = SIGNAL_RULES.get(signal_id)
        declared_fields: list[str] = []
        if rule is not None:
            declared_fields = list(rule.required_facts or []) + list(
                rule.required_derived_facts or []
            )

        missing = [field for field in declared_fields if field not in fact_values]
        blocked_by = result.get("blocked_by", [])

        if level in {"missing_fact", "blocked"}:
            error_type = "missing_field"
        else:
            error_type = "parse_error"

        try:
            from alphabee.data_fetch.recorder import record_failure

            record_failure(
                provider="signal_engine",
                api_name=signal_id,
                symbol=symbol,
                error_type=error_type,
                error_message=error_msg,
                severity="medium" if level == "blocked" else "low",
                missing_fields=missing or blocked_by or None,
            )
        except Exception:
            pass

