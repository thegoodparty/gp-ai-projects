import time
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from broker.browser_fetcher import BrowserFetchResult
from broker.dynamodb_client import ScopeTicket
from broker.endpoints.http_fetch import (
    get_browser_fetcher,
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


@dataclass
class _FakeFetcher:
    result: BrowserFetchResult | None = None
    raise_exc: Exception | None = None
    calls: list[tuple[str, bool]] = field(default_factory=list)

    async def fetch(self, url: str, *, capture_download: bool = False) -> BrowserFetchResult:
        self.calls.append((url, capture_download))
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None, "test must configure result or raise_exc"
        return self.result


def _create_app(fetcher: _FakeFetcher | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_scope_ticket] = _make_ticket
    if fetcher is None:
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/json",
                body=b'[{"EventId": 1, "EventBodyName": "City Council"}]',
                final_url="https://hendersonville-nc.municodemeetings.com/",
            )
        )
    app.dependency_overrides[get_browser_fetcher] = lambda: fetcher
    return app


class TestPublicDomainsAllowed:
    """No domain allowlist — runner has no egress, response flows to a sandboxed
    runner, and URL-based exfil via WebFetch already exists, so restricting by
    domain would not close any risk class. SSRF to private IPs IS blocked
    (see TestSSRFGuards below)."""

    def test_accepts_any_public_com_domain(self):
        client = TestClient(_create_app())
        resp = client.post(
            "/http/fetch",
            json={"url": "https://hendersonville-nc.municodemeetings.com/"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200

    def test_accepts_gov_domain(self):
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/json",
                body=b'{"records": []}',
                final_url="https://linc.osbm.nc.gov/api/records",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://linc.osbm.nc.gov/api/records"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200


class TestSSRFGuards:
    """Block SSRF into private / metadata / loopback IPs.

    Post-Playwright migration: intra-fetch redirect SSRF is enforced inside
    PlaywrightBrowserFetcher's route handler (see test_browser_fetcher). The
    endpoint pre-validates the input URL and post-validates the final URL the
    fetcher returns — both defense-in-depth even if the route handler also
    aborted in flight.
    """

    def test_rejects_rfc1918_private_ips(self):
        client = TestClient(_create_app())
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
        client = TestClient(_create_app())
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
        client = TestClient(_create_app())
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
            assert resp.status_code == 400, f"expected 400 for {url}"
            assert "blocked address range" in resp.json().get("detail", "")

    def test_rejects_loopback(self):
        client = TestClient(_create_app())
        for url in ["https://127.0.0.1/api", "https://localhost/api"]:
            resp = client.post(
                "/http/fetch",
                json={"url": url},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 400, f"expected 400 for {url}"

    def test_rejects_when_browser_lands_on_private_ip(self):
        """If the browser's final URL resolves to a private IP (post-redirect),
        the endpoint must reject the response before returning the body. This
        is the second SSRF gate, after the input-URL check and the route
        handler's per-request abort."""
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/json",
                body=b"{}",
                final_url="https://10.0.0.5/internal",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/start"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400


class TestResponseShape:
    def test_returns_body_and_metadata(self):
        body = b'{"hello": "world"}'
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/json",
                body=body,
                final_url="https://example.com/final",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/start"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == 200
        assert payload["content_type"] == "application/json"
        assert payload["body"] == body.decode("utf-8")
        assert payload["source_url"] == "https://example.com/final"
        assert payload["byte_size"] == len(body)

    def test_body_size_cap_rejects_oversized_response(self):
        oversized = b"x" * (10 * 1024 * 1024 + 1)
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="text/plain",
                body=oversized,
                final_url="https://example.com/big",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/big"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 413

    def test_fetcher_upstream_error_returns_502(self):
        fetcher = _FakeFetcher(
            raise_exc=HTTPException(status_code=502, detail="upstream nav failed: timeout"),
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/x"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 502


class TestAuth:
    def test_rejects_unauthenticated_request(self):
        app = FastAPI()
        app.include_router(router)

        def _raise():
            raise HTTPException(status_code=401, detail="missing broker token")

        app.dependency_overrides[get_scope_ticket] = _raise
        client = TestClient(app)
        resp = client.post("/http/fetch", json={"url": "https://example.com/x"})
        assert resp.status_code == 401
