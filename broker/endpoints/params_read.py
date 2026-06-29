import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from broker.dynamodb_client import ScopeTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/params", tags=["params"])


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.get("/read")
def params_read(ticket: ScopeTicket = Depends(get_scope_ticket)):
    """Return this run's params from its scope ticket.

    Params are minted into the ticket at dispatch (mint-run-token) and held in
    DynamoDB, TTL'd and keyed by this run's broker token. The runner calls this
    only when the dispatch routed params off the ECS env var (it sets
    PARAMS_VIA_BROKER when the serialized params exceed the containerOverrides
    budget); small params still ride PARAMS_JSON inline and never hit this path.
    Authorization is the broker token itself, resolved into `ticket` by the app
    dependency, so a run can only ever read its own params.
    """
    logger.info("params_read ok run_id=%s keys=%d", ticket.run_id, len(ticket.params))
    return JSONResponse(content=ticket.params)
