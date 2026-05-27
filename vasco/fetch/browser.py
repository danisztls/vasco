"""Camoufox singleton browser pool.

Lazy-starts a single Firefox (Camoufox) instance on first `.fetch()`. Pages
are created per URL and closed after content extraction. Shutdown is the
caller's responsibility — `fetch.fetch_one` / `fetch_many` call `.close()`
in a `finally` block.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

try:  # pragma: no cover - camoufox is an optional dep at import time.
    from camoufox.async_api import AsyncCamoufox
except Exception:  # pragma: no cover
    AsyncCamoufox = None  # type: ignore[assignment]


_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
    "Mobile/15E148 Safari/604.1"
)
_MOBILE_VIEWPORT = {"width": 393, "height": 852}


class BrowserPool:
    """Owns one Camoufox browser context for an invocation."""

    def __init__(
        self,
        *,
        headless: bool = True,
        locale: str = "en-US",
        user_data_dir: str = "",
    ) -> None:
        self._headless = headless
        self._locale = locale
        # Persistent profile dir: when non-empty, Camoufox runs in
        # persistent_context mode so cookies (Cloudflare clearance, login
        # sessions, etc.) survive across Vasco runs. In that mode
        # AsyncCamoufox.__aenter__ yields a BrowserContext rather than a
        # Browser — we lose `.new_context()`, so the mobile recovery tier
        # falls back to UA/viewport overrides on a context page.
        self._user_data_dir = _expand_profile_dir(user_data_dir)
        self._is_persistent = bool(self._user_data_dir)
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
            kwargs: dict[str, Any] = {
                "headless": self._headless,
                "locale": (self._locale,),
            }
            if self._is_persistent:
                os.makedirs(self._user_data_dir, exist_ok=True)
                kwargs["persistent_context"] = True
                kwargs["user_data_dir"] = self._user_data_dir
            try:
                self._cm = AsyncCamoufox(**kwargs)
                self._browser = await self._cm.__aenter__()
            except Exception as exc:
                if self._is_persistent and "already running" in str(exc):
                    import logging

                    logging.getLogger(__name__).warning(
                        "persistent profile locked, falling back to ephemeral browser"
                    )
                    self._cm = None
                    self._is_persistent = False
                    fallback = {
                        "headless": self._headless,
                        "locale": (self._locale,),
                    }
                    self._cm = AsyncCamoufox(**fallback)
                    self._browser = await self._cm.__aenter__()
                else:
                    raise

    async def fetch(
        self, url: str, *, deadline_monotonic: float, mobile: bool = False
    ) -> tuple[str, int, dict[str, str]]:
        """Fetch a URL via the browser tier.

        Returns (html, status, headers). Raises asyncio.TimeoutError if the
        deadline has already passed.

        When `mobile=True`, the page is created in a fresh context with iOS
        Safari UA, mobile viewport, and touch — used as a recovery tier
        when desktop Camoufox is blocked.
        """
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                "deadline elapsed before browser fetch could start"
            )

        await self._ensure_started()
        assert self._browser is not None

        context = None
        if mobile and not self._is_persistent:
            # NOTE: `is_mobile` and `has_touch` are Chromium-only in Playwright;
            # Camoufox is Firefox-based, so we stick to UA + viewport +
            # device_scale_factor — the three signals most server-side mobile
            # routers actually inspect. The site never sees the missing touch
            # capability since we don't dispatch events here.
            context = await self._browser.new_context(
                user_agent=_MOBILE_USER_AGENT,
                viewport=_MOBILE_VIEWPORT,
                device_scale_factor=3,
            )
            page = await context.new_page()
        else:
            # Persistent mode: AsyncCamoufox yields a BrowserContext, which has
            # `.new_page()` but no `.new_context()`. Mobile recovery applies UA
            # + viewport per-page; device_scale_factor isn't settable here, but
            # most mobile routing checks UA + viewport.
            page = await self._browser.new_page()
            if mobile:
                await page.set_extra_http_headers({"User-Agent": _MOBILE_USER_AGENT})
                await page.set_viewport_size(_MOBILE_VIEWPORT)
        try:
            remaining_ms = int(max(0.0, deadline_monotonic - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise asyncio.TimeoutError(
                    "deadline elapsed before page.goto could start"
                )
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=remaining_ms,
            )

            remaining_ms = int(max(0.0, deadline_monotonic - time.monotonic()) * 1000)
            if remaining_ms > 0:
                try:
                    await page.wait_for_load_state("networkidle", timeout=remaining_ms)
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
                            str(k): str(v) for k, v in (response.headers or {}).items()
                        }
                    except Exception:
                        headers = {}
            return html, status, headers
        finally:
            try:
                await page.close()
            except Exception:
                pass
            if context is not None:
                try:
                    await context.close()
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


def _expand_profile_dir(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))


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
        user_data_dir = ""
        if cfg is not None:
            try:
                headless = bool(cfg.browser.headless)
                locale = str(cfg.browser.locale)
                user_data_dir = str(cfg.browser.user_data_dir or "")
            except Exception:
                pass
        _pool = BrowserPool(
            headless=headless,
            locale=locale,
            user_data_dir=user_data_dir,
        )
    return _pool


def _reset_for_tests() -> None:
    """Test-only hook: drop the singleton so a fresh one is created."""
    global _pool
    _pool = None
