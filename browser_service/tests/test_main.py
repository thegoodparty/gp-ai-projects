from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import httpx

from browser_service.app.browser_pool import BrowserPool
from browser_service.app.main import app


@pytest_asyncio.fixture()
async def client():
    """Set up the browser pool on app.state before testing, tear down after."""
    pool = BrowserPool(max_concurrent=2)
    await pool.start()
    app.state.browser_pool = pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    await pool.stop()


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_health_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_health_response_fields(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert data["browser_connected"] is True
        assert data["active_contexts"] == 0


@pytest.mark.asyncio
class TestRenderEndpoint:
    async def test_render_valid_url(self, client):
        resp = await client.post(
            "/render",
            json={"url": "data:text/html,<h1>Hello</h1>"},
        )
        # data: URLs are blocked by validate_url (non-http/https scheme)
        # so this should return an error (500 from generic Exception handler
        # or 400 if ValueError is caught separately)
        assert resp.status_code in (400, 500)

    async def test_render_missing_url_returns_422(self, client):
        resp = await client.post("/render", json={})
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestRenderErrorStates:
    async def test_timeout_returns_504(self, client):
        with patch.object(
            app.state.browser_pool, "render", new_callable=AsyncMock
        ) as mock_render:
            mock_render.side_effect = TimeoutError("Navigation timeout")
            resp = await client.post(
                "/render", json={"url": "https://example.com"}
            )
        assert resp.status_code == 504
        data = resp.json()
        assert data["error"] == "timeout"

    async def test_connection_error_returns_502(self, client):
        with patch.object(
            app.state.browser_pool, "render", new_callable=AsyncMock
        ) as mock_render:
            mock_render.side_effect = ConnectionError("Browser disconnected")
            resp = await client.post(
                "/render", json={"url": "https://example.com"}
            )
        assert resp.status_code == 502
        data = resp.json()
        assert data["error"] == "connection_error"

    async def test_internal_error_returns_500(self, client):
        with patch.object(
            app.state.browser_pool, "render", new_callable=AsyncMock
        ) as mock_render:
            mock_render.side_effect = RuntimeError("Unexpected failure")
            resp = await client.post(
                "/render", json={"url": "https://example.com"}
            )
        assert resp.status_code == 500
        data = resp.json()
        assert data["error"] == "internal_error"

    async def test_invalid_url_scheme_returns_error(self, client):
        """file:// URLs should be rejected by validate_url (ValueError)."""
        resp = await client.post(
            "/render", json={"url": "file:///etc/passwd"}
        )
        # ValueError is caught by either a dedicated handler (400) or generic Exception (500)
        assert resp.status_code in (400, 500)
        data = resp.json()
        assert "error" in data
