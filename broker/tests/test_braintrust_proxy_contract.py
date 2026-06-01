"""Real-SDK contract test for the Braintrust broker proxy.

Integration-test finding 8: prove the *actual* installed Braintrust SDK
(`braintrust` 0.17.0) routes login + log ingest through our broker proxy
correctly, exercising the SDK's genuine URL resolution, path-join, header/auth
behavior, and control-plane vs data-plane connection selection.

The existing `test_braintrust_proxy.py` drives the proxy with a hand-authored
HTTP client and asserts against an assumed SDK contract. That can silently
drift from the real SDK (e.g. if a future SDK version changes the login path,
the env-var override names, or how it splits app/api hosts). This test closes
that gap by driving the genuine `braintrust.init_logger(...)` -> span.log ->
flush path and asserting the proxy saw exactly the requests the real SDK emits.

Hermetic design (must NOT touch real braintrust.dev):

  real braintrust SDK (sync `requests`)
        |  set_http_adapter(_ASGIRequestsAdapter)
        v
  _ASGIRequestsAdapter.send()         # translates requests.PreparedRequest
        |  httpx.ASGITransport          into an in-process ASGI call
        v
  FastAPI app mounting braintrust_router
        |  get_upstream_client override
        v
  httpx.AsyncClient(MockTransport(_fake_braintrust))   # fakes braintrust.dev

The SDK uses the synchronous `requests` library, so we cannot hand it an httpx
client directly. Instead we inject a `requests.adapters.HTTPAdapter` subclass
via the SDK's public `braintrust.set_http_adapter(...)` hook. That adapter
bridges every `requests` call the SDK makes into the in-process broker ASGI app
through `httpx.ASGITransport` (the sync analogue of the `_SyncASGIBridge`
pattern in `test_http_head_contract.py`, adapted to the `requests` interface
the SDK's `HTTPConnection` actually calls).

Env wiring (the runner's real production config):
  BRAINTRUST_APP_URL = http://broker/braintrust/app  -> proxy leg "app"
  BRAINTRUST_API_URL = http://broker/braintrust/api  -> proxy leg "api"
  BRAINTRUST_API_KEY = <per-run broker token>        -> proxy verifies + swaps
  BRAINTRUST_ORG_NAME = test-org                      -> org selected from login

The repo's autouse conftest sets BRAINTRUST_API_KEY="" to disable telemetry;
per the repo convention we override it inside this test only (monkeypatch) and
reset both the SDK global state and `shared.braintrust.BraintrustClient`.
"""

import asyncio
import time
from unittest.mock import MagicMock

import httpx
import pytest
import requests
from fastapi import FastAPI

from broker.auth import BrokerTokenAuth
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore
from broker.endpoints.braintrust_proxy import (
    get_braintrust_api_key,
    get_broker_auth,
    get_upstream_client,
    router,
)

# The fake "real" upstream Braintrust key the proxy swaps in. The SDK never
# sees this — it only ever holds VALID_BROKER_TOKEN. If the proxy forwarded the
# client's token instead of swapping, the assertions below would fail.
REAL_UPSTREAM_KEY = "sk-bt-REAL-upstream-key"
VALID_BROKER_TOKEN = "valid-broker-token-abc"


def _make_ticket() -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=VALID_BROKER_TOKEN,
        run_id="run-bt-contract-001",
        organization_slug="org-42",
        experiment_id="voter_targeting",
        scope={"databricks": ["SELECT"]},
        params={"state": "CA"},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch_lambda",
    )


class _RecordedRequest:
    __slots__ = ("method", "host", "path", "authorization")

    def __init__(self, request: httpx.Request):
        self.method = request.method
        self.host = request.url.host
        self.path = request.url.path
        self.authorization = request.headers.get("authorization")


