import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import ScopeTicket
from broker.endpoints.agent_mcp_proxy import (
    get_agent_fleet_id,
    get_agent_mcp_secret,
    get_gp_api_base_url,
    get_http_client,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-test-mcp"
GP_API_BASE_URL = "https://gp-api-dev.goodparty.org"
TEST_SECRET = "test-agent-mcp-secret"
TEST_FLEET_ID = "user_agent_fleet_test"


def _make_ticket(clerk_user_id: str | None = "user_test_abc123") -> ScopeTicket:
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
        clerk_user_id=clerk_user_id,
    )


def _create_app(
    ticket: ScopeTicket | None = None,
    upstream_status: int = 200,
    upstream_body: bytes = b'{"jsonrpc":"2.0","result":{"ok":true}}',
    upstream_content_type: str = "application/json",
    secret: str = TEST_SECRET,
    fleet_id: str = TEST_FLEET_ID,
) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    _ticket = ticket if ticket is not None else _make_ticket()
    app.dependency_overrides[get_scope_ticket] = lambda: _ticket
    app.dependency_overrides[get_agent_mcp_secret] = lambda: secret
    app.dependency_overrides[get_agent_fleet_id] = lambda: fleet_id
    app.dependency_overrides[get_gp_api_base_url] = lambda: GP_API_BASE_URL

    mock_http = MagicMock(spec=httpx.AsyncClient)
    upstream_response = httpx.Response(
        status_code=upstream_status,
        content=upstream_body,
        headers={"content-type": upstream_content_type},
    )
    # aclose is mocked on every upstream so tests can assert cleanup ran
    # without per-test setup. The proxy's `finally: await _safe_aclose()`
    # is the only thing keeping connections from leaking on every code
    # path — the assertion is the regression guard for accidental removal.
    upstream_response.aclose = AsyncMock()
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

    return app, mock_http


