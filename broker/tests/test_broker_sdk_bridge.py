"""Bridge test: broker FastAPI app + pmf_runtime SDK speak the same wire format.

The broker test suite asserts what the FastAPI route emits; the pmf_runtime
test suite asserts what its SDK does given a mocked broker response. Neither
side reconciles. If /http/fetch dropped `X-Upstream-Status` (or renamed it),
both suites would stay green while production would mask upstream 404s as
200s — exactly the kind of silent-success regression we don't want.

This bridge test pipes the real broker FastAPI app through httpx.ASGITransport
into the SDK's httpx.Client, so the SDK round-trips the same wire format the
broker would emit in production. Override `get_browser_fetcher` and
`get_scope_ticket` inside the broker app — everything else is real.

Owns its own in-memory fakes (does not import from sibling test files).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.browser_fetcher import BrowserFetchResult
from broker.dynamodb_client import ScopeTicket
from broker.endpoints.http_fetch import (
    get_browser_fetcher,
    get_scope_ticket,
)
from broker.endpoints.http_fetch import (
    router as http_router,
)
from pmf_engine.runner.pmf_runtime import config as pmf_config
from pmf_engine.runner.pmf_runtime import http as pmf_http

BROKER_TOKEN = "bridge-test-token"


def _make_ticket() -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-bridge-001",
        organization_slug="org-bridge",
        experiment_id="meeting_briefing",
        scope={"http": True},
        params={},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch_lambda",
    )


@dataclass
class _FakeFetcher:
    """Programmable fetcher. Test sets `result` (or `raise_exc`); endpoint
    calls fetch(url) and gets whatever's queued up.

    BrowserFetchResult is polymorphic (Agent A's contract):
      - page response: body=bytes, body_path=None (buffered in memory)
      - download:      body=None, body_path=str (streamed from disk by the
                       endpoint, then unlinked via BackgroundTask)
    `byte_size` is required on both shapes.
    """

    result: BrowserFetchResult | None = None
    raise_exc: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str) -> BrowserFetchResult:
        self.calls.append(url)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None, "test must configure result or raise_exc"
        return self.result


def _build_broker_app(fetcher: _FakeFetcher) -> FastAPI:
    """Minimal broker app: just /http/fetch wired to our fake fetcher and a
    static ticket. We do NOT exercise auth (separately tested) — we trust the
    broker to refuse bad tokens and only test the wire contract for the
    happy and propagated-error paths.
    """
    app = FastAPI()
    app.include_router(http_router)
    app.dependency_overrides[get_scope_ticket] = _make_ticket
    app.dependency_overrides[get_browser_fetcher] = lambda: fetcher
    return app


def _build_test_client_transport(broker_app: FastAPI) -> httpx.MockTransport:
    """Adapter: sync httpx.MockTransport that delegates each request to
    Starlette's TestClient, which runs the ASGI app synchronously.

    Why: pmf_runtime's SDK uses sync httpx.Client. httpx.ASGITransport is
    async-only, so we can't plug an ASGI app directly into a sync client.
    TestClient (built on httpx itself, with portal under the hood) does
    the sync↔async hop for us. Wrapping it in a MockTransport gives the
    SDK's Client a transport-shaped object it can call as if it were
    real network I/O.
    """
    test_client = TestClient(broker_app, base_url="http://broker.test")

    def _handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        forwarded = test_client.request(
            method=request.method,
            url=str(request.url),
            content=body,
            headers=dict(request.headers.items()),
        )
        return httpx.Response(
            status_code=forwarded.status_code,
            headers=list(forwarded.headers.items()),
            content=forwarded.content,
        )

    return httpx.MockTransport(_handler)


def _inject_sdk_client(broker_app: FastAPI) -> None:
    """Pipe the broker app into pmf_runtime's SDK via a TestClient-backed
    sync transport, replacing the real network client. Reset module-global
    config so successive tests don't bleed state.
    """
    pmf_config._config = None
    cfg = pmf_config.init_config("http://broker.test", BROKER_TOKEN)
    cfg._client = httpx.Client(
        transport=_build_test_client_transport(broker_app),
        base_url="http://broker.test",
        headers={"X-Broker-Token": BROKER_TOKEN},
        timeout=30.0,
    )


@pytest.fixture(autouse=True)
def _reset_pmf_config():
    """Each test starts with a fresh SDK config."""
    pmf_config._config = None
    yield
    pmf_config._config = None


class TestGetWireContract:
    """SDK http.get must round-trip the broker's /http/fetch response shape
    end-to-end. Asserts on user-observable fields: status, content_type, body,
    source_url, byte_size."""

    def test_get_html_via_sdk(self):
        body = b"<html>hello</html>"
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="text/html",
                final_url="https://example.com/",
                byte_size=len(body),
                body=body,
            )
        )
        _inject_sdk_client(_build_broker_app(fetcher))

        result = pmf_http.get("https://example.com/")

        assert result["status"] == 200
        # Starlette's StreamingResponse may append `; charset=utf-8` for text
        # media types — assert the type, not the exact header string.
        assert result["content_type"].startswith("text/html")
        assert result["body"] == "<html>hello</html>"
        assert result["source_url"] == "https://example.com/"
        assert result["byte_size"] == len(body)
        assert fetcher.calls == ["https://example.com/"]

    def test_get_propagates_upstream_404(self):
        """If the upstream returns 404, the broker tags the response with
        X-Upstream-Status=404. The SDK must surface that as status=404 —
        NOT collapse to 200 (broker's own response code). Otherwise SDK
        callers can't distinguish 'upstream healthy' from 'upstream missing'.
        """
        body = b"<html>not found</html>"
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=404,
                content_type="text/html",
                final_url="https://example.com/missing",
                byte_size=len(body),
                body=body,
            )
        )
        _inject_sdk_client(_build_broker_app(fetcher))

        result = pmf_http.get("https://example.com/missing")

        assert result["status"] == 404
        assert result["body"] == "<html>not found</html>"

    def test_get_raises_on_binary_content_type(self):
        """http.get is text-only; binary content-types must raise so callers
        notice and switch to http.download. The message must point at
        http.download as the remedy.
        """
        pdf_bytes = b"%PDF-1.7 binary"
        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/pdf",
                final_url="https://example.com/x.pdf",
                byte_size=len(pdf_bytes),
                body=pdf_bytes,
            )
        )
        _inject_sdk_client(_build_broker_app(fetcher))

        with pytest.raises(ValueError, match=r"http\.download"):
            pmf_http.get("https://example.com/x.pdf")


class TestDownloadWireContract:
    """SDK http.download must stream the broker's response to disk and
    return a path + metadata matching the upstream content-type and bytes.
    """

    def test_download_streams_pdf_to_disk(self, tmp_path):
        """End-to-end download: broker streams a PDF from disk (body_path) →
        SDK writes the bytes back out to its own dest → file is byte-identical
        to what the fetcher staged.

        BrowserFetchResult is polymorphic per Agent A's contract:
        downloads use body_path (filesystem path the endpoint streams from
        and then unlinks via BackgroundTask). Page responses use body bytes.
        This test uses the download shape.
        """
        pdf_bytes = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\nfake pdf body"
        # Stage the bytes on disk where the fetcher claims to have them.
        # BackgroundTask in the endpoint will unlink this after the response
        # is sent, so the file is gone by the time the SDK returns.
        src_path = tmp_path / "fetcher_tmp.pdf"
        src_path.write_bytes(pdf_bytes)

        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type="application/pdf",
                final_url="https://example.com/agenda.pdf",
                byte_size=len(pdf_bytes),
                body=None,
                body_path=str(src_path),
            )
        )
        _inject_sdk_client(_build_broker_app(fetcher))

        dest = str(tmp_path / "agenda.pdf")
        result = pmf_http.download("https://example.com/agenda.pdf", dest=dest)

        assert result["path"] == dest
        assert result["content_type"] == "application/pdf"
        assert result["source_url"] == "https://example.com/agenda.pdf"
        assert result["byte_size"] == len(pdf_bytes)
        with open(dest, "rb") as f:
            written = f.read()
        assert written == pdf_bytes
        assert written[:4] == b"%PDF"

    def test_download_extension_inferred_from_content_type(self, tmp_path):
        """When no dest is given, the SDK derives a path from the URL and
        appends an extension based on the upstream content-type. A DOCX
        response must save to a .docx file, not .pdf or .bin.
        """
        docx_bytes = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00fake docx"
        docx_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        src_path = tmp_path / "fetcher_tmp.docx"
        src_path.write_bytes(docx_bytes)

        fetcher = _FakeFetcher(
            result=BrowserFetchResult(
                status=200,
                content_type=docx_ct,
                final_url="https://example.com/agenda",
                byte_size=len(docx_bytes),
                body=None,
                body_path=str(src_path),
            )
        )
        _inject_sdk_client(_build_broker_app(fetcher))

        workspace = tmp_path / "workspace"
        os.environ["PMF_WORKSPACE"] = str(workspace)
        try:
            result = pmf_http.download("https://example.com/agenda")
        finally:
            del os.environ["PMF_WORKSPACE"]

        assert result["path"].endswith(".docx"), result["path"]
        assert result["content_type"] == docx_ct
        assert os.path.exists(result["path"])
        with open(result["path"], "rb") as f:
            assert f.read() == docx_bytes
