from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from broker.auth import hash_service_token
from broker.clerk_client import ClerkClient
from broker.dynamodb_client import ScopeTicketStore
from broker.endpoints.delete_run_token import (
    get_service_token_hash,
    get_ticket_store,
    router,
)
from broker.endpoints.mint_run_token import (
    get_clerk_client as mint_get_clerk_client,
    get_service_token_hash as mint_get_service_token_hash,
    get_ticket_store as mint_get_ticket_store,
    router as mint_router,
)

SERVICE_TOKEN = "test-dispatch-lambda-token"
SERVICE_TOKEN_HASH = hash_service_token(SERVICE_TOKEN)

TABLE_NAME = "scope-tickets-delete"


@pytest.fixture
def moto_env():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb


@pytest.fixture
def app_and_store(moto_env):
    store = ScopeTicketStore(TABLE_NAME, dynamodb_client=moto_env)
    app = FastAPI()
    app.include_router(mint_router)
    app.include_router(router)
    fake_clerk = MagicMock(spec=ClerkClient)
    fake_clerk.create_actor_token = AsyncMock(
        return_value={"url": "https://test.clerk.app/v1/tickets/accept?ticket=jwt"}
    )
    fake_clerk.redeem_actor_token = AsyncMock(return_value={"session_id": "sess_test"})
    app.dependency_overrides[mint_get_ticket_store] = lambda: store
    app.dependency_overrides[mint_get_service_token_hash] = lambda: SERVICE_TOKEN_HASH
    app.dependency_overrides[mint_get_clerk_client] = lambda: fake_clerk
    app.dependency_overrides[get_ticket_store] = lambda: store
    app.dependency_overrides[get_service_token_hash] = lambda: SERVICE_TOKEN_HASH
    return app, store, moto_env


def _mint(client: TestClient, run_id: str) -> str:
    resp = client.post(
        "/internal/mint-run-token",
        json={
            "run_id": run_id,
            "organization_slug": "org-42",
            "experiment_id": "voter_targeting",
            "scope": {"databricks": ["SELECT"]},
            "params": {"state": "CA"},
            "clerk_user_id": "user_test_delete",
        },
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["broker_token"]


class TestDeleteRunTokenAuth:
    def test_missing_auth_header_returns_401(self, app_and_store):
        app, _, _ = app_and_store
        client = TestClient(app)

        resp = client.post(
            "/internal/delete-run-token",
            json={"broker_token": "tok", "run_id": "run-1"},
        )
        assert resp.status_code == 401

    def test_invalid_service_token_returns_401(self, app_and_store):
        app, _, _ = app_and_store
        client = TestClient(app)

        resp = client.post(
            "/internal/delete-run-token",
            json={"broker_token": "tok", "run_id": "run-1"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401


class TestDeleteRunTokenBehavior:
    def test_delete_clears_both_items(self, app_and_store):
        app, _store, ddb = app_and_store
        client = TestClient(app)

        token = _mint(client, "run-DEL-1")

        ticket_before = ddb.get_item(TableName=TABLE_NAME, Key={"pk": {"S": token}})
        lock_before = ddb.get_item(TableName=TABLE_NAME, Key={"pk": {"S": "run:run-DEL-1"}})
        assert "Item" in ticket_before
        assert "Item" in lock_before

        resp = client.post(
            "/internal/delete-run-token",
            json={"broker_token": token, "run_id": "run-DEL-1"},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert resp.status_code == 204

        ticket_after = ddb.get_item(TableName=TABLE_NAME, Key={"pk": {"S": token}})
        lock_after = ddb.get_item(TableName=TABLE_NAME, Key={"pk": {"S": "run:run-DEL-1"}})
        assert "Item" not in ticket_after
        assert "Item" not in lock_after

    def test_delete_allows_remint_with_same_run_id(self, app_and_store):
        app, _store, _ddb = app_and_store
        client = TestClient(app)

        first_token = _mint(client, "run-RETRY-2")
        client.post(
            "/internal/delete-run-token",
            json={"broker_token": first_token, "run_id": "run-RETRY-2"},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

        second_token = _mint(client, "run-RETRY-2")
        assert second_token != first_token

    def test_delete_is_idempotent(self, app_and_store):
        app, _store, _ddb = app_and_store
        client = TestClient(app)

        token = _mint(client, "run-IDEMPOTENT")
        first = client.post(
            "/internal/delete-run-token",
            json={"broker_token": token, "run_id": "run-IDEMPOTENT"},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        second = client.post(
            "/internal/delete-run-token",
            json={"broker_token": token, "run_id": "run-IDEMPOTENT"},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert first.status_code == 204
        assert second.status_code == 204
