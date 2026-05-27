import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.clerk_client import ClerkClient, ClerkClientError
from broker.dynamodb_client import ScopeTicket
from broker.endpoints.agent_mcp_proxy import (
    get_clerk_client,
    get_gp_api_base_url,
    get_http_client,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-test-mcp"
GP_API_BASE_URL = "https://gp-api-dev.goodparty.org"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.fake.jwt"


def _make_ticket(clerk_session_id: str | None = "sess_abc123") -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-mcp-001",
        organization_slug="org-42",
        experiment_id="voter_targeting",
        scope={"databricks": ["SELECT"]},
        params={"state": "CA"},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
        clerk_session_id=clerk_session_id,
    )


def _create_app(
    ticket: ScopeTicket | None = None,
    upstream_status: int = 200,
    upstream_body: bytes = b'{"jsonrpc":"2.0","result":{"ok":true}}',
    upstream_content_type: str = "application/json",
    mint_jwt_error: Exception | None = None,
    mint_jwt_value: str = FAKE_JWT,
) -> tuple[FastAPI, MagicMock, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    _ticket = ticket if ticket is not None else _make_ticket()
    app.dependency_overrides[get_scope_ticket] = lambda: _ticket

    mock_clerk = MagicMock(spec=ClerkClient)
    if mint_jwt_error is not None:
        mock_clerk.get_session_jwt = AsyncMock(side_effect=mint_jwt_error)
    else:
        mock_clerk.get_session_jwt = AsyncMock(return_value=mint_jwt_value)
    app.dependency_overrides[get_clerk_client] = lambda: mock_clerk

    app.dependency_overrides[get_gp_api_base_url] = lambda: GP_API_BASE_URL

    mock_http = MagicMock(spec=httpx.AsyncClient)
    upstream_response = httpx.Response(
        status_code=upstream_status,
        content=upstream_body,
        headers={"content-type": upstream_content_type},
    )
    # The proxy uses build_request + send(stream=True) so it can branch on
    # the upstream content-type and preserve SSE framing for streaming
    # responses (mirrors the anthropic_proxy pattern).
    mock_http.build_request = MagicMock(
        side_effect=lambda **kwargs: httpx.Request(
            method=kwargs["method"],
            url=kwargs["url"],
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )
    )
    mock_http.send = AsyncMock(return_value=upstream_response)
    app.dependency_overrides[get_http_client] = lambda: mock_http

    return app, mock_clerk, mock_http


class TestAgentMcpProxyHappyPath:
    def test_forwards_body_with_bearer_jwt_and_returns_upstream_response(self):
        app, mock_clerk, mock_http = _create_app(
            upstream_status=200,
            upstream_body=b'{"jsonrpc":"2.0","result":{"tools":[]}}',
        )
        client = TestClient(app)

        request_body = b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
        resp = client.post(
            "/agent/mcp",
            content=request_body,
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        assert resp.content == b'{"jsonrpc":"2.0","result":{"tools":[]}}'

        mock_clerk.get_session_jwt.assert_awaited_once_with("sess_abc123")

        mock_http.build_request.assert_called_once()
        call_kwargs = mock_http.build_request.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["url"] == f"{GP_API_BASE_URL}/v1/mcp"
        assert call_kwargs["content"] == request_body
        assert call_kwargs["headers"]["Authorization"] == f"Bearer {FAKE_JWT}"
        assert call_kwargs["headers"]["Content-Type"] == "application/json"
        assert call_kwargs["headers"]["X-Organization-Slug"] == "org-42"
        # send() called with stream=True so the proxy can decide to stream
        # SSE responses through without buffering.
        mock_http.send.assert_awaited_once()
        assert mock_http.send.call_args.kwargs.get("stream") is True

    def test_get_method_also_proxied(self):
        app, _, mock_http = _create_app()
        client = TestClient(app)

        resp = client.get(
            "/agent/mcp",
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert mock_http.build_request.call_args.kwargs["method"] == "GET"


class TestAgentMcpProxyMissingClerkSessionId:
    def test_returns_500_when_ticket_lacks_clerk_session_id(self):
        ticket = _make_ticket(clerk_session_id=None)
        app, mock_clerk, mock_http = _create_app(ticket=ticket)
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b"{}",
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 500
        assert resp.json()["detail"]["reason"] == "ticket_missing_clerk_session_id"
        mock_clerk.get_session_jwt.assert_not_awaited()
        mock_http.send.assert_not_awaited()


class TestAgentMcpProxyClerkMintFailure:
    def test_returns_502_when_clerk_mint_raises(self):
        app, _, mock_http = _create_app(
            mint_jwt_error=ClerkClientError("upstream 500"),
        )
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b"{}",
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["reason"] == "clerk_session_jwt_mint_failed"
        assert "upstream 500" in detail["err"]
        mock_http.send.assert_not_awaited()


class TestAgentMcpProxyUpstreamErrorPassthrough:
    def test_upstream_401_passes_through(self):
        app, _, _ = _create_app(
            upstream_status=401,
            upstream_body=b'{"error":"unauthorized"}',
        )
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b"{}",
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 401
        assert resp.content == b'{"error":"unauthorized"}'

    def test_upstream_500_passes_through(self):
        app, _, _ = _create_app(
            upstream_status=500,
            upstream_body=b'{"error":"internal"}',
        )
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b"{}",
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 500
        assert resp.content == b'{"error":"internal"}'


class TestAgentMcpProxyHeaderHygiene:
    """X-Broker-Token is the broker's internal auth — it must never appear on
    the outbound request to gp-api. Outbound headers are limited to the set
    gp-api needs (Content-Type, Accept, Authorization, X-Organization-Slug)."""

    def test_x_broker_token_is_not_forwarded_to_gp_api(self):
        app, _, mock_http = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0"}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        outbound_headers = mock_http.build_request.call_args.kwargs["headers"]
        # Case-insensitive guard: no broker token under any casing.
        lowered = {k.lower(): v for k, v in outbound_headers.items()}
        assert "x-broker-token" not in lowered, (
            f"X-Broker-Token leaked to gp-api: {outbound_headers}"
        )
        # Sanity: only the four expected headers should be present.
        assert set(lowered.keys()) == {
            "content-type",
            "accept",
            "authorization",
            "x-organization-slug",
        }


class TestAgentMcpProxyAcceptHeader:
    """gp-api's MCP Streamable HTTP transport returns 406 Not Acceptable
    without `Accept: application/json, text/event-stream`. The proxy
    hardcodes this value (rather than forwarding the caller's) because
    callers — including FastAPI TestClient and httpx — routinely send
    `*/*`, which gp-api would still reject."""

    def test_sends_mcp_required_accept_regardless_of_caller(self):
        app, _, mock_http = _create_app()
        client = TestClient(app)

        # Caller sends */* (the TestClient default); proxy still emits the
        # MCP-required value upstream.
        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        outbound = mock_http.build_request.call_args.kwargs["headers"]
        assert outbound["Accept"] == "application/json, text/event-stream"

    def test_caller_supplied_accept_is_overridden(self):
        app, _, mock_http = _create_app()
        client = TestClient(app)

        # Even if the caller explicitly sets a different Accept value, the
        # proxy overrides — the upstream is MCP-specific.
        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0"}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
                "Accept": "text/html",
            },
        )

        assert resp.status_code == 200
        outbound = mock_http.build_request.call_args.kwargs["headers"]
        assert outbound["Accept"] == "application/json, text/event-stream"


class TestAgentMcpProxySseResponse:
    """gp-api's MCP Streamable HTTP transport can respond with either
    `application/json` (single envelope) or `text/event-stream` (SSE for
    streamable tools). The proxy must preserve the streaming shape for SSE
    so large/long-lived streams aren't buffered fully in memory and SSE
    framing isn't lost. Mirrors the anthropic_proxy pattern."""

    def test_sse_upstream_returns_streaming_response(self):
        sse_body = (
            b"event: message\n"
            b'data: {"jsonrpc":"2.0","result":{"chunk":1}}\n\n'
            b"event: message\n"
            b'data: {"jsonrpc":"2.0","result":{"chunk":2}}\n\n'
        )
        app, _, mock_http = _create_app(
            upstream_status=200,
            upstream_body=sse_body,
            upstream_content_type="text/event-stream",
        )
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0","method":"tools/call","id":1}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.content == sse_body
        # send() invoked in streaming mode so the upstream body wasn't
        # buffered into memory before the proxy decided what to return.
        assert mock_http.send.call_args.kwargs.get("stream") is True

    def test_sse_with_charset_in_content_type_still_streams(self):
        # Some servers send `text/event-stream; charset=utf-8`. The branch
        # check is `in upstream_content_type.lower()`, so the charset
        # suffix must not break detection.
        sse_body = b'event: message\ndata: {"chunk":1}\n\n'
        app, _, _ = _create_app(
            upstream_status=200,
            upstream_body=sse_body,
            upstream_content_type="text/event-stream; charset=utf-8",
        )
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b"{}",
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert resp.content == sse_body

    def test_upstream_send_failure_returns_502(self):
        """gp-api unreachable (connection refused, DNS, timeout, etc.)
        surfaces as a structured 502, not a generic 500. Mirrors
        anthropic_proxy's handling of httpx.HTTPError on send."""
        app, _, mock_http = _create_app()
        mock_http.send = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0"}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["reason"] == "gp_api_upstream_failed"
        assert detail["err"] == "ConnectError"

    def test_sse_mid_stream_truncation_yields_synthetic_error_event(self):
        """Mid-stream network failure during SSE iteration: the proxy must
        emit an `event: error` in-band so the downstream MCP client treats
        the truncated response as a failure, not a complete tool result.
        The 200 status was already on the wire when iteration started, so
        an in-band event is the only failure signal available."""
        app, _, mock_http = _create_app(
            upstream_status=200,
            upstream_body=b"event: message\ndata: {\"chunk\":1}\n\n",
            upstream_content_type="text/event-stream",
        )

        # Replace the streamed response with one whose aiter_bytes() yields
        # one chunk and then raises — simulating a connection drop after
        # bytes have already left the wire.
        async def truncating_iter():
            yield b'event: message\ndata: {"chunk":1}\n\n'
            raise httpx.ReadError("stream closed unexpectedly")

        upstream = mock_http.send.return_value
        upstream.aiter_bytes = MagicMock(return_value=truncating_iter())
        upstream.aclose = AsyncMock()

        client = TestClient(app)
        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0","method":"tools/call","id":1}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200  # already on the wire
        # First chunk passed through; then a synthetic SSE error event.
        assert b'data: {"chunk":1}' in resp.content
        assert b"event: error" in resp.content
        assert b"upstream_stream_truncated" in resp.content
        # Stream resource closed even on the error path.
        upstream.aclose.assert_awaited()

    def test_json_upstream_still_returns_plain_response(self):
        # Regression guard for the non-SSE path: the branch must still
        # return a fully-buffered Response (not a stream) when upstream
        # is application/json.
        json_body = b'{"jsonrpc":"2.0","result":{"tools":[]}}'
        app, _, _ = _create_app(
            upstream_status=200,
            upstream_body=json_body,
            upstream_content_type="application/json",
        )
        client = TestClient(app)

        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.content == json_body
