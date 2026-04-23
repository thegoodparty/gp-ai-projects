"""Smoke-test harness for the PMF Engine dispatch→broker→callback spine.

Boots the real `broker.main.app` under moto (DynamoDB + S3 + SQS), runs the
real `dispatch_handler.handler` against a mocked `ecs.run_task`, and lets
individual tests exercise the broker endpoints against a real ticket they
minted themselves.

Scope of the smoke tests (what they catch):
- App wiring: lifespan runs, dependencies resolve, routers mount
- Dispatch: parses SQS, mints a DDB-persisted ticket, would-have-started Fargate
- Broker auth: real ticket round-trips through DDB, x-broker-token header works
- Endpoint contracts: mint, http/fetch, databricks/query, artifact/publish,
  artifact/read, run-status return the documented shapes
- Lifecycle: tickets deleted on terminal status; callbacks land on results queue
- Contract schemas: artifacts use the real contracts from
  `runner/experiments/*.py` so contract drift fails the smoke test

Out of scope (not what these tests catch):
- Anthropic streaming / Claude harness behavior (no agent runs here)
- Real SSRF DNS-resolution (patched to no-op)
- Fleet-local tracker consistency (one process; see test-engineer critic)
- Real Databricks SQL dialect quirks (fake DatabricksClient returns canned rows)
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from broker.endpoints.databricks_query import (
    get_databricks_client as dbx_get_databricks_client,
)
from broker.endpoints.http_fetch import (
    get_httpx_client as http_get_httpx_client,
)
from broker.main import app

SERVICE_TOKEN = "svc-token-smoke-test-0123456789abcdef"
SERVICE_TOKEN_HASH = hashlib.sha256(SERVICE_TOKEN.encode()).hexdigest()

DDB_TABLE = "broker-scope-tickets-smoke"
ARTIFACT_BUCKET = "gp-agent-artifacts-smoke"
RESULTS_QUEUE_NAME = "agent-results-smoke.fifo"
REGION = "us-west-2"


@pytest.fixture
def aws():
    """Moto-mocked AWS with the broker's required resources created."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=DDB_TABLE,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=ARTIFACT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

        sqs = boto3.client("sqs", region_name=REGION)
        q = sqs.create_queue(
            QueueName=RESULTS_QUEUE_NAME,
            Attributes={
                "FifoQueue": "true",
                "ContentBasedDeduplication": "false",
            },
        )
        queue_url = q["QueueUrl"]

        yield {
            "ddb": ddb,
            "s3": s3,
            "sqs": sqs,
            "queue_url": queue_url,
            "bucket": ARTIFACT_BUCKET,
            "table": DDB_TABLE,
        }


