import pytest
from pydantic import ValidationError

from browser_service.app.models import ErrorResponse, RenderRequest, RenderResponse


class TestRenderRequest:
    def test_accepts_url_only(self):
        req = RenderRequest(url="https://example.com")
        assert req.url == "https://example.com"

    def test_defaults(self):
        req = RenderRequest(url="https://example.com")
        assert req.timeout_ms == 30000
        assert req.wait_until == "networkidle"
        assert req.wait_after_load_ms == 0

    def test_custom_values(self):
        req = RenderRequest(
            url="https://example.com",
            timeout_ms=5000,
            wait_until="load",
            wait_after_load_ms=1000,
        )
        assert req.timeout_ms == 5000
        assert req.wait_until == "load"
        assert req.wait_after_load_ms == 1000

    def test_wait_until_domcontentloaded(self):
        req = RenderRequest(url="https://example.com", wait_until="domcontentloaded")
        assert req.wait_until == "domcontentloaded"

    def test_rejects_missing_url(self):
        with pytest.raises(ValidationError):
            RenderRequest()

    def test_rejects_invalid_wait_until(self):
        with pytest.raises(ValidationError):
            RenderRequest(url="https://example.com", wait_until="invalid")


class TestRenderResponse:
    def test_all_fields(self):
        resp = RenderResponse(
            html="<h1>Hello</h1>",
            status_code=200,
            url="https://example.com",
            elapsed_ms=123.45,
        )
        assert resp.html == "<h1>Hello</h1>"
        assert resp.status_code == 200
        assert resp.url == "https://example.com"
        assert resp.elapsed_ms == 123.45


class TestErrorResponse:
    def test_all_fields(self):
        err = ErrorResponse(error="timeout", detail="Request timed out after 30s")
        assert err.error == "timeout"
        assert err.detail == "Request timed out after 30s"
