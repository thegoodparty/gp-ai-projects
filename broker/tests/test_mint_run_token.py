import hashlib
import time
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.auth import hash_service_token
from broker.dynamodb_client import (
    ScopeTicket,
    ScopeTicketStore,
    TicketAlreadyExistsError,
)
from broker.endpoints.mint_run_token import (
    MintRequest,
    MintResponse,
    get_service_token_hash,
    get_ticket_store,
    router,
)

SERVICE_TOKEN = "test-dispatch-lambda-token"
SERVICE_TOKEN_HASH = hash_service_token(SERVICE_TOKEN)


def _create_test_app(
    store: ScopeTicketStore | None = None,
    token_hash: str = SERVICE_TOKEN_HASH,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    _store = store or MagicMock(spec=ScopeTicketStore)

    app.dependency_overrides[get_ticket_store] = lambda: _store
    app.dependency_overrides[get_service_token_hash] = lambda: token_hash

    return app


def _mint_payload(**overrides) -> dict:
    base = {
        "run_id": "run-20260415-001",
        "organization_slug": "org-42",
        "experiment_id": "voter_targeting",
        "scope": {"databricks": ["SELECT"], "tavily": True},
        "params": {"state": "CA", "district": "SD-15"},
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
        visible as a 400 instead of silently clamping to 4h. Silent clamp
        means agent thinks it has 24h and 401s mid-run; row sticks RUNNING
        forever in gp-api.
        """
        store = MagicMock(spec=ScopeTicketStore)
        app = _create_test_app(store=store)
        client = TestClient(app)

        resp = client.post(
            "/internal/mint-run-token",
            json=_mint_payload(exp_ttl_seconds=99999),
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
            json=_mint_payload(exp_ttl_seconds=3600, timeout_seconds=15000),
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
