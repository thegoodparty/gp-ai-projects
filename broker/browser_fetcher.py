"""Browser-rendered fetch with stealth. Backs the unified `/http/fetch` route.

Why: plain httpx is 403'd by Cloudflare's JS challenge on many municipal
agenda sites (e.g. CivicEngage, alvin.gov). A real Chromium + stealth
fingerprint patches gets through, then captures the response — including
PDFs/DOCX/anything that arrives as Content-Disposition: attachment downloads,
not as the navigation response.

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
    async def fetch(self, url: str) -> BrowserFetchResult: ...


class PlaywrightBrowserFetcher:
    """Persistent Chromium + stealth, one context per fetch.

    Cold-launch is ~3s; we pay it once at app startup. Each fetch opens a
    fresh `BrowserContext` (isolated cookies/storage), applies stealth to the
    new page, and intercepts every outbound request through `context.route`
    so we can reject SSRF attempts mid-flight (intermediate redirect hops,
    sub-resources, etc.) — the same posture the previous httpx path enforced
    via per-hop validation in `resolve_redirects`.

    fetch() handles both the page-response path (HTML / API JSON) and the
    download path (PDFs/DOCX served as Content-Disposition: attachment) in
    one call. Content-type for downloads comes from the response listener
    that captured the upstream Content-Type header — NOT hardcoded.
    """

    def __init__(self, max_concurrent: int = 4) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
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

    async def fetch(self, url: str) -> BrowserFetchResult:
        async with self._semaphore:
            return await self._fetch_impl(url)

    async def _fetch_impl(self, url: str) -> BrowserFetchResult:
        from playwright.async_api import Download, Route
        from playwright.async_api import Error as PlaywrightError
        from playwright_stealth import stealth_async  # type: ignore[import-untyped]

        if self._browser is None:
            raise RuntimeError("PlaywrightBrowserFetcher.start() must be awaited before fetch()")

        violations: list[str] = []

        def _raise_if_violation() -> None:
            if violations:
                raise HTTPException(
                    status_code=400,
                    detail=f"SSRF blocked mid-fetch: {violations[0]}",
                )

        async def _route_handler(route: Route) -> None:
            req_url = route.request.url
            try:
                await validate_url(req_url)
            except HTTPException as e:
                violations.append(f"{req_url}: {e.detail}")
                await route.abort()
                return
            await route.continue_()

        # Capture every response Chromium sees so we can recover the real
        # content-type for downloads (which fire via page.on("download")
        # AFTER the response that triggered them has already been emitted).
        # Key: response URL, value: (lowercased content-type, status).
        captured_responses: dict[str, tuple[str, int]] = {}

        def _response_listener(response: object) -> None:
            try:
                resp_url = response.url  # type: ignore[attr-defined]
                headers = response.headers  # type: ignore[attr-defined]
                status = response.status  # type: ignore[attr-defined]
            except AttributeError:
                return
            ct = (headers.get("content-type") or "").split(";")[0].strip().lower()
            captured_responses[resp_url] = (ct, status)

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
            page.on("response", _response_listener)

            response = None
            nav_error: Exception | None = None
            try:
                response = await page.goto(url, timeout=NAVIGATION_TIMEOUT_MS)
            except PlaywrightError as e:
                nav_error = e

            _raise_if_violation()

            # Always wait briefly for a download event — page.goto either
            # returns (page response path) or raises with "Download is
            # starting" (download path); in both cases the download event
            # may arrive slightly later.
            if not downloads:
                for _ in range(int(DOWNLOAD_WAIT_MS / 100)):
                    if downloads:
                        break
                    await page.wait_for_timeout(100)
                    _raise_if_violation()
                    # Short-circuit: if goto succeeded and no download fired
                    # quickly, stop waiting and treat as page response.
                    if response is not None and nav_error is None:
                        break

            if downloads:
                dl = downloads[0]
                final_url = dl.url
                await validate_url(final_url)
                _raise_if_violation()
                body = await _read_download_bytes(dl)
                _raise_if_violation()
                captured = captured_responses.get(final_url)
                content_type = captured[0] if captured and captured[0] else "application/octet-stream"
                return BrowserFetchResult(
                    status=200,
                    content_type=content_type,
                    body=body,
                    final_url=final_url,
                )

            if nav_error is not None:
                logger.warning("playwright navigation error url=%s error=%s", url, nav_error)
                raise HTTPException(
                    status_code=502,
                    detail="upstream navigation failed",
                )

            if response is None:
                raise HTTPException(
                    status_code=502,
                    detail="upstream navigation failed",
                )

            await page.wait_for_timeout(POST_NAV_SETTLE_MS)
            _raise_if_violation()

            status = response.status
            content_type = (response.headers.get("content-type") or "").split(";")[
                0
            ].strip().lower() or "application/octet-stream"
            body = await response.body()
            _raise_if_violation()
            final_url = page.url
            await validate_url(final_url)
            _raise_if_violation()

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


async def _read_download_bytes(download: object) -> bytes:
    """Save Playwright Download to a safely-created temp file, read via
    asyncio.to_thread so the sync I/O doesn't stall the event loop, and clean up.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        await download.save_as(tmp_path)  # type: ignore[attr-defined]
        return await asyncio.to_thread(_read_file_bytes, tmp_path)
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
