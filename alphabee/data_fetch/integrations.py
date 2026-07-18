"""Integration helpers — convenience wrappers for wiring into existing code.

Usage in collector helpers::

    from alphabee.data_fetch.integrations import capture_failure

    async def fetch_data(symbol):
        try:
            return await raw_fetch(symbol)
        except TimeoutError as exc:
            capture_failure(
                provider="tushare", api_name="income", symbol=symbol,
                error_type="timeout", error_message=str(exc),
            )
            raise
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from alphabee.data_fetch.recorder import record_failure

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def capture_failure(
    provider: str,
    api_name: str | None = None,
    symbol: str | None = None,
    error_type: str = "unknown",
    error_message: str | None = None,
    severity: str = "medium",
    missing_fields: list[str] | None = None,
    request_payload: dict | None = None,
    response_snippet: str | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Fire-and-forget wrapper — logs failure to the event database.

    Never raises; any internal errors are logged and swallowed so they
    cannot affect the caller's control flow.
    """
    try:
        record_failure(
            provider=provider,
            api_name=api_name or "unknown",
            error_type=error_type,
            error_message=error_message,
            symbol=symbol,
            severity=severity,
            missing_fields=missing_fields,
            request_payload=request_payload,
            response_snippet=response_snippet,
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
        )
    except Exception:
        logger.warning(
            "capture_failure: failed to record event",
            exc_info=True,
            extra={
                "provider": provider,
                "api_name": api_name,
                "error_type": error_type,
            },
        )


def tracked(
    provider: str,
    api_name: str | None = None,
    severity_map: dict[type[BaseException], str] | None = None,
) -> Callable[[F], F]:
    """Decorator that automatically records failures from decorated functions.

    Usage::

        @tracked(provider="tushare", api_name="income")
        def fetch_income(symbol: str) -> pd.DataFrame:
            ...

    Exception type → severity mapping can be customised via *severity_map*.
    """
    sev_map = severity_map or {}

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            resolved_api = api_name or func.__name__
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                sev = _lookup_severity(exc, sev_map)
                capture_failure(
                    provider=provider,
                    api_name=resolved_api,
                    error_type=_classify_error(exc),
                    error_message=str(exc),
                    severity=sev,
                )
                raise

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            resolved_api = api_name or func.__name__
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                sev = _lookup_severity(exc, sev_map)
                capture_failure(
                    provider=provider,
                    api_name=resolved_api,
                    error_type=_classify_error(exc),
                    error_message=str(exc),
                    severity=sev,
                )
                raise

        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    return decorator


# ── internal helpers ───────────────────────────────────────────────────


def _classify_error(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()

    if "perm" in name or "auth" in name or "forbidden" in msg:
        return "permission"
    if "timeout" in name or "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "rate" in name or "limit" in name or "throttl" in msg or "limit" in msg:
        return "rate_limit"
    if "parse" in name or "json" in name or "decode" in name:
        return "parse_error"
    if "network" in name or "connect" in name or "socket" in name or "dns" in msg:
        return "network"
    if "field" in msg or ("missing" in msg and "field" in msg) or "keyerror" in name:
        return "missing_field"
    if "empty" in msg or "null" in msg or "no data" in msg:
        return "empty_response"
    return "unknown"


def _lookup_severity(exc: BaseException, severity_map: dict[type[BaseException], str]) -> str:
    for exc_type, sev in severity_map.items():
        if isinstance(exc, exc_type):
            return sev
    return "medium"
