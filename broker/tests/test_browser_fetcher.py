"""Tests for PlaywrightBrowserFetcher hardening.

Concerns under test:
  1. Concurrency cap via asyncio.Semaphore.
  2. Polymorphic BrowserFetchResult: page-response returns body (capped at
     PAGE_RESPONSE_MAX_BYTES), download returns body_path on disk.
  3. Initial grace window for late-firing JS-triggered downloads (Cloudflare).
  4. Content-type-conditional post-nav settle (no settle for JSON/text).
  5. BrowserContext leak protection — new_page / stealth / route are inside
     the try block so context.close() runs even when those raise.
  6. aclose() gates in-flight fetches and rejects new fetches with 503.
  7. SSRF re-check at every await boundary.

Fakes substitute for playwright runtime types — no real Chromium required.
"""

from __future__ import annotations

import asyncio
import os
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
        url: str = "https://example.com/",
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-type": "text/html"} if headers is None else headers
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
    """Minimal Page double."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        url: str = "https://example.com/",
        on_settle: Any = None,
        responses_to_emit: list[_FakeResponse] | None = None,
    ) -> None:
        self._response = response
        self.url = url
        self._on_settle = on_settle
        self._download_listeners: list[Any] = []
        self._response_listeners: list[Any] = []
        self._responses_to_emit = responses_to_emit or []
        self._wait_calls = 0
        self._load_state_calls = 0

    def on(self, event: str, handler: Any) -> None:
        if event == "download":
            self._download_listeners.append(handler)
        elif event == "response":
            self._response_listeners.append(handler)

    async def goto(self, url: str, *, timeout: int) -> _FakeResponse | None:
        for resp in self._responses_to_emit:
            for handler in self._response_listeners:
                handler(resp)
        return self._response

    async def wait_for_timeout(self, ms: int) -> None:
        self._wait_calls += 1
        if self._on_settle is not None:
            await self._on_settle()

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self._load_state_calls += 1
        if self._on_settle is not None:
            await self._on_settle()

    def emit_download(self, download: _FakeDownload) -> None:
        for handler in self._download_listeners:
            handler(download)

    def emit_response(self, response: _FakeResponse) -> None:
        for handler in self._response_listeners:
            handler(response)


class _FakeContext:
    def __init__(
        self,
        *,
        page: _FakePage,
        route_handler_holder: list[Any],
        new_page_error: Exception | None = None,
    ) -> None:
        self._page = page
        self._route_handler_holder = route_handler_holder
        self._new_page_error = new_page_error
        self.closed = False

    async def new_page(self) -> _FakePage:
        if self._new_page_error is not None:
            raise self._new_page_error
        return self._page

    async def route(self, pattern: str, handler: Any) -> None:
        self._route_handler_holder.append(handler)

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(
        self,
        page_factory: Any,
        *,
        new_page_error: Exception | None = None,
    ) -> None:
        self._page_factory = page_factory
        self.contexts_opened = 0
        self.contexts: list[_FakeContext] = []
        self.route_handler_holders: list[list[Any]] = []
        self._new_page_error = new_page_error
        self.closed = False

    async def new_context(self, **_kwargs: Any) -> _FakeContext:
        self.contexts_opened += 1
        page = self._page_factory()
        holder: list[Any] = []
        self.route_handler_holders.append(holder)
        ctx = _FakeContext(
            page=page,
            route_handler_holder=holder,
            new_page_error=self._new_page_error,
        )
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


def _patch_stealth(monkeypatch: pytest.MonkeyPatch, *, raise_exc: Exception | None = None) -> None:
    """tf-playwright-stealth's stealth_async expects a real page. Replace with a no-op
    (or with a raising stub if a test needs to simulate stealth_async failing)."""

    async def _noop(_page: Any) -> None:
        if raise_exc is not None:
            raise raise_exc
        return None

    import sys
    import types

    if "playwright_stealth" not in sys.modules:
        mod = types.ModuleType("playwright_stealth")
        sys.modules["playwright_stealth"] = mod
    monkeypatch.setattr("playwright_stealth.stealth_async", _noop, raising=False)


def _patch_playwright_types(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stealth(monkeypatch)


async def _allow_all(_url: str) -> None:
    return None


class TestConcurrencyCap:
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

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        async def run_one() -> BrowserFetchResult:
            return await fetcher.fetch("https://example.com/")

        tasks = [asyncio.create_task(run_one()) for _ in range(6)]
        for _ in range(20):
            await asyncio.sleep(0)
        assert peak <= 4, f"expected peak <= 4 in-flight, got {peak}"

        gate.set()
        results = await asyncio.gather(*tasks)
        assert len(results) == 6
        assert peak == 4, f"expected peak == 4 to confirm cap was binding, got {peak}"

    @pytest.mark.asyncio
    async def test_default_max_concurrent_is_thirty(self) -> None:
        fetcher = PlaywrightBrowserFetcher()
        assert hasattr(fetcher, "_semaphore"), "must expose a semaphore for concurrency cap"
        assert fetcher._semaphore._value == 30


class TestUnifiedFetchSignature:
    @pytest.mark.asyncio
    async def test_fetch_accepts_only_url_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)
        page = _FakePage(response=_FakeResponse(body=b"ok"), url="https://example.com/")
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/")
        assert isinstance(result, BrowserFetchResult)

        with pytest.raises(TypeError):
            await fetcher.fetch("https://example.com/", capture_download=True)  # type: ignore[call-arg]


class TestPolymorphicResult:
    """BrowserFetchResult has exactly one of body / body_path non-None.
    Page-response path → body. Download path → body_path on disk."""

    def test_page_response_dataclass_shape(self) -> None:
        r = BrowserFetchResult(
            status=200,
            content_type="text/html",
            final_url="https://example.com/",
            byte_size=2,
            body=b"ok",
            body_path=None,
        )
        assert r.body == b"ok"
        assert r.body_path is None
        assert r.byte_size == 2

    def test_download_dataclass_shape(self, tmp_path) -> None:
        f = tmp_path / "x.bin"
        f.write_bytes(b"abc")
        r = BrowserFetchResult(
            status=200,
            content_type="application/pdf",
            final_url="https://example.com/x.pdf",
            byte_size=3,
            body=None,
            body_path=str(f),
        )
        assert r.body is None
        assert r.body_path == str(f)
        assert r.byte_size == 3


class TestDownloadPath:
    """Download path: page.on('download') fires, fetcher saves to a temp file
    and returns body_path + byte_size — no in-memory body."""

    @pytest.mark.asyncio
    async def test_download_returns_body_path_not_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        download_url = "https://example.com/agenda.docx"
        docx_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        payload = b"PK\x03\x04 fake docx bytes"

        page = _FakePage(
            response=None,
            url="https://example.com/",
            responses_to_emit=[
                _FakeResponse(
                    url=download_url,
                    status=200,
                    headers={"content-type": f"{docx_ct}; charset=utf-8"},
                ),
            ],
        )

        async def goto(_url: str, *, timeout: int) -> None:
            for resp in page._responses_to_emit:
                for handler in page._response_listeners:
                    handler(resp)
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch(download_url)
        try:
            assert result.body is None, "download must NOT buffer body in memory"
            assert result.body_path is not None
            assert os.path.exists(result.body_path)
            with open(result.body_path, "rb") as f:
                assert f.read() == payload
            assert result.byte_size == len(payload)
            assert result.final_url == download_url
            assert result.content_type == docx_ct
            assert result.status == 200
        finally:
            if result.body_path and os.path.exists(result.body_path):
                os.unlink(result.body_path)

    @pytest.mark.asyncio
    async def test_download_falls_back_to_octet_stream_when_no_response_captured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        payload = b"%PDF-1.4 fake pdf bytes"
        download_url = "https://example.com/agenda.pdf"

        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch(download_url)
        try:
            assert result.body is None
            assert result.body_path is not None
            with open(result.body_path, "rb") as f:
                assert f.read() == payload
            assert result.content_type == "application/octet-stream"
        finally:
            if result.body_path and os.path.exists(result.body_path):
                os.unlink(result.body_path)

    @pytest.mark.asyncio
    async def test_download_revalidates_final_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        download_url = "https://10.0.0.5/internal.pdf"
        payload = b"%PDF-1.4 secret"

        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        async def _validate_url(url: str) -> None:
            if "10.0.0.5" in url:
                raise HTTPException(status_code=400, detail="private IP blocked")

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _validate_url)

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/start")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_does_not_use_deprecated_mktemp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        payload = b"abc"
        download_url = "https://example.com/file.bin"
        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        def boom(*_args: Any, **_kwargs: Any) -> str:
            raise AssertionError("tempfile.mktemp must not be called — use NamedTemporaryFile")

        monkeypatch.setattr("tempfile.mktemp", boom)

        result = await fetcher.fetch(download_url)
        try:
            assert result.body_path is not None
        finally:
            if result.body_path and os.path.exists(result.body_path):
                os.unlink(result.body_path)

    @pytest.mark.asyncio
    async def test_download_exceeding_max_bytes_raises_413_and_unlinks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fetcher enforces MAX_BYTES on download; oversized files must
        be unlinked immediately and a 413 raised."""
        _patch_playwright_types(monkeypatch)

        from broker.browser_fetcher import MAX_BYTES

        # Patch MAX_BYTES to a small value via a small file (test must not
        # allocate >50 MB of body bytes). We approximate by monkeypatching
        # the module constant — the fetcher reads it at call-time.
        small_cap = 16
        monkeypatch.setattr("broker.browser_fetcher.MAX_BYTES", small_cap)
        assert MAX_BYTES != small_cap or True  # silence ruff

        payload = b"x" * (small_cap + 1)
        download_url = "https://example.com/too-big.pdf"
        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        saved_paths: list[str] = []
        orig_save_as = _FakeDownload.save_as

        async def tracking_save_as(self: _FakeDownload, path: str) -> None:
            saved_paths.append(path)
            await orig_save_as(self, path)

        monkeypatch.setattr(_FakeDownload, "save_as", tracking_save_as)

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch(download_url)
        assert exc.value.status_code == 413
        # the temp file must have been unlinked
        for p in saved_paths:
            assert not os.path.exists(p), f"oversized download temp file leaked: {p}"


