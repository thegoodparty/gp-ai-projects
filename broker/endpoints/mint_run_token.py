import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from broker.auth import get_service_token, verify_service_token
from broker.dynamodb_client import (
    ScopeTicket,
    ScopeTicketStore,
    TicketAlreadyExistsError,
)

router = APIRouter(prefix="/internal", tags=["internal"])

MAX_TTL_SECONDS = 14400
# The ticket must outlive the experiment's timeout so the agent's final
# publish/report_status calls don't get 401'd mid-stride (which leaves the
# DB row stuck RUNNING forever). Buffer covers validation + upload + callback.
TTL_BUFFER_SECONDS = 300


IDENTIFIER_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"


class MintRequest(BaseModel):
    run_id: str = Field(..., pattern=IDENTIFIER_PATTERN)
    organization_slug: str = Field(..., pattern=IDENTIFIER_PATTERN)
    experiment_id: str = Field(..., pattern=IDENTIFIER_PATTERN)
    scope: dict
    params: dict
    exp_ttl_seconds: int = 3600
    # Optional — when provided, mint floors exp_ttl_seconds at
    # timeout_seconds + TTL_BUFFER_SECONDS so ticket survives the whole run.
    timeout_seconds: int | None = None
    # Optional — map of dependency experiment_id -> pinned S3 artifact key.
    # When set, artifact_read enforces that dependents read the exact snapshot
    # dispatched against, preserving the STALE invariant for
    # peer_city_benchmarking / meeting_briefing.
    prior_artifact_versions: dict[str, str] | None = None


class MintResponse(BaseModel):
    broker_token: str
    exp: int
    params_clean: dict


def get_ticket_store():
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


def get_service_token_hash():
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


@router.post("/mint-run-token", response_model=MintResponse)
async def mint_run_token(
    request: MintRequest,
    service_token: str = Depends(get_service_token),
    token_hash: str = Depends(get_service_token_hash),
    store: ScopeTicketStore = Depends(get_ticket_store),
):
    if not verify_service_token(service_token, token_hash):
        raise HTTPException(status_code=401, detail="Invalid service token")

    broker_token = str(uuid.uuid4())
    now = int(time.time())

    if request.exp_ttl_seconds > MAX_TTL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"exp_ttl_seconds={request.exp_ttl_seconds} exceeds "
                f"MAX_TTL_SECONDS ({MAX_TTL_SECONDS}s); silent clamping would "
                "let an agent 401 mid-run and leave the row stuck RUNNING"
            ),
        )

    effective_ttl = request.exp_ttl_seconds
    if request.timeout_seconds is not None:
        required_ttl = request.timeout_seconds + TTL_BUFFER_SECONDS
        if required_ttl > MAX_TTL_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"timeout_seconds={request.timeout_seconds} + buffer "
                    f"({TTL_BUFFER_SECONDS}s) exceeds MAX_TTL_SECONDS "
                    f"({MAX_TTL_SECONDS}s); this experiment is misconfigured"
                ),
            )
        effective_ttl = max(effective_ttl, required_ttl)

    exp = now + effective_ttl

    ticket = ScopeTicket(
        pk=broker_token,
        run_id=request.run_id,
        organization_slug=request.organization_slug,
        experiment_id=request.experiment_id,
        scope=request.scope,
        params=request.params,
        exp=exp,
        issued_at=now,
        issued_by="dispatch_lambda",
        prior_artifact_versions=request.prior_artifact_versions,
    )

    try:
        store.put_ticket(ticket)
    except TicketAlreadyExistsError:
        raise HTTPException(status_code=409, detail="Ticket already exists")

    return MintResponse(
        broker_token=broker_token,
        exp=exp,
        params_clean=request.params,
    )
