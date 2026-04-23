import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import ScopeTicket
from broker.endpoints.pdf_fetch import (
    get_httpx_client,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-pdf-test"


def _make_ticket() -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-pdf-001",
        organization_slug="org-99",
        experiment_id="meeting_briefing",
        scope={},
        params={},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


def _build_head(status: int = 200, content_type: str = "application/pdf", content_length: str = "1024") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": content_type, "content-length": content_length},
        request=httpx.Request("HEAD", "https://legistar.granicus.com/cityoffayetteville/x.pdf"),
    )


def _build_get(bytes_payload: bytes, content_type: str = "application/pdf") -> httpx.Response:
    return httpx.Response(
        status_code=200,
        headers={"content-type": content_type, "content-length": str(len(bytes_payload))},
        content=bytes_payload,
        request=httpx.Request("GET", "https://legistar.granicus.com/cityoffayetteville/x.pdf"),
    )


def _create_app(
    ticket: ScopeTicket | None = None,
    head: httpx.Response | None = None,
    get: httpx.Response | None = None,
    get_side_effect: Exception | None = None,
    get_status: int = 200,
    get_content_length: int | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    app.dependency_overrides[get_scope_ticket] = lambda: ticket or _make_ticket()

    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.head = AsyncMock(return_value=head or _build_head())
    if get_side_effect:
        mock_client.stream = MagicMock(side_effect=get_side_effect)
    else:
        body = get.content if get else b"%PDF-1.4 fake bytes"
        cl = get_content_length if get_content_length is not None else len(body)
        status = get_status
        def _stream(method, url, **kw):
            class _Ctx:
                async def __aenter__(self_):
                    resp = MagicMock()
                    resp.status_code = status
                    resp.headers = {
                        "content-type": "application/pdf",
                        "content-length": str(cl),
                    }
                    resp.aclose = AsyncMock(return_value=None)
                    async def _iter(chunk_size=8192):
                        for i in range(0, len(body), chunk_size):
                            yield body[i:i + chunk_size]
                    resp.aiter_bytes = _iter
                    resp.raise_for_status = lambda: None
                    return resp
                async def __aexit__(self_, *a):
                    return False
            return _Ctx()
        mock_client.stream = _stream

    app.dependency_overrides[get_httpx_client] = lambda: mock_client
    return app


class TestUrlValidation:
    def test_https_only_rejects_http(self):
        app = _create_app()
        client = TestClient(app)
        resp = client.post("/pdf/fetch", json={"url": "http://legistar.granicus.com/cityoffayetteville/x.pdf"}, headers={"X-Broker-Token": BROKER_TOKEN})
        assert resp.status_code == 400
        assert "https" in resp.json()["detail"].lower()

    def test_rejects_private_rfc1918(self):
        app = _create_app()
        client = TestClient(app)
        for url in [
            "https://10.0.0.5/x.pdf",
            "https://192.168.1.1/x.pdf",
            "https://172.16.0.1/x.pdf",
        ]:
            resp = client.post("/pdf/fetch", json={"url": url}, headers={"X-Broker-Token": BROKER_TOKEN})
            assert resp.status_code == 400, f"expected 400 for {url}, got {resp.status_code}"

    def test_rejects_link_local_and_metadata(self):
        app = _create_app()
        client = TestClient(app)
        for url in [
            "https://169.254.169.254/latest/meta-data/",
            "https://169.254.170.2/v2/credentials",
            "https://127.0.0.1/x.pdf",
        ]:
            resp = client.post("/pdf/fetch", json={"url": url}, headers={"X-Broker-Token": BROKER_TOKEN})
            assert resp.status_code == 400, f"expected 400 for {url}"

    def test_rejects_loopback_hostname(self):
        app = _create_app()
        client = TestClient(app)
        resp = client.post("/pdf/fetch", json={"url": "https://localhost/x.pdf"}, headers={"X-Broker-Token": BROKER_TOKEN})
        assert resp.status_code == 400

    def test_accepts_arbitrary_public_pdf_host(self):
        # /pdf/fetch has no domain allowlist — only SSRF guards apply.
        # PDF binaries flow to a runner with no egress, so domain restriction
        # would not close any exfil risk class that WebFetch doesn't already open.
        # (example.org resolves to a public IP, passing the SSRF guards.)
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://example.org/report.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200


class TestContentGuards:
    def test_rejects_non_pdf_content_type(self):
        head = _build_head(content_type="text/html")
        app = _create_app(head=head)
        client = TestClient(app)
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/cityoffayetteville/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 415
        assert "pdf" in resp.json()["detail"].lower()

    def test_rejects_oversized_content_length(self):
        head = _build_head(content_length=str(260 * 1024 * 1024))
        app = _create_app(head=head)
        client = TestClient(app)
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/cityoffayetteville/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 413

    def test_accepts_pdf_at_cap_boundary(self):
        head = _build_head(content_length=str(250 * 1024 * 1024))
        app = _create_app(head=head)
        client = TestClient(app)
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/cityoffayetteville/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200


class TestRelativeRedirects:
    """RFC 7231 permits relative Location values. The redirect loop must
    resolve them against the current URL (urljoin), otherwise valid upstream
    behavior breaks: raw assignment treats `/foo` as a scheme-less URL that
    the SSRF guard rejects with 400 'URL must use https scheme'."""

    def _redirect_head(self, location: str) -> httpx.Response:
        return httpx.Response(
            status_code=302,
            headers={"location": location, "content-length": "0"},
            request=httpx.Request("HEAD", "https://legistar.granicus.com/x.pdf"),
        )

    def _create_app_with_head_redirects(
        self,
        head_responses: list[httpx.Response],
    ) -> FastAPI:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_scope_ticket] = lambda: _make_ticket()

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.head = AsyncMock(side_effect=head_responses)

        body = b"%PDF-1.4 bytes"
        def _stream(method, url, **kw):
            class _Ctx:
                async def __aenter__(self_):
                    resp = MagicMock()
                    resp.status_code = 200
                    resp.headers = {
                        "content-type": "application/pdf",
                        "content-length": str(len(body)),
                    }
                    resp.aclose = AsyncMock(return_value=None)
                    async def _iter(chunk_size=8192):
                        for i in range(0, len(body), chunk_size):
                            yield body[i:i + chunk_size]
                    resp.aiter_bytes = _iter
                    resp.raise_for_status = lambda: None
                    return resp
                async def __aexit__(self_, *a):
                    return False
            return _Ctx()
        mock_client.stream = _stream

        app.dependency_overrides[get_httpx_client] = lambda: mock_client
        return app

    def test_relative_location_header_follows_correctly(self):
        app = self._create_app_with_head_redirects([
            self._redirect_head("/redirected/report.pdf"),
            _build_head(),
        ])
        client = TestClient(app)

        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/initial.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200, (
            f"expected 200 following relative redirect, got {resp.status_code} "
            f"detail={resp.text}"
        )

    def test_protocol_relative_location_header_follows_correctly(self):
        app = self._create_app_with_head_redirects([
            self._redirect_head("//www.example.com/report.pdf"),
            _build_head(),
        ])
        client = TestClient(app)

        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/initial.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200, (
            f"expected 200 following protocol-relative redirect, got {resp.status_code} "
            f"detail={resp.text}"
        )


