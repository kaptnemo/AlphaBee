"""Helpers for resolving data storage paths."""

from __future__ import annotations

from pathlib import Path

from alphabee.config import settings
from alphabee.utils.paths import PROJECT_ROOT


def get_data_root() -> Path:
    """Return the configured data root directory, resolved to an absolute path."""
    root = Path(settings.data.root_dir)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def normalize_symbol(symbol: str | None) -> str:
    """Normalize a symbol for filesystem use."""
    value = (symbol or "unknown").strip()
    return value.replace("/", "_").replace("\\", "_") or "unknown"


def get_symbol_data_dir(*parts: str, symbol: str | None = None) -> Path:
    """Return a directory under the configured data root for a symbol."""
    path = get_data_root() / normalize_symbol(symbol)
    for part in parts:
        path /= part
    path.mkdir(parents=True, exist_ok=True)
    return path
