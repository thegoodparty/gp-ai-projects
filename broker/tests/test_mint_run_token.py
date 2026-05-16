import hashlib
import logging
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.auth import hash_service_token
from broker.clerk_client import ClerkClient, ClerkClientError
from broker.dynamodb_client import (
    ScopeTicket,
    ScopeTicketStore,
    TicketAlreadyExistsError,
)
from broker.endpoints.mint_run_token import (
    MintRequest,
    MintResponse,
    get_clerk_client,
    get_service_token_hash,
    get_ticket_store,
    router,
)

SERVICE_TOKEN = "test-dispatch-lambda-token"
SERVICE_TOKEN_HASH = hash_service_token(SERVICE_TOKEN)
DEFAULT_CLERK_USER_ID = "user_test_abc123"
DEFAULT_ACTOR_TOKEN_URL = "https://test.clerk.app/v1/tickets/accept?ticket=jwt"


def _make_fake_clerk(
    session_id: str = "sess_default",
    actor_token_url: str = DEFAULT_ACTOR_TOKEN_URL,
) -> MagicMock:
    fake = MagicMock(spec=ClerkClient)
    fake.create_actor_token = AsyncMock(return_value={"url": actor_token_url})
    fake.redeem_actor_token = AsyncMock(return_value={"session_id": session_id})
    return fake


