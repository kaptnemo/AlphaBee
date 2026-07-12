"""Deterministic fingerprint for deduplicating data fetch failures."""

from __future__ import annotations

import hashlib


def compute_fingerprint(
    provider: str,
    api_name: str,
    error_type: str,
    missing_fields: list[str] | None = None,
    error_prefix: str | None = None,
) -> str:
    """Generate a stable fingerprint for grouping similar failures.

    Two failures with the same *provider*, *api_name*, *error_type*,
    *missing_fields* (sorted), and a leading slice of the error message
    produce the same fingerprint — enabling deduplication at the issue level.
    """
    parts: list[str] = [provider, api_name, error_type]

    if missing_fields:
        parts.append(",".join(sorted(missing_fields)))

    if error_prefix:
        parts.append(error_prefix[:80])

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