def _fake_braintrust(calls: list[_RecordedRequest]):
    """MockTransport handler standing in for the real braintrust.dev hosts.

    Records (method, host, path, authorization) for every forwarded request and
    returns the minimal responses the SDK needs to complete a login + project
    register + logs3 ingest round trip.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_RecordedRequest(request))
        path = request.url.path

        if request.method == "POST" and path == "/api/apikey/login":
            return httpx.Response(
                200,
                json={
                    "org_info": [
                        {
                            "id": "o1",
                            "name": "test-org",
                            "api_url": "https://api.braintrust.dev",
                            "proxy_url": "https://api.braintrust.dev",
                        }
                    ]
                },
            )
        if request.method == "POST" and path == "/api/project/register":
            return httpx.Response(
                200,
                json={"project": {"id": "p1", "name": "pmf-engine-test"}},
            )
        if request.method == "POST" and path == "/logs3":
            return httpx.Response(200, json={})
        # ping / anything else the SDK may probe
        return httpx.Response(200, json={})

    return handler


def _build_broker_app(calls: list[_RecordedRequest]) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    store = MagicMock(spec=ScopeTicketStore)

    def _get_ticket(token: str):
        return _make_ticket() if token == VALID_BROKER_TOKEN else None

    store.get_ticket.side_effect = _get_ticket
    broker_auth = BrokerTokenAuth(store=store)

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_braintrust(calls)))

    app.dependency_overrides[get_broker_auth] = lambda: broker_auth
    app.dependency_overrides[get_upstream_client] = lambda: upstream_client
    app.dependency_overrides[get_braintrust_api_key] = lambda: REAL_UPSTREAM_KEY
    return app


class _ASGIRequestsAdapter(requests.adapters.HTTPAdapter):
    """Routes the SDK's synchronous `requests` calls into the in-process broker
    ASGI app via httpx.ASGITransport, so the genuine Braintrust SDK reaches our
    proxy without any network I/O.

    The SDK's HTTPConnection mounts this adapter on its requests.Session for
    both http:// and https://. We translate the outgoing requests.PreparedRequest
    into an httpx.Request against the ASGITransport, run it on a fresh event
    loop, and translate the httpx.Response back into a requests.Response.
    """

    def __init__(self, app: FastAPI):
        super().__init__()
        self._transport = httpx.ASGITransport(app=app)

    def send(self, request, **kwargs):  # type: ignore[override]
        body = request.body
        if isinstance(body, str):
            body = body.encode("utf-8")
        httpx_request = httpx.Request(
            method=request.method,
            url=request.url,
            headers=dict(request.headers),
            content=body,
        )

        async def _run() -> httpx.Response:
            resp = await self._transport.handle_async_request(httpx_request)
            await resp.aread()
            return resp

        httpx_resp = asyncio.run(_run())

        resp = requests.models.Response()
        resp.status_code = httpx_resp.status_code
        resp._content = httpx_resp.content
        resp.url = str(httpx_request.url)
        resp.headers = requests.structures.CaseInsensitiveDict(httpx_resp.headers)
        resp.request = request
        resp.encoding = "utf-8"
        return resp


@pytest.fixture
def _reset_braintrust_state():
    """Reset both the SDK global login state and the shared client singleton so
    no prior test's login leaks in and our login actually fires through the
    proxy."""
    import braintrust.logger as bt_logger

    from shared.braintrust import BraintrustClient

    BraintrustClient.reset_instance()
    bt_logger._internal_reset_global_state()
    prior_adapter = bt_logger._http_adapter
    yield
    bt_logger._http_adapter = prior_adapter
    bt_logger._internal_reset_global_state()
    BraintrustClient.reset_instance()


class TestBraintrustRealSDKContract:
    def test_real_sdk_routes_login_and_ingest_through_proxy_with_swapped_key(
        self, monkeypatch, _reset_braintrust_state
    ):
        import braintrust

        calls: list[_RecordedRequest] = []
        app = _build_broker_app(calls)

        # Point the genuine SDK at the in-process broker proxy. The runner sets
        # exactly these two env vars in production.
        monkeypatch.setenv("BRAINTRUST_APP_URL", "http://broker/braintrust/app")
        monkeypatch.setenv("BRAINTRUST_API_URL", "http://broker/braintrust/api")
        # Override conftest's BRAINTRUST_API_KEY="" — the SDK sends this broker
        # token; the proxy verifies it and swaps in REAL_UPSTREAM_KEY.
        monkeypatch.setenv("BRAINTRUST_API_KEY", VALID_BROKER_TOKEN)
        monkeypatch.setenv("BRAINTRUST_ORG_NAME", "test-org")
        # Avoid any unrelated public-url probing during permalink/metadata.
        monkeypatch.delenv("BRAINTRUST_APP_PUBLIC_URL", raising=False)
        monkeypatch.delenv("BRAINTRUST_PROXY_URL", raising=False)

        braintrust.set_http_adapter(_ASGIRequestsAdapter(app))

        # Synchronous flush so the logs3 ingest completes before we assert.
        logger = braintrust.init_logger(project="pmf-engine-test", async_flush=False)
        with logger.start_span(name="contract-span", type="task") as span:
            span.log(input={"q": "hello"}, output={"a": "world"})
        logger.flush()

        by_path = {(c.method, c.host, c.path): c for c in calls}

        # 1. Login went to the control plane host (www.braintrust.dev) via leg "app".
        login = next(
            (c for c in calls if c.method == "POST" and c.path == "/api/apikey/login"),
            None,
        )
        assert login is not None, f"no login call recorded; calls={[(c.method, c.host, c.path) for c in calls]}"
        assert login.host == "www.braintrust.dev"
        assert login.authorization == f"Bearer {REAL_UPSTREAM_KEY}"

        # 2. logs3 ingest went to the data plane host (api.braintrust.dev) via leg "api".
        ingest = next(
            (c for c in calls if c.method == "POST" and c.path == "/logs3"),
            None,
        )
        assert ingest is not None, f"no logs3 ingest recorded; calls={[(c.method, c.host, c.path) for c in calls]}"
        assert ingest.host == "api.braintrust.dev"
        assert ingest.authorization == f"Bearer {REAL_UPSTREAM_KEY}"

        # 3. EVERY forwarded request carried the swapped real key, never the
        #    broker token. This is the core security contract: the runner's
        #    per-run token never reaches Braintrust.
        for c in calls:
            assert c.authorization == f"Bearer {REAL_UPSTREAM_KEY}", (
                f"request {c.method} {c.host}{c.path} forwarded "
                f"unexpected auth {c.authorization!r}"
            )
        assert all(c.authorization != f"Bearer {VALID_BROKER_TOKEN}" for c in calls)

        # Sanity: the control-plane register call (proof the SDK completed the
        # full lazy-login metadata path against the proxy, not just a stub).
        assert ("POST", "www.braintrust.dev", "/api/project/register") in by_path