def _create_test_app(
    store: ScopeTicketStore | None = None,
    token_hash: str = SERVICE_TOKEN_HASH,
    clerk_client: ClerkClient | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    _store = store or MagicMock(spec=ScopeTicketStore)
    _clerk = clerk_client or _make_fake_clerk()

    app.dependency_overrides[get_ticket_store] = lambda: _store
    app.dependency_overrides[get_service_token_hash] = lambda: token_hash
    app.dependency_overrides[get_clerk_client] = lambda: _clerk

    return app


def _mint_payload(**overrides) -> dict:
    base = {
        "run_id": "run-20260415-001",
        "organization_slug": "org-42",
        "experiment_id": "voter_targeting",
        "scope": {"databricks": ["SELECT"], "tavily": True},
        "params": {"state": "CA", "district": "SD-15"},
        "clerk_user_id": DEFAULT_CLERK_USER_ID,
    }
    base.update(overrides)
    return base


class TestMintRunTokenSuccess:
    def test_returns_200_with_broker_token_and_exp(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        before = int(time.time())
        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        after = int(time.time())

        assert resp.status_code == 200
        body = resp.json()

        uuid.UUID(body["broker_token"])

        assert body["exp"] >= before + 3600
        assert body["exp"] <= after + 3600
        assert body["params_clean"] == {"state": "CA", "district": "SD-15"}

    def test_stores_scope_ticket_in_dynamodb(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        assert resp.status_code == 200
        store.put_ticket.assert_called_once()

        ticket: ScopeTicket = store.put_ticket.call_args[0][0]
        assert ticket.pk == resp.json()["broker_token"]
        assert ticket.run_id == "run-20260415-001"
        assert ticket.organization_slug == "org-42"
        assert ticket.experiment_id == "voter_targeting"
        assert ticket.issued_by == "dispatch_lambda"


class TestMintRunTokenAuth:
    def test_missing_auth_header_returns_401(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post("/internal/mint-run-token", json=_mint_payload())
        assert resp.status_code == 401

    def test_invalid_service_token_returns_401(self):
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


class TestMintRunTokenTTLCap:
    def test_ttl_above_max_is_rejected(self):
        """Caller asks for a TTL beyond MAX_TTL_SECONDS — reject loudly so a
        misconfigured dispatch (e.g., experiment with absurd timeout) is
        visible as a 400 instead of silently clamping. Silent clamp means
        agent thinks it has more time and 401s mid-run; row sticks RUNNING
        forever in gp-api.
        """
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(exp_ttl_seconds=999999),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 400
        assert "exp_ttl_seconds" in resp.json()["detail"].lower() or "max" in resp.json()["detail"].lower()

    def test_ttl_below_cap_honored(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        before = int(time.time())
        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(exp_ttl_seconds=1800),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        after = int(time.time())

        assert resp.status_code == 200
        body = resp.json()
        assert body["exp"] >= before + 1800
        assert body["exp"] <= after + 1800


class TestMintRunTokenTTLVsTimeout:
    """The ticket MUST outlive the experiment's timeout, or the agent's publish
    call will 401 at the finish line and the row sticks in RUNNING forever.
    Mint enforces exp >= timeout_seconds + buffer when the caller supplies
    timeout_seconds, even if they request a shorter exp_ttl_seconds.
    """

    def test_ttl_floor_matches_timeout_plus_buffer(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        before = int(time.time())
        # Caller asks for a too-short TTL relative to the experiment timeout.
        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(exp_ttl_seconds=600, timeout_seconds=3000),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        after = int(time.time())

        assert resp.status_code == 200
        body = resp.json()
        # Floor = timeout (3000) + buffer (300) = 3300 seconds.
        assert body["exp"] >= before + 3300
        assert body["exp"] <= after + 3300

    def test_ttl_honored_when_already_exceeds_timeout(self):
        """If caller already requests enough TTL, keep what they asked for."""
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        before = int(time.time())
        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(exp_ttl_seconds=3900, timeout_seconds=3000),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        after = int(time.time())

        assert resp.status_code == 200
        body = resp.json()
        assert body["exp"] >= before + 3900
        assert body["exp"] <= after + 3900

    def test_ttl_cap_still_enforced_when_timeout_large(self):
        """Timeout + buffer can't exceed MAX_TTL_SECONDS — reject loudly so
        ops notices the misconfigured experiment rather than silently clamping.
        """
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(exp_ttl_seconds=3600, timeout_seconds=200000),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 400
        assert "timeout_seconds" in resp.json()["detail"].lower()


class TestMintRunTokenConflict:
    def test_duplicate_ticket_returns_409(self):
        store = MagicMock(spec=ScopeTicketStore)
        store.put_ticket.side_effect = TicketAlreadyExistsError("already exists")
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 409

    def test_duplicate_run_id_returns_409(self):
        import boto3
        from moto import mock_aws

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName="scope-tickets-conflict",
                AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST",
            )
            store = ScopeTicketStore("scope-tickets-conflict", dynamodb_client=ddb)
            app = _create_test_app(store=store)
            client = TestClient(app)

            first = client.post(
                "/internal/mint-run-token",
                json=_mint_payload(run_id="run-SQS-redelivery"),
                headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            )
            assert first.status_code == 200

            second = client.post(
                "/internal/mint-run-token",
                json=_mint_payload(run_id="run-SQS-redelivery"),
                headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            )
            assert second.status_code == 409


class TestMintRunTokenIdentifierValidation:
    """Identifiers are composed into S3 keys like
    `{experiment_id}/{organization_slug}/latest.json`. A poisoned value like
    `../other_org` would let a run escape its intended prefix. Pydantic
    validation rejects unsafe identifiers at the boundary.
    """

    def test_rejects_run_id_with_path_traversal(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(run_id="../../other"),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 422

    def test_rejects_organization_slug_with_slash(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(organization_slug="org/../foo"),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 422

    def test_rejects_experiment_id_too_long(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(experiment_id="a" * 65),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 422

    def test_accepts_valid_identifiers(self):
        """Regression guard — the validator must still accept legit
        production values like slugs with hyphens and snake_case experiment IDs.
        """
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(
                run_id="run-abc123",
                organization_slug="yakima-city-council-2",
                experiment_id="voter_targeting",
            ),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 200


class TestMintRunTokenPriorArtifactVersions:
    """STALE invariant: `peer_city_benchmarking`/`meeting_briefing` must read
    the exact district_intel snapshot they were dispatched against. Dispatch
    supplies `prior_artifact_versions` on mint; the ticket persists the map so
    artifact_read can enforce the pin.
    """

    def test_prior_artifact_versions_roundtrips_to_ticket(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        pinned = {"district_intel": "district_intel/org/run-1/artifact.json"}
        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(prior_artifact_versions=pinned),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        assert resp.status_code == 200
        store.put_ticket.assert_called_once()
        ticket: ScopeTicket = store.put_ticket.call_args[0][0]
        assert ticket.prior_artifact_versions == pinned

    def test_prior_artifact_versions_optional(self):
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        assert resp.status_code == 200
        ticket: ScopeTicket = store.put_ticket.call_args[0][0]
        assert ticket.prior_artifact_versions is None


class TestMintRunTokenActorTokenRedemption:
    """The mint endpoint mints a Clerk actor token directly (broker-side, no
    gp-api hop), redeems it for a session id, and persists the session id on
    the ticket. The broker later uses this session id to mint fresh JWTs
    (cached) for each outbound MCP/HTTP call.
    """

    def test_clerk_user_id_optional_skips_clerk_dance(self):
        """Callers that don't need MCP-proxy access (just /http/fetch, /pdf/fetch,
        artifact_* etc.) can omit clerk_user_id. Mint then skips the Clerk
        actor-token round trip, persists clerk_session_id=None on the ticket,
        and returns 200. agent_mcp_proxy will 4xx tickets without a session id
        with reason=ticket_missing_clerk_session_id."""
        store = MagicMock(spec=ScopeTicketStore)
        clerk = _make_fake_clerk()
        app = _create_test_app(store=store, clerk_client=clerk)
        client = TestClient(app)

        payload = _mint_payload()
        del payload["clerk_user_id"]

        resp = client.post(
            "/internal/mint-run-token",
            json=payload,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        assert resp.status_code == 200
        clerk.create_actor_token.assert_not_awaited()
        clerk.redeem_actor_token.assert_not_awaited()
        ticket: ScopeTicket = store.put_ticket.call_args[0][0]
        assert ticket.clerk_session_id is None

    def test_session_id_persisted_on_ticket(self):
        store = MagicMock(spec=ScopeTicketStore)
        clerk = _make_fake_clerk(session_id="sess_abc123")
        app = _create_test_app(store=store, clerk_client=clerk)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        assert resp.status_code == 200
        clerk.create_actor_token.assert_awaited_once_with(DEFAULT_CLERK_USER_ID)
        clerk.redeem_actor_token.assert_awaited_once_with(DEFAULT_ACTOR_TOKEN_URL)
        ticket: ScopeTicket = store.put_ticket.call_args[0][0]
        assert ticket.clerk_session_id == "sess_abc123"

    def test_creation_failure_returns_502_with_reason(self):
        store = MagicMock(spec=ScopeTicketStore)
        clerk = MagicMock(spec=ClerkClient)
        clerk.create_actor_token = AsyncMock(side_effect=ClerkClientError("upstream 422 user_not_found"))
        clerk.redeem_actor_token = AsyncMock()
        app = _create_test_app(store=store, clerk_client=clerk)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        assert resp.status_code == 502
        body = resp.json()
        assert body["detail"]["reason"] == "clerk_actor_token_creation_failed"
        clerk.redeem_actor_token.assert_not_awaited()
        store.put_ticket.assert_not_called()

    def test_redemption_failure_returns_502_with_reason(self):
        store = MagicMock(spec=ScopeTicketStore)
        clerk = MagicMock(spec=ClerkClient)
        clerk.create_actor_token = AsyncMock(return_value={"url": DEFAULT_ACTOR_TOKEN_URL})
        clerk.redeem_actor_token = AsyncMock(side_effect=ClerkClientError("upstream 410 actor_token_already_used"))
        app = _create_test_app(store=store, clerk_client=clerk)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        assert resp.status_code == 502
        body = resp.json()
        assert body["detail"]["reason"] == "clerk_actor_token_redemption_failed"
        store.put_ticket.assert_not_called()


class TestFailureLogging:
    """Every failure path on mint must surface a structured warning so on-call
    can grep CloudWatch when a dispatch run mysteriously fails to mint. Without
    these logs the endpoint is a black box — a non-2xx response goes out and
    no operator-visible breadcrumb exists. Success is logged at info so the
    optional-Clerk path (clerk_session=present|absent) is observable too.

    Each assertion checks (a) a stable greppable failure-mode token and
    (b) the run_id is included so a specific run can be traced end-to-end.
    """

    LOGGER_NAME = "broker.endpoints.mint_run_token"

    def test_logs_warning_on_invalid_service_token(self, caplog):
        caplog.set_level(logging.WARNING, logger=self.LOGGER_NAME)
        app = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(run_id="run-bad-token"),
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401
        assert any("invalid_service_token" in r.message for r in caplog.records if r.name == self.LOGGER_NAME), (
            f"missing invalid_service_token warning; got: {[r.message for r in caplog.records]}"
        )

    def test_logs_warning_on_ttl_above_cap(self, caplog):
        caplog.set_level(logging.WARNING, logger=self.LOGGER_NAME)
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(run_id="run-ttl-cap", exp_ttl_seconds=999999),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 400
        assert any(
            "ttl_above_cap" in r.message and "run_id=run-ttl-cap" in r.message
            for r in caplog.records
            if r.name == self.LOGGER_NAME
        ), f"missing ttl_above_cap warning; got: {[r.message for r in caplog.records]}"

    def test_logs_warning_on_timeout_plus_buffer_above_cap(self, caplog):
        caplog.set_level(logging.WARNING, logger=self.LOGGER_NAME)
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(
                run_id="run-timeout-cap",
                exp_ttl_seconds=3600,
                timeout_seconds=200000,
            ),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 400
        assert any(
            "timeout_plus_buffer_above_cap" in r.message and "run_id=run-timeout-cap" in r.message
            for r in caplog.records
            if r.name == self.LOGGER_NAME
        ), f"missing timeout_plus_buffer_above_cap warning; got: {[r.message for r in caplog.records]}"

    def test_logs_warning_on_clerk_creation_failure(self, caplog):
        caplog.set_level(logging.WARNING, logger=self.LOGGER_NAME)
        store = MagicMock(spec=ScopeTicketStore)
        clerk = MagicMock(spec=ClerkClient)
        clerk.create_actor_token = AsyncMock(side_effect=ClerkClientError("upstream 422 user_not_found"))
        clerk.redeem_actor_token = AsyncMock()
        app = _create_test_app(store=store, clerk_client=clerk)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(run_id="run-clerk-fail"),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 502
        assert any(
            "clerk_actor_token_creation_failed" in r.message and "run_id=run-clerk-fail" in r.message
            for r in caplog.records
            if r.name == self.LOGGER_NAME
        ), f"missing clerk_actor_token_creation_failed warning; got: {[r.message for r in caplog.records]}"

    def test_logs_warning_on_clerk_redemption_failure(self, caplog):
        caplog.set_level(logging.WARNING, logger=self.LOGGER_NAME)
        store = MagicMock(spec=ScopeTicketStore)
        clerk = MagicMock(spec=ClerkClient)
        clerk.create_actor_token = AsyncMock(return_value={"url": DEFAULT_ACTOR_TOKEN_URL})
        clerk.redeem_actor_token = AsyncMock(side_effect=ClerkClientError("upstream 410 actor_token_already_used"))
        app = _create_test_app(store=store, clerk_client=clerk)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(run_id="run-redeem-fail"),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 502
        assert any(
            "clerk_actor_token_redemption_failed" in r.message and "run_id=run-redeem-fail" in r.message
            for r in caplog.records
            if r.name == self.LOGGER_NAME
        ), f"missing clerk_actor_token_redemption_failed warning; got: {[r.message for r in caplog.records]}"

    def test_logs_warning_on_ticket_collision(self, caplog):
        caplog.set_level(logging.WARNING, logger=self.LOGGER_NAME)
        store = MagicMock(spec=ScopeTicketStore)
        store.put_ticket.side_effect = TicketAlreadyExistsError("already exists")
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(run_id="run-collision"),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 409
        assert any(
            "ticket_already_exists" in r.message and "run_id=run-collision" in r.message
            for r in caplog.records
            if r.name == self.LOGGER_NAME
        ), f"missing ticket_already_exists warning; got: {[r.message for r in caplog.records]}"

    def test_logs_info_on_success_with_clerk_user_id(self, caplog):
        caplog.set_level(logging.INFO, logger=self.LOGGER_NAME)
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(run_id="run-ok-clerk"),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 200
        assert any(
            "mint_run_token ok" in r.message
            and "run_id=run-ok-clerk" in r.message
            and "clerk_session=present" in r.message
            for r in caplog.records
            if r.name == self.LOGGER_NAME and r.levelno == logging.INFO
        ), f"missing success info log with clerk_session=present; got: {[r.message for r in caplog.records]}"

    def test_logs_info_on_success_without_clerk_user_id(self, caplog):
        caplog.set_level(logging.INFO, logger=self.LOGGER_NAME)
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        payload = _mint_payload(run_id="run-ok-no-clerk")
        del payload["clerk_user_id"]
        resp = client.post(
            "/internal/mint-run-token",
            json=payload,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 200
        assert any(
            "mint_run_token ok" in r.message
            and "run_id=run-ok-no-clerk" in r.message
            and "clerk_session=absent" in r.message
            for r in caplog.records
            if r.name == self.LOGGER_NAME and r.levelno == logging.INFO
        ), f"missing success info log with clerk_session=absent; got: {[r.message for r in caplog.records]}"