class TestAgentMcpProxyHappyPath:
    def test_forwards_body_with_broker_signed_jwt_and_returns_upstream_response(self):
        app, mock_http = _create_app(
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

        mock_http.build_request.assert_called_once()
        call_kwargs = mock_http.build_request.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["url"] == f"{GP_API_BASE_URL}/v1/mcp"
        assert call_kwargs["content"] == request_body

        # Verify JWT is a valid HS256 broker-signed token with correct claims.
        auth_header = call_kwargs["headers"]["Authorization"]
        assert auth_header.startswith("Bearer ")
        raw_token = auth_header.removeprefix("Bearer ")
        decoded = jwt.decode(raw_token, TEST_SECRET, algorithms=["HS256"], audience="gp-api")
        assert decoded["iss"] == "gp-broker"
        assert decoded["aud"] == "gp-api"
        assert decoded["sub"] == "user_test_abc123"
        assert decoded["act"] == {"sub": TEST_FLEET_ID}
        assert decoded["run_id"] == "run-mcp-001"
        assert decoded["exp"] - decoded["iat"] == 120

        assert call_kwargs["headers"]["X-Organization-Slug"] == "org-42"
        # send() called with stream=True so the proxy can decide to stream
        # SSE responses through without buffering.
        mock_http.send.assert_awaited_once()
        assert mock_http.send.call_args.kwargs.get("stream") is True
        # finally: aclose must run on the non-SSE happy path — without
        # this assertion, deleting `finally: await _safe_aclose()` would
        # leave every non-SSE connection leaking in production but tests
        # green. Symmetric with the SSE happy path's aclose assertion.
        mock_http.send.return_value.aclose.assert_awaited_once()

    def test_get_method_also_proxied(self):
        app, mock_http = _create_app()
        client = TestClient(app)

        resp = client.get(
            "/agent/mcp",
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert mock_http.build_request.call_args.kwargs["method"] == "GET"


class TestAgentMcpProxyMissingClerkUserId:
    def test_returns_500_when_ticket_lacks_clerk_user_id(self):
        ticket = _make_ticket(clerk_user_id=None)
        app, mock_http = _create_app(ticket=ticket)
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
        assert resp.json()["detail"]["reason"] == "ticket_missing_clerk_user_id"
        mock_http.send.assert_not_awaited()


class TestAgentMcpProxyUpstreamErrorPassthrough:
    def test_upstream_401_passes_through(self):
        app, _ = _create_app(
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
        app, _ = _create_app(
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
        app, mock_http = _create_app()
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
        assert "x-broker-token" not in lowered, f"X-Broker-Token leaked to gp-api: {outbound_headers}"
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
        app, mock_http = _create_app()
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
        app, mock_http = _create_app()
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
        app, mock_http = _create_app(
            upstream_status=200,
            upstream_body=sse_body,
            upstream_content_type="text/event-stream",
        )
        upstream = mock_http.send.return_value
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

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.content == sse_body
        # send() invoked in streaming mode so the upstream body wasn't
        # buffered into memory before the proxy decided what to return.
        assert mock_http.send.call_args.kwargs.get("stream") is True
        # finally block must close the upstream even on the happy path —
        # without this assertion, deleting `finally: await aclose()` would
        # leave every SSE connection leaking in production but tests green.
        upstream.aclose.assert_awaited_once()

    def test_sse_with_charset_in_content_type_still_streams(self):
        # Some servers send `text/event-stream; charset=utf-8`. The branch
        # check is `in upstream_content_type.lower()`, so the charset
        # suffix must not break detection.
        sse_body = b'event: message\ndata: {"chunk":1}\n\n'
        app, _ = _create_app(
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
        app, mock_http = _create_app()
        mock_http.send = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
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

    def test_non_sse_aread_failure_returns_502(self):
        """JSON-path body read fails mid-flight (connection drop after
        headers are in, before body fully read). aread() raises — must
        surface as a structured 502, not a generic 500. aclose() still
        runs via the finally."""
        app, mock_http = _create_app(
            upstream_content_type="application/json",
        )
        upstream = mock_http.send.return_value
        upstream.aread = AsyncMock(side_effect=httpx.ReadError("connection reset by peer"))

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
        assert detail["err"] == "ReadError"
        upstream.aclose.assert_awaited()

    def test_non_sse_aclose_failure_does_not_mask_upstream_error(self):
        """If aread() raises (-> HTTPException 502) AND aclose() itself
        also raises (realistic on a connection that just dropped mid-
        read), the in-flight HTTPException must survive. `_safe_aclose`
        swallows the aclose exception so FastAPI's HTTPException handler
        produces the structured 502 instead of an unstructured 500."""
        app, mock_http = _create_app(
            upstream_content_type="application/json",
        )
        upstream = mock_http.send.return_value
        upstream.aread = AsyncMock(side_effect=httpx.ReadError("connection reset by peer"))
        upstream.aclose = AsyncMock(side_effect=httpx.NetworkError("aclose also failed"))

        client = TestClient(app)
        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0"}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        # The structured 502 from aread's HTTPError survives — aclose's
        # NetworkError was swallowed by _safe_aclose.
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["reason"] == "gp_api_upstream_failed"
        assert detail["err"] == "ReadError"
        upstream.aclose.assert_awaited()

    def test_non_sse_aclose_failure_on_happy_path_does_not_break_response(self):
        """On the happy path: aread succeeds, response body is ready, but
        aclose raises during cleanup. The successful response must still
        reach the client. `_safe_aclose` swallows the exception so the
        already-built Response is returned intact."""
        json_body = b'{"jsonrpc":"2.0","result":{"ok":true}}'
        app, mock_http = _create_app(
            upstream_status=200,
            upstream_body=json_body,
            upstream_content_type="application/json",
        )
        upstream = mock_http.send.return_value
        upstream.aclose = AsyncMock(side_effect=httpx.NetworkError("aclose failed during cleanup"))

        client = TestClient(app)
        resp = client.post(
            "/agent/mcp",
            content=b'{"jsonrpc":"2.0"}',
            headers={
                "X-Broker-Token": BROKER_TOKEN,
                "Content-Type": "application/json",
            },
        )

        # Happy 200 survives the aclose failure — body intact, status
        # unchanged. aclose was still attempted.
        assert resp.status_code == 200
        assert resp.content == json_body
        upstream.aclose.assert_awaited_once()

    def test_sse_aclose_failure_does_not_break_streaming_response(self):
        """SSE path: aclose raises in the generator's finally after the
        stream content was successfully iterated. Without `_safe_aclose`,
        the raise would propagate up through the StreamingResponse
        generator and surface as a stream error (after the chunks already
        reached the wire). With the swallow, the response completes
        cleanly."""
        sse_body = b'event: message\ndata: {"chunk":1}\n\n'
        app, mock_http = _create_app(
            upstream_status=200,
            upstream_body=sse_body,
            upstream_content_type="text/event-stream",
        )
        upstream = mock_http.send.return_value
        upstream.aclose = AsyncMock(side_effect=httpx.NetworkError("aclose failed during cleanup"))

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
        assert resp.headers["content-type"].startswith("text/event-stream")
        # Stream content delivered intact despite aclose failure.
        assert resp.content == sse_body
        upstream.aclose.assert_awaited_once()

    def test_sse_mid_stream_truncation_yields_synthetic_error_event(self):
        """Mid-stream network failure during SSE iteration: the proxy must
        emit an `event: error` in-band so the downstream MCP client treats
        the truncated response as a failure, not a complete tool result.
        The 200 status was already on the wire when iteration started, so
        an in-band event is the only failure signal available."""
        app, mock_http = _create_app(
            upstream_status=200,
            upstream_body=b'event: message\ndata: {"chunk":1}\n\n',
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
        app, mock_http = _create_app(
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
        # finally: aclose must close the upstream even on the happy path.
        # Asserting here (in addition to the canonical happy-path test
        # above) covers this branch specifically, so removing the finally
        # block fails both tests rather than passing silently.
        mock_http.send.return_value.aclose.assert_awaited_once()
