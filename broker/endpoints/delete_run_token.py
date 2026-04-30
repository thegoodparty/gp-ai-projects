from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from broker.auth import get_service_token, verify_service_token
from broker.dynamodb_client import ScopeTicketStore
from broker.endpoints.mint_run_token import IDENTIFIER_PATTERN

router = APIRouter(prefix="/internal", tags=["internal"])


class DeleteRequest(BaseModel):
    broker_token: str
    run_id: str = Field(..., pattern=IDENTIFIER_PATTERN)


def get_ticket_store():
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


def get_service_token_hash():
    raise NotImplementedError("must be overridden via dependency_overrides")  # pragma: no cover


@router.post("/delete-run-token", status_code=204)
async def delete_run_token(
    request: DeleteRequest,
    service_token: str = Depends(get_service_token),
    token_hash: str = Depends(get_service_token_hash),
    store: ScopeTicketStore = Depends(get_ticket_store),
):
    if not verify_service_token(service_token, token_hash):
        raise HTTPException(status_code=401, detail="Invalid service token")

    store.delete_ticket_and_run_lock(request.broker_token, request.run_id)
    return Response(status_code=204)
