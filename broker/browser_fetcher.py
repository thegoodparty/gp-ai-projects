"""Browser-rendered fetch with stealth. Drop-in replacement for the httpx
fetch path that was previously used by `/http/fetch` and `/pdf/fetch`.

Why: plain httpx is 403'd by Cloudflare's JS challenge on many municipal
agenda sites (e.g. CivicEngage, alvin.gov). A real Chromium + stealth
fingerprint patches gets through, then captures the response — including
PDFs that arrive as Content-Disposition: attachment downloads, not as the
navigation response.

DI shape: endpoints depend on the `BrowserFetcher` protocol so tests can
inject an in-memory fake. Production wiring constructs a single
`PlaywrightBrowserFetcher` at app startup (browser kept warm across requests,
fresh context per request) and registers it via FastAPI dependency_overrides.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException

from broker.ssrf_guard import validate_url

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

NAVIGATION_TIMEOUT_MS = 45_000
DOWNLOAD_WAIT_MS = 30_000
POST_NAV_SETTLE_MS = 1_500


@dataclass(frozen=True)
class BrowserFetchResult:
    status: int
    content_type: str
    body: bytes
    final_url: str


class BrowserFetcher(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        capture_download: bool = False,
    ) -> BrowserFetchResult: ...


class PlaywrightBrowserFetcher:
    """Persistent Chromium + stealth, one context per fetch.

    Cold-launch is ~3s; we pay it once at app startup. Each fetch opens a
    fresh `BrowserContext` (isolated cookies/storage), applies stealth to the
    new page, and intercepts every outbound request through `context.route`
    so we can reject SSRF attempts mid-flight (intermediate redirect hops,
    sub-resources, etc.) — the same posture the previous httpx path enforced
    via per-hop validation in `resolve_redirects`.

    `capture_download=True` is for endpoints (like /pdf/fetch) that need to
    grab the file when the upstream sends Content-Disposition: attachment.
    page.goto() raises with "Download is starting" in that case and the bytes
    arrive via the `download` event — see the working reference at
    /tmp/fetch_alvin_stealth.py (Alvin TX agenda PDF).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(
        self,
        url: str,
        *,
        capture_download: bool = False,
    ) -> BrowserFetchResult:
        from playwright.async_api import Download, Route
        from playwright.async_api import Error as PlaywrightError
        from playwright_stealth import stealth_async  # type: ignore[import-untyped]

        if self._browser is None:
            raise RuntimeError(
                "PlaywrightBrowserFetcher.start() must be awaited before fetch()"
            )

        violations: list[str] = []

        async def _route_handler(route: Route) -> None:
            req_url = route.request.url
            try:
                await validate_url(req_url)
            except HTTPException as e:
                violations.append(f"{req_url}: {e.detail}")
                await route.abort()
                return
            await route.continue_()

        context = await self._browser.new_context(
            user_agent=USER_AGENT,
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        try:
            await stealth_async(page)
            await context.route("**/*", _route_handler)

            downloads: list[Download] = []
            page.on("download", lambda d: downloads.append(d))

            response = None
            nav_error: Exception | None = None
            try:
                response = await page.goto(url, timeout=NAVIGATION_TIMEOUT_MS)
            except PlaywrightError as e:
                nav_error = e

            if violations:
                raise HTTPException(
                    status_code=400,
                    detail=f"SSRF blocked mid-fetch: {violations[0]}",
                )

            if capture_download:
                # The download path is normal here — page.goto raises when
                # navigation becomes a download. Wait briefly for the event.
                if not downloads:
                    for _ in range(int(DOWNLOAD_WAIT_MS / 100)):
                        if downloads:
                            break
                        await page.wait_for_timeout(100)

                if downloads:
                    dl = downloads[0]
                    final_url = dl.url
                    try:
                        await validate_url(final_url)
                    except HTTPException:
                        raise
                    tmp_path = tempfile.mktemp(suffix=".bin")
                    try:
                        await dl.save_as(tmp_path)
                        with open(tmp_path, "rb") as f:
                            body = f.read()
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                    return BrowserFetchResult(
                        status=200,
                        content_type="application/pdf",
                        body=body,
                        final_url=final_url,
                    )

                # No download fired — fall through to treat as a regular page
                # response. If nav_error is set, the upstream actually failed.
                if nav_error is not None:
                    raise HTTPException(
                        status_code=502,
                        detail=f"upstream navigation failed: {nav_error}",
                    )

            if nav_error is not None:
                raise HTTPException(
                    status_code=502,
                    detail=f"upstream navigation failed: {nav_error}",
                )

            if response is None:
                raise HTTPException(
                    status_code=502,
                    detail="upstream returned no response",
                )

            await page.wait_for_timeout(POST_NAV_SETTLE_MS)

            status = response.status
            content_type = (
                (response.headers.get("content-type") or "")
                .split(";")[0]
                .strip()
                or "application/octet-stream"
            )
            body = await response.body()
            final_url = page.url

            return BrowserFetchResult(
                status=status,
                content_type=content_type,
                body=body,
                final_url=final_url,
            )
        finally:
            try:
                await context.close()
            except Exception:
                logger.warning("failed to close browser context", exc_info=True)
