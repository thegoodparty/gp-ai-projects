import pytest
import httpx

from shared.browser_client import BrowserServiceClient


@pytest.fixture
def mock_transport():
    """Returns a callable that builds an httpx.MockTransport from a handler function."""
    def _factory(handler):
        return httpx.MockTransport(handler)
    return _factory


class TestInstantiation:
    def test_default_base_url(self):
        client = BrowserServiceClient()
        assert client.base_url == "http://browser-service.browser-service.internal:8000"

    def test_custom_base_url(self):
        client = BrowserServiceClient(base_url="http://localhost:9999")
        assert client.base_url == "http://localhost:9999"


class TestRender:
    @pytest.mark.asyncio
    async def test_sends_correct_post_request(self, mock_transport):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "html": "<h1>Hello</h1>",
                    "status_code": 200,
                    "url": "https://example.com",
                    "elapsed_ms": 123.4,
                },
            )

        client = BrowserServiceClient(base_url="http://test")
        client.client = httpx.AsyncClient(transport=mock_transport(handler), base_url="http://test")

        result = await client.render("https://example.com", timeout_ms=5000, wait_until="load")

        assert captured["method"] == "POST"
        assert captured["url"] == "http://test/render"
        import json
        body = json.loads(captured["body"])
        assert body == {"url": "https://example.com", "timeout_ms": 5000, "wait_until": "load"}
        assert result == {
            "html": "<h1>Hello</h1>",
            "status_code": 200,
            "url": "https://example.com",
            "elapsed_ms": 123.4,
        }

    @pytest.mark.asyncio
    async def test_sends_default_params(self, mock_transport):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "html": "",
                    "status_code": 200,
                    "url": "https://example.com",
                    "elapsed_ms": 0.0,
                },
            )

        client = BrowserServiceClient(base_url="http://test")
        client.client = httpx.AsyncClient(transport=mock_transport(handler), base_url="http://test")

        await client.render("https://example.com")

        import json
        body = json.loads(captured["body"])
        assert body["timeout_ms"] == 30000
        assert body["wait_until"] == "networkidle"

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self, mock_transport):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Internal Server Error"})

        client = BrowserServiceClient(base_url="http://test")
        client.client = httpx.AsyncClient(transport=mock_transport(handler), base_url="http://test")

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.render("https://example.com")
        assert exc_info.value.response.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_client_error(self, mock_transport):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "Validation error"})

        client = BrowserServiceClient(base_url="http://test")
        client.client = httpx.AsyncClient(transport=mock_transport(handler), base_url="http://test")

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.render("https://example.com")
        assert exc_info.value.response.status_code == 422


class TestHealth:
    @pytest.mark.asyncio
    async def test_sends_get_to_health(self, mock_transport):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={"status": "ok", "browser_connected": True, "active_contexts": 0},
            )

        client = BrowserServiceClient(base_url="http://test")
        client.client = httpx.AsyncClient(transport=mock_transport(handler), base_url="http://test")

        result = await client.health()

        assert captured["method"] == "GET"
        assert captured["url"] == "http://test/health"
        assert result == {"status": "ok", "browser_connected": True, "active_contexts": 0}

    @pytest.mark.asyncio
    async def test_raises_on_unhealthy(self, mock_transport):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"status": "unhealthy"})

        client = BrowserServiceClient(base_url="http://test")
        client.client = httpx.AsyncClient(transport=mock_transport(handler), base_url="http://test")

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.health()
        assert exc_info.value.response.status_code == 503


class TestClose:
    @pytest.mark.asyncio
    async def test_close(self, mock_transport):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        client = BrowserServiceClient(base_url="http://test")
        client.client = httpx.AsyncClient(transport=mock_transport(handler), base_url="http://test")

        await client.close()
        assert client.client.is_closed
