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
    mock_http.request = AsyncMock(return_value=upstream_response)
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

        mock_http.request.assert_awaited_once()
        call_kwargs = mock_http.request.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["url"] == f"{GP_API_BASE_URL}/agent/mcp"
        assert call_kwargs["content"] == request_body
        assert call_kwargs["headers"]["Authorization"] == f"Bearer {FAKE_JWT}"
        assert call_kwargs["headers"]["Content-Type"] == "application/json"
        assert call_kwargs["headers"]["X-Organization-Slug"] == "org-42"

    def test_get_method_also_proxied(self):
        app, _, mock_http = _create_app()
        client = TestClient(app)

        resp = client.get(
            "/agent/mcp",
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert mock_http.request.call_args.kwargs["method"] == "GET"


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
        mock_http.request.assert_not_awaited()


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
        mock_http.request.assert_not_awaited()


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
    the outbound request to gp-api. Only Content-Type and Authorization should
    be forwarded."""

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
        outbound_headers = mock_http.request.call_args.kwargs["headers"]
        # Case-insensitive guard: no broker token under any casing.
        lowered = {k.lower(): v for k, v in outbound_headers.items()}
        assert "x-broker-token" not in lowered, (
            f"X-Broker-Token leaked to gp-api: {outbound_headers}"
        )
        # Sanity: only the three expected headers should be present.
        assert set(lowered.keys()) == {"content-type", "authorization", "x-organization-slug"}
