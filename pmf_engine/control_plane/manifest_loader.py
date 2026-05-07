"""Lambda-side manifest routing loader.

The dispatch Lambda no longer ships with a bundled DISPATCH_REGISTRY. Instead
it fetches `index.json` + per-experiment `manifest.json` from the metadata
bucket on each warm invocation, with a /tmp TTL cache so warm invocations are
free.

This decouples Lambda deploys from experiment additions: a new experiment
landing in s3://agent-experiment-metadata-{env}/ is dispatchable within ~60s
with no Lambda zip rebuild.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TypedDict

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

INDEX_KEY = "index.json"
DEFAULT_TTL_SECONDS = 60.0
_MANIFEST_CACHE_MAX = 256
_TABLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


class ManifestLoaderError(RuntimeError):
    """Raised when the loader cannot serve usable routing for an experiment.

    Subclassed into Transient vs Malformed so dispatch can tag the operator-
    facing CloudWatch metric with the right error_type (transient noise vs
    publish-pipeline bug). Both subclasses still derive from this base so any
    existing `except ManifestLoaderError` catch still works.
    """


class ManifestLoaderTransientError(ManifestLoaderError):
    """S3-side issues: AccessDenied, ServiceUnavailable, SlowDown, NoSuchBucket,
    transient 5xx. Dispatch can fall back to the bundled registry; SQS will
    retry. Operator alarm if rate is non-zero."""


class ManifestLoaderMalformedError(ManifestLoaderError):
    """Content-side issues: corrupt JSON, missing 'experiments' array, missing
    required manifest fields. Indicates a publish-pipeline bug — meta-schema
    validation should have caught this before upload. Dispatch falls back for
    availability, but operator should be paged: bundled registry may diverge
    from what the publish was trying to ship."""


class _IndexEntry(TypedDict, total=False):
    id: str
    version: int
    mode: str
    manifest_key: str


class ManifestRoutingLoader:
    """Resolves experiment_id → routing dict.

    Routing dict shape (see `_project_routing`):
        {model, timeout_seconds, input_schema, scope,
         manifest_version_id, instruction_version_id}
    """

    def __init__(
        self,
        bucket: str,
        s3_client,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ):
        if not bucket:
            raise ValueError("bucket required")
        self._bucket = bucket
        self._s3 = s3_client
        self._ttl = ttl_seconds
        self._index_cache: tuple[list[_IndexEntry], float] | None = None
        # Two distinct caches with distinct value shapes — each typed honestly
        # so a future contributor can't dereference the wrong tuple. Sharing
        # one dict keyed by string-prefix convention used to "work" only
        # because the experiment_id regex (^[a-z][a-z0-9_]*$) rejected the
        # leading underscore that separated the namespaces; that protection
        # was upstream and brittle.
        self._manifest_cache: dict[str, tuple[tuple[dict, str | None], float]] = {}
        self._instruction_version_cache: dict[str, tuple[str | None, float]] = {}

    # ---------- public ----------

    def routing_for(self, experiment_id: str) -> dict | None:
        """Return per-experiment routing fields for ECS RunTask, plus the S3
        VersionIds of the manifest + instruction at fetch time.

        The VersionIds become container env vars (MANIFEST_VERSION_ID,
        INSTRUCTION_VERSION_ID) so the Fargate runner pins its broker fetch
        to the exact bytes Lambda saw. Closes the publish-during-run race.

        KNOWN LIMITATION (best-effort pinning): the manifest GET and the
        instruction HEAD are independent S3 calls. If a publish lands
        between the manifest GET and the instruction HEAD, the routing
        dict pairs an older manifest VersionId with a newer instruction
        VersionId. Consequences are bounded: the runner validates its
        artifact against the manifest's `output_schema` (the older one we
        pinned), so contract validation still works against a consistent
        manifest. The instruction text being newer is the harm — agent
        playbook may not match the schema. See RUNBOOK for operator
        recovery if a divergence is observed in production.
        """
        entry = self._find_index_entry(experiment_id)
        if entry is None:
            return None
        manifest, manifest_version_id = self._fetch_manifest(
            experiment_id, entry.get("manifest_key", f"{experiment_id}/manifest.json"),
        )
        instruction_version_id = self._fetch_instruction_version_id(
            experiment_id, entry.get("instruction_key", f"{experiment_id}/instruction.md"),
        )
        routing = _project_routing(manifest, experiment_id=experiment_id)
        routing["manifest_version_id"] = manifest_version_id
        routing["instruction_version_id"] = instruction_version_id
        return routing

    def known_experiments(self) -> list[str]:
        return [e["id"] for e in self._load_index()]

    # ---------- internal ----------

    def _find_index_entry(self, experiment_id: str) -> _IndexEntry | None:
        index = self._load_index()
        for entry in index:
            if entry.get("id") == experiment_id:
                return entry
        return None

    def _load_index(self) -> list[_IndexEntry]:
        now = time.monotonic()
        if self._index_cache and now - self._index_cache[1] < self._ttl:
            return self._index_cache[0]
        body, _index_version_id = self._get_object(INDEX_KEY, label="index")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            raise ManifestLoaderMalformedError(f"index.json is not valid JSON: {e}") from e
        experiments = payload.get("experiments")
        if not isinstance(experiments, list):
            raise ManifestLoaderMalformedError("index.json missing 'experiments' array")
        self._index_cache = (experiments, now)
        return experiments

    def _fetch_manifest(self, experiment_id: str, manifest_key: str) -> tuple[dict, str | None]:
        """Returns (manifest_dict, s3_version_id_or_None)."""
        now = time.monotonic()
        cached = self._manifest_cache.get(experiment_id)
        if cached and now - cached[1] < self._ttl:
            return cached[0]
        body, version_id = self._get_object(manifest_key, label=f"manifest:{experiment_id}")
        try:
            manifest = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            raise ManifestLoaderMalformedError(
                f"manifest for '{experiment_id}' is not valid JSON: {e}"
            ) from e
        cached_value = (manifest, version_id)
        if len(self._manifest_cache) >= _MANIFEST_CACHE_MAX:
            self._manifest_cache.clear()
        self._manifest_cache[experiment_id] = (cached_value, now)
        return cached_value

    def _fetch_instruction_version_id(self, experiment_id: str, instruction_key: str) -> str | None:
        """HEAD-like fetch just to capture VersionId. Body is discarded — Lambda
        doesn't need the instruction text, only the version pin to forward to
        the runner. Cached alongside manifest under the same TTL.

        Error-handling contract (load-bearing):
        - `NoSuchKey` → return None. The instruction file is genuinely absent
          (publish bug or experiment dir incomplete). Dispatch proceeds; the
          runner will surface the missing instruction at fetch time.
        - Any other ClientError (AccessDenied, ServiceUnavailable, SlowDown,
          NoSuchBucket, throttling, transient 5xx) → raise ManifestLoaderError.
          These all indicate the version pin is silently lost; the whole point
          of capturing VersionId is to defeat the publish-during-run race, so
          falling back to "latest" defeats the system. Failing loud lets SQS
          retry and surfaces IAM / availability regressions instead of masking
          them as silently-degraded dispatches.
        """
        now = time.monotonic()
        cached = self._instruction_version_cache.get(experiment_id)
        if cached and now - cached[1] < self._ttl:
            return cached[0]
        try:
            response = self._s3.head_object(Bucket=self._bucket, Key=instruction_key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            request_id = e.response.get("ResponseMetadata", {}).get("RequestId", "")
            # boto3 head_object returns "404" (HTTP status, no XML body) for
            # missing keys; get_object returns "NoSuchKey" (parsed from the
            # XML error body). Match all three so we route absence the same
            # way regardless of which API the caller used.
            if code in ("NoSuchKey", "NoSuchVersion", "404"):
                logger.warning(
                    "instruction object absent — proceeding without version pin: "
                    "experiment_id=%s key=%s bucket=%s code=%s request_id=%s",
                    experiment_id, instruction_key, self._bucket, code, request_id,
                )
                return None
            logger.error(
                "S3 HeadObject failed for instruction (NOT swallowed — version pin lost): "
                "experiment_id=%s key=%s bucket=%s code=%s request_id=%s",
                experiment_id, instruction_key, self._bucket, code, request_id,
                exc_info=True,
            )
            raise ManifestLoaderTransientError(
                f"failed to head instruction s3://{self._bucket}/{instruction_key} "
                f"for '{experiment_id}': {code} (request_id={request_id})"
            ) from e
        version_id = response.get("VersionId")
        if len(self._instruction_version_cache) >= _MANIFEST_CACHE_MAX:
            self._instruction_version_cache.clear()
        self._instruction_version_cache[experiment_id] = (version_id, now)
        return version_id

    def _get_object(self, key: str, label: str) -> tuple[bytes, str | None]:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            request_id = e.response.get("ResponseMetadata", {}).get("RequestId", "")
            logger.error(
                "S3 GetObject failed bucket=%s key=%s label=%s code=%s request_id=%s",
                self._bucket, key, label, code, request_id,
                exc_info=True,
            )
            if code in ("NoSuchKey", "NoSuchVersion", "404"):
                raise ManifestLoaderMalformedError(
                    f"S3 object missing for {label}: code={code} key={key}"
                ) from e
            raise ManifestLoaderTransientError(
                f"S3 GetObject failed for {label}: code={code}"
            ) from e
        return response["Body"].read(), response.get("VersionId")


def _validate_scope(scope: dict, experiment_id: str) -> None:
    """Defense-in-depth validation of `manifest.scope`.

    Publish-time meta-schema validation already enforces this in runbooks,
    but a stale/corrupted index entry or a manual S3 edit could bypass that.
    Raise ManifestLoaderMalformedError if the scope shape is invalid.
    """
    tables = scope.get("allowed_tables", [])
    if not isinstance(tables, list):
        raise ManifestLoaderMalformedError(
            f"{experiment_id}: scope.allowed_tables must be a list"
        )
    for t in tables:
        if not isinstance(t, str) or not _TABLE_PATTERN.match(t):
            raise ManifestLoaderMalformedError(
                f"{experiment_id}: invalid scope.allowed_tables entry: {t!r}"
            )
    max_rows = scope.get("max_rows", 50000)
    if not isinstance(max_rows, int) or not (1 <= max_rows <= 1_000_000):
        raise ManifestLoaderMalformedError(
            f"{experiment_id}: scope.max_rows must be int 1..1000000"
        )


def _project_routing(manifest: dict, experiment_id: str = "") -> dict:
    """Project the full manifest down to the routing fields the Lambda needs.

    Lambda no longer carries cpu/memory/harness/contract.s3_key_template/
    contract.type/param_builder_key — those were either unused at dispatch
    (cpu/memory: task definition is fixed in terraform; s3_key_template:
    runner hard-codes <id>/<run_id>/artifact.json) or single-valued / 1:1
    derived (harness=claude_sdk; param_builder_key derives from mode in
    gp-api).
    """
    scope = manifest.get("scope") or {}
    if scope:
        _validate_scope(scope, experiment_id or manifest.get("id", "<unknown>"))
    return {
        "model": manifest.get("model", "sonnet"),
        "timeout_seconds": manifest.get("timeout_seconds", 600),
        # JSON Schema Draft-07 the dispatcher validates message["params"] against
        # before launching Fargate. Replaces the older `required_params` array.
        "input_schema": manifest.get("input_schema") or {},
        # Broker scope: which Databricks tables the runner can query + row cap.
        # Empty/absent means no Databricks access (web-only experiment).
        "scope": scope,
    }
