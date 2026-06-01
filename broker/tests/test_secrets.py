import json
from unittest.mock import MagicMock, patch

import pytest

from broker.secrets import BrokerSecrets, load_secrets, load_secrets_from_env


FULL_SECRET = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "TAVILY_API_KEY": "tavily-test",
    "DATABRICKS_SERVER_HOSTNAME": "db-host.cloud.databricks.com",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc",
    "DATABRICKS_API_KEY": "dapi-test",
    "SERVICE_TOKEN_HASH": "abc123hash",
    "CLERK_SECRET_KEY": "sk_test_clerk",
    "CLERK_FRONTEND_API_BASE": "https://test.clerk.app",
    "GP_API_BASE_URL": "https://gp-api-dev.goodparty.org",
    "AGENT_FLEET_CLERK_ID": "user_agent_fleet_test",
    "AGENT_MCP_TOKEN_SECRET": "test-agent-mcp-secret",
    "RESULTS_QUEUE_URL": "https://sqs.us-west-2.amazonaws.com/123/results.fifo",
    "BRAINTRUST_API_KEY": "sk-bt-test",
}


class TestLoadSecrets:
    def test_parses_secretsmanager_json_into_dataclass(self):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps(FULL_SECRET),
        }

        with patch("broker.secrets.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_client
            result = load_secrets("AI_SECRETS_DEV")

        mock_client.get_secret_value.assert_called_once_with(SecretId="AI_SECRETS_DEV")
        assert isinstance(result, BrokerSecrets)
        assert result.anthropic_api_key == "sk-ant-test"
        assert result.tavily_api_key == "tavily-test"
        assert result.databricks_server_hostname == "db-host.cloud.databricks.com"
        assert result.databricks_http_path == "/sql/1.0/warehouses/abc"
        assert result.databricks_api_key == "dapi-test"
        assert result.service_token_hash == "abc123hash"
        assert result.clerk_secret_key == "sk_test_clerk"
        assert result.clerk_frontend_api_base == "https://test.clerk.app"
        assert result.agent_fleet_clerk_id == "user_agent_fleet_test"
        assert result.agent_mcp_token_secret == "test-agent-mcp-secret"
        assert result.results_queue_url == "https://sqs.us-west-2.amazonaws.com/123/results.fifo"
        assert result.braintrust_api_key == "sk-bt-test"

    def test_braintrust_api_key_is_optional(self):
        without_bt = {k: v for k, v in FULL_SECRET.items() if k != "BRAINTRUST_API_KEY"}
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": json.dumps(without_bt)}

        with patch("broker.secrets.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_client
            result = load_secrets("AI_SECRETS_DEV")

        assert result.braintrust_api_key == ""

    def test_missing_required_field_raises_valueerror(self):
        incomplete = {k: v for k, v in FULL_SECRET.items() if k != "ANTHROPIC_API_KEY"}
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps(incomplete),
        }

        with patch("broker.secrets.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_client
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                load_secrets("AI_SECRETS_DEV")


class TestLoadSecretsFromEnv:
    def test_reads_all_fields_from_env(self, monkeypatch):
        for key, value in FULL_SECRET.items():
            monkeypatch.setenv(key, value)

        result = load_secrets_from_env()

        assert isinstance(result, BrokerSecrets)
        assert result.anthropic_api_key == "sk-ant-test"
        assert result.results_queue_url == "https://sqs.us-west-2.amazonaws.com/123/results.fifo"

    def test_missing_required_env_var_raises_valueerror(self, monkeypatch):
        for key, value in FULL_SECRET.items():
            if key != "SERVICE_TOKEN_HASH":
                monkeypatch.setenv(key, value)
        monkeypatch.delenv("SERVICE_TOKEN_HASH", raising=False)

        with pytest.raises(ValueError, match="SERVICE_TOKEN_HASH"):
            load_secrets_from_env()
