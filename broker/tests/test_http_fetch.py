import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import ScopeTicket
from broker.endpoints.http_fetch import (
    get_httpx_client,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-http-test"


def _make_ticket() -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-http-001",
        organization_slug="org-1",
        experiment_id="meeting_briefing",
        scope={},
        params={},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


def _create_app(
    response_body: bytes = b'[{"EventId": 1, "EventBodyName": "City Council"}]',
    response_status: int = 200,
    content_type: str = "application/json",
    content_length_override: str | None = None,
    get_side_effect: Exception | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    app.dependency_overrides[get_scope_ticket] = _make_ticket

    mock_client = MagicMock(spec=httpx.AsyncClient)
    if get_side_effect:
        mock_client.get = AsyncMock(side_effect=get_side_effect)
    else:
        headers = {"content-type": content_type}
        if content_length_override is not None:
            headers["content-length"] = content_length_override
        else:
            headers["content-length"] = str(len(response_body))
        mock_client.get = AsyncMock(
            return_value=httpx.Response(
                status_code=response_status,
                content=response_body,
                headers=headers,
                request=httpx.Request("GET", "https://example.com/x"),
            )
        )

    app.dependency_overrides[get_httpx_client] = lambda: mock_client
    return app


class TestPublicDomainsAllowed:
    """No domain allowlist — runner has no egress, response flows to a sandboxed
    runner, and URL-based exfil via WebFetch already exists, so restricting by
    domain would not close any risk class. SSRF to private IPs IS blocked
    (see TestSSRFGuards below)."""

    def test_accepts_any_public_com_domain(self):
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/http/fetch",
            json={"url": "https://hendersonville-nc.municodemeetings.com/"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200

    def test_accepts_gov_domain(self):
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/http/fetch",
            json={"url": "https://linc.osbm.nc.gov/api/records"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200


class TestSSRFGuards:
    """Block SSRF into private / metadata / loopback IPs. Mirrors pdf_fetch."""

    def test_rejects_rfc1918_private_ips(self):
        app = _create_app()
        client = TestClient(app)
        for url in [
            "https://10.0.0.5/api",
            "https://192.168.1.1/api",
            "https://172.16.0.1/api",
        ]:
            resp = client.post(
                "/http/fetch",
                json={"url": url},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 400, f"expected 400 for {url}, got {resp.status_code}"

    def test_rejects_aws_and_ecs_metadata_endpoints(self):
        app = _create_app()
        client = TestClient(app)
        for url in [
            "https://169.254.169.254/latest/meta-data/",
            "https://169.254.170.2/v2/credentials",
        ]:
            resp = client.post(
                "/http/fetch",
                json={"url": url},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 400, f"expected 400 for {url}"

    def test_rejects_ipv4_mapped_ipv6_metadata(self):
        """::ffff:169.254.169.254 is IPv4-mapped IPv6 pointing at IMDS.
        Python's IPv6Address.is_private returns False for mapped v4 ranges,
        so without explicit ipv4_mapped handling this bypasses the guard."""
        app = _create_app()
        client = TestClient(app)
        for url in [
            "https://[::ffff:169.254.169.254]/latest/meta-data/",
            "https://[::ffff:10.0.0.1]/api",
            "https://[::ffff:127.0.0.1]/api",
        ]:
            resp = client.post(
                "/http/fetch",
                json={"url": url},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 400, f"expected 400 for {url}, got {resp.status_code}"
            assert "blocked address range" in resp.json().get("detail", "")

    def test_rejects_loopback(self):
        app = _create_app()
        client = TestClient(app)
        for url in ["https://127.0.0.1/api", "https://localhost/api"]:
            resp = client.post(
                "/http/fetch",
                json={"url": url},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 400, f"expected 400 for {url}"


class TestSSRFRedirectReValidation:
    """Upstream redirects are attacker-controlled. If httpx follows redirects
    transparently, an allowed public URL can 302 to http://169.254.169.254
    (AWS IMDS) or http://10.x.x.x (internal services) and the broker's
    pre-request _validate_url has been bypassed entirely. Each redirect hop
    must pass validation or the request is rejected.
    """

    def _redirect_response(self, location: str) -> httpx.Response:
        return httpx.Response(
            status_code=302,
            headers={"location": location, "content-length": "0"},
            content=b"",
            request=httpx.Request("GET", "https://example.com/x"),
        )

    def _ok_response(self) -> httpx.Response:
        body = b'{"ok": true}'
        return httpx.Response(
            status_code=200,
            content=body,
            headers={"content-type": "application/json", "content-length": str(len(body))},
            request=httpx.Request("GET", "https://example.com/x"),
        )

    def _create_app_with_redirects(self, responses: list[httpx.Response]) -> FastAPI:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_scope_ticket] = _make_ticket

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=responses)
        app.dependency_overrides[get_httpx_client] = lambda: mock_client
        return app

    def test_redirect_to_imds_is_rejected(self):
        app = self._create_app_with_redirects([
            self._redirect_response("https://169.254.169.254/latest/meta-data/"),
        ])
        client = TestClient(app)

        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/legitimate"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400
        assert "169.254" in resp.json()["detail"] or "blocked" in resp.json()["detail"].lower()

    def test_redirect_to_rfc1918_is_rejected(self):
        app = self._create_app_with_redirects([
            self._redirect_response("https://10.0.0.5/internal"),
        ])
        client = TestClient(app)

        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/legitimate"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400

    def test_redirect_to_public_host_is_followed(self):
        """Legitimate redirects to other public hosts still work."""
        app = self._create_app_with_redirects([
            self._redirect_response("https://example.com/final"),
            self._ok_response(),
        ])
        client = TestClient(app)

        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/initial"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert resp.json()["source_url"] == "https://example.com/final"

    def test_redirect_loop_cap(self):
        """Cap redirects at a low count. An attacker can't hold the broker
        in an endless redirect chain to burn resources."""
        app = self._create_app_with_redirects([
            self._redirect_response(f"https://example.com/hop-{i}") for i in range(10)
        ])
        client = TestClient(app)

        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/initial"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400
        assert "redirect" in resp.json()["detail"].lower()

    def test_relative_location_header_follows_correctly(self):
        """RFC 7231 permits relative Location values. Raw assignment treats
        `/redirected/path` as a full URL, losing the scheme+host — the next
        _validate_url call then 400s with 'URL must use https scheme'.
        urljoin(current_url, location) resolves root-relative against the
        current URL's scheme+host."""
        app = self._create_app_with_redirects([
            self._redirect_response("/redirected/path"),
            self._ok_response(),
        ])
        client = TestClient(app)

        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/initial"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200, (
            f"expected 200 following relative redirect, got {resp.status_code} "
            f"detail={resp.json().get('detail')}"
        )
        assert resp.json()["source_url"] == "https://example.com/redirected/path"

    def test_protocol_relative_location_header_follows_correctly(self):
        """Protocol-relative Location (`//other.example.com/foo`) must inherit
        the scheme of the current URL. urljoin handles this; raw assignment
        produces a schemeless URL that the SSRF guard rejects."""
        app = self._create_app_with_redirects([
            self._redirect_response("//www.example.com/foo"),
            self._ok_response(),
        ])
        client = TestClient(app)

        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/initial"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200, (
            f"expected 200 following protocol-relative redirect, got {resp.status_code} "
            f"detail={resp.json().get('detail')}"
        )
        assert resp.json()["source_url"] == "https://www.example.com/foo"

    def test_validate_url_called_for_initial_url_and_every_redirect_hop(self, monkeypatch):
        """Regression guard against a refactor that drops per-hop validation.
        Without this assertion, a refactor that moves _validate_url back out of
        the loop (or sets follow_redirects=True) could pass the existing tests
        because they only check the FINAL outcome — not that the SSRF guard
        actually ran on each hop's URL.
        """
        from broker.endpoints import http_fetch as mod

        validated: list[str] = []
        real_validate = mod._validate_url

        async def spy_validate(url: str) -> None:
            validated.append(url)
            await real_validate(url)

        monkeypatch.setattr(mod, "_validate_url", spy_validate)

        app = self._create_app_with_redirects([
            self._redirect_response("https://example.com/hop-1"),
            self._redirect_response("https://example.com/hop-2"),
            self._ok_response(),
        ])
        client = TestClient(app)

        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/initial"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        # Initial URL + each redirect target = 3 validations.
        assert validated == [
            "https://example.com/initial",
            "https://example.com/hop-1",
            "https://example.com/hop-2",
        ]


class TestUrlValidation:
    def test_rejects_http_scheme(self):
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/http/fetch",
            json={"url": "http://webapi.legistar.com/v1/x"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400
        assert "https" in resp.json()["detail"].lower()


class TestSizeGuards:
    def test_rejects_oversized_content_length(self):
        app = _create_app(content_length_override=str(11 * 1024 * 1024))
        client = TestClient(app)
        resp = client.post(
            "/http/fetch",
            json={"url": "https://linc.osbm.nc.gov/big.json"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 413


class TestHappyPath:
    def test_returns_body_and_metadata(self):
        body = b'[{"EventId": 42, "EventBodyName": "City Council"}]'
        app = _create_app(response_body=body, content_type="application/json")
        client = TestClient(app)
        resp = client.post(
            "/http/fetch",
            json={"url": "https://webapi.legistar.com/v1/cityoffayetteville/events"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == 200
        assert data["content_type"] == "application/json"
        assert data["body"] == body.decode()
        assert data["source_url"] == "https://webapi.legistar.com/v1/cityoffayetteville/events"

    def test_passes_through_upstream_404(self):
        app = _create_app(response_body=b"not found", response_status=404, content_type="text/plain")
        client = TestClient(app)
        resp = client.post(
            "/http/fetch",
            json={"url": "https://webapi.legistar.com/v1/missing"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        # Proxy returns 200 with upstream status in body — lets agent decide how to handle
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == 404
        assert data["body"] == "not found"


class TestNetworkErrorHandling:
    def test_upstream_connection_error_returns_502(self):
        app = _create_app(get_side_effect=httpx.ConnectError("connection refused"))
        client = TestClient(app)
        resp = client.post(
            "/http/fetch",
            json={"url": "https://webapi.legistar.com/v1/x"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 502


class TestAuth:
    def test_rejects_unauthenticated(self):
        app = FastAPI()
        app.include_router(router)

        def _raise():
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="missing broker token")

        app.dependency_overrides[get_scope_ticket] = _raise
        client = TestClient(app)
        resp = client.post("/http/fetch", json={"url": "https://example.com/x"})
        assert resp.status_code == 401
