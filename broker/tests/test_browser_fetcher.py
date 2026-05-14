"""Tests for PlaywrightBrowserFetcher hardening.

Three concerns under test:
  1. Concurrency cap via asyncio.Semaphore (replacing dead `_lock`).
  2. Safe temp-file handling + non-blocking read in download path.
  3. SSRF re-check after awaits that may trigger sub-resource requests.

Fakes substitute for playwright runtime types — no real Chromium required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from broker.browser_fetcher import (
    BrowserFetchResult,
    PlaywrightBrowserFetcher,
)


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = _FakeRequest(url)
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = headers or {"content-type": "text/html"}
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeDownload:
    def __init__(self, url: str, payload: bytes) -> None:
        self.url = url
        self._payload = payload

    async def save_as(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self._payload)


class _FakePage:
    """Minimal Page double. Tests can inject hooks to simulate goto/timeout
    behavior — including sub-resource requests that arrive after goto() returns.
    """

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        url: str = "https://example.com/",
        on_settle: Any = None,
    ) -> None:
        self._response = response
        self.url = url
        self._on_settle = on_settle
        self._download_listeners: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        if event == "download":
            self._download_listeners.append(handler)

    async def goto(self, url: str, *, timeout: int) -> _FakeResponse | None:
        return self._response

    async def wait_for_timeout(self, ms: int) -> None:
        if self._on_settle is not None:
            await self._on_settle()

    def emit_download(self, download: _FakeDownload) -> None:
        for handler in self._download_listeners:
            handler(download)


class _FakeContext:
    def __init__(
        self,
        *,
        page: _FakePage,
        route_handler_holder: list[Any],
    ) -> None:
        self._page = page
        self._route_handler_holder = route_handler_holder
        self._closed = False

    async def new_page(self) -> _FakePage:
        return self._page

    async def route(self, pattern: str, handler: Any) -> None:
        self._route_handler_holder.append(handler)

    async def close(self) -> None:
        self._closed = True


class _FakeBrowser:
    def __init__(self, page_factory: Any) -> None:
        self._page_factory = page_factory
        self.contexts_opened = 0
        self.route_handler_holders: list[list[Any]] = []

    async def new_context(self, **_kwargs: Any) -> _FakeContext:
        self.contexts_opened += 1
        page = self._page_factory()
        holder: list[Any] = []
        self.route_handler_holders.append(holder)
        return _FakeContext(page=page, route_handler_holder=holder)


def _patch_stealth(monkeypatch: pytest.MonkeyPatch) -> None:
    """tf-playwright-stealth's stealth_async expects a real page. Replace with a no-op."""

    async def _noop(_page: Any) -> None:
        return None

    import sys
    import types

    if "playwright_stealth" not in sys.modules:
        mod = types.ModuleType("playwright_stealth")
        sys.modules["playwright_stealth"] = mod
    monkeypatch.setattr("playwright_stealth.stealth_async", _noop, raising=False)


def _patch_playwright_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """browser_fetcher.fetch() imports types lazily from playwright.async_api.
    The real `from playwright.async_api import Error as PlaywrightError` works
    because playwright is installed; we only need stealth_async to not blow up.
    """
    _patch_stealth(monkeypatch)


class TestConcurrencyCap:
    """Fix 1: replace dead _lock with semaphore(max_concurrent)."""

    @pytest.mark.asyncio
    async def test_caps_concurrent_fetches_at_max_concurrent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        in_flight = 0
        peak = 0
        gate = asyncio.Event()

        async def on_settle() -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await gate.wait()
            in_flight -= 1

        def make_page() -> _FakePage:
            return _FakePage(
                response=_FakeResponse(body=b"ok"),
                url="https://example.com/",
                on_settle=on_settle,
            )

        fetcher = PlaywrightBrowserFetcher(max_concurrent=4)
        fetcher._browser = _FakeBrowser(make_page)  # type: ignore[assignment]

        async def _allow_url(_url: str) -> None:
            return None

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_url)

        async def run_one() -> BrowserFetchResult:
            return await fetcher.fetch("https://example.com/")

        tasks = [asyncio.create_task(run_one()) for _ in range(6)]
        # Yield so tasks can advance until they hit the on_settle barrier.
        for _ in range(20):
            await asyncio.sleep(0)
        assert peak <= 4, f"expected peak <= 4 in-flight, got {peak}"

        gate.set()
        results = await asyncio.gather(*tasks)
        assert len(results) == 6
        assert peak == 4, f"expected peak == 4 to confirm cap was binding, got {peak}"

    @pytest.mark.asyncio
    async def test_default_max_concurrent_is_four(self) -> None:
        fetcher = PlaywrightBrowserFetcher()
        assert hasattr(fetcher, "_semaphore"), "must expose a semaphore for concurrency cap"
        assert fetcher._semaphore._value == 4


