"""End-to-end: token minted via /internal/mint-run-token works against /http/fetch.

Catches token-shape drift between mint's response (`broker_token` in JSON body)
and /http/fetch's resolution (`X-Broker-Token` header → ScopeTicketStore.get_ticket).
Today the broker test files exercise these endpoints in isolation — if mint
ever switched to returning a different key, or /http/fetch's header resolver
expected a different lookup, both suites would stay green while prod broke.

This file owns its in-memory fakes; it does not import from sibling test files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from broker.auth import AuthError, hash_service_token
from broker.browser_fetcher import BrowserFetchResult
from broker.clerk_client import ClerkClient
from broker.dynamodb_client import ScopeTicket, TicketAlreadyExistsError
from broker.endpoints.http_fetch import (
    get_browser_fetcher as http_get_browser_fetcher,
)
from broker.endpoints.http_fetch import (
    get_scope_ticket as http_get_scope_ticket,
)
from broker.endpoints.http_fetch import (
    router as http_router,
)
from broker.endpoints.mint_run_token import (
    get_clerk_client,
    get_service_token_hash,
    get_ticket_store,
)
from broker.endpoints.mint_run_token import (
    router as mint_router,
)

SERVICE_TOKEN = "test-dispatch-lambda-token"
SERVICE_TOKEN_HASH = hash_service_token(SERVICE_TOKEN)


class _InMemoryTicketStore:
    """In-memory stand-in for ScopeTicketStore. Same surface as the real store
    (put_ticket / get_ticket) so it can be substituted for both endpoints.
    """

    def __init__(self) -> None:
        self._tickets: dict[str, ScopeTicket] = {}

    def put_ticket(self, ticket: ScopeTicket) -> None:
        if ticket.pk in self._tickets:
            existing = self._tickets[ticket.pk]
            if existing.exp > int(time.time()):
                raise TicketAlreadyExistsError(f"ticket exists for pk={ticket.pk}")
        self._tickets[ticket.pk] = ticket

    def get_ticket(self, broker_token: str) -> ScopeTicket | None:
        ticket = self._tickets.get(broker_token)
        if ticket is None:
            return None
        if ticket.exp <= int(time.time()):
            return None
        return ticket

    def insert_raw(self, ticket: ScopeTicket) -> None:
        """Bypass put_ticket validation for testing expired-ticket scenarios."""
        self._tickets[ticket.pk] = ticket


@dataclass
class _FakeFetcher:
    """Fake fetcher returning a configured BrowserFetchResult.

    BrowserFetchResult is polymorphic (Agent A's contract):
      - page response: body=bytes, body_path=None
      - download:      body=None, body_path=str (endpoint streams from disk)
    These E2E tests exercise the page-response path only — the body_path
    shape is covered by test_broker_sdk_bridge.py's download tests.
    """

    result: BrowserFetchResult
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str) -> BrowserFetchResult:
        self.calls.append(url)
        return self.result


def _make_fake_clerk() -> MagicMock:
    fake = MagicMock(spec=ClerkClient)
    fake.create_actor_token = AsyncMock(return_value={"url": "https://fake.clerk.app/sign_in_tokens/tok_1?token=jwt"})
    fake.redeem_actor_token = AsyncMock(return_value={"session_id": "sess_fake"})
    return fake


def _make_app(
    store: _InMemoryTicketStore,
    fetcher_result: BrowserFetchResult,
) -> tuple[FastAPI, _FakeFetcher]:
    """Build a FastAPI app with BOTH mint and http_fetch routers wired up
    against the same in-memory ticket store.
    """

    app = FastAPI()
    app.include_router(mint_router)
    app.include_router(http_router)

    @app.exception_handler(AuthError)
    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": exc.reason_code})

    fetcher = _FakeFetcher(result=fetcher_result)

    def _resolve_scope_ticket(request: Request) -> ScopeTicket:
        token = request.headers.get("x-broker-token", "")
        if not token:
            raise AuthError("missing_broker_token")
        ticket = store.get_ticket(token)
        if ticket is None:
            raise AuthError("scope_ticket_missing")
        return ticket

    app.dependency_overrides[get_ticket_store] = lambda: store
    app.dependency_overrides[get_service_token_hash] = lambda: SERVICE_TOKEN_HASH
    app.dependency_overrides[get_clerk_client] = lambda: _make_fake_clerk()
    app.dependency_overrides[http_get_scope_ticket] = _resolve_scope_ticket
    app.dependency_overrides[http_get_browser_fetcher] = lambda: fetcher

    return app, fetcher


def _mint_payload(**overrides) -> dict:
    base = {
        "run_id": "run-e2e-001",
        "organization_slug": "org-e2e",
        "experiment_id": "meeting_briefing",
        "scope": {"http": True},
        "params": {"district": "SD-1"},
    }
    base.update(overrides)
    return base


class TestMintToFetchE2E:
    """Mint a token, then immediately use it on /http/fetch — that single
    round trip is the contract the production lambda → broker → agent uses.
    """

    def test_minted_token_unlocks_http_fetch_clerkless(self):
        """Mint with no clerk_user_id (the clerkless mint path that
        /http/fetch supports) → the returned broker_token authorizes
        /http/fetch and the fake fetcher's body is streamed back unchanged.
        """
        store = _InMemoryTicketStore()
        body = b'[{"EventId": 42, "EventName": "City Council"}]'
        url = "https://example.com/api/meetings"
        result = BrowserFetchResult(
            status=200,
            content_type="application/json",
            final_url=url,
            byte_size=len(body),
            body=body,
        )
        app, fetcher = _make_app(store, result)
        client = TestClient(app)

        mint_resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert mint_resp.status_code == 200, mint_resp.text
        broker_token = mint_resp.json()["broker_token"]
        assert isinstance(broker_token, str) and broker_token

        fetch_resp = client.post(
            "/http/fetch",
            json={"url": url},
            headers={"X-Broker-Token": broker_token},
        )

        assert fetch_resp.status_code == 200, fetch_resp.text
        assert fetch_resp.content == body
        assert fetch_resp.headers["x-source-url"] == url
        assert fetcher.calls == [url]

    def test_unminted_token_rejected_on_http_fetch(self):
        """A token that was never minted (or already deleted) must 401 —
        not surface a 500 from a missing-ticket KeyError, not silently
        return a default ticket."""
        store = _InMemoryTicketStore()
        result = BrowserFetchResult(
            status=200,
            content_type="text/html",
            final_url="https://example.com/",
            byte_size=len(b"<html></html>"),
            body=b"<html></html>",
        )
        app, _ = _make_app(store, result)
        client = TestClient(app)

        fetch_resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/"},
            headers={"X-Broker-Token": "00000000-0000-0000-0000-000000000000"},
        )
        assert fetch_resp.status_code == 401

    def test_expired_token_rejected_on_http_fetch(self):
        """A ticket whose exp is in the past must not authorize /http/fetch.
        The store's get_ticket implementation filters expired rows; this
        test inserts an already-expired ticket directly and confirms the
        chain rejects it.
        """
        store = _InMemoryTicketStore()
        now = int(time.time())
        expired_token = "expired-token-e2e"
        store.insert_raw(
            ScopeTicket(
                pk=expired_token,
                run_id="run-expired",
                organization_slug="org-e2e",
                experiment_id="meeting_briefing",
                scope={"http": True},
                params={},
                exp=now - 60,
                issued_at=now - 3600,
                issued_by="dispatch_lambda",
            )
        )
        result = BrowserFetchResult(
            status=200,
            content_type="text/html",
            final_url="https://example.com/",
            byte_size=len(b"<html></html>"),
            body=b"<html></html>",
        )
        app, _ = _make_app(store, result)
        client = TestClient(app)

        fetch_resp = client.post(
            "/http/fetch",
            json={"url": "https://example.com/"},
            headers={"X-Broker-Token": expired_token},
        )
        assert fetch_resp.status_code == 401
