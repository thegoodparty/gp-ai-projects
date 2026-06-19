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
    qa_manifest_key: str
    qa_keys: list[str]


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
        # Per-experiment cache of the qa folder's {basename: version_id} pins
        # (contract G). Same role as the attachment cache, separate map.
        self._qa_version_cache: dict[str, tuple[dict[str, str], float]] = {}

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
        # QA gate folder pins (contract F → G). When the index entry carries no
        # qa_manifest_key the experiment publishes no qa/ folder, so the map is
        # {} and the dispatch guard omits QA_VERSION_IDS — byte-identical no-qa.
        # Defensive: publish_experiments.py always writes qa_keys as a list, but
        # a malformed/legacy index entry could store it as a string. Passing a
        # string downstream would let `[qa_manifest_key, *qa_keys]` iterate it
        # character-by-character, silently dropping the real qa key from
        # QA_VERSION_IDS. Treat a non-list as empty here too (mirrors the guard
        # in _fetch_qa_version_ids).
        qa_keys = entry.get("qa_keys") or []
        if not isinstance(qa_keys, list):
            logger.warning(
                "qa_keys for %s is %s, not a list; ignoring (only qa manifest pinned)",
                experiment_id,
                type(qa_keys).__name__,
            )
            qa_keys = []
        qa_version_ids = self._fetch_qa_version_ids(
            experiment_id,
            entry.get("qa_manifest_key"),
            qa_keys,
        )
        routing = _project_routing(manifest, experiment_id=experiment_id)
        routing["manifest_version_id"] = manifest_version_id
        routing["instruction_version_id"] = instruction_version_id
        routing["attachment_version_ids"] = attachment_version_ids
        routing["qa_version_ids"] = qa_version_ids
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

    def _fetch_version_ids(
        self,
        experiment_id: str,
        subdir: str,
        keys: list[str],
        cache: dict[str, tuple[dict[str, str], float]],
    ) -> dict[str, str]:
        """HEAD each key under `{experiment_id}/{subdir}/`, return {basename:
        VersionId}. Shared by `_fetch_attachment_version_ids` (subdir=
        "attachments") and `_fetch_qa_version_ids` (subdir="qa"), so a future
        change to the pinning contract lands on both surfaces at once.

        Error-handling contract (load-bearing, identical for both subdirs):
        - `NoSuchKey` / `NoSuchVersion` / `"404"` on a single key: WARN-log +
          skip that basename. Dispatch proceeds; the runner surfaces the
          missing file via the broker fetch when it materializes the workspace.
        - Any other ClientError (AccessDenied, ServiceUnavailable, SlowDown,
          throttling, transient 5xx): raise ManifestLoaderTransientError.
          Falling through to "latest" defeats the entire pinning system the
          dispatch-time HEAD was designed to guarantee.
        - Wrong-prefix key (doesn't start with `{experiment_id}/{subdir}/`),
          non-string key, or a key already seen: WARN-log + skip. Defense in
          depth against publish-pipeline bugs / manual S3 edits, and the
          seen-set dedupes a key that appears more than once (e.g. a qa
          manifest listed in both qa_manifest_key and qa_keys).
        - None VersionId is dropped — on an UNVERSIONED bucket head_object
          returns no VersionId, so every pin is None and the map ends up empty
          (manifest.json is never special-cased to fabricate a pin).

        Cached per-experiment under the same TTL as manifest+instruction so
        warm Lambdas don't re-HEAD on every invocation.
        """
        now = time.monotonic()
        cached = cache.get(experiment_id)
        if cached and now - cached[1] < self._ttl:
            return cached[0]

        result: dict[str, str] = {}
        expected_prefix = f"{experiment_id}/{subdir}/"
        seen: set[str] = set()
        for k in keys:
            if not isinstance(k, str) or k in seen:
                continue
            seen.add(k)
            if not k.startswith(expected_prefix):
                logger.warning(
                    "%s key has unexpected prefix — skipping: "
                    "experiment_id=%s key=%r expected_prefix=%s",
                    subdir,
                    experiment_id,
                    k,
                    expected_prefix,
                )
                continue
            basename = k.split(f"/{subdir}/", 1)[1]
            try:
                response = self._s3.head_object(Bucket=self._bucket, Key=k)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                request_id = e.response.get("ResponseMetadata", {}).get("RequestId", "")
                if code in ("NoSuchKey", "NoSuchVersion", "404"):
                    logger.warning(
                        "%s object absent — proceeding without version pin: "
                        "experiment_id=%s key=%s bucket=%s code=%s request_id=%s",
                        subdir,
                        experiment_id,
                        k,
                        self._bucket,
                        code,
                        request_id,
                    )
                    continue
                logger.error(
                    "S3 HeadObject failed for %s (NOT swallowed — version pin lost): "
                    "experiment_id=%s key=%s bucket=%s code=%s request_id=%s",
                    subdir,
                    experiment_id,
                    k,
                    self._bucket,
                    code,
                    request_id,
                    exc_info=True,
                )
                raise ManifestLoaderTransientError(
                    f"failed to head {subdir} file s3://{self._bucket}/{k} "
                    f"for '{experiment_id}': {code} (request_id={request_id})"
                ) from e
            version_id = response.get("VersionId")
            if version_id is not None:
                result[basename] = version_id

        if len(cache) >= _MANIFEST_CACHE_MAX:
            cache.clear()
        cache[experiment_id] = (result, now)
        return result

    def _fetch_attachment_version_ids(
        self, experiment_id: str, attachment_keys: list[str]
    ) -> dict[str, str]:
        """HEAD each attachment key, return {basename: VersionId}.

        Thin wrapper over `_fetch_version_ids` (subdir="attachments"); see that
        method for the full error-handling contract. The runner will surface a
        missing attachment via the broker fetch at workspace-materialize time.
        """
        return self._fetch_version_ids(
            experiment_id,
            "attachments",
            attachment_keys,
            self._attachment_version_cache,
        )

    def _fetch_qa_version_ids(
        self,
        experiment_id: str,
        qa_manifest_key: str | None,
        qa_keys: list[str],
    ) -> dict[str, str]:
        """HEAD the qa folder's manifest + each qa key, return {basename:
        VersionId} (contract G).

        Thin wrapper over `_fetch_version_ids` (subdir="qa"); see that method
        for the full error-handling contract. Specific to qa:
        - No `qa_manifest_key` → the experiment publishes no qa/ folder; return
          {} so the dispatch guard omits QA_VERSION_IDS (byte-identical no-qa).
        - The manifest key is prepended to the qa keys. `qa_keys` already
          EXCLUDES manifest.json (carried separately), but the shared helper's
          seen-set dedupes if an overlap ever appears, so each distinct key is
          HEADed once.
        - Contract-G reality: on an UNVERSIONED bucket every pin is None and
          the map is empty — manifest.json is never special-cased to fabricate
          a pin.
        """
        if not isinstance(qa_manifest_key, str) or not qa_manifest_key:
            return {}
        # Defensive: publish_experiments.py always writes qa_keys as a list, but
        # a malformed/legacy index entry could store it as a string. Splatting a
        # string into `[qa_manifest_key, *qa_keys]` would iterate it CHARACTER BY
        # CHARACTER, so the real qa key never gets HEADed and its VersionId
        # silently drops out of QA_VERSION_IDS. Treat a non-list as empty.
        if not isinstance(qa_keys, list):
            logger.warning(
                "qa_keys for %s is %s, not a list; ignoring (only qa manifest pinned)",
                experiment_id,
                type(qa_keys).__name__,
            )
            qa_keys = []
        return self._fetch_version_ids(
            experiment_id,
            "qa",
            [qa_manifest_key, *qa_keys],
            self._qa_version_cache,
        )

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


def _validate_runtime_fields(manifest: dict, experiment_id: str) -> None:
    runtime = manifest.get("runtime")
    if runtime is None:
        return
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

    eid = experiment_id or manifest.get("id", "<unknown>")

    if manifest.get("runtime") is not None:
        _validate_runtime_fields(manifest, eid)

    # Write-action experiment fields (ENG-10128). Validated and projected only
    # when at least one is present, so legacy Databricks/web-only manifests
    # produce a routing dict with the same keys they did before.
    if any(manifest.get(f) is not None for f in _WRITE_ACTION_FIELDS):
        _validate_write_action_fields(manifest, eid)
        for field in _WRITE_ACTION_FIELDS:
            value = manifest.get(field)
            if value is not None:
                routing[field] = value

    return routing
