import asyncio
import logging
import time

from playwright.async_api import async_playwright, Browser, Playwright
from playwright_stealth import Stealth

from browser_service.app.config import Settings
from browser_service.app.models import RenderResponse
from browser_service.app.stealth import (
    get_random_locale,
    get_random_user_agent,
    get_random_viewport,
    get_stealth_headers,
)
from browser_service.app.validation import validate_url

logger = logging.getLogger(__name__)

settings = Settings()


class BrowserPool:
    def __init__(self, max_concurrent: int = settings.MAX_CONCURRENT_CONTEXTS):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_contexts = 0
        self._lock = asyncio.Lock()
        self.browser: Browser | None = None
        self._playwright: Playwright | None = None

    @property
    def active_contexts(self) -> int:
        return self._active_contexts

    async def start(self) -> None:
        await self.stop()  # clean up any existing resources
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ]
        )

    async def stop(self) -> None:
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _ensure_browser(self) -> None:
        async with self._lock:
            if self.browser is None or not self.browser.is_connected():
                logger.warning("Browser not connected, restarting...")
                await self.start()

    async def render(
        self,
        url: str,
        timeout_ms: int = settings.DEFAULT_TIMEOUT_MS,
        wait_until: str = "networkidle",
        wait_after_load_ms: int = 0,
    ) -> RenderResponse:
        validate_url(url)  # SSRF protection
        await self._ensure_browser()

        async with self._semaphore:
            async with self._lock:
                self._active_contexts += 1
            try:
                return await self._do_render(url, timeout_ms, wait_until, wait_after_load_ms)
            finally:
                async with self._lock:
                    self._active_contexts -= 1

    async def _do_render(
        self,
        url: str,
        timeout_ms: int,
        wait_until: str,
        wait_after_load_ms: int,
    ) -> RenderResponse:
        start = time.monotonic()

        viewport = get_random_viewport()
        context = await self.browser.new_context(
            user_agent=get_random_user_agent(),
            viewport=viewport,
            locale=get_random_locale(),
            extra_http_headers=get_stealth_headers(),
        )
        try:
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)

            response = await page.goto(
                url,
                timeout=timeout_ms,
                wait_until=wait_until,
            )

            if wait_after_load_ms > 0:
                await page.wait_for_timeout(wait_after_load_ms)

            html = await page.content()
            final_url = page.url
            if response is None:
                logger.warning("page.goto returned None for url=%s", url)
            status_code = response.status if response else 200
            elapsed_ms = (time.monotonic() - start) * 1000

            return RenderResponse(
                html=html,
                status_code=status_code,
                url=final_url,
                elapsed_ms=elapsed_ms,
            )
        finally:
            await context.close()
