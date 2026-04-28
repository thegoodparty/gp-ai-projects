import logging
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from broker.dynamodb_client import ScopeTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

_FILENAME_ALLOWLIST = re.compile(r"[A-Za-z0-9_.\-/]{1,200}")

# No broker-side per-file cap. The runner's `_collect_workspace_files` already
# caps at 50MB per file / 200MB total, and each upload is authenticated via a
# short-lived scope ticket tied to a specific run we launched. Double-capping
# at the broker just created silent drops for large artifacts (municipal
# meeting packets are routinely 40-60 MB).


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_s3_client():  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_artifact_bucket() -> str:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/upload-logs")
def upload_logs(
    files: list[UploadFile] = File(...),
    ticket: ScopeTicket = Depends(get_scope_ticket),
    s3_client=Depends(get_s3_client),
    bucket: str = Depends(get_artifact_bucket),
):
    uploaded: list[str] = []

    for f in files:
        filename = f.filename or ""
        if (
            not filename
            or not _FILENAME_ALLOWLIST.fullmatch(filename)
            or ".." in filename
            or filename.startswith("/")
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filename: {filename!r}",
            )

        data = f.file.read()

        key = f"{ticket.experiment_id}/{ticket.run_id}/logs/{filename}"
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
            )
        except Exception:
            logger.error(
                "S3 upload-logs failed run_id=%s key=%s bucket=%s",
                ticket.run_id, key, bucket,
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail="Failed to upload logs to S3")

        uploaded.append(key)

    return {"uploaded": uploaded}
