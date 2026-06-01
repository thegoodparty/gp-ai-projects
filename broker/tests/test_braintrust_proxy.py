import logging
import time
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.auth import BrokerTokenAuth
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore
from broker.endpoints.braintrust_proxy import (
    get_braintrust_api_key,
    get_broker_auth,
    get_upstream_client,
    router,
)

FAKE_BT_KEY = "sk-bt-real-braintrust-key"
VALID_BROKER_TOKEN = "valid-broker-token-abc"


def _make_ticket() -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=VALID_BROKER_TOKEN,
        run_id="run-001",
        organization_slug="org-42",
        experiment_id="voter_targeting",
        scope={"databricks": ["SELECT"]},
        params={"state": "CA"},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch_lambda",
    )


def _recording_handler(calls: list[httpx.Request]):
    """Upstream handler that records every request it receives and asserts the
    real Braintrust key was swapped in. Routes by host: api.braintrust.dev is
    the data plane (/logs3 ingest), www.braintrust.dev is the control plane
    (login/metadata)."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == f"Bearer {FAKE_BT_KEY}"
        if request.url.host == "api.braintrust.dev":
            return httpx.Response(200, json={"ok": True})
        if request.url.host == "www.braintrust.dev":
            return httpx.Response(
                200,
                json={"org_info": [{"id": "o1", "name": "gp", "api_url": "https://api.braintrust.dev"}]},
            )
        return httpx.Response(404)

    return handler


def _create_test_app(
    broker_auth: BrokerTokenAuth | None = None,
    upstream_transport: httpx.MockTransport | None = None,
    api_key: str = FAKE_BT_KEY,
    calls: list[httpx.Request] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    if broker_auth is None:
        store = MagicMock(spec=ScopeTicketStore)
        store.get_ticket.return_value = _make_ticket()
        broker_auth = BrokerTokenAuth(store=store)

    transport = upstream_transport or httpx.MockTransport(_recording_handler(calls if calls is not None else []))
    # No base_url: the proxy builds absolute URLs to two fixed Braintrust hosts.
    upstream_client = httpx.AsyncClient(transport=transport)

    app.dependency_overrides[get_broker_auth] = lambda: broker_auth
    app.dependency_overrides[get_upstream_client] = lambda: upstream_client
    app.dependency_overrides[get_braintrust_api_key] = lambda: api_key

    return app


class TestBraintrustProxyForwarding:
    def test_forwards_logs_ingest_to_data_plane_with_swapped_auth(self):
        calls: list[httpx.Request] = []
        app = _create_test_app(calls=calls)
        client = TestClient(app)

        resp = client.post(
            "/braintrust/api/logs3",
            content=b'{"rows":[{"span":"x"}]}',
            headers={"authorization": f"Bearer {VALID_BROKER_TOKEN}", "content-type": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert len(calls) == 1
        assert calls[0].url.host == "api.braintrust.dev"
        assert calls[0].url.path == "/logs3"
        assert calls[0].content == b'{"rows":[{"span":"x"}]}'

    def test_forwards_login_to_control_plane(self):
        calls: list[httpx.Request] = []
        app = _create_test_app(calls=calls)
        client = TestClient(app)

        resp = client.post(
            "/braintrust/app/api/apikey/login",
            content=b"{}",
            headers={"authorization": f"Bearer {VALID_BROKER_TOKEN}"},
        )

        assert resp.status_code == 200
        assert "org_info" in resp.json()
        assert len(calls) == 1
        assert calls[0].url.host == "www.braintrust.dev"
        assert calls[0].url.path == "/api/apikey/login"

    def test_forwards_get_request(self):
        calls: list[httpx.Request] = []
        app = _create_test_app(calls=calls)
        client = TestClient(app)

        resp = client.get(
            "/braintrust/app/api/project",
            headers={"authorization": f"Bearer {VALID_BROKER_TOKEN}"},
        )

        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0].method == "GET"
        assert calls[0].url.host == "www.braintrust.dev"
        assert calls[0].url.path == "/api/project"


class TestBraintrustProxyAuth:
    def test_invalid_broker_token_returns_401_and_skips_upstream(self):
        store = MagicMock(spec=ScopeTicketStore)
        store.get_ticket.return_value = None
        calls: list[httpx.Request] = []
        app = _create_test_app(broker_auth=BrokerTokenAuth(store=store), calls=calls)
        client = TestClient(app)

        resp = client.post(
            "/braintrust/api/logs3",
            content=b"{}",
            headers={"authorization": "Bearer bogus-token"},
        )

        assert resp.status_code == 401
        assert calls == []

    def test_missing_authorization_returns_401(self):
        calls: list[httpx.Request] = []
        app = _create_test_app(calls=calls)
        client = TestClient(app)

        resp = client.post("/braintrust/api/logs3", content=b"{}")

        assert resp.status_code == 401
        assert calls == []

    def test_unconfigured_braintrust_key_returns_503_and_skips_upstream(self):
        calls: list[httpx.Request] = []
        app = _create_test_app(api_key="", calls=calls)
        client = TestClient(app)

        resp = client.post(
            "/braintrust/api/logs3",
            content=b"{}",
            headers={"authorization": f"Bearer {VALID_BROKER_TOKEN}"},
        )

        assert resp.status_code == 503
        assert calls == []

    def test_unconfigured_key_warning_includes_run_id_correlation(self, caplog):
        app = _create_test_app(api_key="")
        client = TestClient(app)

        with caplog.at_level(logging.WARNING, logger="broker.endpoints.braintrust_proxy"):
            resp = client.post(
                "/braintrust/api/logs3",
                content=b"{}",
                headers={"authorization": f"Bearer {VALID_BROKER_TOKEN}"},
            )

        assert resp.status_code == 503
        warnings = [r for r in caplog.records if "not configured" in r.getMessage()]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "run-001" in message
        assert "leg=api" in message


class TestBraintrustProxyRouting:
    def test_unknown_leg_returns_404_and_skips_upstream(self):
        calls: list[httpx.Request] = []
        app = _create_test_app(calls=calls)
        client = TestClient(app)

        resp = client.post(
            "/braintrust/bogus/x",
            content=b"{}",
            headers={"authorization": f"Bearer {VALID_BROKER_TOKEN}"},
        )

        assert resp.status_code == 404
        assert calls == []


class TestBraintrustProxyUpstreamErrors:
    def test_upstream_error_returns_502(self):
        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        app = _create_test_app(upstream_transport=httpx.MockTransport(failing_handler))
        client = TestClient(app)

        resp = client.post(
            "/braintrust/api/logs3",
            content=b"{}",
            headers={"authorization": f"Bearer {VALID_BROKER_TOKEN}"},
        )

        assert resp.status_code == 502
