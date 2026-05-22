from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class SyncTTLCache:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[object, tuple[float, T]] = {}
        self._inflight: dict[object, threading.Event] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: object, compute: Callable[[], T]) -> T:
        while True:
            with self._lock:
                cached = self._cache.get(key)
                now = time.monotonic()
                if cached is not None and cached[0] > now:
                    return cached[1]

                event = self._inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    owner = True
                else:
                    owner = False

            if owner:
                break
            event.wait()

        try:
            value = compute()
        except Exception:
            with self._lock:
                current = self._inflight.pop(key, None)
                if current is not None:
                    current.set()
            raise

        with self._lock:
            self._cache[key] = (time.monotonic() + self.ttl_seconds, value)
            current = self._inflight.pop(key, None)
            if current is not None:
                current.set()
        return value


class AsyncTTLCache:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[object, tuple[float, T]] = {}
        self._inflight: dict[object, asyncio.Task[T]] = {}
        self._lock = asyncio.Lock()

    async def get_or_compute(self, key: object, compute: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            cached = self._cache.get(key)
            now = time.monotonic()
            if cached is not None and cached[0] > now:
                return cached[1]

            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(compute())
                self._inflight[key] = task

        try:
            value = await task
        except Exception:
            async with self._lock:
                current = self._inflight.get(key)
                if current is task:
                    self._inflight.pop(key, None)
            raise

        async with self._lock:
            self._cache[key] = (time.monotonic() + self.ttl_seconds, value)
            current = self._inflight.get(key)
            if current is task:
                self._inflight.pop(key, None)
        return value