class TestPageResponsePath:
    @pytest.mark.asyncio
    async def test_returns_real_content_type_from_response_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        body = b"<html><body>ok</body></html>"
        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/landed",
                status=200,
                headers={"content-type": "TEXT/HTML; charset=UTF-8"},
                body=body,
            ),
            url="https://example.com/landed",
        )

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/start")
        assert result.body == body
        assert result.body_path is None
        assert result.byte_size == len(body)
        assert result.status == 200
        assert result.content_type == "text/html"
        assert result.final_url == "https://example.com/landed"

    @pytest.mark.asyncio
    async def test_page_response_revalidates_final_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        page = _FakePage(
            response=_FakeResponse(body=b"x", headers={"content-type": "text/html"}),
            url="https://10.0.0.5/internal",
        )

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        async def _validate_url(url: str) -> None:
            if "10.0.0.5" in url:
                raise HTTPException(status_code=400, detail="private IP blocked")

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _validate_url)

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/start")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_content_type_defaults_to_octet_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/",
                status=200,
                headers={},
                body=b"raw bytes",
            ),
            url="https://example.com/",
        )

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/")
        assert result.content_type == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_page_response_exceeding_page_max_raises_413(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Page-response path enforces the tighter PAGE_RESPONSE_MAX_BYTES cap
        before buffering response.body() into RAM."""
        _patch_playwright_types(monkeypatch)

        monkeypatch.setattr("broker.browser_fetcher.PAGE_RESPONSE_MAX_BYTES", 16)

        body = b"x" * 17
        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/big",
                status=200,
                headers={"content-type": "text/html"},
                body=body,
            ),
            url="https://example.com/big",
        )
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/big")
        assert exc.value.status_code == 413


class TestNavigationFailure:
    @pytest.mark.asyncio
    async def test_nav_error_with_no_download_raises_generic_502(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        from playwright.async_api import Error as PlaywrightError

        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            raise PlaywrightError("net::ERR_NAME_NOT_RESOLVED at https://internal.example.com/")

        page.goto = goto  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]

        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/")
        assert exc.value.status_code == 502
        assert exc.value.detail == "upstream navigation failed"


class TestDownloadGraceWindow:
    """Cloudflare-challenged sites trigger downloads 200-500 ms after page.goto
    returns. The fetcher must wait an initial grace window for downloads to
    fire even when goto returned successfully, otherwise we ship the challenge
    HTML back to the caller instead of the file."""

    @pytest.mark.asyncio
    async def test_late_fired_download_after_successful_goto_is_captured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        download_url = "https://example.com/agenda.pdf"
        payload = b"%PDF late fire"
        challenge_html = b"<html>cloudflare challenge</html>"

        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/start",
                status=200,
                headers={"content-type": "application/pdf"},
                body=challenge_html,
            ),
            url="https://example.com/start",
        )

        # Fire the download on the FIRST wait_for_timeout call — this is the
        # grace window the fetcher must always wait, even after a successful
        # goto with a non-textual content-type.
        async def wait_for_timeout(ms: int) -> None:
            page._wait_calls += 1
            if page._wait_calls == 1:
                page.emit_download(_FakeDownload(download_url, payload))

        page.wait_for_timeout = wait_for_timeout  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/start")
        try:
            assert result.body_path is not None, (
                "fetcher exited the wait loop too early — must give downloads "
                "an initial grace window even when goto() returned a response"
            )
            with open(result.body_path, "rb") as f:
                assert f.read() == payload
        finally:
            if result.body_path and os.path.exists(result.body_path):
                os.unlink(result.body_path)

    @pytest.mark.asyncio
    async def test_textual_response_skips_binary_grace_and_settle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For JSON/text responses (NOT HTML), the fetcher must skip both the
        secondary binary download grace AND the post-nav networkidle settle.
        That's the whole point of the content-type-conditional waits — JSON
        REST endpoints should not pay HTML/binary tax."""
        _patch_playwright_types(monkeypatch)

        body = b'{"ok":true}'
        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/api",
                status=200,
                headers={"content-type": "application/json"},
                body=body,
            ),
            url="https://example.com/api",
        )

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/api")
        assert result.body == body
        # JSON must never trigger the networkidle settle wait.
        assert page._load_state_calls == 0, (
            "JSON responses must not wait_for_load_state(networkidle)"
        )
        # Only the initial download grace should fire (≤ 1 budget worth of slices).
        # Binary grace would add 3× more slices; that's the regression we guard against.
        from broker.browser_fetcher import (
            BINARY_DOWNLOAD_WAIT_MS,
            INITIAL_DOWNLOAD_GRACE_MS,
        )

        initial_slices = max(int(INITIAL_DOWNLOAD_GRACE_MS / 100), 1)
        binary_slices = max(int(BINARY_DOWNLOAD_WAIT_MS / 100), 1)
        # Strict bound: we paid initial grace at most. Should be well below
        # initial + binary, which is the worst case if the conditional was wrong.
        assert page._wait_calls <= initial_slices, (
            f"JSON response must not pay binary grace; got {page._wait_calls} "
            f"calls, expected ≤ {initial_slices} (binary cap would be {initial_slices + binary_slices})"
        )

    @pytest.mark.asyncio
    async def test_nav_error_path_waits_full_download_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When goto raises (download path with no response), the loop should
        be willing to wait the full DOWNLOAD_WAIT_MS for the download event."""
        _patch_playwright_types(monkeypatch)

        from playwright.async_api import Error as PlaywrightError

        download_url = "https://example.com/late.pdf"
        payload = b"%PDF very late"
        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            raise PlaywrightError("Download is starting")

        page.goto = goto  # type: ignore[method-assign]

        # Fire on the 5th wait — proves the fetcher kept waiting past the
        # initial grace window when there is no response.
        async def wait_for_timeout(ms: int) -> None:
            page._wait_calls += 1
            if page._wait_calls == 5:
                page.emit_download(_FakeDownload(download_url, payload))

        page.wait_for_timeout = wait_for_timeout  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/")
        try:
            assert result.body_path is not None
        finally:
            if result.body_path and os.path.exists(result.body_path):
                os.unlink(result.body_path)


class TestPostNavSettleConditional:
    """Post-nav settle wait should be conditional on content-type. JSON/XML/text
    pay nothing. HTML and binary content-types may have late sub-resource or
    download triggers and pay up to POST_NAV_SETTLE_MS via networkidle wait."""

    @pytest.mark.asyncio
    async def test_json_response_does_not_wait_for_networkidle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        body = b'{"ok":true}'
        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/api",
                status=200,
                headers={"content-type": "application/json"},
                body=body,
            ),
            url="https://example.com/api",
        )

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/api")
        assert result.body == body
        assert page._load_state_calls == 0, (
            "JSON response must not wait_for_load_state(networkidle) — "
            "POST_NAV_SETTLE_MS should be conditional"
        )

    @pytest.mark.asyncio
    async def test_html_response_waits_for_networkidle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        body = b"<html></html>"
        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/",
                status=200,
                headers={"content-type": "text/html"},
                body=body,
            ),
            url="https://example.com/",
        )

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/")
        assert result.body == body
        assert page._load_state_calls == 1, (
            "HTML responses must wait_for_load_state(networkidle, timeout=POST_NAV_SETTLE_MS)"
        )

    @pytest.mark.asyncio
    async def test_networkidle_timeout_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """networkidle can legitimately time out on noisy pages — fetcher must
        swallow that and continue."""
        _patch_playwright_types(monkeypatch)

        from playwright.async_api import Error as PlaywrightError

        body = b"<html></html>"
        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/",
                status=200,
                headers={"content-type": "text/html"},
                body=body,
            ),
            url="https://example.com/",
        )

        async def wait_for_load_state(state: str, *, timeout: int) -> None:
            page._load_state_calls += 1
            raise PlaywrightError("Timeout 1500ms exceeded.")

        page.wait_for_load_state = wait_for_load_state  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        result = await fetcher.fetch("https://example.com/")
        assert result.body == body


class TestSSRFRecheckAfterAwaits:
    @pytest.mark.asyncio
    async def test_subresource_ssrf_during_settle_raises_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The route handler fires a violation during the post-nav settle.
        That post-settle _raise_if_violation must catch it.

        Note: with the new content-type-conditional settle, this test uses
        text/html so the settle path runs. The on_settle hook only fires on
        the wait_for_load_state call (not earlier wait_for_timeout grace
        windows) so we can verify the post-settle check catches violations.
        """
        _patch_playwright_types(monkeypatch)

        async def validate_url(url: str) -> None:
            if "169.254.169.254" in url:
                raise HTTPException(status_code=400, detail="metadata IP blocked")

        monkeypatch.setattr("broker.browser_fetcher.validate_url", validate_url)

        browser_holder: list[_FakeBrowser] = []

        async def on_settle() -> None:
            handler = browser_holder[0].route_handler_holders[-1][0]
            route = _FakeRoute("https://169.254.169.254/latest/meta-data/")
            await handler(route)

        page = _FakePage(
            response=_FakeResponse(body=b"<html></html>", headers={"content-type": "text/html"}),
            url="https://example.com/",
        )

        # Only fire the SSRF subresource on the load-state call (post-settle).
        # Earlier wait_for_timeout grace windows must remain no-ops so the
        # violation is specifically attributed to post-settle.
        page._on_settle = None
        async def wait_for_load_state(state: str, *, timeout: int) -> None:
            page._load_state_calls += 1
            await on_settle()

        page.wait_for_load_state = wait_for_load_state  # type: ignore[method-assign]

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
            await fetcher.fetch("https://example.com/")
        assert exc.value.status_code == 400
        assert "SSRF blocked mid-fetch" in exc.value.detail