@pytest.fixture
def broker_env(aws, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DYNAMO_TABLE_NAME", DDB_TABLE)
    monkeypatch.setenv("ARTIFACT_BUCKET", ARTIFACT_BUCKET)
    monkeypatch.setenv("RESULTS_QUEUE_URL", aws["queue_url"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-smoke-placeholder")
    monkeypatch.setenv("SERVICE_TOKEN_HASH", SERVICE_TOKEN_HASH)


@pytest.fixture
def broker_client(broker_env, aws):
    """TestClient with a booted broker app.

    Enters the app as a context manager so FastAPI's `lifespan` runs — that's
    where dependency overrides are registered. Without this, every endpoint
    call 500s with 'NotImplementedError: must be overridden'.
    """
    # ssrf_guard does a real DNS lookup; neutralize it for smoke tests. SSRF
    # correctness is covered by `broker/tests/test_ssrf_guard.py` — smoke
    # tests verify the spine wires through, not the guard itself.
    async def _allow_all_urls(url: str):
        return None

    with patch(
        "broker.ssrf_guard.validate_url",
        side_effect=_allow_all_urls,
    ):
        with TestClient(app) as client:
            yield client


def mint_ticket(
    client: TestClient,
    *,
    experiment_id: str,
    run_id: str,
    organization_slug: str,
    params: dict,
    scope: dict,
    timeout_seconds: int = 600,
    prior_artifact_versions: dict | None = None,
) -> str:
    """Mint a broker_token via the real /internal/mint-run-token endpoint."""
    body: dict = {
        "run_id": run_id,
        "organization_slug": organization_slug,
        "experiment_id": experiment_id,
        "scope": scope,
        "params": params,
        "exp_ttl_seconds": timeout_seconds + 300,
        "timeout_seconds": timeout_seconds,
    }
    if prior_artifact_versions is not None:
        body["prior_artifact_versions"] = prior_artifact_versions
    resp = client.post(
        "/internal/mint-run-token",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        json=body,
    )
    assert resp.status_code == 200, f"mint failed: {resp.status_code} {resp.text}"
    return resp.json()["broker_token"]


def read_one_callback(aws) -> dict:
    """Pop one message from the results SQS queue and parse its envelope."""
    resp = aws["sqs"].receive_message(
        QueueUrl=aws["queue_url"],
        MaxNumberOfMessages=1,
        WaitTimeSeconds=0,
    )
    msgs = resp.get("Messages", [])
    assert len(msgs) == 1, (
        f"expected exactly 1 callback on the results queue, got {len(msgs)}"
    )
    return json.loads(msgs[0]["Body"])


def drain_callbacks(aws) -> list[dict]:
    """Drain ALL messages from the results queue.

    Pairs with the "exactly one callback per run" regressions (dispatch swallow
    + runner double-emit): callers assert `len(drain_callbacks(aws)) == 1` to
    prove no duplicate emits land during a terminal transition.
    """
    collected: list[dict] = []
    while True:
        resp = aws["sqs"].receive_message(
            QueueUrl=aws["queue_url"],
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            break
        for m in msgs:
            collected.append(json.loads(m["Body"]))
            aws["sqs"].delete_message(
                QueueUrl=aws["queue_url"],
                ReceiptHandle=m["ReceiptHandle"],
            )
    return collected


def ticket_exists(aws, broker_token: str) -> bool:
    resp = aws["ddb"].get_item(
        TableName=DDB_TABLE,
        Key={"pk": {"S": broker_token}},
    )
    return "Item" in resp


class FakeDatabricksClient:
    """In-memory fake for `broker.endpoints.databricks_query.DatabricksClient`.

    Returns canned rows regardless of the SQL (the smoke test verifies that
    the rewriter + scope checks + tracker increment + JSON response shape are
    all wired up end-to-end — NOT that Databricks executes the SQL correctly,
    which is covered by `broker/tests/test_sql_rewriter.py`).
    """

    def __init__(self, columns: list[str], rows: list[list]):
        self._columns = columns
        self._rows = rows
        self.calls: list[tuple[str, dict | None]] = []

    def execute(
        self, sql: str, parameters: dict | None = None
    ) -> tuple[list[str], list[list]]:
        self.calls.append((sql, parameters))
        return self._columns, self._rows


@pytest.fixture
def fake_databricks():
    """Factory — each test installs its own canned rows via `install(client, …)`."""

    def _install(client: TestClient, columns: list[str], rows: list[list]):
        fake = FakeDatabricksClient(columns=columns, rows=rows)
        client.app.dependency_overrides[dbx_get_databricks_client] = lambda: fake
        return fake

    return _install


@pytest.fixture
def fake_http():
    """Factory — installs an `httpx.AsyncClient` backed by a `MockTransport`
    so `/http/fetch` returns canned responses for URLs the test declares.
    """

    def _install(client: TestClient, routes: dict[str, dict]):
        def handler(request: httpx.Request) -> httpx.Response:
            key = str(request.url)
            route = routes.get(key)
            if route is None:
                return httpx.Response(404, text=f"no mock route for {key}")
            return httpx.Response(
                status_code=route.get("status", 200),
                headers=route.get("headers", {"content-type": "text/html"}),
                content=route["body"].encode("utf-8"),
            )

        transport = httpx.MockTransport(handler)
        fake_client = httpx.AsyncClient(transport=transport, timeout=30)
        client.app.dependency_overrides[http_get_httpx_client] = lambda: fake_client
        return fake_client

    return _install
