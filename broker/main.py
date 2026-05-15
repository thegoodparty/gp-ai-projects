import os
from contextlib import asynccontextmanager

import boto3
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from broker.auth import AuthError, BrokerTokenAuth
from broker.callback_sender import CallbackSender
from broker.clerk_client import ClerkClient
from broker.data_query_tracker import DataQueryTracker
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore
from broker.endpoints.agent_mcp_proxy import (
    get_clerk_client as agent_mcp_get_clerk_client,
    get_gp_api_base_url as agent_mcp_get_gp_api_base_url,
    get_http_client as agent_mcp_get_http_client,
    get_scope_ticket as agent_mcp_get_scope_ticket,
    router as agent_mcp_router,
)
from broker.endpoints.anthropic_proxy import (
    get_anthropic_api_key,
    get_broker_auth,
    get_upstream_client,
    router as anthropic_router,
)
from broker.endpoints.artifact_publish import (
    get_artifact_bucket as publish_get_artifact_bucket,
    get_broker_token_raw as publish_get_broker_token_raw,
    get_callback_sender as publish_get_callback_sender,
    get_data_query_tracker as publish_get_data_query_tracker,
    get_s3_client as publish_get_s3_client,
    get_scope_ticket as publish_get_scope_ticket,
    get_ticket_store as publish_get_ticket_store,
    router as publish_router,
)
from broker.endpoints.artifact_read import (
    get_artifact_bucket as read_get_artifact_bucket,
    get_s3_client as read_get_s3_client,
    get_scope_ticket as read_get_scope_ticket,
    router as read_router,
)
from broker.endpoints.databricks_query import (
    get_data_query_tracker as dbx_get_data_query_tracker,
    get_databricks_client as dbx_get_databricks_client,
    get_scope_ticket as dbx_get_scope_ticket,
    router as databricks_router,
)
from broker.endpoints.mint_run_token import (
    get_clerk_client,
    get_service_token_hash,
    get_ticket_store,
    router as mint_router,
)
from broker.endpoints.delete_run_token import (
    get_service_token_hash as delete_get_service_token_hash,
    get_ticket_store as delete_get_ticket_store,
    router as delete_router,
)
from broker.endpoints.http_fetch import (
    get_browser_fetcher as http_get_browser_fetcher,
    get_scope_ticket as http_get_scope_ticket,
    router as http_router,
)
from broker.browser_fetcher import PlaywrightBrowserFetcher  # noqa: I001
from broker.endpoints.run_status import (
    get_artifact_bucket as status_get_artifact_bucket,
    get_broker_token_raw as status_get_broker_token_raw,
    get_callback_sender as status_get_callback_sender,
    get_data_query_tracker as status_get_data_query_tracker,
    get_s3_client as status_get_s3_client,
    get_scope_ticket as status_get_scope_ticket,
    get_ticket_store as status_get_ticket_store,
    router as status_router,
)
from broker.endpoints.upload_logs import (
    get_artifact_bucket as upload_get_artifact_bucket,
    get_s3_client as upload_get_s3_client,
    get_scope_ticket as upload_get_scope_ticket,
    router as upload_router,
)
from broker.endpoints.experiment_manifest import (
    get_experiment_metadata_bucket as exp_get_experiment_metadata_bucket,
    get_s3_client as exp_get_s3_client,
    get_scope_ticket as exp_get_scope_ticket,
    router as experiment_manifest_router,
)
from broker.secrets import load_secrets_from_env

_LOCAL_ENVS = ("local", "development", "test")


def _resolve_table_name() -> str:
    """Resolve DynamoDB table name at app startup. Deferred to runtime (not
    module import) so tests that import broker.main without env vars set
    don't crash at collection. Fails closed for AWS envs if not injected."""
    name = os.environ.get("DYNAMO_TABLE_NAME")
    if name:
        return name
    env = os.environ.get("ENVIRONMENT", "").strip().lower()
    if env in _LOCAL_ENVS:
        return "broker-scope-tickets-local"
    raise RuntimeError(
        f"DYNAMO_TABLE_NAME is required in environment={env!r}. "
        "ECS task def should inject it (see infrastructure/modules/broker/main.tf). "
        "For local dev, set ENVIRONMENT=local to use the broker-scope-tickets-local default."
    )


def _resolve_scope_ticket(broker_auth):
    from broker.auth import get_broker_token as _extract_token

    def _resolver(request):
        token = request.headers.get("x-broker-token", "")
        return broker_auth.verify(token)

    return _resolver


