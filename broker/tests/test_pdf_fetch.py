import time
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from broker.browser_fetcher import BrowserFetchResult
from broker.dynamodb_client import ScopeTicket
from broker.endpoints.pdf_fetch import (
    get_browser_fetcher,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-pdf-test"
SOURCE_URL = "https://legistar.granicus.com/cityoffayetteville/x.pdf"


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
                content_type="application/pdf",
                body=b"%PDF-1.4 fake bytes",
                final_url=SOURCE_URL,
            )
        )
    app.dependency_overrides[get_browser_fetcher] = lambda: fetcher
    return app


class TestUrlValidation:
    def test_https_only_rejects_http(self):
        client = TestClient(_create_app())
        resp = client.post(
            "/pdf/fetch",
            json={"url": "http://legistar.granicus.com/cityoffayetteville/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400
        assert "https" in resp.json()["detail"].lower()

    def test_rejects_private_rfc1918(self):
        client = TestClient(_create_app())
        for url in [
            "https://10.0.0.5/x.pdf",
            "https://192.168.1.1/x.pdf",
            "https://172.16.0.1/x.pdf",
        ]:
            resp = client.post(
                "/pdf/fetch",
                json={"url": url},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 400, f"expected 400 for {url}, got {resp.status_code}"

    def test_rejects_link_local_and_metadata(self):
        client = TestClient(_create_app())
        for url in [
            "https://169.254.169.254/latest/meta-data/",
            "https://169.254.170.2/v2/credentials",
            "https://127.0.0.1/x.pdf",
        ]:
            resp = client.post(
                "/pdf/fetch",
                json={"url": url},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 400, f"expected 400 for {url}"

    def test_rejects_loopback_hostname(self):
        client = TestClient(_create_app())
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://localhost/x.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400

    def test_rejects_when_browser_lands_on_private_ip(self):
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/pdf",
                body=b"%PDF-1.4 bytes",
                final_url="https://10.0.0.5/internal.pdf",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://example.com/start.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400

    def test_accepts_arbitrary_public_pdf_host(self):
        client = TestClient(_create_app())
        resp = client.post(
            "/pdf/fetch",
            json={"url": "https://example.org/report.pdf"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200


class TestContentGuards:
    def test_rejects_non_pdf_content_type(self):
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="text/html",
                body=b"<html>not a pdf</html>",
                final_url=SOURCE_URL,
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/pdf/fetch",
            json={"url": SOURCE_URL},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 415
        assert "pdf" in resp.json()["detail"].lower()

    def test_rejects_oversized_pdf_body(self):
        oversized = b"%PDF-1.4 " + b"x" * (251 * 1024 * 1024)
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/pdf",
                body=oversized,
                final_url=SOURCE_URL,
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/pdf/fetch",
            json={"url": SOURCE_URL},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 413


class TestStreamingProxy:
    def test_returns_pdf_bytes_and_headers(self):
        payload = b"%PDF-1.4 body bytes here"
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/pdf",
                body=payload,
                final_url=SOURCE_URL,
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/pdf/fetch",
            json={"url": SOURCE_URL, "purpose": "budget"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers.get("x-byte-size") == str(len(payload))
        assert resp.headers.get("x-source-url") == SOURCE_URL
        assert resp.content == payload

    def test_fetcher_upstream_error_returns_502(self):
        fetcher = _FakeFetcher(
            raise_exc=HTTPException(status_code=502, detail="upstream nav failed"),
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/pdf/fetch",
            json={"url": SOURCE_URL},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 502

    def test_fetcher_passes_capture_download_true(self):
        """PDFs from sites with Content-Disposition: attachment arrive as
        browser-triggered downloads, not as the navigation response. The
        endpoint must ask the fetcher to capture downloads, not the page DOM."""
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/pdf",
                body=b"%PDF-1.4 ok",
                final_url=SOURCE_URL,
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/pdf/fetch",
            json={"url": SOURCE_URL},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert fetcher.calls == [(SOURCE_URL, True)]


class TestAuth:
    def test_rejects_unauthenticated_request(self):
        app = FastAPI()
        app.include_router(router)

        def _raise():
            raise HTTPException(status_code=401, detail="missing broker token")

        app.dependency_overrides[get_scope_ticket] = _raise
        client = TestClient(app)
        resp = client.post("/pdf/fetch", json={"url": SOURCE_URL})
        assert resp.status_code == 401
