"""Serve PMF experiment manifests + instructions to the quarantined runner.

The Fargate runner cannot reach S3 directly (egress-only-to-broker security
group). This endpoint is the runner's window into the metadata bucket. The
ticket's experiment_id is the only experiment a given run is allowed to see.
"""

import json
import logging
import os
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from broker.dynamodb_client import ScopeTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/experiment", tags=["experiment"])

EXPERIMENT_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
S3_VERSION_ID_PATTERN = r"^[A-Za-z0-9._\-]{1,1024}$"

# DoS / resource-exhaustion caps. Picked so a publisher accident (uploading
# a PDF or an LLM dump) can't OOM the broker or saturate the fetch executor.
#   - MAX_ATTACHMENT_BYTES: per-object size cap. Real publisher attachments
#     today are <100 KB; 5 MiB gives plenty of headroom.
#   - MAX_ATTACHMENTS_PER_EXPERIMENT: count cap. A well-behaved experiment
#     ships 1-5 attachments; 32 covers the worst legitimate case and stops
#     a malformed index from spawning thousands of GETs.
#   - MAX_FETCH_WORKERS: shared thread pool bound. Replaces the old
#     `max_workers = 2 + len(attachment_specs)` pattern that grew per request.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS_PER_EXPERIMENT = 32
MAX_FETCH_WORKERS = 16

# Module-level shared executor. Bound to MAX_FETCH_WORKERS so concurrent
# requests share a fixed thread budget instead of each request spawning its
# own ThreadPoolExecutor (which sums to unbounded threads under load).
_FETCH_EXECUTOR: ThreadPoolExecutor | None = None


def _get_fetch_executor() -> ThreadPoolExecutor:
    """Lazy-init the shared fetch executor. Lazy so import-time doesn't
    spawn threads (cleaner for unit-test startup)."""
    global _FETCH_EXECUTOR
    if _FETCH_EXECUTOR is None:
        _FETCH_EXECUTOR = ThreadPoolExecutor(
            max_workers=MAX_FETCH_WORKERS,
            thread_name_prefix="broker-fetch",
        )
    return _FETCH_EXECUTOR


def _reset_fetch_executor_for_test() -> None:
    """Shut down + clear the shared executor so test isolation is clean."""
    global _FETCH_EXECUTOR
    if _FETCH_EXECUTOR is not None:
        _FETCH_EXECUTOR.shutdown(wait=False)
        _FETCH_EXECUTOR = None


class _LRUCache:
    """Bounded LRU cache for fetched S3 objects.

    Replaces the old flush-on-full pattern (`.clear()` when full) — that
    pattern evicted hot manifest/instruction entries every time a different
    experiment caused the cache to overflow, defeating the cache's purpose.

    Keys are (bucket, key, version_id). When the version_id is None
    (unpinned), the bytes-at-S3 can change under us — those entries are
    skipped entirely (see `_fetch_object_cached`). When a version_id is
    pinned, the bytes are immutable per S3's contract — TTL=None on `get`
    is correctness-safe.
    """

    def __init__(self, maxsize: int) -> None:
        self._d: OrderedDict[tuple[Any, ...], tuple[bytes, str | None, float]] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: tuple[Any, ...], ttl: float | None) -> tuple[bytes, str | None] | None:
        entry = self._d.get(key)
        if entry is None:
            return None
        body, vid, inserted_at = entry
        if ttl is not None and time.monotonic() - inserted_at > ttl:
            self._d.pop(key, None)
            return None
        self._d.move_to_end(key)
        return body, vid

    def put(self, key: tuple[Any, ...], body: bytes, vid: str | None) -> None:
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = (body, vid, time.monotonic())
        while len(self._d) > self._maxsize:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


_OBJECT_CACHE_MAX = 512
_OBJECT_CACHE = _LRUCache(_OBJECT_CACHE_MAX)

_INDEX_CACHE: dict[str, tuple[dict, float]] = {}
_INDEX_TTL = 60.0

# Process-cached CloudWatch client. boto3.client() does endpoint resolution +
# credential fetching + TLS setup (~100-300ms) — cheap once, expensive per
# call. Lazy-init at first metric emission so import-time has no AWS deps.
_cw_client = None


def _get_cw_client():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def _reset_cw_client_for_tests() -> None:
    global _cw_client
    _cw_client = None


