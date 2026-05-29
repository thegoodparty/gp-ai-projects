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
        # Per-experiment cache of {basename: version_id}. Single-source-of-
        # truth for the runner's attachment pin so a publish during the
        # dispatch→runner window can't swap bytes out under us.
        self._attachment_version_cache: dict[str, tuple[dict[str, str], float]] = {}

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
            experiment_id,
            entry.get("manifest_key", f"{experiment_id}/manifest.json"),
        )
        instruction_version_id = self._fetch_instruction_version_id(
            experiment_id,
            entry.get("instruction_key", f"{experiment_id}/instruction.md"),
        )
        attachment_keys = entry.get("attachment_keys") or []
        attachment_version_ids = self._fetch_attachment_version_ids(
            experiment_id, attachment_keys
        )
        routing = _project_routing(manifest, experiment_id=experiment_id)
        routing["manifest_version_id"] = manifest_version_id
        routing["instruction_version_id"] = instruction_version_id
        routing["attachment_version_ids"] = attachment_version_ids
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
            raise ManifestLoaderMalformedError(f"manifest for '{experiment_id}' is not valid JSON: {e}") from e
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
                    experiment_id,
                    instruction_key,
                    self._bucket,
                    code,
                    request_id,
                )
                return None
            logger.error(
                "S3 HeadObject failed for instruction (NOT swallowed — version pin lost): "
                "experiment_id=%s key=%s bucket=%s code=%s request_id=%s",
                experiment_id,
                instruction_key,
                self._bucket,
                code,
                request_id,
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

    def _fetch_attachment_version_ids(
        self, experiment_id: str, attachment_keys: list[str]
    ) -> dict[str, str]:
        """HEAD each attachment key, return {basename: VersionId}.

        Error-handling contract mirrors `_fetch_instruction_version_id`:
        - `NoSuchKey` / `NoSuchVersion` / `"404"` on a single attachment:
          WARN-log + skip that basename. Dispatch proceeds; the runner will
          surface the missing attachment via the broker fetch when it tries
          to materialize the workspace file.
        - Any other ClientError (AccessDenied, ServiceUnavailable, SlowDown,
          throttling, transient 5xx): raise ManifestLoaderTransientError.
          Falling through to "latest" defeats the entire pinning system the
          dispatch-time HEAD was designed to guarantee.
        - Wrong-prefix attachment_key (doesn't start with
          `{experiment_id}/attachments/`): WARN-log + skip. Defense in depth
          against publish-pipeline bugs / manual S3 edits.

        Cached under the same TTL as manifest+instruction so warm Lambdas
        don't re-HEAD on every invocation.
        """
        now = time.monotonic()
        cached = self._attachment_version_cache.get(experiment_id)
        if cached and now - cached[1] < self._ttl:
            return cached[0]

        result: dict[str, str] = {}
        expected_prefix = f"{experiment_id}/attachments/"
        for ak in attachment_keys:
            if not isinstance(ak, str) or not ak.startswith(expected_prefix):
                logger.warning(
                    "attachment_key has unexpected prefix — skipping: "
                    "experiment_id=%s key=%r expected_prefix=%s",
                    experiment_id,
                    ak,
                    expected_prefix,
                )
                continue
            basename = ak.split("/attachments/", 1)[1]
            try:
                response = self._s3.head_object(Bucket=self._bucket, Key=ak)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                request_id = e.response.get("ResponseMetadata", {}).get("RequestId", "")
                if code in ("NoSuchKey", "NoSuchVersion", "404"):
                    logger.warning(
                        "attachment object absent — proceeding without version pin: "
                        "experiment_id=%s key=%s bucket=%s code=%s request_id=%s",
                        experiment_id,
                        ak,
                        self._bucket,
                        code,
                        request_id,
                    )
                    continue
                logger.error(
                    "S3 HeadObject failed for attachment (NOT swallowed — version pin lost): "
                    "experiment_id=%s key=%s bucket=%s code=%s request_id=%s",
                    experiment_id,
                    ak,
                    self._bucket,
                    code,
                    request_id,
                    exc_info=True,
                )
                raise ManifestLoaderTransientError(
                    f"failed to head attachment s3://{self._bucket}/{ak} "
                    f"for '{experiment_id}': {code} (request_id={request_id})"
                ) from e
            version_id = response.get("VersionId")
            if version_id is not None:
                result[basename] = version_id

        if len(self._attachment_version_cache) >= _MANIFEST_CACHE_MAX:
            self._attachment_version_cache.clear()
        self._attachment_version_cache[experiment_id] = (result, now)
        return result

    def _get_object(self, key: str, label: str) -> tuple[bytes, str | None]:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            request_id = e.response.get("ResponseMetadata", {}).get("RequestId", "")
            logger.error(
                "S3 GetObject failed bucket=%s key=%s label=%s code=%s request_id=%s",
                self._bucket,
                key,
                label,
                code,
                request_id,
                exc_info=True,
            )
            if code in ("NoSuchKey", "NoSuchVersion", "404"):
                raise ManifestLoaderMalformedError(f"S3 object missing for {label}: code={code} key={key}") from e
            raise ManifestLoaderTransientError(f"S3 GetObject failed for {label}: code={code}") from e
        return response["Body"].read(), response.get("VersionId")


def _validate_scope(scope: dict, experiment_id: str) -> None:
    """Defense-in-depth validation of `manifest.scope`.

    Publish-time meta-schema validation already enforces this in runbooks,
    but a stale/corrupted index entry or a manual S3 edit could bypass that.
    Raise ManifestLoaderMalformedError if the scope shape is invalid.
    """
    tables = scope.get("allowed_tables", [])
    if not isinstance(tables, list):
        raise ManifestLoaderMalformedError(f"{experiment_id}: scope.allowed_tables must be a list")
    for t in tables:
        if not isinstance(t, str) or not _TABLE_PATTERN.match(t):
            raise ManifestLoaderMalformedError(f"{experiment_id}: invalid scope.allowed_tables entry: {t!r}")
    max_rows = scope.get("max_rows", 50000)
    if not isinstance(max_rows, int) or not (1 <= max_rows <= 1_000_000):
        raise ManifestLoaderMalformedError(f"{experiment_id}: scope.max_rows must be int 1..1000000")


_WRITE_ACTION_FIELDS = (
    "system_prompt",
    "permission_mode",
    "allowed_external_tools",
)
# Claude Agent SDK supports "default" | "acceptEdits" | "plan" | "bypassPermissions".
# We deliberately allowlist only the two we've reviewed for the Fargate sandbox:
# "default" (interactive prompts disabled in a non-tty container, so this is
# effectively deny-tool-use) and "bypassPermissions" (the existing PMF runner
# default — agent runs unattended in an isolated container with a scoped IAM
# role). Adding "acceptEdits"/"plan" requires a code-review pass on the harness
# side, so we'd rather force that than let a manifest publish silently widen
# the permission surface.
_PERMISSION_MODE_VALUES = frozenset({"default", "bypassPermissions"})
# Conventions, not platform limits. Picked to keep manifests reviewable and
# to bound the runner's manifest fetch (broker GET /experiment/manifest).
# system_prompt is the largest field; 50K leaves ~10× headroom over the
# longest prompts we've seen in practice. Tool names match the Claude SDK's
# tool identifiers, which are short.
_MAX_SYSTEM_PROMPT_LEN = 50_000
_MAX_TOOL_NAME_LEN = 64


def _validate_write_action_fields(manifest: dict, experiment_id: str) -> None:
    """Defense-in-depth validation for the optional write-action top-level
    manifest fields (ENG-10128). Mirrors `_validate_scope`'s pattern: publish-
    time meta-schema is the primary check; this catches stale/corrupted
    manifests and manual S3 edits before the Fargate task launches.

    Validates each field independently: callers may set any subset. The
    dispatch-time discriminator for "this is a write-action experiment" is
    the presence of `system_prompt` OR `permission_mode` (see
    `dispatch_handler._is_write_action`); every field present here must be
    well-formed regardless of which others are present.
    """
    permission_mode = manifest.get("permission_mode")
    if permission_mode is not None:
        if not isinstance(permission_mode, str) or permission_mode not in _PERMISSION_MODE_VALUES:
            raise ManifestLoaderMalformedError(
                f"{experiment_id}: permission_mode must be one of "
                f"{sorted(_PERMISSION_MODE_VALUES)}; got {permission_mode!r}"
            )

    system_prompt = manifest.get("system_prompt")
    if system_prompt is not None:
        if (
            not isinstance(system_prompt, str)
            or not system_prompt.strip()
            or len(system_prompt) > _MAX_SYSTEM_PROMPT_LEN
        ):
            raise ManifestLoaderMalformedError(
                f"{experiment_id}: system_prompt must be a non-empty string ≤ {_MAX_SYSTEM_PROMPT_LEN} chars"
            )

    tools = manifest.get("allowed_external_tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise ManifestLoaderMalformedError(f"{experiment_id}: allowed_external_tools must be a list")
        for t in tools:
            if not isinstance(t, str) or not t.strip() or len(t) > _MAX_TOOL_NAME_LEN:
                raise ManifestLoaderMalformedError(f"{experiment_id}: invalid allowed_external_tools entry: {t!r}")

    # runtime.max_parallel_subagents — parallel research fan-out opt-in.
    runtime = manifest.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, dict):
            raise ManifestLoaderMalformedError(f"{experiment_id}: runtime must be an object")
        mps = runtime.get("max_parallel_subagents")
        if mps is not None and (isinstance(mps, bool) or not isinstance(mps, int) or mps < 0):
            raise ManifestLoaderMalformedError(
                f"{experiment_id}: runtime.max_parallel_subagents must be a non-negative integer; got {mps!r}"
            )
        mtt = runtime.get("max_thinking_tokens")
        if mtt is not None and (isinstance(mtt, bool) or not isinstance(mtt, int) or mtt < 0):
            raise ManifestLoaderMalformedError(
                f"{experiment_id}: runtime.max_thinking_tokens must be a non-negative integer; got {mtt!r}"
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

    routing: dict = {
        "model": manifest.get("model", "sonnet"),
        "timeout_seconds": manifest.get("timeout_seconds", 600),
        # JSON Schema Draft-07 the dispatcher validates message["params"] against
        # before launching Fargate. Replaces the older `required_params` array.
        "input_schema": manifest.get("input_schema") or {},
        # Broker scope: which Databricks tables the runner can query + row cap.
        # Empty/absent means no Databricks access (web-only experiment).
        "scope": scope,
    }

    # Write-action experiment fields (ENG-10128). Validated and projected only
    # when at least one is present, so legacy Databricks/web-only manifests
    # produce a routing dict with the same keys they did before.
    if any(manifest.get(f) is not None for f in _WRITE_ACTION_FIELDS):
        _validate_write_action_fields(manifest, experiment_id or manifest.get("id", "<unknown>"))
        for field in _WRITE_ACTION_FIELDS:
            value = manifest.get(field)
            if value is not None:
                routing[field] = value

    return routing
