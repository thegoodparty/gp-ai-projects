import json
import logging

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from broker.dynamodb_client import ScopeTicket
from broker.sanitizer import fence_content

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifact", tags=["artifact"])

IDENTIFIER_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"


class ArtifactReadRequest(BaseModel):
    experiment_id: str = Field(..., pattern=IDENTIFIER_PATTERN)
    # Optional pinned run-scoped key. When set, must match the entry in the
    # ticket's `prior_artifact_versions` for the same experiment_id — otherwise
    # 403. This preserves the STALE invariant: a dependent experiment reads
    # the exact snapshot it was dispatched against, not whatever latest.json
    # points at when the dependent gets around to reading.
    artifact_key: str | None = None
    # Legacy knob, unused in the new flow; kept for compat with older callers.
    latest: bool = True


class ArtifactReadResponse(BaseModel):
    content: str
    artifact: dict


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_s3_client():  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_artifact_bucket() -> str:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/read", response_model=ArtifactReadResponse)
def artifact_read(
    req: ArtifactReadRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    s3_client=Depends(get_s3_client),
    bucket: str = Depends(get_artifact_bucket),
):
    prior_versions = ticket.prior_artifact_versions or {}
    pinned = prior_versions.get(req.experiment_id)

    # Close the legacy-fallback gap: a ticket for experiment A may only read
    # artifacts for experiment A (self-read) or for an experiment declared in
    # its prior_artifact_versions. Otherwise a compromised agent could
    # enumerate arbitrary experiment artifacts via the legacy latest.json path.
    if req.experiment_id != ticket.experiment_id and req.experiment_id not in prior_versions:
        raise HTTPException(status_code=403, detail="Artifact access denied")

    if req.artifact_key is not None:
        # Caller explicitly requested a specific snapshot — must match what
        # dispatch pinned for this experiment. Reject if there's no pin, or
        # the pin doesn't match.
        if pinned is None or pinned != req.artifact_key:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"artifact_key '{req.artifact_key}' is not authorized for this "
                    f"ticket (pinned: {pinned or 'none'}) — dependent experiments "
                    "must read the snapshot pinned at dispatch time"
                ),
            )
        s3_key = pinned
    elif pinned is not None:
        # No key requested but a pin exists — read the pinned snapshot by
        # default so dependents get deterministic behavior.
        s3_key = pinned
    else:
        # Legacy fallback — no pin, no explicit key. Serve latest.json
        # (transition path until every dispatch populates prior_artifact_versions).
        s3_key = f"{req.experiment_id}/{ticket.organization_slug}/latest.json"

    try:
        s3_response = s3_client.get_object(Bucket=bucket, Key=s3_key)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            raise HTTPException(status_code=404, detail=f"Artifact not found: {s3_key}")
        logger.error(
            "S3 artifact_read failed run_id=%s experiment_id=%s key=%s bucket=%s code=%s",
            ticket.run_id,
            req.experiment_id,
            s3_key,
            bucket,
            error_code,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="S3 read error")

    raw_bytes = s3_response["Body"].read()
    try:
        artifact = json.loads(raw_bytes)
    except (json.JSONDecodeError, ValueError):
        logger.error(
            "Corrupt artifact body run_id=%s experiment_id=%s key=%s bucket=%s",
            ticket.run_id,
            req.experiment_id,
            s3_key,
            bucket,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Artifact decode error")

    content_str = json.dumps(artifact)

    fenced = fence_content(content_str, source=s3_key)

    return ArtifactReadResponse(content=fenced, artifact=artifact)
