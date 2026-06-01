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

Memory model:
  - page-response path returns `body: bytes` (Playwright `response.body()`
    has no streaming API). Capped at PAGE_RESPONSE_MAX_BYTES (10 MB) — the
    pages we navigate are HTML/JSON, never multi-hundred-MB.
  - download path returns `body_path: str` pointing at a temp file. Caller
    streams from disk and is responsible for unlinking. Capped at MAX_BYTES
    (250 MB) checked at-rest via os.path.getsize(), so we never amplify the
    file into a buffer.
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


class _ViolationTracker:
    """Tracks SSRF policy violations seen while a page renders.

    A violation on the MAIN navigation/document request is fatal — the caller
    asked to fetch an SSRF target directly, so the whole fetch must fail. A
    violation on a SUB-RESOURCE (third-party tracker, ad, analytics script) is
    aborted by the route handler for safety but is NOT fatal: real pages embed
    dozens of third-party resources, many on dead or non-allowlisted domains,
    and failing the whole fetch on a benign embedded tracker turned legitimate
    public pages into 'SSRF blocked mid-fetch' red herrings. Blocking the
    sub-resource request (via route.abort) already provides the protection;
    discarding the legitimately-fetched main page on top of that adds none.
    """

    def __init__(self) -> None:
        self._fatal: str | None = None

    def record(self, url: str, detail: str, is_navigation: bool) -> None:
        # Keep only the first navigation (main-document) violation — that's the
        # one that makes the fetch fatal. Sub-resource violations are aborted by
        # the caller but intentionally not recorded as fatal.
        if is_navigation and self._fatal is None:
            self._fatal = f"{url}: {detail}"

    def fatal(self) -> str | None:
        return self._fatal

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

NAVIGATION_TIMEOUT_MS = 45_000
DOWNLOAD_WAIT_MS = 30_000
INITIAL_DOWNLOAD_GRACE_MS = 800
BINARY_DOWNLOAD_WAIT_MS = 3_000
POST_NAV_SETTLE_MS = 1_500

# Shared with broker.endpoints.http_fetch — the endpoint references this
# constant for clarity, but the fetcher enforces it (downloads check at-rest
# size before returning).
MAX_BYTES = 250 * 1024 * 1024  # 250 MB — download path
PAGE_RESPONSE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — page-response (in-memory)

_ACLOSE_DRAIN_TIMEOUT_S = 30


@dataclass(frozen=True)
class BrowserFetchResult:
    """Polymorphic: exactly one of body / body_path is non-None.

    body: bytes for page-response path (HTML, JSON, XML — buffered in memory,
    capped at PAGE_RESPONSE_MAX_BYTES).

    body_path: filesystem path for downloads (PDF, DOCX, etc.) — caller streams
    from disk and is responsible for unlinking.
    """

    status: int
    content_type: str
    final_url: str
    byte_size: int
    body: bytes | None = None
    body_path: str | None = None


class BrowserFetcher(Protocol):
    async def fetch(self, url: str) -> BrowserFetchResult: ...


def _is_binary_content_type(ct: str) -> bool:
    """True if the content-type indicates a binary payload that may have a
    late-arriving download trigger (PDF, DOCX, octet-stream, etc.). Used to
    decide whether to pay the secondary BINARY_DOWNLOAD_WAIT_MS grace."""
    if not ct:
        return False
    ct = ct.lower()
    if ct.startswith("application/pdf"):
        return True
    if ct.startswith("application/octet-stream"):
        return True
    if ct.startswith("application/zip"):
        return True
    if ct.startswith("application/vnd.openxmlformats"):
        return True
    if ct.startswith("application/msword"):
        return True
    if ct.startswith("application/vnd.ms-"):
        return True
    if ct.startswith("image/") or ct.startswith("audio/") or ct.startswith("video/"):
        return True
    return False


def _is_textual_content_type(ct: str) -> bool:
    if not ct:
        return False
    ct = ct.lower()
    return (
        ct.startswith("text/")
        or ct.startswith("application/json")
        or ct.startswith("application/xml")
        or ct.startswith("application/ld+json")
    )


