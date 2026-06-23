import time

import pytest

from broker.dynamodb_client import ScopeTicket
from broker.secrets import BrokerSecrets


@pytest.fixture
def fake_scope_ticket() -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk="broker-token-test-abc123",
        run_id="run-20260415-001",
        organization_slug="org-42",
        experiment_id="voter_targeting",
        scope={"databricks": ["SELECT"], "s3": ["PutObject"]},
        params={"state": "CA", "district": "SD-15", "election_type": "general"},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


@pytest.fixture
def fake_secrets() -> BrokerSecrets:
    return BrokerSecrets(
        anthropic_api_key="sk-ant-fake-key-for-testing",
        databricks_server_hostname="test-workspace.cloud.databricks.com",
        databricks_http_path="/sql/1.0/warehouses/test123",
        databricks_api_key="dapi-fake-key-for-testing",
        service_token_hash="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        clerk_secret_key="sk_test_fake_clerk_secret",
        clerk_frontend_api_base="https://fake.clerk.app",
        gp_api_base_url="https://gp-api-dev.goodparty.org",
        agent_fleet_clerk_id="user_agent_fleet_test",
        agent_mcp_token_secret="test-agent-mcp-secret",
        results_queue_url="https://sqs.us-west-2.amazonaws.com/333022194791/agent-results-dev.fifo",
    )
