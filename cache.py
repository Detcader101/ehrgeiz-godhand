"""TTL cache + single-flight dedupe for outbound HTTP calls.

Sits in front of every wavu/ewgf lookup. Two jobs:

1. **Cache** successful results for a short TTL so a rapid sequence of clicks
   from one user (Verify -> My Profile -> Refresh) doesn't make the same
   request three times.
2. **Single-flight** concurrent requests for the same key — if two users in
   different guilds refresh the same Tekken ID at once, exactly one outbound
   request happens; both callers receive the same result (or the same
   exception).

Failures are *not* cached — a user who typed a wrong Tekken ID can correct
it and retry without waiting for a TTL. The single-flight dedupe still
protects against burst storms during outages.

Bot runs as one process per host, so in-process state is enough; no redis.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

DEFAULT_TTL_S = 300.0  # 5 minutes


class _Entry:
    __slots__ = ("expires_at", "value")

    def __init__(self, expires_at: float, value: object) -> None:
        self.expires_at = expires_at
        self.value = value


class TTLCache:
    """Designed for single-event-loop use (which is how discord.py runs).
    Not thread-safe; doesn't need to be."""

    def __init__(self, *, default_ttl_s: float = DEFAULT_TTL_S) -> None:
        self._default_ttl_s = default_ttl_s
        self._entries: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    async def get_or_fetch(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        *,
        ttl_s: float | None = None,
        force_refresh: bool = False,
    ) -> T:
        """Return cached value if fresh; otherwise call `factory()` exactly
        once across concurrent callers and cache the result.

        `force_refresh=True` bypasses an existing cache entry but still uses
        single-flight: it's safe to call from a user "Refresh" action without
        causing thundering-herd against the upstream source.
        """
        if not force_refresh:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.expires_at > time.monotonic():
                    return entry.value  # type: ignore[return-value]
                del self._entries[key]

        inflight = self._inflight.get(key)
        if inflight is not None:
            # Someone is already fetching this key; piggyback.
            return await inflight  # type: ignore[no-any-return]

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = fut
        try:
            result = await factory()
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
                # Mark retrieved so asyncio doesn't log "exception was never
                # retrieved" when there are no concurrent piggybackers.
                fut.exception()
            raise
        else:
            if not fut.done():
                fut.set_result(result)
            ttl = self._default_ttl_s if ttl_s is None else ttl_s
            self._entries[key] = _Entry(time.monotonic() + ttl, result)
            return result
        finally:
            self._inflight.pop(key, None)