@asynccontextmanager
async def lifespan(app: FastAPI):
    secrets = load_secrets_from_env()
    store = ScopeTicketStore(table_name=_resolve_table_name())
    broker_auth = BrokerTokenAuth(store=store)
    upstream_client = httpx.AsyncClient(base_url="https://api.anthropic.com", timeout=300)
    s3_client = boto3.client("s3")
    sqs_client = boto3.client("sqs")
    # Shared async client used by anthropic_proxy and agent_mcp_proxy. The
    # unified /http/fetch endpoint routes through PlaywrightBrowserFetcher
    # below — plain httpx is 403'd by Cloudflare's JS challenge on muni sites.
    http_client = httpx.AsyncClient(timeout=30)
    browser_fetcher = PlaywrightBrowserFetcher()
    await browser_fetcher.start()
    callback_sender = CallbackSender(sqs_client=sqs_client, queue_url=secrets.results_queue_url)
    # Per-ticket counter feeding the artifact_publish anti-fabrication gate.
    # Process-local; broker restart mid-run rejects publish (strictly safer
    # than accepting a synthetic artifact from an agent whose data calls
    # all failed).
    data_query_tracker = DataQueryTracker()
    clerk_client = ClerkClient(
        secret_key=secrets.clerk_secret_key,
        frontend_api_base=secrets.clerk_frontend_api_base,
        agent_fleet_clerk_id=secrets.agent_fleet_clerk_id,
    )
    artifact_bucket = os.environ.get("ARTIFACT_BUCKET", "gp-agent-artifacts-dev")
    env = os.environ.get("ENVIRONMENT", "dev").strip().lower()
    experiment_metadata_bucket = os.environ.get(
        "EXPERIMENT_METADATA_BUCKET",
        f"agent-experiment-metadata-{env}",
    )

    from fastapi import Request
    from broker.auth import AuthError

    def _resolve_ticket_from_request(request: Request) -> ScopeTicket:
        token = request.headers.get("x-broker-token", "")
        if not token:
            raise AuthError("missing_broker_token")
        return broker_auth.verify(token)

    def _resolve_broker_token_raw(request: Request) -> str:
        return request.headers.get("x-broker-token", "")

    app.dependency_overrides[get_ticket_store] = lambda: store
    app.dependency_overrides[get_service_token_hash] = lambda: secrets.service_token_hash
    app.dependency_overrides[get_clerk_client] = lambda: clerk_client
    app.dependency_overrides[delete_get_ticket_store] = lambda: store
    app.dependency_overrides[delete_get_service_token_hash] = lambda: secrets.service_token_hash
    app.dependency_overrides[get_broker_auth] = lambda: broker_auth
    app.dependency_overrides[get_upstream_client] = lambda: upstream_client
    app.dependency_overrides[get_anthropic_api_key] = lambda: secrets.anthropic_api_key

    app.dependency_overrides[publish_get_scope_ticket] = _resolve_ticket_from_request
    app.dependency_overrides[publish_get_s3_client] = lambda: s3_client
    app.dependency_overrides[publish_get_callback_sender] = lambda: callback_sender
    app.dependency_overrides[publish_get_ticket_store] = lambda: store
    app.dependency_overrides[publish_get_broker_token_raw] = _resolve_broker_token_raw
    app.dependency_overrides[publish_get_artifact_bucket] = lambda: artifact_bucket
    app.dependency_overrides[publish_get_data_query_tracker] = lambda: data_query_tracker

    app.dependency_overrides[read_get_scope_ticket] = _resolve_ticket_from_request
    app.dependency_overrides[read_get_s3_client] = lambda: s3_client
    app.dependency_overrides[read_get_artifact_bucket] = lambda: artifact_bucket

    app.dependency_overrides[status_get_scope_ticket] = _resolve_ticket_from_request
    app.dependency_overrides[status_get_s3_client] = lambda: s3_client
    app.dependency_overrides[status_get_callback_sender] = lambda: callback_sender
    app.dependency_overrides[status_get_ticket_store] = lambda: store
    app.dependency_overrides[status_get_broker_token_raw] = _resolve_broker_token_raw
    app.dependency_overrides[status_get_artifact_bucket] = lambda: artifact_bucket
    app.dependency_overrides[status_get_data_query_tracker] = lambda: data_query_tracker

    app.dependency_overrides[dbx_get_scope_ticket] = _resolve_ticket_from_request
    from broker.endpoints.databricks_query import DatabricksClient
    dbx_client = DatabricksClient(
        server_hostname=secrets.databricks_server_hostname,
        http_path=secrets.databricks_http_path,
        access_token=secrets.databricks_api_key,
    ) if secrets.databricks_server_hostname else None
    app.dependency_overrides[dbx_get_databricks_client] = lambda: dbx_client
    app.dependency_overrides[dbx_get_data_query_tracker] = lambda: data_query_tracker

    app.dependency_overrides[upload_get_scope_ticket] = _resolve_ticket_from_request
    app.dependency_overrides[upload_get_s3_client] = lambda: s3_client
    app.dependency_overrides[upload_get_artifact_bucket] = lambda: artifact_bucket

    app.dependency_overrides[exp_get_scope_ticket] = _resolve_ticket_from_request
    app.dependency_overrides[exp_get_s3_client] = lambda: s3_client
    app.dependency_overrides[exp_get_experiment_metadata_bucket] = lambda: experiment_metadata_bucket

    app.dependency_overrides[http_get_scope_ticket] = _resolve_ticket_from_request
    app.dependency_overrides[http_get_browser_fetcher] = lambda: browser_fetcher

    app.dependency_overrides[agent_mcp_get_scope_ticket] = _resolve_ticket_from_request
    app.dependency_overrides[agent_mcp_get_clerk_client] = lambda: clerk_client
    app.dependency_overrides[agent_mcp_get_gp_api_base_url] = lambda: secrets.gp_api_base_url
    app.dependency_overrides[agent_mcp_get_http_client] = lambda: http_client

    try:
        yield
    finally:
        await upstream_client.aclose()
        await http_client.aclose()
        await clerk_client.aclose()
        await browser_fetcher.aclose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError):
    return JSONResponse(
        status_code=401,
        content={"detail": exc.reason_code},
    )


app.include_router(mint_router)
app.include_router(delete_router)
app.include_router(anthropic_router)
app.include_router(publish_router)
app.include_router(read_router)
app.include_router(status_router)
app.include_router(databricks_router)
app.include_router(upload_router)
app.include_router(http_router)
app.include_router(experiment_manifest_router)
app.include_router(agent_mcp_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