class TestContextLeakProtection:
    """If new_page() or stealth_async raise, context.close() must still run."""

    @pytest.mark.asyncio
    async def test_context_closed_when_new_page_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        page = _FakePage(response=_FakeResponse(body=b"ok"))
        browser = _FakeBrowser(lambda: page, new_page_error=RuntimeError("boom"))
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = browser  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        with pytest.raises(RuntimeError):
            await fetcher.fetch("https://example.com/")

        assert len(browser.contexts) == 1
        assert browser.contexts[0].closed, (
            "context.close() must run even when new_page() raises"
        )

    @pytest.mark.asyncio
    async def test_context_closed_when_stealth_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_stealth(monkeypatch, raise_exc=RuntimeError("stealth boom"))

        page = _FakePage(response=_FakeResponse(body=b"ok"))
        browser = _FakeBrowser(lambda: page)
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = browser  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        with pytest.raises(RuntimeError):
            await fetcher.fetch("https://example.com/")

        assert browser.contexts[0].closed, (
            "context.close() must run even when stealth_async raises"
        )


class TestAcloseGate:
    """aclose() must reject new fetches with 503 and drain in-flight ones."""

    @pytest.mark.asyncio
    async def test_fetch_after_aclose_raises_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_playwright_types(monkeypatch)

        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = _FakeBrowser(lambda: _FakePage(response=_FakeResponse(body=b"ok")))  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        await fetcher.aclose()

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/")
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_aclose_drains_in_flight_fetches_before_closing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        gate = asyncio.Event()
        fetch_returned_before_close = {"ok": False}

        async def on_settle() -> None:
            await gate.wait()

        page = _FakePage(
            response=_FakeResponse(body=b"<html></html>", headers={"content-type": "text/html"}),
            url="https://example.com/",
        )

        async def wait_for_load_state(state: str, *, timeout: int) -> None:
            page._load_state_calls += 1
            await on_settle()

        page.wait_for_load_state = wait_for_load_state  # type: ignore[method-assign]

        fetcher = PlaywrightBrowserFetcher(max_concurrent=2)
        fetcher._browser = _FakeBrowser(lambda: page)  # type: ignore[assignment]
        monkeypatch.setattr("broker.browser_fetcher.validate_url", _allow_all)

        async def do_fetch() -> None:
            await fetcher.fetch("https://example.com/")
            fetch_returned_before_close["ok"] = True

        task = asyncio.create_task(do_fetch())
        for _ in range(20):
            await asyncio.sleep(0)

        # Start aclose; it should hang waiting for the in-flight task.
        close_task = asyncio.create_task(fetcher.aclose())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not close_task.done(), "aclose() must wait for in-flight fetches"

        gate.set()
        await asyncio.wait_for(task, timeout=2)
        await asyncio.wait_for(close_task, timeout=2)
        assert fetch_returned_before_close["ok"]


