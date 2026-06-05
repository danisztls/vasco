"""Cross-consumer coordination for vascod's fetch path.

Two mechanisms, both meaningful only because every consumer funnels through one
resident process:

- **Single-flight:** concurrent *identical* fetches (claudinho's gather, MCP, the
  CLI all at once) collapse to a single in-flight fetch; every caller awaits the
  same result. The work runs in a coordinator-owned task, so one caller's
  cancellation (e.g. a dropped connection) can never kill the shared fetch.
- **Per-domain rate limit:** a domain can be paced centrally (min-interval per
  registered domain) instead of each process pacing itself blind to the others.
  Applied only when the fetch will actually hit the network — a cache hit skips
  it (using the same ``cache.get`` the pipeline uses, so "hit" means the same
  thing here as in ``fetch_one``).
- **Per-domain concurrency cap:** a registered domain can be limited to N
  simultaneous in-flight network fetches (a per-domain semaphore), so one origin
  never gets a wall of concurrent connections (the bot tell) and can't hog every
  global browser slot. Different domains never block each other; cache hits skip
  it too.

Both pacing gates run *before* ``fetch_one`` is called, and ``fetch_one`` starts
its own deadline clock internally (``now + deadline``) — so time spent waiting in
the queue is never charged against a fetch's budget.

Scope: the ``fetch`` op, and ``fetch_many`` (which the daemon runs as a
coordinated gather over ``fetch``). ``extract``/``answer``/``map`` call
``fetch_one`` internally, so they are coordinated by the shared cache but not
single-flighted — a deliberate 80/20 (the hot, high-concurrency path is
``fetch``; claudinho uses only that).

Invariant: every coordinated fetch is deadline-bounded by ``fetch_one`` itself,
so a single-flight task always completes and can never strand its waiters (e.g.
across a host suspend) — see the plan's suspend/resume analysis.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from vasco import cache as _cache_mod
from vasco.fetch import fetch_one as _fetch_one

log = logging.getLogger(__name__)


class _DomainRateLimiter:
    """Per-registered-domain min-interval gate.

    Different domains never block each other; concurrent requests to one domain
    are spaced by ``min_interval``. Disabled when ``rps <= 0``.
    """

    def __init__(self, rps: float) -> None:
        self._min_interval = (1.0 / rps) if rps and rps > 0 else 0.0
        self._next_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._min_interval > 0

    async def acquire(self, domain: str) -> None:
        if self._min_interval <= 0:
            return
        # Reserve this domain's next slot under a brief lock, then sleep outside
        # it so other domains proceed concurrently.
        async with self._lock:
            now = asyncio.get_running_loop().time()
            start = max(now, self._next_at.get(domain, 0.0))
            self._next_at[domain] = start + self._min_interval
            wait = start - now
        if wait > 0:
            await asyncio.sleep(wait)


class _DomainConcurrencyLimiter:
    """Per-registered-domain cap on simultaneous in-flight fetches.

    Each domain gets its own lazily-created ``asyncio.Semaphore(n)``; different
    domains never contend. ``acquire(domain)`` is an async context manager held
    for the duration of the fetch. Disabled (a no-op) when ``n <= 0``.
    """

    def __init__(self, n: int) -> None:
        self._n = n if n and n > 0 else 0
        self._sems: dict[str, asyncio.Semaphore] = {}

    @property
    def enabled(self) -> bool:
        return self._n > 0

    def slot(self, domain: str) -> Any:
        if self._n <= 0:
            return _nullcontext()
        sem = self._sems.get(domain)
        if sem is None:
            sem = asyncio.Semaphore(self._n)
            self._sems[domain] = sem
        return sem


class _nullcontext:
    """Async no-op context manager (the limiter-disabled path)."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> None:
        return None


class Coordinator:
    """Owns the resident cache and coordinates fetches across all consumers."""

    def __init__(self, cfg: Any, cache: Any) -> None:
        self._cfg = cfg
        self._cache = cache
        svc = getattr(cfg, "service", None)
        self._single_flight = bool(getattr(svc, "single_flight", True))
        self._limiter = _DomainRateLimiter(
            float(getattr(svc, "rate_limit_rps", 0.0) or 0.0)
        )
        self._concurrency = _DomainConcurrencyLimiter(
            int(getattr(svc, "max_concurrent_per_domain", 0) or 0)
        )
        self._inflight: dict[tuple[Any, ...], asyncio.Task] = {}

    async def fetch(
        self,
        url: str,
        *,
        mode: str = "auto",
        deadline: float = 30.0,
        use_cache: bool = True,
        refresh: bool = False,
        raw: bool = False,
    ) -> dict[str, Any]:
        kw = dict(
            mode=mode, deadline=deadline, use_cache=use_cache, refresh=refresh, raw=raw
        )
        if not self._single_flight:
            return await self._run(url, **kw)

        # Key on everything that changes the result, so e.g. a refresh request
        # never joins a non-refresh one and gets stale data.
        key = (
            _cache_mod.normalize_url(url),
            mode,
            bool(raw),
            bool(refresh),
            bool(use_cache),
        )
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._run(url, **kw))
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._inflight.pop(k, None))
        # shield so a caller's cancellation doesn't cancel the shared fetch.
        return await asyncio.shield(task)

    async def _run(
        self,
        url: str,
        *,
        mode: str,
        deadline: float,
        use_cache: bool,
        refresh: bool,
        raw: bool,
    ) -> dict[str, Any]:
        kw = dict(
            mode=mode, deadline=deadline, use_cache=use_cache, refresh=refresh, raw=raw
        )
        # Pacing only matters for fetches that will hit the network — a cache hit
        # does no I/O, so it skips the queue entirely.
        pace = (self._limiter.enabled or self._concurrency.enabled) and not (
            self._is_cache_hit(url, use_cache=use_cache, refresh=refresh)
        )
        if not pace:
            return await self._fetch(url, **kw)

        domain = _cache_mod.registered_domain(url)
        # The concurrency slot is held for the whole fetch (caps simultaneous
        # connections to one origin); the min-interval spaces the *starts* within
        # it. Both gate here, before `_fetch_one` arms its own deadline, so a
        # request that waits in the queue keeps its full fetch budget.
        async with self._concurrency.slot(domain):
            if self._limiter.enabled:
                await self._limiter.acquire(domain)
            return await self._fetch(url, **kw)

    async def _fetch(
        self,
        url: str,
        *,
        mode: str,
        deadline: float,
        use_cache: bool,
        refresh: bool,
        raw: bool,
    ) -> dict[str, Any]:
        return await _fetch_one(
            url,
            mode=mode,
            deadline=deadline,
            use_cache=use_cache,
            refresh=refresh,
            raw=raw,
            cache=self._cache,
            cfg=self._cfg,
        )

    def _is_cache_hit(self, url: str, *, use_cache: bool, refresh: bool) -> bool:
        if not use_cache or refresh:
            return False
        try:
            return self._cache.get(_cache_mod.normalize_url(url)) is not None
        except Exception:
            return False