class PlaywrightBrowserFetcher:
    # 30 concurrent contexts on a 4 vCPU / 8 GB Fargate task. Stress-tested
    # incrementally on 2026-05-16:
    #   max_concurrent=8,  100 reqs burst:  CPU peak ~30%, mem peak ~8%
    #   max_concurrent=20, 200 reqs burst:  CPU peak ~50%, mem peak ~8.5%
    # Per-context CPU cost is ~2.5% (sub-linear vs. linear projection because
    # network-wait dominates each fetch's lifecycle, so 30 contexts don't all
    # hit JS-exec at the same instant). Predicted at 30 concurrent: ~75% peak
    # CPU and ~12% memory — above the 55% CPU autoscale target, so sustained
    # bursts will reliably trigger the ECS service to add a second task.
    # Intentional: prefer scale-out under demand to leaving throughput on the
    # table. Memory stays well clear of the 70% target.
    #
    # If sustained load keeps the service scaled out at high cost, the next
    # move is the path-based pool split described in broker/README.md
    # (/http/fetch gets its own target group with a memory-tuned task size).
    def __init__(self, max_concurrent: int = 30) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._closing = False
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
        """Drain in-flight fetches, then close the browser.

        Sets _closing so any new fetch() call rejects with 503 immediately.
        Acquires all semaphore permits to wait for in-flight fetches to drain
        (with a 30s ceiling — if a fetch is stuck past that, we log and tear
        down anyway to keep deploys moving)."""
        self._closing = True
        acquired = 0
        try:
            for _ in range(self._max_concurrent):
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=_ACLOSE_DRAIN_TIMEOUT_S
                )
                acquired += 1
        except TimeoutError:
            logger.warning(
                "browser_fetcher.aclose timed out waiting for in-flight fetches"
            )
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = None
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            finally:
                self._playwright = None
                for _ in range(acquired):
                    self._semaphore.release()

    async def fetch(self, url: str) -> BrowserFetchResult:
        if self._closing:
            raise HTTPException(status_code=503, detail="browser fetcher shutting down")
        async with self._semaphore:
            if self._closing:
                raise HTTPException(
                    status_code=503, detail="browser fetcher shutting down"
                )
            return await self._fetch_impl(url)

    async def _fetch_impl(self, url: str) -> BrowserFetchResult:
        from playwright.async_api import Download, Route
        from playwright.async_api import Error as PlaywrightError
        from playwright_stealth import stealth_async  # type: ignore[import-untyped]

        if self._browser is None:
            raise RuntimeError(
                "PlaywrightBrowserFetcher.start() must be awaited before fetch()"
            )

        tracker = _ViolationTracker()

        def _raise_if_violation() -> None:
            fatal = tracker.fatal()
            if fatal is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"SSRF blocked mid-fetch: {fatal}",
                )

        async def _route_handler(route: Route) -> None:
            req_url = route.request.url
            try:
                await validate_url(req_url)
            except HTTPException as e:
                # Block the request either way (safety). Only a violation on the
                # main navigation/document request is fatal; a blocked third-party
                # sub-resource (tracker/ad) must not fail the whole page fetch.
                is_navigation = route.request.is_navigation_request()
                tracker.record(req_url, str(e.detail), is_navigation)
                if not is_navigation:
                    logger.debug("aborted SSRF sub-resource (non-fatal): %s", req_url)
                await route.abort()
                return
            await route.continue_()

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
        try:
            page = await context.new_page()
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

            # 1) Initial grace window — Cloudflare and other JS challenges
            # frequently trigger downloads 200-500 ms after page.goto settles.
            # Always wait this regardless of response state.
            await self._wait_for_download(
                page, downloads, INITIAL_DOWNLOAD_GRACE_MS, _raise_if_violation
            )

            if not downloads:
                # 2) If goto raised (download path with no response), keep waiting
                # the full DOWNLOAD_WAIT_MS for the download event.
                if response is None or nav_error is not None:
                    remaining = max(DOWNLOAD_WAIT_MS - INITIAL_DOWNLOAD_GRACE_MS, 0)
                    await self._wait_for_download(
                        page, downloads, remaining, _raise_if_violation
                    )
                else:
                    # 3) Successful navigation with a response: only pay the
                    # secondary download window for binary content-types.
                    ct_initial = (
                        (response.headers.get("content-type") or "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if _is_binary_content_type(ct_initial):
                        await self._wait_for_download(
                            page,
                            downloads,
                            BINARY_DOWNLOAD_WAIT_MS,
                            _raise_if_violation,
                        )
                    # textual content-types: no extra wait — no download is coming

            if downloads:
                dl = downloads[0]
                final_url = dl.url
                await validate_url(final_url)
                _raise_if_violation()
                body_path, byte_size = await _save_download_to_disk(dl)
                # asyncio.to_thread inside _save_download_to_disk yielded to
                # the event loop. A sub-resource SSRF could have appended to
                # violations[] during that window; the file is already on
                # disk. Unlink before raising — the endpoint never sees the
                # result so its BackgroundTask cleanup won't run.
                if tracker.fatal():
                    try:
                        await asyncio.to_thread(os.unlink, body_path)
                    except OSError:
                        logger.warning(
                            "failed to unlink leaked download temp path=%s",
                            body_path,
                        )
                    _raise_if_violation()
                captured = captured_responses.get(final_url)
                content_type = (
                    captured[0]
                    if captured and captured[0]
                    else "application/octet-stream"
                )
                return BrowserFetchResult(
                    status=200,
                    content_type=content_type,
                    final_url=final_url,
                    byte_size=byte_size,
                    body=None,
                    body_path=body_path,
                )

            if nav_error is not None:
                logger.warning(
                    "playwright navigation error url=%s error=%s", url, nav_error
                )
                raise HTTPException(
                    status_code=502,
                    detail="upstream navigation failed",
                )

            if response is None:
                raise HTTPException(
                    status_code=502,
                    detail="upstream navigation failed",
                )

            status = response.status
            content_type = (response.headers.get("content-type") or "").split(";")[
                0
            ].strip().lower() or "application/octet-stream"

            # Conditional settle: only HTML and binary responses may have
            # late sub-resources / late download triggers. JSON/XML/text get
            # zero settle. networkidle timeout is acceptable — we've already
            # waited the budgeted window.
            if _is_binary_content_type(content_type) or content_type.startswith(
                "text/html"
            ):
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=POST_NAV_SETTLE_MS
                    )
                except PlaywrightError:
                    pass
                _raise_if_violation()

                # A late download may have fired during the networkidle wait.
                if downloads:
                    dl = downloads[0]
                    final_url = dl.url
                    await validate_url(final_url)
                    _raise_if_violation()
                    body_path, byte_size = await _save_download_to_disk(dl)
                    # See the early-download path: temp file is already on disk
                    # if a sub-resource SSRF fired during the asyncio.to_thread
                    # yield. Unlink before raising so the file doesn't leak.
                    if tracker.fatal():
                        try:
                            await asyncio.to_thread(os.unlink, body_path)
                        except OSError:
                            logger.warning(
                                "failed to unlink leaked download temp path=%s",
                                body_path,
                            )
                        _raise_if_violation()
                    captured = captured_responses.get(final_url)
                    ct = (
                        captured[0]
                        if captured and captured[0]
                        else "application/octet-stream"
                    )
                    return BrowserFetchResult(
                        status=200,
                        content_type=ct,
                        final_url=final_url,
                        byte_size=byte_size,
                        body=None,
                        body_path=body_path,
                    )

            body = await response.body()
            _raise_if_violation()
            if len(body) > PAGE_RESPONSE_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"page response exceeded {PAGE_RESPONSE_MAX_BYTES} bytes",
                )
            final_url = page.url
            await validate_url(final_url)
            _raise_if_violation()

            return BrowserFetchResult(
                status=status,
                content_type=content_type,
                final_url=final_url,
                byte_size=len(body),
                body=body,
                body_path=None,
            )
        finally:
            try:
                await context.close()
            except Exception:
                logger.warning("failed to close browser context", exc_info=True)

    async def _wait_for_download(
        self,
        page: object,
        downloads: list,
        budget_ms: int,
        raise_if_violation,
    ) -> None:
        """Spin-wait for a download event up to budget_ms, in 100 ms slices.
        Exits early if a download fires. Re-checks SSRF violations every
        slice so a route-handler abort during the wait raises promptly."""
        if budget_ms <= 0:
            return
        slices = max(int(budget_ms / 100), 1)
        for _ in range(slices):
            if downloads:
                return
            await page.wait_for_timeout(100)  # type: ignore[attr-defined]
            raise_if_violation()
            if downloads:
                return


async def _save_download_to_disk(download: object) -> tuple[str, int]:
    """Save Playwright Download to a safely-created temp file. Returns
    (path, byte_size). Enforces MAX_BYTES at-rest; unlinks and raises 413 if
    the saved file exceeds the cap.

    Caller (the endpoint) is responsible for unlinking the path AFTER the
    response is streamed back to the client.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        await download.save_as(tmp_path)  # type: ignore[attr-defined]
        byte_size = await asyncio.to_thread(os.path.getsize, tmp_path)
        if byte_size > MAX_BYTES:
            try:
                await asyncio.to_thread(os.unlink, tmp_path)
            except OSError:
                logger.warning(
                    "failed to unlink oversized download tmp_path=%s", tmp_path
                )
            raise HTTPException(
                status_code=413,
                detail=f"download exceeded {MAX_BYTES} bytes",
            )
        return tmp_path, byte_size
    except HTTPException:
        raise
    except Exception:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass
        raise
