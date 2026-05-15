"""Opt-in live tests for PlaywrightBrowserFetcher.

These tests require:
  1. `BROKER_LIVE_TESTS=1` in the environment.
  2. A working Playwright Chromium install (`playwright install chromium`).
  3. Internet access (they hit example.com and alvin.gov).

They are excluded from CI and pre-commit by default — every test in this
file is skipped at collection time unless `BROKER_LIVE_TESTS=1` is set.
Run on demand to verify the Cloudflare bypass still works:

    BROKER_LIVE_TESTS=1 uv run pytest broker/tests/test_browser_fetcher_live.py -v

Why this file exists:
The unit suite uses _FakePage everywhere — fast, deterministic, but
incapable of catching regressions in the actual Cloudflare-bypass
capability. If `playwright-stealth` breaks, Chromium upgrades patches its
fingerprint, or Cloudflare changes its challenge, the suite stays green
while production silently 403s on every PDF/DOCX from a Cloudflare-
fronted muni site. This file is the only thing that catches that class
of failure.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BROKER_LIVE_TESTS") != "1",
    reason=(
        "set BROKER_LIVE_TESTS=1 to run live network tests "
        "(requires Chromium installed via `playwright install chromium`)"
    ),
)


@pytest.mark.network
@pytest.mark.asyncio
async def test_baseline_public_html_works():
    """example.com is the canary — it has no JS challenge, no Cloudflare,
    no auth. If this test fails, the fetcher is broken at the most basic
    level (Chromium didn't launch, route handler is wrong, body capture
    is wrong) and there's no point running the Cloudflare-challenge test.
    """
    from broker.browser_fetcher import PlaywrightBrowserFetcher

    fetcher = PlaywrightBrowserFetcher()
    await fetcher.start()
    try:
        result = await fetcher.fetch("https://example.com/")

        assert result.status == 200, f"expected 200, got {result.status}"
        assert result.content_type.startswith("text/html"), result.content_type
        assert b"Example Domain" in result.body
    finally:
        await fetcher.aclose()


@pytest.mark.network
@pytest.mark.asyncio
async def test_cloudflare_challenged_pdf_via_alvin_gov():
    """alvin.gov serves agenda PDFs behind Cloudflare's JS challenge. Plain
    httpx is 403'd; the whole reason this fetcher exists is to get through.
    If this test fails, agents won't be able to read agenda PDFs from any
    Cloudflare-fronted muni site — `meeting_briefing` and `district_intel`
    are dead in the water.

    Asserts content-type is application/pdf, body is multi-MB (sanity:
    the real PDF is ~24 MB; we don't pin the exact size since the upstream
    PDF may rev), and bytes start with %PDF magic.
    """
    from broker.browser_fetcher import PlaywrightBrowserFetcher

    fetcher = PlaywrightBrowserFetcher()
    await fetcher.start()
    try:
        result = await fetcher.fetch("https://www.alvin.gov/AgendaCenter/ViewFile/Agenda/_04162026-434")

        # Alvin's PDF (24 MB) goes through the download path — Playwright
        # captures it via page.on("download", ...) and the fetcher returns
        # body_path (file on disk), not body (in-memory). Body would also
        # blow the 10 MB PAGE_RESPONSE_MAX_BYTES cap if it tried the buffered
        # path. Read magic bytes from disk.
        assert result.content_type == "application/pdf", result.content_type
        assert result.body is None, "PDF must go through the download path, not buffered"
        assert result.body_path is not None, (
            "expected download path (body_path), not inline body — fetcher routing bug"
        )
        assert os.path.exists(result.body_path)
        try:
            file_size = os.path.getsize(result.body_path)
            assert file_size > 1_000_000, (
                f"expected multi-MB PDF, got {file_size} bytes — Cloudflare bypass "
                "likely broken (returned a challenge response instead)"
            )
            assert result.byte_size == file_size, (
                f"BrowserFetchResult.byte_size ({result.byte_size}) must match "
                f"on-disk size ({file_size})"
            )
            with open(result.body_path, "rb") as f:
                magic = f.read(4)
            assert magic == b"%PDF", (
                f"expected PDF magic, got {magic!r} — Cloudflare bypass "
                "likely broken or upstream returned an error page"
            )
        finally:
            try:
                os.unlink(result.body_path)
            except OSError:
                pass
    finally:
        await fetcher.aclose()


@pytest.mark.network
@pytest.mark.asyncio
async def test_aclose_idempotent():
    """aclose() must be safe to call twice — once during normal shutdown,
    once via a finally block in error paths. If it raises on the second
    call, ECS task teardown logs a noisy exception every time the broker
    container stops.
    """
    from broker.browser_fetcher import PlaywrightBrowserFetcher

    fetcher = PlaywrightBrowserFetcher()
    await fetcher.start()
    await fetcher.aclose()
    # Second close must not raise.
    await fetcher.aclose()
