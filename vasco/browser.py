"""Camoufox singleton browser pool.

Lazy-starts a single Firefox (Camoufox) instance on first `.fetch()`. Pages
are created per URL and closed after content extraction. Shutdown is the
caller's responsibility — `fetch.fetch_one` / `fetch_many` call `.close()`
in a `finally` block.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

try:  # pragma: no cover - camoufox is an optional dep at import time.
    from camoufox.async_api import AsyncCamoufox
except Exception:  # pragma: no cover
    AsyncCamoufox = None  # type: ignore[assignment]


class BrowserPool:
    """Owns one Camoufox browser context for an invocation."""

    def __init__(self, *, headless: bool = True, locale: str = "en-US") -> None:
        self._headless = headless
        self._locale = locale
        self._lock = asyncio.Lock()
        self._cm: Any | None = None
        self._browser: Any | None = None

    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            if AsyncCamoufox is None:
                raise RuntimeError(
                    "camoufox is not installed; cannot start browser tier"
                )
            self._cm = AsyncCamoufox(
                headless=self._headless,
                locale=(self._locale,),
            )
            self._browser = await self._cm.__aenter__()

    async def fetch(
        self, url: str, *, deadline_monotonic: float
    ) -> tuple[str, int, dict[str, str]]:
        """Fetch a URL via the browser tier.

        Returns (html, status, headers). Raises asyncio.TimeoutError if the
        deadline has already passed.
        """
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                "deadline elapsed before browser fetch could start"
            )

        await self._ensure_started()
        assert self._browser is not None

        page = await self._browser.new_page()
        try:
            remaining_ms = int(
                max(0.0, deadline_monotonic - time.monotonic()) * 1000
            )
            if remaining_ms <= 0:
                raise asyncio.TimeoutError(
                    "deadline elapsed before page.goto could start"
                )
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=remaining_ms,
            )

            remaining_ms = int(
                max(0.0, deadline_monotonic - time.monotonic()) * 1000
            )
            if remaining_ms > 0:
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=remaining_ms
                    )
                except Exception:
                    pass

            html = await page.content()
            status = int(response.status) if response is not None else 0
            headers: dict[str, str] = {}
            if response is not None:
                try:
                    raw = await response.all_headers()
                    headers = {str(k): str(v) for k, v in raw.items()}
                except Exception:
                    try:
                        headers = {
                            str(k): str(v)
                            for k, v in (response.headers or {}).items()
                        }
                    except Exception:
                        headers = {}
            return html, status, headers
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def close(self) -> None:
        if self._cm is None:
            return
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception:
            pass
        finally:
            self._cm = None
            self._browser = None


_pool: BrowserPool | None = None


def get_browser(cfg: Any | None = None) -> BrowserPool:
    """Return the process-wide BrowserPool singleton.

    First call constructs the object (consulting cfg.browser if provided) but
    does NOT start Firefox; the browser is started on the first `.fetch()`.
    Subsequent calls return the same instance; passed cfg is ignored once the
    pool exists.
    """
    global _pool
    if _pool is None:
        headless = True
        locale = "en-US"
        if cfg is not None:
            try:
                headless = bool(cfg.browser.headless)
                locale = str(cfg.browser.locale)
            except Exception:
                pass
        _pool = BrowserPool(headless=headless, locale=locale)
    return _pool


def _reset_for_tests() -> None:
    """Test-only hook: drop the singleton so a fresh one is created."""
    global _pool
    _pool = None
