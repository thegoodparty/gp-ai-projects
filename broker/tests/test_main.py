from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from broker.auth import AuthError
from broker.dynamodb_client import ScopeTicketStore
from broker.secrets import BrokerSecrets


class _NoopFetcher:
    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _fake_secrets() -> BrokerSecrets:
    return BrokerSecrets(
        anthropic_api_key="sk-ant-fake",
        tavily_api_key="tvly-fake",
        databricks_server_hostname="test.databricks.com",
        databricks_http_path="/sql/test",
        databricks_api_key="dapi-fake",
        service_token_hash="fakehash",
        clerk_secret_key="sk_test_fake",
        clerk_frontend_api_base="https://fake.clerk.app",
        gp_api_base_url="https://gp-api-dev.goodparty.org",
        agent_fleet_clerk_id="user_agent_fleet_test",
        results_queue_url="https://sqs.us-west-2.amazonaws.com/123/queue.fifo",
    )


class TestHealthEndpoint:
    def test_returns_200_ok(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "local")
        with (
            patch("broker.main.load_secrets_from_env", return_value=_fake_secrets()),
            patch("broker.main.ScopeTicketStore") as mock_store_cls,
            patch("broker.main.PlaywrightBrowserFetcher", return_value=_NoopFetcher()),
        ):
            mock_store_cls.return_value = MagicMock(spec=ScopeTicketStore)

            from broker.main import app

            with TestClient(app) as client:
                resp = client.get("/health")

            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}


class TestTableNameResolution:
    def test_uses_injected_env_var_in_aws_env(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "prod")
        monkeypatch.setenv("DYNAMO_TABLE_NAME", "broker-scope-tickets-prod")
        from broker.main import _resolve_table_name
        assert _resolve_table_name() == "broker-scope-tickets-prod"

    def test_fails_closed_when_unset_in_aws_env(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "prod")
        monkeypatch.delenv("DYNAMO_TABLE_NAME", raising=False)
        from broker.main import _resolve_table_name
        with pytest.raises(RuntimeError, match="DYNAMO_TABLE_NAME"):
            _resolve_table_name()

    @pytest.mark.parametrize("env", ["local", "development", "test"])
    def test_defaults_in_local_envs(self, monkeypatch, env):
        monkeypatch.setenv("ENVIRONMENT", env)
        monkeypatch.delenv("DYNAMO_TABLE_NAME", raising=False)
        from broker.main import _resolve_table_name
        assert _resolve_table_name() == "broker-scope-tickets-local"


class TestAuthErrorHandler:
    def test_auth_error_returns_401_json(self):
        app = FastAPI()

        @app.exception_handler(AuthError)
        async def auth_error_handler(request: Request, exc: AuthError):
            return JSONResponse(status_code=401, content={"detail": exc.reason_code})

        @app.get("/trigger")
        async def trigger():
            raise AuthError("test_reason")

        client = TestClient(app)
        resp = client.get("/trigger")

        assert resp.status_code == 401
        assert resp.json() == {"detail": "test_reason"}
