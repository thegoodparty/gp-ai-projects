import httpx
import pytest

from pmf_engine.runner.pmf_runtime.config import init_config
from pmf_engine.runner.pmf_runtime.http import get


def _inject_client(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://broker")
    cfg = init_config("http://broker", "tok")
    cfg._client = client
    return cfg


class TestGet:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_get_returns_status_body_and_content_type(self):
        def handler(request):
            assert request.url.path == "/http/fetch"
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "content_type": "application/json",
                    "body": '[{"EventId": 1}]',
                    "source_url": "https://webapi.legistar.com/v1/x/events",
                    "byte_size": 16,
                },
            )

        _inject_client(handler)
        result = get("https://webapi.legistar.com/v1/x/events")

        assert result["status"] == 200
        assert result["content_type"] == "application/json"
        assert result["body"] == '[{"EventId": 1}]'
        assert result["source_url"] == "https://webapi.legistar.com/v1/x/events"

    def test_upstream_404_returned_as_data_not_raised(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": 404,
                    "content_type": "text/plain",
                    "body": "not found",
                    "source_url": "https://webapi.legistar.com/v1/missing",
                    "byte_size": 9,
                },
            )

        _inject_client(handler)
        result = get("https://webapi.legistar.com/v1/missing")
        assert result["status"] == 404
        assert result["body"] == "not found"

    def test_broker_403_raises_value_error_with_detail(self):
        def handler(request):
            return httpx.Response(403, json={"detail": "not on the broker allowlist"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="allowlist"):
            get("https://badsite.com/exfil")

    def test_broker_400_raises_value_error(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "URL must use https scheme"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="https"):
            get("http://insecure.gov/x")

    def test_passes_purpose_to_broker(self):
        captured = {}

        def handler(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"status": 200, "content_type": "application/json", "body": "[]", "source_url": "x", "byte_size": 2},
            )

        _inject_client(handler)
        get("https://webapi.legistar.com/v1/x", purpose="list events")
        assert captured["body"]["purpose"] == "list events"
