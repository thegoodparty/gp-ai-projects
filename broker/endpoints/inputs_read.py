import logging

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from broker.dynamodb_client import ScopeTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inputs", tags=["inputs"])

# Mirrors gp-api's user-agenda MAX_UPLOAD_BYTES (75 MB). Defense-in-depth:
# anything bigger than the upload-time cap shouldn't exist in the input bucket,
# but rejecting here keeps a misconfigured upstream from OOM-ing the runner.
MAX_INPUT_BYTES = 75 * 1024 * 1024


class InputsReadRequest(BaseModel):
    bucket: str = Field(..., min_length=1, max_length=255)
    key: str = Field(..., min_length=1, max_length=1024)


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_s3_client():  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/read")
def inputs_read(
    req: InputsReadRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    s3_client=Depends(get_s3_client),
):
    if not ticket.input_files:
        raise HTTPException(
            status_code=403,
            detail="No input files authorized for this run",
        )
    if not any(
        f.bucket == req.bucket and f.key == req.key for f in ticket.input_files
    ):
        raise HTTPException(
            status_code=403,
            detail="Input file not authorized for this run",
        )

    try:
        s3_response = s3_client.get_object(Bucket=req.bucket, Key=req.key)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            raise HTTPException(
                status_code=404,
                detail=f"Input file not found: s3://{req.bucket}/{req.key}",
            )
        logger.error(
            "S3 inputs_read failed run_id=%s bucket=%r key=%r code=%s",
            ticket.run_id,
            req.bucket,
            req.key,
            error_code,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="S3 read error")

    content_length = s3_response.get("ContentLength")
    if content_length is not None and content_length > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Input file exceeds {MAX_INPUT_BYTES}-byte cap "
                f"(size={content_length})"
            ),
        )

    # Bound the in-memory load even when the ContentLength header is missing
    # — boto3's StreamingBody.read(amt) reads at most amt bytes. We read
    # MAX_INPUT_BYTES + 1 so that any object exceeding the cap by even one
    # byte produces a body longer than the cap and we can reject without
    # loading the rest. Defends against an OOM via an oversized object that
    # somehow slipped past the upload-time cap and lacks Content-Length.
    body = s3_response["Body"].read(MAX_INPUT_BYTES + 1)
    if len(body) > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Input file exceeds {MAX_INPUT_BYTES}-byte cap "
                f"(size>={len(body)})"
            ),
        )

    logger.info(
        "inputs_read ok run_id=%s bucket=%r key=%r bytes=%d",
        ticket.run_id,
        req.bucket,
        req.key,
        len(body),
    )
    return Response(content=body, media_type="application/octet-stream")
