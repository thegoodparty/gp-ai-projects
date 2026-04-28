import json
import logging
import time
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.auth import AuthError, BrokerTokenAuth
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore
from broker.endpoints.anthropic_proxy import (
    get_anthropic_api_key,
    get_broker_auth,
    get_upstream_client,
    router,
)

FAKE_API_KEY = "sk-ant-real-key-for-proxy"
VALID_BROKER_TOKEN = "valid-broker-token-abc"


def _make_ticket(expired: bool = False) -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=VALID_BROKER_TOKEN,
        run_id="run-001",
        organization_slug="org-42",
        experiment_id="voter_targeting",
        scope={"databricks": ["SELECT"]},
        params={"state": "CA"},
        exp=now + (-3600 if expired else 3600),
        issued_at=now,
        issued_by="dispatch_lambda",
    )


def _upstream_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/messages":
        body = json.loads(request.content)

        assert request.headers["x-api-key"] == FAKE_API_KEY
        assert request.headers["anthropic-version"] == "2023-06-01"

        if body.get("stream"):
            content = (
                b"event: content_block_start\n"
                b'data: {"type":"content_block_start","index":0}\n\n'
                b"event: content_block_delta\n"
                b'data: {"type":"content_block_delta","delta":{"text":"Hello"}}\n\n'
                b"event: message_stop\n"
                b'data: {"type":"message_stop"}\n\n'
            )
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
            )

        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
            },
        )

    return httpx.Response(404)


def _error_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(502, json={"error": "upstream failure"})


def _create_test_app(
    broker_auth: BrokerTokenAuth | None = None,
    upstream_transport: httpx.MockTransport | None = None,
    api_key: str = FAKE_API_KEY,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    if broker_auth is None:
        store = MagicMock(spec=ScopeTicketStore)
        store.get_ticket.return_value = _make_ticket()
        broker_auth = BrokerTokenAuth(store=store)

    transport = upstream_transport or httpx.MockTransport(_upstream_handler)
    upstream_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")

    app.dependency_overrides[get_broker_auth] = lambda: broker_auth
    app.dependency_overrides[get_upstream_client] = lambda: upstream_client
    app.dependency_overrides[get_anthropic_api_key] = lambda: api_key

    return app


class TestAnthropicProxyNonStreaming:
    def test_proxies_request_and_injects_api_key(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
            headers={"x-api-key": VALID_BROKER_TOKEN},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["content"][0]["text"] == "Hello"

    def test_replaces_broker_token_with_real_key(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
            headers={"x-api-key": VALID_BROKER_TOKEN},
        )

        assert resp.status_code == 200


class TestSdkTelemetryNoOps:
    def test_event_logging_batch_returns_204(self):
        # Claude Agent SDK POSTs telemetry to /api/event_logging/batch. We don't
        # proxy it, so the SDK used to get 404s (and retry 264 times per run,
        # polluting logs). A 204 short-circuits the retry loop cheaply.
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/api/event_logging/batch",
            json={"events": [{"type": "telemetry", "data": "x"}]},
            headers={"x-api-key": VALID_BROKER_TOKEN},
        )

        assert resp.status_code == 204
        assert resp.content == b""

    def test_event_logging_batch_no_auth_required(self):
        # SDK fires telemetry without broker token too — return 204 either way.
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/api/event_logging/batch",
            json={"events": []},
        )

        assert resp.status_code == 204


class TestAnthropicProxyAuth:
    def test_missing_api_key_returns_401(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
        )

        assert resp.status_code == 401

    def test_expired_ticket_returns_401(self):
        store = MagicMock(spec=ScopeTicketStore)
        store.get_ticket.return_value = None
        broker_auth = BrokerTokenAuth(store=store)

        app = _create_test_app(broker_auth=broker_auth)
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
            headers={"x-api-key": "expired-token"},
        )

        assert resp.status_code == 401


