import logging

import httpx


logger = logging.getLogger(__name__)


class BrowserServiceClient:
    """Async client for the browser rendering service."""

    def __init__(self, base_url: str = "http://browser-service.browser-service.internal:8000"):
        self.base_url = base_url
        self.client = httpx.AsyncClient(base_url=base_url, timeout=60.0)
        logger.info("BrowserServiceClient initialized with base_url=%s", base_url)

    async def render(self, url: str, timeout_ms: int = 30000, wait_until: str = "networkidle") -> dict:
        """POST /render to get rendered page content.

        Args:
            url: The URL to render.
            timeout_ms: Browser navigation timeout in milliseconds.
            wait_until: Playwright wait_until strategy.

        Returns:
            Response JSON from the browser service.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses.
        """
        logger.info("Rendering url=%s timeout_ms=%d wait_until=%s", url, timeout_ms, wait_until)
        response = await self.client.post(
            "/render",
            json={"url": url, "timeout_ms": timeout_ms, "wait_until": wait_until},
        )
        response.raise_for_status()
        return response.json()

    async def health(self) -> dict:
        """GET /health to check service health.

        Returns:
            Response JSON from the health endpoint.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses.
        """
        logger.info("Checking browser service health")
        response = await self.client.get("/health")
        response.raise_for_status()
        return response.json()

    async def close(self):
        """Close the underlying httpx client."""
        await self.client.aclose()
        logger.info("BrowserServiceClient closed")