class TestSafeTempFileAndAsyncRead:
    """Fix 2: stop using tempfile.mktemp + sync read inside async function."""

    @pytest.mark.asyncio
    async def test_download_path_returns_bytes_via_async_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        payload = b"%PDF-1.4 fake pdf bytes"
        download_url = "https://example.com/agenda.pdf"

        async def fire_download() -> None:
            pass

        page = _FakePage(response=None, url="https://example.com/", on_settle=fire_download)

        async def goto(_url: str, *, timeout: int) -> None:
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]

        def make_page() -> _FakePage:
            return page

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(make_page)  # type: ignore[assignment]

        async def _allow_url(_url: str) -> None:
            return None

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_url)

        result = await fetcher.fetch(download_url, capture_download=True)
        assert result.body == payload
        assert result.final_url == download_url
        assert result.content_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_does_not_use_deprecated_mktemp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        payload = b"abc"
        download_url = "https://example.com/file.pdf"
        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        async def _allow_url(_url: str) -> None:
            return None

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_url)

        def boom(*_args: Any, **_kwargs: Any) -> str:
            raise AssertionError("tempfile.mktemp must not be called — use NamedTemporaryFile")

        monkeypatch.setattr("tempfile.mktemp", boom)

        result = await fetcher.fetch(download_url, capture_download=True)
        assert result.body == payload


class TestSSRFRecheckAfterAwaits:
    """Fix 3: violations[] must be re-checked after wait_for_timeout (and after
    other awaits that may dispatch sub-resource fetches), not just once after goto."""

    @pytest.mark.asyncio
    async def test_subresource_ssrf_during_settle_raises_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        async def validate_url(url: str) -> None:
            if "169.254.169.254" in url:
                raise HTTPException(status_code=400, detail="metadata IP blocked")

        monkeypatch.setattr("broker.browser_fetcher.validate_url", validate_url)

        browser_holder: list[_FakeBrowser] = []

        async def on_settle() -> None:
            # By the time wait_for_timeout fires, context.route() has run and
            # the route handler is registered in the most recent holder.
            handler = browser_holder[0].route_handler_holders[-1][0]
            route = _FakeRoute("https://169.254.169.254/latest/meta-data/")
            await handler(route)

        page = _FakePage(
            response=_FakeResponse(body=b"ok"),
            url="https://example.com/",
            on_settle=on_settle,
        )

        browser = _FakeBrowser(lambda: page)
        browser_holder.append(browser)
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = browser  # type: ignore[assignment]

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/")
        assert exc.value.status_code == 400
        assert "SSRF blocked mid-fetch" in exc.value.detail

    @pytest.mark.asyncio
    async def test_subresource_ssrf_during_download_wait_raises_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        async def validate_url(url: str) -> None:
            if "10.0.0.1" in url:
                raise HTTPException(status_code=400, detail="private IP blocked")

        monkeypatch.setattr("broker.browser_fetcher.validate_url", validate_url)

        browser_holder: list[_FakeBrowser] = []
        page = _FakePage(response=None, url="https://example.com/")
        fired = {"hit": False}

        async def goto(_url: str, *, timeout: int) -> None:
            return None

        page.goto = goto  # type: ignore[method-assign]

        async def wait_for_timeout(ms: int) -> None:
            if not fired["hit"]:
                fired["hit"] = True
                handler = browser_holder[0].route_handler_holders[-1][0]
                route = _FakeRoute("https://10.0.0.1/internal")
                await handler(route)

        page.wait_for_timeout = wait_for_timeout  # type: ignore[method-assign]

        browser = _FakeBrowser(lambda: page)
        browser_holder.append(browser)
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = browser  # type: ignore[assignment]

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/", capture_download=True)
        assert exc.value.status_code == 400
        assert "SSRF blocked mid-fetch" in exc.value.detail
