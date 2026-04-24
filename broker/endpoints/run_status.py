import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from broker.auth import get_broker_token
from broker.callback_sender import CallbackSender
from broker.data_query_tracker import DataQueryTracker
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

# Agent may report any of these via /run-status. `success` is NOT in this set
# — success must only flow through /artifact/publish so the data-required
# guard (DataQueryTracker) can't be bypassed. `timeout` is accepted here but
# translated to `failed` before the callback is sent, because gp-api's zod
# consumer only accepts `success | failed | contract_violation`.
AgentReportableStatus = Literal[
    "failed", "contract_violation", "timeout"
]

# Statuses the broker forwards to gp-api on the callback wire. Must stay in
# sync with gp-api/src/queue/queue.types.ts (zod enum).
TERMINAL_STATUSES = {"failed", "contract_violation"}


class RunStatusRequest(BaseModel):
    status: AgentReportableStatus
    reason_code: str | None = None
    detail: str | None = None
    duration_seconds: float | None = None
    cost_usd: float | None = None
    rejected_artifact: dict | None = None


class RunStatusResponse(BaseModel):
    callback_sent: bool


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError

def get_s3_client():  # pragma: no cover
    raise NotImplementedError

def get_callback_sender() -> CallbackSender:  # pragma: no cover
    raise NotImplementedError

def get_ticket_store() -> ScopeTicketStore:  # pragma: no cover
    raise NotImplementedError

def get_broker_token_raw() -> str:  # pragma: no cover
    raise NotImplementedError

def get_artifact_bucket() -> str:  # pragma: no cover
    raise NotImplementedError

def get_data_query_tracker() -> DataQueryTracker:  # pragma: no cover
    raise NotImplementedError


@router.post("/run-status", response_model=RunStatusResponse)
def run_status(
    req: RunStatusRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    s3_client=Depends(get_s3_client),
    callback_sender: CallbackSender = Depends(get_callback_sender),
    store: ScopeTicketStore = Depends(get_ticket_store),
    broker_token: str = Depends(get_broker_token_raw),
    bucket: str = Depends(get_artifact_bucket),
    tracker: DataQueryTracker = Depends(get_data_query_tracker),
):
    if req.status == "contract_violation" and req.rejected_artifact:
        quarantine_key = f"rejected/{ticket.run_id}.json"
        try:
            # IfNoneMatch="*" makes the forensic record write-once at the S3
            # layer. A retry for the same run_id must preserve the first
            # rejected artifact — silently overwriting would erase the
            # evidence of the original contract violation.
            s3_client.put_object(
                Bucket=bucket,
                Key=quarantine_key,
                Body=json.dumps(req.rejected_artifact),
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except Exception as quarantine_err:
            error_code = ""
            try:
                error_code = quarantine_err.response["Error"]["Code"]  # type: ignore[attr-defined]
            except (AttributeError, KeyError, TypeError):
                pass
            if error_code in ("PreconditionFailed", "412"):
                logger.warning(
                    "quarantine already exists for run_id=%s — preserving "
                    "first forensic record", ticket.run_id,
                )
            else:
                logger.error(
                    "quarantine S3 write failed run_id=%s key=%s",
                    ticket.run_id, quarantine_key,
                    exc_info=True,
                )

    # Translate `timeout` → `failed`+reason_code so gp-api's zod consumer
    # accepts the callback (zod enum has no `timeout`).
    if req.status == "timeout":
        wire_status = "failed"
        wire_reason_code = "timeout"
    else:
        wire_status = req.status
        wire_reason_code = req.reason_code or ""

    callback_sender.send_result(
        run_id=ticket.run_id,
        organization_slug=ticket.organization_slug,
        experiment_id=ticket.experiment_id,
        status=wire_status,
        reason_code=wire_reason_code,
        detail=req.detail or "",
        duration_seconds=req.duration_seconds or 0,
        cost_usd=req.cost_usd or 0,
    )

    # Delete ticket + run-lock on any terminal state (failed/contract_violation/timeout —
    # the agent is done regardless of what wire_status we emitted). Cleaning up
    # the run-lock here is load-bearing: without it, a legitimate re-dispatch of
    # the same run_id 409s against the stale lock until TTL expires.
    if req.status in ("failed", "contract_violation", "timeout"):
        try:
            store.delete_ticket_and_run_lock(broker_token, ticket.run_id)
        except Exception:
            logger.error(
                "ticket/run-lock delete failed after terminal run_status run_id=%s broker_token_prefix=%s",
                ticket.run_id, broker_token[:8],
                exc_info=True,
            )
        try:
            tracker.clear(ticket.pk)
        except Exception:
            logger.error(
                "tracker clear failed after terminal run_status run_id=%s",
                ticket.run_id,
                exc_info=True,
            )

    return RunStatusResponse(callback_sent=True)
