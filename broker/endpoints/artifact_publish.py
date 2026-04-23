import json
import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from broker.auth import get_broker_token
from broker.callback_sender import CallbackSender
from broker.data_query_tracker import DataQueryTracker
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore
from broker.pii_scanner import scan_artifact

DATA_REQUIRED_EXPERIMENTS = {"voter_targeting", "walking_plan"}

_PII_ENABLED_VALUES = {"1", "true", "yes"}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifact", tags=["artifact"])

_DANGEROUS_HTML_RE = re.compile(
    r"<script|<img\b|javascript:", re.IGNORECASE
)

# The downstream agent's sanitizer.fence_content wraps artifact text in
# <untrusted_web_content>...</untrusted_web_content> so the reading agent
# treats it as data, not instructions. An upstream agent embedding either
# tag in its artifact can break out of the fence and inject "system" text
# into a downstream experiment (e.g., peer_city_benchmarking reading
# district_intel). _DANGEROUS_HTML_RE doesn't cover this, so reject
# explicitly.
_FENCE_BREAKOUT_RE = re.compile(r"</?untrusted_web_content\b", re.IGNORECASE)


class PublishRequest(BaseModel):
    artifact: dict


class PublishResponse(BaseModel):
    artifact_key: str
    artifact_bucket: str
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


def _collect_strings(obj, path: str = "") -> list[tuple[str, str]]:
    results = []
    if isinstance(obj, str):
        results.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(_collect_strings(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(_collect_strings(item, f"{path}[{i}]"))
    return results


def _check_html(artifact: dict) -> str | None:
    for field_path, value in _collect_strings(artifact):
        if _DANGEROUS_HTML_RE.search(value):
            return f"Raw HTML detected in field '{field_path}'"
    return None


def _check_fence_breakout(artifact: dict) -> str | None:
    for field_path, value in _collect_strings(artifact):
        if _FENCE_BREAKOUT_RE.search(value):
            return (
                f"fence-breakout token detected in field '{field_path}': "
                "artifacts cannot contain <untrusted_web_content> tags "
                "(would escape the downstream agent's data fence)"
            )
    return None


@router.post("/publish", response_model=PublishResponse)
def artifact_publish(
    req: PublishRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    s3_client=Depends(get_s3_client),
    callback_sender: CallbackSender = Depends(get_callback_sender),
    store: ScopeTicketStore = Depends(get_ticket_store),
    broker_token: str = Depends(get_broker_token_raw),
    bucket: str = Depends(get_artifact_bucket),
    tracker: DataQueryTracker = Depends(get_data_query_tracker),
):
    if ticket.experiment_id in DATA_REQUIRED_EXPERIMENTS and tracker.get(ticket.pk) == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"NoDataQueriesSucceeded: experiment '{ticket.experiment_id}' requires "
                "real voter data but no Databricks query succeeded during this run. "
                "Refusing to publish — this prevents synthetic/fabricated artifacts "
                "from being accepted when data sources are unreachable."
            ),
        )

    if os.environ.get("ENABLE_PII_SCANNER", "").strip().lower() in _PII_ENABLED_VALUES:
        pii_matches = scan_artifact(req.artifact)
        if pii_matches:
            fields = ", ".join(m.field_path or "unknown" for m in pii_matches)
            raise HTTPException(status_code=400, detail=f"PII detected in artifact fields: {fields}")

    html_error = _check_html(req.artifact)
    if html_error:
        raise HTTPException(status_code=400, detail=f"Raw HTML not allowed: {html_error}")

    fence_error = _check_fence_breakout(req.artifact)
    if fence_error:
        raise HTTPException(status_code=400, detail=fence_error)

    artifact_json = json.dumps(req.artifact)
    latest_key = f"{ticket.experiment_id}/{ticket.organization_slug}/latest.json"
    run_key = f"{ticket.experiment_id}/{ticket.run_id}/artifact.json"

    try:
        # Write the immutable per-run archive FIRST so the mutable latest.json
        # pointer can never outrun it. If the archive put fails, latest.json is
        # not yet updated, keeping S3 internally consistent.
        # IfNoneMatch=* makes the archive write-once at the S3 layer — a
        # second publish for the same run_id (e.g., from a leaked broker_token
        # bypassing post-publish ticket-delete) will 412 instead of silently
        # overwriting the immutable record peer_city_benchmarking depends on.
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=run_key,
                Body=artifact_json,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except Exception as run_err:
            error_code = ""
            try:
                error_code = run_err.response["Error"]["Code"]  # type: ignore[attr-defined]
            except (AttributeError, KeyError, TypeError):
                pass
            if error_code in ("PreconditionFailed", "412"):
                logger.warning(
                    "duplicate publish blocked for run_id=%s experiment_id=%s "
                    "(archive already exists)",
                    ticket.run_id, ticket.experiment_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Artifact for run {ticket.run_id} was already published; "
                        "duplicate publish refused (archive is immutable)"
                    ),
                )
            raise
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=latest_key,
                Body=artifact_json,
                ContentType="application/json",
            )
        except Exception as latest_err:
            logger.warning(
                "latest.json update failed run_id=%s experiment_id=%s key=%s bucket=%s: %s. "
                "Archive write succeeded; callback carries run-scoped key. latest.json is "
                "a best-effort convenience pointer and is eventually consistent.",
                ticket.run_id, ticket.experiment_id, latest_key, bucket, latest_err,
                exc_info=True,
            )
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "S3 publish failed run_id=%s experiment_id=%s bucket=%s",
            ticket.run_id, ticket.experiment_id, bucket,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to publish artifact to S3")

    # Callback carries the run-scoped immutable key. If we pointed gp-api at
    # latest.json, a subsequent regeneration of this (or dependent) experiment
    # would silently change what a SUCCESS run "produced", breaking the STALE
    # invariant for peer_city_benchmarking and any other dependent experiment.
    callback_sender.send_result(
        run_id=ticket.run_id,
        organization_slug=ticket.organization_slug,
        experiment_id=ticket.experiment_id,
        status="success",
        artifact_key=run_key,
        artifact_bucket=bucket,
    )

    try:
        store.delete_ticket_and_run_lock(broker_token, ticket.run_id)
    except Exception:
        logger.error(
            "ticket/run-lock delete failed after publish run_id=%s broker_token_prefix=%s",
            ticket.run_id, broker_token[:8],
            exc_info=True,
        )
    try:
        tracker.clear(ticket.pk)
    except Exception:
        logger.error(
            "tracker clear failed after publish run_id=%s",
            ticket.run_id,
            exc_info=True,
        )

    return PublishResponse(
        artifact_key=run_key,
        artifact_bucket=bucket,
        callback_sent=True,
    )
