import json
import os
from dataclasses import dataclass

import boto3


_REQUIRED_FIELDS = {
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "SERVICE_TOKEN_HASH": "service_token_hash",
    "CLERK_SECRET_KEY": "clerk_secret_key",
    "CLERK_FRONTEND_API_BASE": "clerk_frontend_api_base",
    "GP_API_BASE_URL": "gp_api_base_url",
    "AGENT_FLEET_CLERK_ID": "agent_fleet_clerk_id",
    "AGENT_MCP_TOKEN_SECRET": "agent_mcp_token_secret",
}

_OPTIONAL_FIELDS = {
    "TAVILY_API_KEY": "tavily_api_key",
    "DATABRICKS_SERVER_HOSTNAME": "databricks_server_hostname",
    "DATABRICKS_HTTP_PATH": "databricks_http_path",
    "DATABRICKS_API_KEY": "databricks_api_key",
    "RESULTS_QUEUE_URL": "results_queue_url",
    "BRAINTRUST_API_KEY": "braintrust_api_key",
}

_FIELD_MAP = {**_REQUIRED_FIELDS, **_OPTIONAL_FIELDS}


@dataclass(frozen=True)
class BrokerSecrets:
    anthropic_api_key: str
    service_token_hash: str
    clerk_secret_key: str
    clerk_frontend_api_base: str
    gp_api_base_url: str
    agent_fleet_clerk_id: str
    agent_mcp_token_secret: str
    tavily_api_key: str = ""
    databricks_server_hostname: str = ""
    databricks_http_path: str = ""
    databricks_api_key: str = ""
    results_queue_url: str = ""
    braintrust_api_key: str = ""


def _parse_secrets(raw: dict[str, str]) -> BrokerSecrets:
    kwargs = {}
    for env_key, field_name in _REQUIRED_FIELDS.items():
        value = raw.get(env_key)
        if not value:
            raise ValueError(f"Missing required secret field: {env_key}")
        kwargs[field_name] = value
    for env_key, field_name in _OPTIONAL_FIELDS.items():
        kwargs[field_name] = raw.get(env_key, "")
    return BrokerSecrets(**kwargs)


def load_secrets(secret_name: str) -> BrokerSecrets:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    raw = json.loads(response["SecretString"])
    return _parse_secrets(raw)


def load_secrets_from_env() -> BrokerSecrets:
    raw = {}
    for env_key in _REQUIRED_FIELDS:
        value = os.environ.get(env_key)
        if not value:
            raise ValueError(f"Missing required secret field: {env_key}")
        raw[env_key] = value
    for env_key in _OPTIONAL_FIELDS:
        raw[env_key] = os.environ.get(env_key, "")
    return _parse_secrets(raw)