class TestAnthropicProxyHeaderConsistency:
    def test_rejects_mismatched_x_api_key_and_x_broker_token(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
            headers={
                "x-api-key": VALID_BROKER_TOKEN,
                "x-broker-token": "some-other-token-xyz",
            },
        )

        assert resp.status_code == 400
        body = resp.json()
        assert "header_token_mismatch" in body.get("detail", "")

    def test_accepts_matching_x_api_key_and_x_broker_token(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
            headers={
                "x-api-key": VALID_BROKER_TOKEN,
                "x-broker-token": VALID_BROKER_TOKEN,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["content"][0]["text"] == "Hello"


class TestAnthropicProxyUpstreamErrors:
    def test_upstream_502_propagated(self):
        transport = httpx.MockTransport(_error_handler)
        app = _create_test_app(upstream_transport=transport)
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
            headers={"x-api-key": VALID_BROKER_TOKEN},
        )

        assert resp.status_code == 502


class TestAnthropicProxyUpstreamTransportFailures:
    def test_upstream_connect_error_returns_502_and_logs(self, caplog):
        def raise_connect(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        transport = httpx.MockTransport(raise_connect)
        app = _create_test_app(upstream_transport=transport)
        client = TestClient(app, raise_server_exceptions=False)

        with caplog.at_level(logging.WARNING, logger="broker.endpoints.anthropic_proxy"):
            resp = client.post(
                "/anthropic/v1/messages",
                json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
                headers={"x-api-key": VALID_BROKER_TOKEN},
            )

        assert resp.status_code == 502
        body = resp.json()
        detail = body.get("detail", "")
        assert "connection refused" not in detail
        assert "anthropic upstream failed" in detail.lower()

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "broker.endpoints.anthropic_proxy"
        ]
        assert len(warning_records) >= 1, "expected a WARNING log on upstream failure"
        combined = " ".join(r.getMessage() for r in warning_records)
        assert "run-001" in combined
        assert "ConnectError" in combined

    def test_upstream_read_timeout_returns_502(self, caplog):
        def raise_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        transport = httpx.MockTransport(raise_timeout)
        app = _create_test_app(upstream_transport=transport)
        client = TestClient(app, raise_server_exceptions=False)

        with caplog.at_level(logging.WARNING, logger="broker.endpoints.anthropic_proxy"):
            resp = client.post(
                "/anthropic/v1/messages",
                json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 100},
                headers={"x-api-key": VALID_BROKER_TOKEN},
            )

        assert resp.status_code == 502
        body = resp.json()
        detail = body.get("detail", "")
        assert "read timed out" not in detail

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "broker.endpoints.anthropic_proxy"
        ]
        assert len(warning_records) >= 1
        combined = " ".join(r.getMessage() for r in warning_records)
        assert "ReadTimeout" in combined


class TestAnthropicProxyStreaming:
    def test_sse_chunks_relayed(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/anthropic/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 100,
                "stream": True,
            },
            headers={"x-api-key": VALID_BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        body = resp.text
        assert "content_block_start" in body
        assert "content_block_delta" in body
        assert "message_stop" in body


class TestAnthropicProxyStreamTruncation:
    """Regression guard for R8 finding: mid-stream upstream failures used to
    log WARN and close silently, so the Claude SDK received a truncated SSE
    message and either (a) silently accepted partial content as a complete
    turn, or (b) raised a non-descript error without run_id correlation.
    Fix: log ERROR with run_id + org + model + exc_info, AND yield a synthetic
    SSE `event: error` frame before closing so the SDK fails loudly.
    """

    def _truncating_upstream(self) -> httpx.MockTransport:
        """Upstream that yields one SSE chunk then raises RemoteProtocolError."""

        class _TruncatingByteStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (
                    b"event: content_block_start\n"
                    b'data: {"type":"content_block_start","index":0}\n\n'
                )
                raise httpx.RemoteProtocolError("peer closed mid-stream")

            async def aclose(self):
                pass

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=_TruncatingByteStream(),
                headers={"content-type": "text/event-stream"},
            )

        return httpx.MockTransport(handler)

    def test_stream_truncation_yields_sse_error_event_and_logs_error(self, caplog):
        app = _create_test_app(upstream_transport=self._truncating_upstream())
        client = TestClient(app)

        with caplog.at_level(logging.ERROR, logger="broker.endpoints.anthropic_proxy"):
            resp = client.post(
                "/anthropic/v1/messages",
                json={
                    "model": "claude-opus-4-20250514",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 100,
                    "stream": True,
                },
                headers={"x-api-key": VALID_BROKER_TOKEN},
            )

        # 200 was already on the wire before the truncation — can't change that.
        assert resp.status_code == 200

        # The initial chunk reached the client.
        assert "content_block_start" in resp.text

        # Synthetic SSE error event is appended so the agent SDK sees a loud
        # failure rather than silently accepting a truncated message as done.
        assert 'event: error' in resp.text, (
            f"expected synthetic SSE error event on stream truncation; got body:\n{resp.text}"
        )
        assert 'upstream_stream_truncated' in resp.text

        # ERROR (not WARN) with full context for on-call correlation.
        errs = [
            r for r in caplog.records
            if r.levelno >= logging.ERROR
            and r.name == "broker.endpoints.anthropic_proxy"
        ]
        assert errs, (
            f"expected ERROR-level log from anthropic_proxy on stream truncation; "
            f"got: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        msg = errs[0].getMessage()
        assert "run-001" in msg  # run_id
        assert "org-42" in msg  # organization_slug
        assert "claude-opus-4-20250514" in msg  # model
        assert "RemoteProtocolError" in msg  # exc_type
        assert errs[0].exc_info is not None  # stack trace attached