class TestDownloadTempFileLeakOnSSRFViolation:
    """When _save_download_to_disk yields via asyncio.to_thread, a sub-resource
    route-handler can append to violations[] during the yield. The next
    _raise_if_violation() raises HTTP 400 — but body_path is already on disk.
    The endpoint never sees the result, so its BackgroundTask cleanup never
    runs. Without explicit cleanup at this checkpoint, the temp file leaks
    forever (until container restart, or never on persistent TMPDIR).
    """

    @pytest.mark.asyncio
    async def test_early_download_path_unlinks_on_post_save_ssrf_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_playwright_types(monkeypatch)

        async def validate_url(url: str) -> None:
            if "10.0.0.5" in url:
                raise HTTPException(status_code=400, detail="private IP blocked")

        monkeypatch.setattr("broker.browser_fetcher.validate_url", validate_url)

        download_url = "https://example.com/agenda.pdf"
        payload = b"%PDF-1.4 fake"
        browser_holder: list[_FakeBrowser] = []
        page = _FakePage(response=None, url="https://example.com/")

        async def goto(_url: str, *, timeout: int) -> None:
            page.emit_download(_FakeDownload(download_url, payload))
            return None

        page.goto = goto  # type: ignore[method-assign]

        # Capture the path that gets written so we can assert it's unlinked.
        captured_paths: list[str] = []
        from broker import browser_fetcher as bf_module
        orig_save = bf_module._save_download_to_disk

        async def save_then_violate(download):
            path, size = await orig_save(download)
            captured_paths.append(path)
            # Simulate a sub-resource SSRF firing during the asyncio.to_thread
            # yield inside _save_download_to_disk: invoke the route handler
            # with a private IP so violations[] picks it up.
            handler = browser_holder[0].route_handler_holders[-1][0]
            await handler(_FakeRoute("https://10.0.0.5/internal-subresource"))
            return path, size

        monkeypatch.setattr(
            "broker.browser_fetcher._save_download_to_disk", save_then_violate
        )

        browser = _FakeBrowser(lambda: page)
        browser_holder.append(browser)
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = browser  # type: ignore[assignment]

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch(download_url)
        assert exc.value.status_code == 400
        assert "SSRF blocked mid-fetch" in exc.value.detail
        assert len(captured_paths) == 1, "save was called exactly once"
        leaked = captured_paths[0]
        assert not os.path.exists(leaked), (
            f"download temp file leaked after post-save SSRF violation: {leaked}"
        )

    @pytest.mark.asyncio
    async def test_late_download_path_unlinks_on_post_save_ssrf_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same bug, post-settle path: late download fires during
        wait_for_load_state, save_as runs, route handler appends a violation
        during the asyncio.to_thread yield, _raise_if_violation must clean up
        before raising."""
        _patch_playwright_types(monkeypatch)

        async def validate_url(url: str) -> None:
            if "10.0.0.5" in url:
                raise HTTPException(status_code=400, detail="private IP blocked")

        monkeypatch.setattr("broker.browser_fetcher.validate_url", validate_url)

        download_url = "https://example.com/agenda.pdf"
        payload = b"%PDF-1.4 fake"
        browser_holder: list[_FakeBrowser] = []
        page = _FakePage(
            response=_FakeResponse(
                url="https://example.com/", status=200,
                headers={"content-type": "text/html"}, body=b"<html></html>",
            ),
            url="https://example.com/",
        )
        # Suppress download fires in the grace window so we end up on the
        # post-settle late-download path. Return the FakeResponse so we take
        # the page-response → settle branch (download will fire inside
        # wait_for_load_state below).
        async def goto(_url: str, *, timeout: int):
            return page._response
        page.goto = goto  # type: ignore[method-assign]

        # Fire the download from inside wait_for_load_state (the post-settle path).
        async def wait_for_load_state(state: str, *, timeout: int) -> None:
            page._load_state_calls += 1
            page.emit_download(_FakeDownload(download_url, payload))
        page.wait_for_load_state = wait_for_load_state  # type: ignore[method-assign]
        page._on_settle = None

        captured_paths: list[str] = []
        from broker import browser_fetcher as bf_module
        orig_save = bf_module._save_download_to_disk

        async def save_then_violate(download):
            path, size = await orig_save(download)
            captured_paths.append(path)
            handler = browser_holder[0].route_handler_holders[-1][0]
            await handler(_FakeRoute("https://10.0.0.5/late-subresource"))
            return path, size

        monkeypatch.setattr(
            "broker.browser_fetcher._save_download_to_disk", save_then_violate
        )

        browser = _FakeBrowser(lambda: page)
        browser_holder.append(browser)
        fetcher = PlaywrightBrowserFetcher()
        fetcher._browser = browser  # type: ignore[assignment]

        with pytest.raises(HTTPException) as exc:
            await fetcher.fetch("https://example.com/")
        assert exc.value.status_code == 400
        assert len(captured_paths) == 1
        leaked = captured_paths[0]
        assert not os.path.exists(leaked), (
            f"late-download temp file leaked after post-save SSRF violation: {leaked}"
        )
