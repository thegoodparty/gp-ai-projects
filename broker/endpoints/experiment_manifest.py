"""Serve PMF experiment manifests + instructions to the quarantined runner.

The Fargate runner cannot reach S3 directly (egress-only-to-broker security
group). This endpoint is the runner's window into the metadata bucket. The
ticket's experiment_id is the only experiment a given run is allowed to see.
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from broker.dynamodb_client import ScopeTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/experiment", tags=["experiment"])

EXPERIMENT_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
S3_VERSION_ID_PATTERN = r"^[A-Za-z0-9._\-]{1,1024}$"

_OBJECT_CACHE: dict[tuple[str, str, str], tuple[bytes, str | None]] = {}
_OBJECT_CACHE_MAX = 512

_INDEX_CACHE: dict[str, tuple[dict, float]] = {}
_INDEX_TTL = 60.0


def _emit_metric(metric_name: str, dimensions: list[dict]) -> None:
    """Emit a CloudWatch metric. Swallows all exceptions — metric emission must
    never fail the calling code. Broker has a no-cross-package-deps rule
    (see broker/CLAUDE.md), so this is local instead of using shared.metrics.
    """
    try:
        cw = boto3.client("cloudwatch")
        cw.put_metric_data(
            Namespace="Broker",
            MetricData=[{
                "MetricName": metric_name,
                "Value": 1,
                "Unit": "Count",
                "Dimensions": dimensions,
            }],
        )
    except Exception as e:
        logger.warning(
            "MetricEmissionFailed metric=%s exc_type=%s: %s",
            metric_name, type(e).__name__, e, exc_info=True,
        )


class ExperimentManifestRequest(BaseModel):
    experiment_id: str = Field(..., pattern=EXPERIMENT_ID_PATTERN)
    # Pin to specific S3 object versions (captured by the dispatch Lambda at
    # routing time). Closes the publish-during-run race window: every Fargate
    # task reads the exact bytes Lambda saw, no matter how long it takes to
    # start. Unset = "latest" — only safe in dev/local where determinism
    # doesn't matter.
    manifest_version_id: str | None = Field(None, pattern=S3_VERSION_ID_PATTERN)
    instruction_version_id: str | None = Field(None, pattern=S3_VERSION_ID_PATTERN)


class ExperimentManifestResponse(BaseModel):
    manifest: dict
    instruction: str
    # Surfaced for audit logging. The runner records what version IDs it
    # actually got so a future operator can re-fetch the same bytes by
    # VersionId weeks later (until S3 lifecycle expires noncurrent versions).
    resolved_manifest_version_id: str | None = None
    resolved_instruction_version_id: str | None = None


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_s3_client():  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_experiment_metadata_bucket() -> str:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def _fetch_object(
    s3_client,
    bucket: str,
    key: str,
    ticket_run_id: str,
    version_id: str | None = None,
) -> tuple[bytes, str | None]:
    """Returns (body_bytes, resolved_version_id)."""
    kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    try:
        response = s3_client.get_object(**kwargs)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchVersion", "404"):
            detail = (
                f"manifest object not found: {key}"
                + (f" (version {version_id})" if version_id else "")
            )
            raise HTTPException(status_code=404, detail=detail)
        logger.error(
            "S3 manifest fetch failed run_id=%s key=%s bucket=%s version_id=%s code=%s",
            ticket_run_id, key, bucket, version_id, code, exc_info=True,
        )
        _emit_metric("broker_s3_manifest_fetch_failure", [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "error_code", "Value": code or "unknown"},
        ])
        raise HTTPException(status_code=500, detail="manifest store unavailable")
    return response["Body"].read(), response.get("VersionId")


def _fetch_object_cached(
    s3_client,
    bucket: str,
    key: str,
    ticket_run_id: str,
    version_id: str | None,
) -> tuple[bytes, str | None]:
    """Cache wrapper around _fetch_object.

    Cache by (bucket, key, version_id) — when version_id is pinned, S3 bytes
    are immutable, so the cache hit is correctness-safe. Skip cache when
    version_id is None (dev/local "latest"), since latest can change under us.
    """
    if version_id is not None:
        cached = _OBJECT_CACHE.get((bucket, key, version_id))
        if cached is not None:
            return cached
    body, resolved_version = _fetch_object(s3_client, bucket, key, ticket_run_id, version_id)
    if version_id is not None:
        if len(_OBJECT_CACHE) >= _OBJECT_CACHE_MAX:
            _OBJECT_CACHE.clear()
        _OBJECT_CACHE[(bucket, key, version_id)] = (body, resolved_version)
    return body, resolved_version


def _fetch_index_json(s3_client, bucket: str) -> dict:
    """Fetch index.json with a 60s TTL cache. On fetch failure, return stale
    cached value if present, else an empty experiments list — empty causes the
    orphan check to deny all manifests, which is safer than allowing them.
    """
    now = time.monotonic()
    cached = _INDEX_CACHE.get(bucket)
    if cached and now - cached[1] < _INDEX_TTL:
        return cached[0]
    try:
        resp = s3_client.get_object(Bucket=bucket, Key="index.json")
        index = json.loads(resp["Body"].read())
    except Exception as e:
        logger.warning("index.json fetch failed bucket=%s exc=%s", bucket, e, exc_info=True)
        if cached:
            return cached[0]
        return {"experiments": []}
    _INDEX_CACHE[bucket] = (index, now)
    return index


def _reset_caches_for_test() -> None:
    """Clear module-level caches between tests. Call from a fixture."""
    _OBJECT_CACHE.clear()
    _INDEX_CACHE.clear()


@router.post("/manifest", response_model=ExperimentManifestResponse)
def experiment_manifest(
    req: ExperimentManifestRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    s3_client=Depends(get_s3_client),
    bucket: str = Depends(get_experiment_metadata_bucket),
):
    if req.experiment_id != ticket.experiment_id:
        # A run scoped to experiment A cannot peek at experiment B's manifest.
        # Defense in depth on top of the dispatch flow setting EXPERIMENT_ID
        # from the ticket — log + 403 if anything ever drifts. Security
        # boundary breach: ERROR + metric for SNS → Slack alerting.
        logger.error(
            "scope_violation_attempt errorType=cross_experiment_manifest_read "
            "run_id=%s ticket_experiment=%s requested=%s",
            ticket.run_id, ticket.experiment_id, req.experiment_id,
        )
        _emit_metric("broker_scope_violation_attempt", [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "endpoint", "Value": "experiment_manifest"},
        ])
        raise HTTPException(status_code=403, detail="manifest access denied for this run's scope")

    # Defense in depth: refuse orphan reads. When an experiment is removed from
    # runbooks, the per-experiment manifest stays in S3 forever. A scope ticket
    # minted before removal can still try to read it (TTL up to 4hr). Block
    # those by checking against the canonical index.json.
    index = _fetch_index_json(s3_client, bucket)
    experiments = index.get("experiments", []) if isinstance(index, dict) else []
    if not any(isinstance(e, dict) and e.get("id") == ticket.experiment_id for e in experiments):
        logger.error(
            "orphan_manifest_blocked experiment_id=%s run_id=%s",
            ticket.experiment_id, ticket.run_id,
        )
        _emit_metric("broker_orphan_manifest_blocked", [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "experiment_id", "Value": ticket.experiment_id},
        ])
        raise HTTPException(status_code=404, detail="experiment not currently registered")

    manifest_key = f"{req.experiment_id}/manifest.json"
    instruction_key = f"{req.experiment_id}/instruction.md"

    # Parallelize the two GETs. When both VersionIds are pinned the cached
    # path returns immediately so the ThreadPoolExecutor overhead is the only
    # cost; when unpinned/uncached we cut latency roughly in half.
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_m = ex.submit(
            _fetch_object_cached,
            s3_client, bucket, manifest_key, ticket.run_id, req.manifest_version_id,
        )
        fut_i = ex.submit(
            _fetch_object_cached,
            s3_client, bucket, instruction_key, ticket.run_id, req.instruction_version_id,
        )
        manifest_bytes, manifest_resolved_version = fut_m.result()
        instruction_bytes, instruction_resolved_version = fut_i.result()

    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, ValueError):
        logger.error(
            "manifest_decode_error errorType=manifest_decode "
            "experiment_id=%s run_id=%s key=%s bucket=%s version_id=%s",
            req.experiment_id, ticket.run_id, manifest_key, bucket, req.manifest_version_id,
            exc_info=True,
        )
        _emit_metric("broker_manifest_decode_error", [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "experiment_id", "Value": req.experiment_id},
        ])
        raise HTTPException(status_code=500, detail="manifest decode error")

    try:
        instruction_text = instruction_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.error(
            "instruction_decode_error errorType=instruction_decode "
            "experiment_id=%s run_id=%s key=%s version_id=%s",
            req.experiment_id, ticket.run_id, instruction_key, req.instruction_version_id,
            exc_info=True,
        )
        _emit_metric("broker_instruction_decode_error", [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "experiment_id", "Value": req.experiment_id},
        ])
        raise HTTPException(status_code=500, detail="instruction decode error")

    return ExperimentManifestResponse(
        manifest=manifest,
        instruction=instruction_text,
        resolved_manifest_version_id=manifest_resolved_version,
        resolved_instruction_version_id=instruction_resolved_version,
    )
