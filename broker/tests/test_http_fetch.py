import logging
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
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str) -> BrowserFetchResult:
        self.calls.append(url)
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
            assert "blocked" in resp.json().get("detail", "").lower()

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
    """Unified route streams raw upstream bytes back to the caller. No JSON
    wrapping — body is the body, metadata rides on response headers."""

    def test_returns_raw_body_as_response_body(self):
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
        assert resp.content == body
        assert resp.headers["content-type"] == "application/json"
        assert resp.headers["x-source-url"] == "https://example.com/final"
        assert resp.headers["x-byte-size"] == str(len(body))
        assert resp.headers["x-upstream-status"] == "200"

    def test_returns_pdf_bytes_passthrough(self):
        """Bug fix: previously /pdf/fetch hardcoded content-type=application/pdf
        for downloads. Unified route must pass through whatever the fetcher
        captured from the upstream response listener."""
        pdf_bytes = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>"
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/pdf",
                body=pdf_bytes,
                final_url="https://legistar.granicus.com/x.pdf",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://legistar.granicus.com/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert resp.content == pdf_bytes
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["x-source-url"] == "https://legistar.granicus.com/x.pdf"
        assert resp.headers["x-byte-size"] == str(len(pdf_bytes))

    def test_returns_docx_content_type_passthrough(self):
        """When upstream serves a DOCX download, the unified route must
        preserve the real content-type — not mislabel it as application/pdf
        like the old /pdf/fetch did."""
        docx_bytes = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00fake docx zip body"
        docx_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type=docx_ct,
                body=docx_bytes,
                final_url="https://example.com/agenda.docx",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/agenda.docx"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert resp.content == docx_bytes
        assert resp.headers["content-type"] == docx_ct

    def test_upstream_status_propagates_to_header(self):
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=404,
                content_type="text/html",
                body=b"<html>not found</html>",
                final_url="https://example.com/missing",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/missing"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        # endpoint always returns 200 on successful fetch — upstream status is in header
        assert resp.status_code == 200
        assert resp.headers["x-upstream-status"] == "404"


class TestSizeCap:
    def test_body_over_250mb_rejected(self):
        oversized = b"x" * (250 * 1024 * 1024 + 1)
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

    def test_body_at_250mb_accepted(self):
        max_body = b"x" * (250 * 1024 * 1024)
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/octet-stream",
                body=max_body,
                final_url="https://example.com/edge",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/edge"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200


class TestUpstreamFailure:
    def test_fetcher_upstream_error_returns_502(self):
        fetcher = _FakeFetcher(
            raise_exc=HTTPException(status_code=502, detail="upstream navigation failed: timeout"),
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


class TestFailureLogging:
    """On-call must be able to grep CloudWatch for 'http_fetch failed' to see
    every 4xx/5xx the endpoint emits."""

    def test_ssrf_rejection_emits_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="broker.endpoints.http_fetch")
        client = TestClient(_create_app())
        resp = client.post(
            "/http/fetch",
            json={"url": "https://10.0.0.5/api"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "http_fetch failed" in r.getMessage()
        ]
        assert len(warnings) == 1, f"expected one warning, got {[r.getMessage() for r in caplog.records]}"
        msg = warnings[0].getMessage()
        assert "run_id=run-http-001" in msg
        assert "status=400" in msg
        assert "url=https://10.0.0.5/api" in msg

    def test_upstream_502_emits_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="broker.endpoints.http_fetch")
        fetcher = _FakeFetcher(
            raise_exc=HTTPException(status_code=502, detail="upstream navigation failed: timeout"),
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/x"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 502
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "http_fetch failed" in r.getMessage()
        ]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "status=502" in msg

    def test_oversized_response_emits_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="broker.endpoints.http_fetch")
        oversized = b"x" * (250 * 1024 * 1024 + 1)
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
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "http_fetch failed" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "status=413" in warnings[0].getMessage()


class TestFetcherCallShape:
    """The unified fetcher protocol drops capture_download. Endpoint must
    call fetch(url) with no extra kwargs."""

    def test_calls_fetcher_with_url_only(self):
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="text/html",
                body=b"<html></html>",
                final_url="https://example.com/",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert fetcher.calls == ["https://example.com/"]