class TestStreamingProxy:
    def test_returns_pdf_bytes_and_headers(self):
        payload = b"%PDF-1.4 body bytes here"
        app = _create_app(head=_build_head(content_length=str(len(payload))))
        client = TestClient(app)
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/cityoffayetteville/x.pdf", "purpose": "budget"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers.get("x-byte-size") == str(len(payload))
        assert resp.headers.get("x-source-url") == "https://legistar.granicus.com/cityoffayetteville/x.pdf"

    def test_upstream_5xx_returns_502_not_200_with_truncated_body(self):
        app = _create_app(get_status=500)
        client = TestClient(app)
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/cityoffayetteville/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 502, (
            f"expected 502 when upstream GET returns 500, got {resp.status_code} "
            f"(StreamingResponse sent 200 headers before generator could raise)"
        )

    def test_get_content_length_exceeds_cap_returns_413_before_streaming(self):
        # HEAD returns no content-length, so the HEAD-time check doesn't catch it.
        # GET reveals content-length > MAX_BYTES — must 413 before committing to 200.
        head = httpx.Response(
            status_code=200,
            headers={"content-type": "application/pdf"},
            request=httpx.Request("HEAD", "https://legistar.granicus.com/cityoffayetteville/x.pdf"),
        )
        oversized = 260 * 1024 * 1024
        app = _create_app(head=head, get_content_length=oversized)
        client = TestClient(app)
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://legistar.granicus.com/cityoffayetteville/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 413, (
            f"expected 413 when GET content-length exceeds cap, got {resp.status_code}"
        )

    def test_rejects_unauthenticated_request(self):
        app = FastAPI()
        app.include_router(router)

        def _raise():
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="missing broker token")

        app.dependency_overrides[get_scope_ticket] = _raise
        client = TestClient(app)
        resp = client.post("/pdf/fetch", json={"url": "https://legistar.granicus.com/cityoffayetteville/x.pdf"})
        assert resp.status_code == 401
