import logging
import os
import tempfile
import time
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from broker.browser_fetcher import USER_AGENT, BrowserFetchResult
from broker.dynamodb_client import ScopeTicket
from broker.endpoints.http_fetch import (
    get_browser_fetcher,
    get_http_client,
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


def _page_result(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/json",
    final_url: str = "https://example.com/",
) -> BrowserFetchResult:
    return BrowserFetchResult(
        status=status,
        content_type=content_type,
        final_url=final_url,
        byte_size=len(body),
        body=body,
        body_path=None,
    )


def _download_result(
    payload: bytes,
    *,
    status: int = 200,
    content_type: str = "application/pdf",
    final_url: str = "https://example.com/file.pdf",
) -> tuple[BrowserFetchResult, str]:
    """Write payload to a temp file and return (result, path) — caller must
    unlink path AFTER the response is consumed (the endpoint normally does this
    via BackgroundTask, but tests need explicit cleanup)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    tmp.write(payload)
    tmp.close()
    return (
        BrowserFetchResult(
            status=status,
            content_type=content_type,
            final_url=final_url,
            byte_size=len(payload),
            body=None,
            body_path=tmp.name,
        ),
        tmp.name,
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
            result=_page_result(
                b'[{"EventId": 1, "EventBodyName": "City Council"}]',
                final_url="https://hendersonville-nc.municodemeetings.com/",
            )
        )
    app.dependency_overrides[get_browser_fetcher] = lambda: fetcher
    return app


class TestPublicDomainsAllowed:
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
            result=_page_result(
                b'{"records": []}',
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
        fetcher = _FakeFetcher(
            result=_page_result(b"{}", final_url="https://10.0.0.5/internal"),
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/start"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 400


class TestResponseShape:
    def test_returns_raw_body_as_response_body(self):
        body = b'{"hello": "world"}'
        fetcher = _FakeFetcher(
            result=_page_result(body, final_url="https://example.com/final"),
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

    def test_returns_pdf_bytes_passthrough_from_download_path(self):
        """Download path streams bytes from disk."""
        pdf_bytes = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>"
        result, tmp_path = _download_result(
            pdf_bytes,
            content_type="application/pdf",
            final_url="https://legistar.granicus.com/x.pdf",
        )
        try:
            fetcher = _FakeFetcher(result=result)
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
            # BackgroundTask should have unlinked the temp file after response.
            assert not os.path.exists(tmp_path), (
                "download temp file must be unlinked via BackgroundTask after the response is sent"
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_returns_docx_content_type_passthrough(self):
        docx_bytes = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00fake docx zip body"
        docx_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        result, tmp_path = _download_result(
            docx_bytes,
            content_type=docx_ct,
            final_url="https://example.com/agenda.docx",
        )
        try:
            fetcher = _FakeFetcher(result=result)
            client = TestClient(_create_app(fetcher))
            resp = client.post(
                "/http/fetch",
                json={"url": "https://example.com/agenda.docx"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 200
            assert resp.content == docx_bytes
            assert resp.headers["content-type"] == docx_ct
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_upstream_status_propagates_to_header(self):
        fetcher = _FakeFetcher(
            result=_page_result(
                b"<html>not found</html>",
                status=404,
                content_type="text/html",
                final_url="https://example.com/missing",
            )
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/missing"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert resp.headers["x-upstream-status"] == "404"


class TestSizeCap:
    """The fetcher enforces size caps internally (download MAX_BYTES,
    page-response PAGE_RESPONSE_MAX_BYTES). The endpoint just propagates
    a 413 if the fetcher raises one."""

    def test_413_from_fetcher_propagates(self):
        fetcher = _FakeFetcher(
            raise_exc=HTTPException(status_code=413, detail="response exceeded 250 MB"),
        )
        client = TestClient(_create_app(fetcher))
        resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/big"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 413


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
        fetcher = _FakeFetcher(
            raise_exc=HTTPException(status_code=413, detail="response exceeded 250 MB"),
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
    def test_calls_fetcher_with_url_only(self):
        fetcher = _FakeFetcher(
            result=_page_result(
                b"<html></html>",
                content_type="text/html",
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


class TestDownloadStreaming:
    """Download path streams from disk and cleans up the temp file."""

    def test_download_temp_file_is_unlinked_after_response(self):
        payload = b"chunk-streamed-payload" * 100
        result, tmp_path = _download_result(payload, content_type="application/pdf")
        try:
            fetcher = _FakeFetcher(result=result)
            client = TestClient(_create_app(fetcher))
            resp = client.post(
                "/http/fetch",
                json={"url": "https://example.com/x.pdf"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 200
            assert resp.content == payload
            assert not os.path.exists(tmp_path), (
                "BackgroundTask must unlink the temp file after the response is fully sent"
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_download_byte_size_header_matches_file_size(self):
        payload = b"a" * 1024  # 1 KB — well under 50 MB test limit
        result, tmp_path = _download_result(payload, content_type="application/pdf")
        try:
            fetcher = _FakeFetcher(result=result)
            client = TestClient(_create_app(fetcher))
            resp = client.post(
                "/http/fetch",
                json={"url": "https://example.com/x.pdf"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 200
            assert resp.headers["x-byte-size"] == str(len(payload))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# /http/head — fast non-browser liveness check (rung 2: WebSearch -> head -> fetch)
# ---------------------------------------------------------------------------
def _head_app(handler, monkeypatch, *, allow=lambda u: True):
    async def _validate(url: str) -> None:
        if not allow(url):
            raise HTTPException(status_code=400, detail="SSRF blocked")
    monkeypatch.setattr("broker.endpoints.http_fetch.validate_url", _validate)
    monkeypatch.setattr("broker.ssrf_guard.validate_url", _validate)
    app = FastAPI()
    app.include_router(router)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_scope_ticket] = _make_ticket
    app.dependency_overrides[get_http_client] = lambda: client
    return app


class TestHttpHead:
    def test_returns_status_via_plain_head(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.method == "HEAD"  # no browser, no GET body
            return httpx.Response(200)
        app = _head_app(handler, monkeypatch)
        r = TestClient(app).post("/http/head", json={"url": "https://example.gov/page"})
        assert r.status_code == 200
        assert r.json()["status"] == 200
        assert r.json()["final_url"] == "https://example.gov/page"

    def test_falls_back_to_get_when_head_405(self, monkeypatch):
        seen: dict[str, httpx.Headers] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen[req.method] = req.headers
            return httpx.Response(405) if req.method == "HEAD" else httpx.Response(200)
        app = _head_app(handler, monkeypatch)
        r = TestClient(app).post("/http/head", json={"url": "https://example.gov/p"})
        assert r.json()["status"] == 200
        assert seen["GET"]["range"] == "bytes=0-0"
        assert USER_AGENT in seen["GET"]["user-agent"]

    def test_follows_redirect_and_validates_each_hop(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/old":
                return httpx.Response(301, headers={"location": "https://example.gov/new"})
            return httpx.Response(200)
        app = _head_app(handler, monkeypatch)
        r = TestClient(app).post("/http/head", json={"url": "https://example.gov/old"})
        assert r.json()["status"] == 200
        assert r.json()["final_url"].endswith("/new")

    def test_blocks_ssrf_on_redirect_target(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            if "10.0.0.5" in str(req.url):
                return httpx.Response(200)
            return httpx.Response(302, headers={"location": "http://10.0.0.5/internal"})
        # allow the public origin, block the private redirect target. If per-hop
        # validation were dropped, the 10.0.0.5 hop would return a clean 200
        # instead of a 400 — so a passing 400 here proves the guard fired on the
        # redirect target, not merely on the initial URL.
        app = _head_app(handler, monkeypatch, allow=lambda u: "10.0.0.5" not in u)
        r = TestClient(app).post("/http/head", json={"url": "https://example.gov/start"})
        assert r.status_code == 400
        assert "SSRF blocked" in r.json()["detail"]

    def test_blocks_ssrf_on_initial_url(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200)
        app = _head_app(handler, monkeypatch, allow=lambda u: False)
        r = TestClient(app).post("/http/head", json={"url": "http://169.254.169.254/"})
        assert r.status_code == 400
        assert "SSRF blocked" in r.json()["detail"]

    def test_transport_connect_error_maps_to_502(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")
        app = _head_app(handler, monkeypatch)
        r = TestClient(app).post("/http/head", json={"url": "https://example.gov/down"})
        assert r.status_code == 502
        assert "connection failed" in r.json()["detail"]
        assert "ConnectError" in r.json()["detail"]

    def test_transport_timeout_maps_to_504(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow")
        app = _head_app(handler, monkeypatch)
        r = TestClient(app).post("/http/head", json={"url": "https://example.gov/slow"})
        assert r.status_code == 504
        assert "timeout after" in r.json()["detail"]
