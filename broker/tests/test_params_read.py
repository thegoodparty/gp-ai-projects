import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import ScopeTicket
from broker.endpoints.params_read import get_scope_ticket, router

BROKER_TOKEN = "broker-token-test-abc123"


def _make_ticket(params: dict) -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-001",
        organization_slug="org-42",
        experiment_id="opponent_research",
        scope={},
        params=params,
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


def _create_app(ticket: ScopeTicket) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_scope_ticket] = lambda: ticket
    return app


class TestParamsRead:
    def test_returns_the_ticket_params(self):
        params = {
            "opponent": {"full_name": "Jane Doe"},
            "candidate_platform": {"issues": "roads, schools, taxes"},
        }
        client = TestClient(_create_app(_make_ticket(params)))

        resp = client.get("/params/read", headers={"X-Broker-Token": BROKER_TOKEN})

        assert resp.status_code == 200
        assert resp.json() == params

    def test_returns_large_params_intact(self):
        # The whole point: params that would blow the ECS env-var budget round
        # trip exactly through the broker ticket.
        params = {"candidate_platform": {"issues": "x" * 18000}}
        client = TestClient(_create_app(_make_ticket(params)))

        resp = client.get("/params/read", headers={"X-Broker-Token": BROKER_TOKEN})

        assert resp.status_code == 200
        assert resp.json() == params
        assert len(resp.json()["candidate_platform"]["issues"]) == 18000

    def test_returns_empty_object_when_params_empty(self):
        client = TestClient(_create_app(_make_ticket({})))

        resp = client.get("/params/read", headers={"X-Broker-Token": BROKER_TOKEN})

        assert resp.status_code == 200
        assert resp.json() == {}