def _emit_metric(metric_name: str, dimensions: list[dict]) -> None:
    """Emit a CloudWatch metric. Swallows all exceptions — metric emission must
    never fail the calling code. Broker has a no-cross-package-deps rule
    (see broker/CLAUDE.md), so this is local instead of using shared.metrics.
    """
    try:
        _get_cw_client().put_metric_data(
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


ATTACHMENT_BASENAME_PATTERN = r"^[A-Za-z0-9._\-]{1,255}$"
_ATTACHMENT_BASENAME_RE = re.compile(ATTACHMENT_BASENAME_PATTERN)


def _is_safe_attachment_basename(name: object) -> bool:
    """Single source of truth for "is this a safe attachment basename".

    Three places need the same answer: the request validator (where keys
    are checked against the basename pattern), the loop over the index
    entry's `attachment_keys` (where we derive a basename from an S3 key),
    and any future surface that maps user-supplied names into S3 keys.
    Defining the rule once eliminates drift.
    """
    if not isinstance(name, str):
        return False
    if name in (".", ".."):
        return False
    return bool(_ATTACHMENT_BASENAME_RE.fullmatch(name))


class ExperimentManifestRequest(BaseModel):
    experiment_id: str = Field(..., pattern=EXPERIMENT_ID_PATTERN)
    # Pin to specific S3 object versions (captured by the dispatch Lambda at
    # routing time). Closes the publish-during-run race window: every Fargate
    # task reads the exact bytes Lambda saw, no matter how long it takes to
    # start. Unset = "latest" — only safe in dev/local where determinism
    # doesn't matter.
    manifest_version_id: str | None = Field(None, pattern=S3_VERSION_ID_PATTERN)
    instruction_version_id: str | None = Field(None, pattern=S3_VERSION_ID_PATTERN)
    # Per-attachment VersionId pins, symmetric with manifest_version_id /
    # instruction_version_id. Keys are basenames (matching the response's
    # attachments dict); values are S3 VersionIds. Unset basenames fall
    # through to "latest" per the same rules as the other two fields.
    attachment_version_ids: dict[str, str] | None = None

    @field_validator("attachment_version_ids")
    @classmethod
    def _validate_attachment_version_ids(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        """Reject keys / values that don't match the basename / VersionId
        patterns. Defends the S3 key construction below: we map a basename
        to `<experiment_id>/attachments/<basename>`, and a key containing
        '..' or '/' would point at an unrelated S3 object.

        Uses `_is_safe_attachment_basename` for the key check so this
        validator stays in sync with the index-loop's basename guard.
        """
        if v is None:
            return v
        version_re = re.compile(S3_VERSION_ID_PATTERN)
        for name, version in v.items():
            if not _is_safe_attachment_basename(name):
                raise ValueError(f"attachment_version_ids key {name!r} is not a safe basename")
            # Pydantic's dict[str,str] coerces non-string values to strings
            # in lax mode; check the runtime type before the regex to keep
            # rejections explicit.
            if not isinstance(version, str) or not version_re.match(version):
                raise ValueError(f"attachment_version_ids value for {name!r} is not a valid S3 VersionId")
        return v


class ExperimentManifestResponse(BaseModel):
    manifest: dict
    instruction: str
    # Surfaced for audit logging. The runner records what version IDs it
    # actually got so a future operator can re-fetch the same bytes by
    # VersionId weeks later (until S3 lifecycle expires noncurrent versions).
    resolved_manifest_version_id: str | None = None
    resolved_instruction_version_id: str | None = None
    # Sidecar files published alongside the manifest. Keyed by basename
    # (e.g. "reference_catalog.md") so the runner can write each one as
    # /workspace/<basename> with no path translation. UTF-8 strings only —
    # if binary attachments are ever needed, add a parallel
    # `attachments_binary: dict[str, str]` (base64) rather than coercing.
    attachments: dict[str, str] = Field(default_factory=dict)
    resolved_attachment_version_ids: dict[str, str] = Field(default_factory=dict)


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_s3_client():  # pragma: no cover
    """Override in app wiring. Note for production wiring:
    the real boto3 client should be constructed with
    `Config(max_pool_connections=MAX_FETCH_WORKERS)` so the fetch
    executor's threads don't starve on a small default urllib3 pool.
    """
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_experiment_metadata_bucket() -> str:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def _fetch_object(
    s3_client,
    bucket: str,
    key: str,
    ticket_run_id: str,
    version_id: str | None = None,
    label: str = "object",
    size_cap_bytes: int | None = None,
) -> tuple[bytes, str | None]:
    """Returns (body_bytes, resolved_version_id).

    `label` parameterizes the 404 detail so an attachment 404 doesn't read
    "manifest not found" (misleading). The full S3 key is kept in the log
    line (for debugging) but stripped from the public detail (it leaks
    internal pathing — bucket layout, experiment_id, etc).

    `size_cap_bytes`, when set, makes the read short-circuit any object
    whose ContentLength (or actual body length, if ContentLength lies)
    exceeds the cap. Defense against publisher accidents OOMing the broker.
    """
    kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    try:
        response = s3_client.get_object(**kwargs)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchVersion", "404"):
            logger.warning(
                "S3 %s fetch returned 404 run_id=%s key=%s bucket=%s version_id=%s",
                label, ticket_run_id, key, bucket, version_id,
            )
            # Public detail intentionally omits the S3 key — no internal
            # pathing leaks across the broker boundary. Operators have the
            # log line above for diagnostics.
            raise HTTPException(status_code=404, detail=f"{label} not found") from e
        logger.error(
            "S3 %s fetch failed run_id=%s key=%s bucket=%s version_id=%s code=%s",
            label, ticket_run_id, key, bucket, version_id, code, exc_info=True,
        )
        _emit_metric("broker_s3_manifest_fetch_failure", [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "error_code", "Value": code or "unknown"},
        ])
        raise HTTPException(status_code=500, detail="manifest store unavailable") from e
    if size_cap_bytes is not None:
        # ContentLength is reported by S3 on every GetObject; check it
        # before reading so we short-circuit a 1 GB attachment without
        # ever materializing the bytes in memory.
        declared = response.get("ContentLength")
        if isinstance(declared, int) and declared > size_cap_bytes:
            logger.error(
                "S3 %s exceeds size cap run_id=%s key=%s bucket=%s declared=%d cap=%d",
                label, ticket_run_id, key, bucket, declared, size_cap_bytes,
            )
            _emit_metric("broker_attachment_size_cap_exceeded", [
                {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
                {"Name": "label", "Value": label},
            ])
            raise HTTPException(status_code=502, detail=f"{label} exceeds size cap")
        # Bounded read: even if ContentLength was missing or lying, asking
        # for cap+1 bytes lets us detect oversize without holding gigabytes.
        body = response["Body"].read(size_cap_bytes + 1)
        if len(body) > size_cap_bytes:
            logger.error(
                "S3 %s body exceeds size cap (post-read) run_id=%s key=%s bucket=%s actual=%d cap=%d",
                label, ticket_run_id, key, bucket, len(body), size_cap_bytes,
            )
            _emit_metric("broker_attachment_size_cap_exceeded", [
                {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
                {"Name": "label", "Value": label},
            ])
            raise HTTPException(status_code=502, detail=f"{label} exceeds size cap")
        return body, response.get("VersionId")
    return response["Body"].read(), response.get("VersionId")


def _fetch_object_cached(
    s3_client,
    bucket: str,
    key: str,
    ticket_run_id: str,
    version_id: str | None,
    label: str = "object",
    size_cap_bytes: int | None = None,
) -> tuple[bytes, str | None]:
    """Cache wrapper around _fetch_object.

    Cache by (bucket, key, version_id) — when version_id is pinned, S3 bytes
    are immutable per S3 contract, so the cache hit is correctness-safe and
    we can use ttl=None. Skip cache when version_id is None (dev/local
    "latest"), since latest can change under us.
    """
    if version_id is not None:
        cached = _OBJECT_CACHE.get((bucket, key, version_id), ttl=None)
        if cached is not None:
            return cached
    body, resolved_version = _fetch_object(
        s3_client, bucket, key, ticket_run_id, version_id,
        label=label, size_cap_bytes=size_cap_bytes,
    )
    if version_id is not None:
        _OBJECT_CACHE.put((bucket, key, version_id), body, resolved_version)
    return body, resolved_version


def _fetch_index_json(s3_client, bucket: str) -> dict:
    """Fetch index.json with a 60s TTL cache. On fetch failure, return stale
    cached value if present, else an empty experiments list — empty causes the
    orphan check to deny all manifests, which is safer than allowing them.

    Both fallback paths emit `broker_index_fetch_failure` with a `fallback`
    dimension so operators can alert on the empty-fallback case (= total
    broker blackout: every manifest 404s until index.json comes back).
    The stale-cache path is degraded but still serving, so it's a WARNING
    log; the empty-fallback path is ERROR.
    """
    now = time.monotonic()
    cached = _INDEX_CACHE.get(bucket)
    if cached and now - cached[1] < _INDEX_TTL:
        return cached[0]
    try:
        resp = s3_client.get_object(Bucket=bucket, Key="index.json")
        index = json.loads(resp["Body"].read())
    except Exception as e:
        env = os.environ.get("ENVIRONMENT", "unknown")
        if cached:
            logger.warning(
                "index.json fetch failed bucket=%s exc=%s — falling back to stale cache",
                bucket, e, exc_info=True,
            )
            _emit_metric("broker_index_fetch_failure", [
                {"Name": "Environment", "Value": env},
                {"Name": "fallback", "Value": "stale_cache"},
            ])
            return cached[0]
        logger.error(
            "index.json fetch failed bucket=%s exc=%s — falling back to empty (broker blackout)",
            bucket, e, exc_info=True,
        )
        _emit_metric("broker_index_fetch_failure", [
            {"Name": "Environment", "Value": env},
            {"Name": "fallback", "Value": "empty"},
        ])
        return {"experiments": []}
    _INDEX_CACHE[bucket] = (index, now)
    return index


def _reset_caches_for_test() -> None:
    """Clear module-level caches between tests. Call from a fixture."""
    _OBJECT_CACHE.clear()
    _INDEX_CACHE.clear()


def _emit_index_drift(experiment_id: str, run_id: str, drift_kind: str, detail: str) -> None:
    """Single helper for the four index-drift error sites.

    Index drift = publisher/control-plane bug: the canonical attachment
    list contains something we can't safely fetch. Old code logged at
    WARNING + silently `continue`d, so operators never saw it in
    CloudWatch. Now it's ERROR + metric so SNS → Slack can alert.
    """
    logger.error(
        "attachment_index_drift drift_kind=%s experiment_id=%s run_id=%s detail=%s",
        drift_kind, experiment_id, run_id, detail,
    )
    _emit_metric("broker_attachment_index_drift", [
        {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
        {"Name": "drift_kind", "Value": drift_kind},
        {"Name": "experiment_id", "Value": experiment_id},
    ])


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
    index_entry = next(
        (e for e in experiments if isinstance(e, dict) and e.get("id") == ticket.experiment_id),
        None,
    )
    if index_entry is None:
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

    # The index entry lists every attachment key the publisher uploaded. Fetch
    # only those — never trust a runner-supplied basename to map into an S3
    # key. attachment_version_ids in the request acts as a per-basename pin
    # override; absent entries fall through to "latest".
    raw = index_entry.get("attachment_keys")
    if raw is None:
        raw_attachment_keys: list = []
    elif isinstance(raw, list):
        raw_attachment_keys = raw
    else:
        # Drift: a string here would have iterated character-by-character
        # in the old code, producing nonsense S3 GETs per character.
        _emit_index_drift(
            ticket.experiment_id, ticket.run_id,
            "non_list_attachment_keys", f"type={type(raw).__name__}",
        )
        raw_attachment_keys = []

    attachment_specs: list[tuple[str, str, str | None]] = []  # (basename, s3_key, version_id)
    requested_version_pins = req.attachment_version_ids or {}
    prefix = f"{ticket.experiment_id}/attachments/"
    for ak in raw_attachment_keys:
        if not isinstance(ak, str):
            _emit_index_drift(
                ticket.experiment_id, ticket.run_id,
                "non_string_key", f"type={type(ak).__name__}",
            )
            continue
        # ak must be "<experiment_id>/attachments/<basename>". The publisher
        # enforces this shape; this is defense-in-depth against a malformed
        # index.json. Skip anything that doesn't match.
        if not ak.startswith(prefix):
            _emit_index_drift(
                ticket.experiment_id, ticket.run_id,
                "wrong_prefix", f"key={ak}",
            )
            continue
        basename = ak[len(prefix):]
        # Use the unified safe-basename rule — same check the request
        # validator applies. Keeps the two surfaces in sync.
        if not _is_safe_attachment_basename(basename):
            _emit_index_drift(
                ticket.experiment_id, ticket.run_id,
                "unsafe_basename", f"basename={basename!r}",
            )
            continue
        attachment_specs.append((basename, ak, requested_version_pins.get(basename)))

    # Cap attachment count per experiment. A malformed index with thousands
    # of keys would otherwise spawn thousands of futures on the shared
    # executor. Truncate + alert; never silently grow.
    if len(attachment_specs) > MAX_ATTACHMENTS_PER_EXPERIMENT:
        logger.error(
            "attachment_count_exceeded experiment_id=%s run_id=%s declared=%d cap=%d",
            ticket.experiment_id, ticket.run_id,
            len(attachment_specs), MAX_ATTACHMENTS_PER_EXPERIMENT,
        )
        _emit_metric("broker_attachment_count_exceeded", [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "experiment_id", "Value": ticket.experiment_id},
        ])
        attachment_specs = attachment_specs[:MAX_ATTACHMENTS_PER_EXPERIMENT]

    # Parallelize manifest + instruction + every attachment GET via the
    # shared, bounded fetch executor. Replaces per-request executors
    # (which summed to unbounded threads under load). When all
    # VersionIds are pinned the cached path returns immediately; when
    # unpinned/uncached the executor cuts latency to ~max(fetch_time).
    ex = _get_fetch_executor()
    fut_m = ex.submit(
        _fetch_object_cached,
        s3_client, bucket, manifest_key, ticket.run_id, req.manifest_version_id,
        "manifest", None,
    )
    fut_i = ex.submit(
        _fetch_object_cached,
        s3_client, bucket, instruction_key, ticket.run_id, req.instruction_version_id,
        "instruction", None,
    )
    attachment_futures = [
        (
            basename,
            ex.submit(
                _fetch_object_cached,
                s3_client, bucket, s3_key, ticket.run_id, version_id,
                "attachment", MAX_ATTACHMENT_BYTES,
            ),
        )
        for basename, s3_key, version_id in attachment_specs
    ]
    manifest_bytes, manifest_resolved_version = fut_m.result()
    instruction_bytes, instruction_resolved_version = fut_i.result()
    attachment_results: list[tuple[str, bytes, str | None]] = [
        (basename, *fut.result())
        for basename, fut in attachment_futures
    ]

    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, ValueError) as decode_err:
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
        raise HTTPException(status_code=500, detail="manifest decode error") from decode_err

    try:
        instruction_text = instruction_bytes.decode("utf-8")
    except UnicodeDecodeError as decode_err:
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
        raise HTTPException(status_code=500, detail="instruction decode error") from decode_err

    attachments: dict[str, str] = {}
    resolved_attachment_versions: dict[str, str] = {}
    for basename, body, resolved_version in attachment_results:
        try:
            attachments[basename] = body.decode("utf-8")
        except UnicodeDecodeError as decode_err:
            # Binary attachments aren't supported yet — fail loud so a
            # publisher accident (e.g. uploading a PDF) doesn't silently
            # corrupt the workspace write downstream. Add an
            # attachments_binary base64 channel when this becomes a real
            # requirement, not a guess.
            logger.error(
                "attachment_decode_error errorType=attachment_decode "
                "experiment_id=%s run_id=%s basename=%s",
                req.experiment_id, ticket.run_id, basename,
                exc_info=True,
            )
            _emit_metric("broker_attachment_decode_error", [
                {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
                {"Name": "experiment_id", "Value": req.experiment_id},
            ])
            raise HTTPException(status_code=500, detail="attachment decode error") from decode_err
        if resolved_version is not None:
            resolved_attachment_versions[basename] = resolved_version

    return ExperimentManifestResponse(
        manifest=manifest,
        instruction=instruction_text,
        resolved_manifest_version_id=manifest_resolved_version,
        resolved_instruction_version_id=instruction_resolved_version,
        attachments=attachments,
        resolved_attachment_version_ids=resolved_attachment_versions,
    )
