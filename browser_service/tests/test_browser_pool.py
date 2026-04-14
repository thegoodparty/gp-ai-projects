import asyncio
from unittest.mock import patch

import pytest

from browser_service.app.browser_pool import BrowserPool
from browser_service.app.models import RenderResponse


def _allow_data_urls(url: str) -> None:
    """No-op validate_url for tests that use data: URLs."""
    pass


class TestBrowserPoolInit:
    def test_can_instantiate_with_max_concurrent(self):
        pool = BrowserPool(max_concurrent=3)
        assert pool.max_concurrent == 3

    def test_default_max_concurrent(self):
        pool = BrowserPool()
        assert pool.max_concurrent == 5


@pytest.mark.asyncio
class TestBrowserPoolLifecycle:
    async def test_start_launches_browser(self):
        pool = BrowserPool(max_concurrent=2)
        try:
            await pool.start()
            assert pool.browser is not None
            assert pool.browser.is_connected()
        finally:
            await pool.stop()

    async def test_stop_closes_browser(self):
        pool = BrowserPool(max_concurrent=2)
        await pool.start()
        await pool.stop()
        assert pool.browser is None


@pytest.mark.asyncio
class TestBrowserPoolRender:
    @patch("browser_service.app.browser_pool.validate_url", _allow_data_urls)
    async def test_render_returns_render_response(self):
        pool = BrowserPool(max_concurrent=2)
        await pool.start()
        try:
            result = await pool.render("data:text/html,<h1>Hello</h1>")
            assert isinstance(result, RenderResponse)
            assert "Hello" in result.html
            assert result.status_code == 200
        finally:
            await pool.stop()

    @patch("browser_service.app.browser_pool.validate_url", _allow_data_urls)
    async def test_render_captures_final_url(self):
        pool = BrowserPool(max_concurrent=2)
        await pool.start()
        try:
            result = await pool.render("data:text/html,<p>Test</p>")
            assert "data:" in result.url
        finally:
            await pool.stop()

    @patch("browser_service.app.browser_pool.validate_url", _allow_data_urls)
    async def test_render_elapsed_ms_is_positive(self):
        pool = BrowserPool(max_concurrent=2)
        await pool.start()
        try:
            result = await pool.render("data:text/html,<p>Test</p>")
            assert result.elapsed_ms > 0
        finally:
            await pool.stop()

    async def test_render_rejects_non_http_url(self):
        pool = BrowserPool(max_concurrent=2)
        await pool.start()
        try:
            with pytest.raises(ValueError, match="not allowed"):
                await pool.render("file:///etc/passwd")
        finally:
            await pool.stop()


@pytest.mark.asyncio
class TestBrowserPoolConcurrency:
    @patch("browser_service.app.browser_pool.validate_url", _allow_data_urls)
    async def test_active_contexts_never_exceeds_max(self):
        max_concurrent = 2
        pool = BrowserPool(max_concurrent=max_concurrent)
        await pool.start()

        peak = 0
        original_do_render = pool._do_render

        async def slow_render(*args, **kwargs):
            nonlocal peak
            current = pool.active_contexts
            if current > peak:
                peak = current
            await asyncio.sleep(0.1)  # hold the context open briefly
            return await original_do_render(*args, **kwargs)

        pool._do_render = slow_render

        try:
            tasks = [pool.render(f"data:text/html,<p>{i}</p>") for i in range(6)]
            await asyncio.gather(*tasks)
            assert peak <= max_concurrent, f"Peak concurrent contexts ({peak}) exceeded max ({max_concurrent})"
            assert pool.active_contexts == 0
        finally:
            await pool.stop()
