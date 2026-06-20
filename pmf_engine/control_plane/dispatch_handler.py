from __future__ import annotations

import json
import os
import re

import boto3
import httpx
from jsonschema import Draft7Validator

_EXPERIMENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

try:
    from shared.logger import get_logger

    logger = get_logger(__name__)
except (ImportError, OSError):
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    from .broker_client import BrokerClient, BrokerError
    from .jsonschema_errors import format_validation_errors
    from .manifest_loader import (
        ManifestLoaderError,
        ManifestLoaderMalformedError,
        ManifestLoaderTransientError,
        ManifestRoutingLoader,
    )
    from .scope_derivation import derive_scope
except ImportError:
    from broker_client import BrokerClient, BrokerError
    from jsonschema_errors import format_validation_errors  # type: ignore[no-redef]
    from manifest_loader import (  # type: ignore[no-redef]
        ManifestLoaderError,
        ManifestLoaderMalformedError,
        ManifestLoaderTransientError,
        ManifestRoutingLoader,
    )
    from scope_derivation import derive_scope  # type: ignore[no-redef]

_ecs_client = None
_sqs_client = None
_cw_client = None
_secrets_client = None
_service_token: str | None = None
_manifest_loader: ManifestRoutingLoader | None = None
_broker_client: BrokerClient | None = None
_validator_cache: dict[str, Draft7Validator] = {}
_VALIDATOR_CACHE_MAX = 64

# Fields whose presence on a projected routing dict signals a write-action
# experiment. Mirrors manifest_loader._WRITE_ACTION_FIELDS minus
# `allowed_external_tools`, which is a tool-list a read-action experiment
# could plausibly carry (e.g. WebFetch on a Databricks experiment) and is
# therefore not a write-action signal on its own.
_WRITE_ACTION_DISCRIMINATORS = ("system_prompt", "permission_mode")


def _is_write_action(experiment: dict) -> bool:
    return any(experiment.get(f) is not None for f in _WRITE_ACTION_DISCRIMINATORS)


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def get_service_token() -> str:
    """Fetch the broker service token from Secrets Manager, cached per warm
    container. Reading at runtime keeps the secret out of Terraform state and
    out of lambda:GetFunctionConfiguration, unlike a plaintext env var. A
    fetch failure propagates so the launch/mint path treats it as transient."""
    global _service_token
    if _service_token is None:
        resp = _get_secrets_client().get_secret_value(SecretId=SERVICE_TOKENS_SECRET_ARN)
        _service_token = json.loads(resp["SecretString"])["SERVICE_TOKEN"]
    return _service_token


def reset_service_token_for_tests() -> None:
    global _service_token, _secrets_client
    _service_token = None
    _secrets_client = None


def get_broker_client() -> BrokerClient:
    """Process-cached BrokerClient. Safe across threads — BrokerClient holds
    only the URL + service token; httpx is invoked at module-level per call.
    """
    global _broker_client
    if _broker_client is None:
        _broker_client = BrokerClient(BROKER_URL, get_service_token())
    return _broker_client


def reset_broker_client_for_tests() -> None:
    global _broker_client
    _broker_client = None


def _input_validator(experiment_id: str, manifest_version_id: str | None, input_schema: dict) -> Draft7Validator:
    """Cached Draft7Validator per (experiment_id, manifest_version_id).

    Schema construction is non-trivial (refs/format-checker setup); reusing
    the validator across dispatch records is the win. New manifest version
    publishes get a new cache key so stale schemas can't linger.

    When `manifest_version_id is None` (unversioned bucket / publish-time
    drift), refuse to cache: a stale validator would persist forever in
    the warm Lambda. Build a fresh one each call instead.
    """
    if manifest_version_id is None:
        return Draft7Validator(input_schema)
    key = f"{experiment_id}:{manifest_version_id}"
    cached = _validator_cache.get(key)
    if cached is not None:
        return cached
    if len(_validator_cache) >= _VALIDATOR_CACHE_MAX:
        _validator_cache.clear()
    validator = Draft7Validator(input_schema)
    _validator_cache[key] = validator
    return validator


def reset_validator_cache_for_tests() -> None:
    _validator_cache.clear()


def get_manifest_loader() -> ManifestRoutingLoader:
    """Returns a process-cached ManifestRoutingLoader.

    EXPERIMENT_METADATA_BUCKET is required and validated upfront via
    `_missing_critical_config()` so a missing bucket triggers the per-message
    error-callback path (not an uncaught RuntimeError that crashes the batch).
    """
    global _manifest_loader
    if _manifest_loader is None:
        bucket = os.environ.get("EXPERIMENT_METADATA_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError(
                "EXPERIMENT_METADATA_BUCKET env var is required for dispatch. "
                "Set it on the Lambda function (terraform: pmf-engine-control-plane)."
            )
        _manifest_loader = ManifestRoutingLoader(
            bucket=bucket,
            s3_client=boto3.client("s3"),
        )
    return _manifest_loader


def reset_manifest_loader_for_tests() -> None:
    global _manifest_loader
    _manifest_loader = None


def _resolve_routing(experiment_id: str, run_id: str = "") -> tuple[dict | None, list[str]]:
    """Look up routing from the S3 manifest loader.

    Returns (routing_or_none, list_of_known_experiment_ids_for_diagnostics).
    `routing is None` means the loader successfully read the index but the
    experiment_id is not registered — caller signals "unknown experiment".

    Loader failures (transient S3 / malformed manifest) raise
    ManifestLoaderTransientError or ManifestLoaderMalformedError. Both emit
    a `manifest_loader_fallback` CloudWatch metric with `error_type` and
    `Environment` dimensions so operators can alarm separately:
        transient → SQS will retry — usually self-heals
        malformed → publish-pipeline bug — page someone

    The handler converts both into SQS-retry signals (transient) or
    error-callback signals (malformed) — there is no in-process fallback.
    """
    loader = get_manifest_loader()
    try:
        routing = loader.routing_for(experiment_id)
        known = sorted(loader.known_experiments()) if routing is None else []
        return routing, known
    except ManifestLoaderError as e:
        error_type = "malformed" if isinstance(e, ManifestLoaderMalformedError) else "transient"
        logger.error(
            "manifest_loader_failure experiment_id=%s run_id=%s error_type=%s error=%s",
            experiment_id,
            run_id,
            error_type,
            e,
            exc_info=True,
        )
        _emit_metric(
            "manifest_loader_fallback",
            [
                {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
                {"Name": "experiment_id", "Value": experiment_id},
                {"Name": "error_type", "Value": error_type},
            ],
        )
        raise


def get_ecs_client():
    global _ecs_client
    if _ecs_client is None:
        _ecs_client = boto3.client("ecs")
    return _ecs_client


JOB_TABLE_NAME = os.environ.get("JOB_TABLE_NAME", "")

_job_store = None


def get_job_store():
    global _job_store
    if _job_store is None:
        try:
            from .job_store import JobStore  # local import keeps cold-start lean
        except ImportError:
            from job_store import JobStore  # type: ignore[no-redef]
        _job_store = JobStore(JOB_TABLE_NAME)
    return _job_store


def reset_job_store_for_tests() -> None:
    global _job_store
    _job_store = None


def get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def get_cw_client():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def _emit_metric(metric_name: str, dimensions: list[dict]):
    try:
        get_cw_client().put_metric_data(
            Namespace="PMFEngine",
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": dimensions,
                }
            ],
        )
    except Exception as e:
        logger.warning(
            "MetricEmissionFailed metric=%s exc_type=%s: %s",
            metric_name,
            type(e).__name__,
            e,
            exc_info=True,
        )


def emit_dispatch_metric(metric_name: str, experiment_id: str):
    _emit_metric(
        metric_name,
        [
            {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
            {"Name": "ExperimentId", "Value": experiment_id},
        ],
    )


def send_error_callback(
    message: dict,
    error: str,
    callback_queue_url: str,
    dedup_id: str | None = None,
) -> bool:
    """Send a `failed` callback to gp-api's results queue.

    Returns True if the SQS send succeeded, False otherwise (missing queue
    URL, SQS outage, or IAM regression). Callers use the return value to
    decide whether to add to `batch_item_failures`: if the callback did NOT
    reach gp-api, the dispatch message itself must be retried so we can try
    the callback again — otherwise the run row is stuck PENDING forever.

    Wire format MUST match what the broker's CallbackSender emits so gp-api's
    AgentExperimentResultSchema (zod) parses it.
    """
    if not callback_queue_url:
        logger.error(
            "send_error_callback: no callback_queue_url configured; "
            "cannot notify gp-api of dispatch failure for run %s",
            message.get("run_id", "unknown"),
        )
        return False
    try:
        run_id = message.get("run_id", "unknown")
        body = json.dumps(
            {
                "type": "agentExperimentResult",
                "data": {
                    "experimentId": message.get("experiment_type", "unknown"),
                    "runId": run_id,
                    "organizationSlug": message.get("organization_slug", "unknown"),
                    "status": "failed",
                    "error": error,
                    "detail": error,
                    "reasonCode": "DispatchError",
                },
            }
        )
        get_sqs_client().send_message(
            QueueUrl=callback_queue_url,
            MessageBody=body,
            MessageGroupId="agentExperiments",
            MessageDeduplicationId=dedup_id or f"{run_id}-failed",
        )
        logger.info(f"Sent error callback for run {run_id}: {error}")
        return True
    except Exception as e:
        logger.exception(f"Failed to send error callback: {e}")
        return False


ECS_CLUSTER_ARN = os.environ.get("ECS_CLUSTER_ARN", "")
ECS_TASK_DEFINITION = os.environ.get("ECS_TASK_DEFINITION", "")
ECS_SUBNET_IDS = [s for s in os.environ.get("ECS_SUBNET_IDS", "").split(",") if s]
ECS_SECURITY_GROUP_ID = os.environ.get("ECS_SECURITY_GROUP_ID", "")
RESULTS_QUEUE_URL = os.environ.get("RESULTS_QUEUE_URL", "")
BROKER_URL = os.environ.get("BROKER_URL", "")
SERVICE_TOKENS_SECRET_ARN = os.environ.get("SERVICE_TOKENS_SECRET_ARN", "")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "pmf-engine")


def _missing_critical_config() -> list[str]:
    missing = []
    if not ECS_CLUSTER_ARN:
        missing.append("ECS_CLUSTER_ARN")
    if not ECS_TASK_DEFINITION:
        missing.append("ECS_TASK_DEFINITION")
    if not ECS_SUBNET_IDS:
        missing.append("ECS_SUBNET_IDS")
    if not ECS_SECURITY_GROUP_ID:
        missing.append("ECS_SECURITY_GROUP_ID")
    if not RESULTS_QUEUE_URL:
        missing.append("RESULTS_QUEUE_URL")
    if not BROKER_URL:
        missing.append("BROKER_URL")
    if not SERVICE_TOKENS_SECRET_ARN:
        missing.append("SERVICE_TOKENS_SECRET_ARN")
    if not os.environ.get("EXPERIMENT_METADATA_BUCKET", "").strip():
        missing.append("EXPERIMENT_METADATA_BUCKET")
    if not JOB_TABLE_NAME:
        missing.append("JOB_TABLE_NAME")
    # ENVIRONMENT drives the expected `_input_files` bucket name. Without it,
    # _validate_input_files raises ValueError on any dispatch carrying user
    # uploads — surface the misconfig via the standard
    # "dispatch-misconfig" error-callback path instead of letting the message
    # dead-letter on an uncaught parse error.
    if not os.environ.get("ENVIRONMENT", "").strip():
        missing.append("ENVIRONMENT")
    return missing


MAX_PARAMS_JSON_BYTES = 6000

# Cap on the serialized INPUT_FILES_JSON env var the dispatch sets on the
# Fargate task. AWS ECS RunTask limits the total `containerOverrides[]`
# environment payload, and our other env vars (PARAMS_JSON, ANTHROPIC_BASE_URL,
# etc.) already consume budget. With realistic agenda-upload entries
# (~150–300 bytes each), 4000 bytes covers 10+ entries; with worst-case
# max-length entries (~1.3 KB each) it covers ~3. Reject larger payloads at
# dispatch with a clean error callback rather than letting RunTask fail
# silently on oversize overrides.
MAX_INPUT_FILES_JSON_BYTES = 4000

_PRIOR_ARTIFACT_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/artifact\.json$")


_PRIOR_ARTIFACT_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_prior_artifact_versions(versions) -> None:
    if versions is None:
        return
    if not isinstance(versions, dict):
        raise ValueError(f"prior_artifact_versions must be an object, got {type(versions).__name__}")
    if len(versions) > 10:
        raise ValueError(f"prior_artifact_versions too large: {len(versions)} entries")
    for key, value in versions.items():
        if not isinstance(key, str) or not _PRIOR_ARTIFACT_KEY_RE.fullmatch(key):
            raise ValueError(f"prior_artifact_versions key must match [A-Za-z0-9_-]{{1,64}}: got {key!r}")
        if not isinstance(value, str) or not _PRIOR_ARTIFACT_VALUE_RE.fullmatch(value):
            raise ValueError(
                f"prior_artifact_versions[{key!r}] must match "
                f"'<experiment_id>/<run_id>/artifact.json' pattern "
                f"(segments [A-Za-z0-9_-]{{1,64}}): got {value!r}"
            )


# Mirrored in broker InputFileRef.dest and runner input_files._DEST_RE —
# three-gate defense since `dest` becomes a basename under /workspace/input/.
# `{0,254}` after the leading char bounds total length at 255 to match the
# broker's Pydantic Field max_length.
_INPUT_FILE_DEST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")
_INPUT_FILE_REQUIRED_KEYS = {"bucket", "key", "dest"}
# Mirrors prior_artifact_versions cap.
_MAX_INPUT_FILES = 10


def _expected_inputs_bucket() -> str:
    """The single bucket dispatch is allowed to authorize for /inputs/read in
    this environment. Derived from the ENVIRONMENT env var (`dev`/`qa`/`prod`),
    matching what gp-ai-projects Terraform creates and what the broker IAM
    grants GetObject on. Any other bucket name in `_input_files[i].bucket` is
    rejected — defense in depth atop the broker's ScopeTicket allowlist.
    """
    env = os.environ.get("ENVIRONMENT", "").strip().lower()
    return f"gp-agent-run-inputs-{env}" if env else ""


def _validate_input_files(value) -> None:
    """Validate the shape of `_input_files` before it reaches mint / Fargate.

    Parallels `_validate_prior_artifact_versions`. Each entry must carry
    {bucket, key, dest} where `dest` is a safe basename (the runner writes
    `/workspace/input/<dest>`, so a slash or `..` here would escape the
    workspace despite broker + runner re-checks). `bucket` must equal the
    single expected inputs bucket for this environment — no caller is
    legitimately authorized to reference any other bucket.
    """
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError(f"_input_files must be an array, got {type(value).__name__}")
    if len(value) > _MAX_INPUT_FILES:
        raise ValueError(f"_input_files too large: {len(value)} entries (max {_MAX_INPUT_FILES})")
    expected_bucket = _expected_inputs_bucket()
    if not expected_bucket:
        raise ValueError(
            "_input_files cannot be validated: ENVIRONMENT env var missing on dispatch lambda; "
            "set ENVIRONMENT to one of dev/qa/prod (see modules/pmf-engine-control-plane/main.tf)"
        )
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"_input_files[{i}] must be an object, got {type(entry).__name__}")
        missing = _INPUT_FILE_REQUIRED_KEYS - set(entry.keys())
        if missing:
            raise ValueError(f"_input_files[{i}] missing required keys: {sorted(missing)}")
        bucket, key, dest = entry["bucket"], entry["key"], entry["dest"]
        if not isinstance(bucket, str) or bucket != expected_bucket:
            raise ValueError(f"_input_files[{i}].bucket must be {expected_bucket!r}: got {bucket!r}")
        if not isinstance(key, str) or not (0 < len(key) <= 1024):
            raise ValueError(f"_input_files[{i}].key must be a 1-1024 char string: got {key!r}")
        if not isinstance(dest, str) or not _INPUT_FILE_DEST_RE.fullmatch(dest):
            raise ValueError(
                f"_input_files[{i}].dest must be a simple filename "
                f"matching [A-Za-z0-9_][A-Za-z0-9._-]*: got {dest!r}"
            )


# Dispatch-envelope metadata that ships inside params. The `_` prefix marks
# a key as runner-orchestration, not agent input: stripped from params before
# input_schema validation and before PARAMS_JSON is built, then re-attached
# to the top-level message for downstream code. Unknown `_`-prefixed keys
# raise so typos don't silently vanish.
_RESERVED_ENVELOPE_KEYS = {"_input_files"}


def parse_dispatch_message(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Invalid message body: {e}") from e

    for field in ("experiment_type", "organization_slug", "run_id"):
        if not data.get(field):
            raise ValueError(f"Missing required field: {field}")

    if not isinstance(data["experiment_type"], str) or not _EXPERIMENT_ID_RE.match(data["experiment_type"]):
        raise ValueError(f"experiment_type must match {_EXPERIMENT_ID_RE.pattern}")
    if not isinstance(data["run_id"], str) or not _IDENTIFIER_RE.match(data["run_id"]):
        raise ValueError("run_id must match [a-zA-Z0-9_-]{1,64}")
    if not isinstance(data["organization_slug"], str) or not _IDENTIFIER_RE.match(data["organization_slug"]):
        raise ValueError("organization_slug must match [a-zA-Z0-9_-]{1,64}")
    # clerk_user_id is optional. When omitted, broker mint skips the Clerk
    # actor-token round trip and the ticket has clerk_session_id=None.
    # Experiments that hit /agent-mcp will be 4xx'd by that route's guard.
    if "clerk_user_id" in data and data["clerk_user_id"] is not None and not isinstance(data["clerk_user_id"], str):
        raise ValueError("clerk_user_id must be a string when provided")

    priority = data.get("priority", "DEFAULT")
    if priority not in ("HIGH", "DEFAULT"):
        raise ValueError("priority must be 'HIGH' or 'DEFAULT'")
    data["priority"] = priority

    if data.get("params") is None:
        data["params"] = {}

    # Envelope-strip pass. Must happen BEFORE input_schema validation
    # (otherwise the manifest would need to list `_input_files` in its
    # schema) and BEFORE building PARAMS_JSON (otherwise the agent's env
    # would carry runner-orchestration data). Non-dict params is handled
    # later in the handler loop with an InvalidParamsType error callback,
    # so we just skip stripping when params isn't a dict.
    if isinstance(data["params"], dict):
        unknown_envelope_keys = [
            k for k in data["params"] if isinstance(k, str) and k.startswith("_") and k not in _RESERVED_ENVELOPE_KEYS
        ]
        if unknown_envelope_keys:
            raise ValueError(
                f"params contains unknown _-prefixed key(s) "
                f"{sorted(unknown_envelope_keys)}; reserved for dispatch envelope only"
            )
        input_files = data["params"].pop("_input_files", None)
        if input_files is not None:
            _validate_input_files(input_files)
            # Re-attach on the top-level dispatch dict so downstream code
            # (mint call, INPUT_FILES_JSON env builder) reads it like any
            # other dispatch field — symmetric with prior_artifact_versions.
            data["_input_files"] = input_files

    _validate_prior_artifact_versions(data.get("prior_artifact_versions"))
    return data


def build_container_overrides(
    experiment: dict,
    message: dict,
    broker_token: str,
    broker_url: str,
    container_name: str,
    params_json: str | None = None,
) -> dict:
    if params_json is None:
        params_json = json.dumps(message["params"])
    env = [
        {"name": "EXPERIMENT_ID", "value": message["experiment_type"]},
        {"name": "RUN_ID", "value": message["run_id"]},
        {"name": "ORGANIZATION_SLUG", "value": message["organization_slug"]},
        {"name": "AGENT_MODEL", "value": experiment["model"]},
        {"name": "BROKER_TOKEN", "value": broker_token},
        {"name": "BROKER_URL", "value": broker_url},
        {"name": "ANTHROPIC_BASE_URL", "value": f"{broker_url}/anthropic"},
        {"name": "ANTHROPIC_API_KEY", "value": broker_token},
        # Braintrust SDK routes through the broker like Anthropic does: the task
        # SG only allows broker egress, so direct api.braintrust.dev calls are
        # blocked. APP_URL/API_URL force the SDK's control-plane (login) and
        # data-plane (/logs3 ingest) legs through the broker proxy; the runner
        # authenticates with the broker token, the broker swaps in the real key.
        {"name": "BRAINTRUST_API_KEY", "value": broker_token},
        {"name": "BRAINTRUST_APP_URL", "value": f"{broker_url}/braintrust/app"},
        {"name": "BRAINTRUST_API_URL", "value": f"{broker_url}/braintrust/api"},
        {"name": "PARAMS_JSON", "value": params_json},
        {"name": "TIMEOUT_SECONDS", "value": str(experiment.get("timeout_seconds", 600))},
        # QA_JUDGES configures the runbooks qa-spine pluggable LLM judge registry
        # (format: name:provider:model,...). Routes through the same broker proxy
        # the runner already uses for the agent — no new Secrets Manager entries,
        # no new egress. Same-family Phase 1/2 (Sonnet + Opus with adversarial
        # system prompt) is the documented in-Fargate path; cross-family (e.g.
        # Gemini Phase 2) is deferred until/if a broker route for Google exists.
        {"name": "QA_JUDGES", "value": "claude:anthropic:claude-sonnet-4-6,opus:anthropic:claude-opus-4-7"},
    ]
    # Pin the runner to the exact S3 object versions Lambda fetched at routing
    # time. Without this, a publish during the dispatch→start window could
    # let the runner read different bytes than Lambda routed against.
    if experiment.get("manifest_version_id"):
        env.append({"name": "MANIFEST_VERSION_ID", "value": experiment["manifest_version_id"]})
    if experiment.get("instruction_version_id"):
        env.append({"name": "INSTRUCTION_VERSION_ID", "value": experiment["instruction_version_id"]})
    # Attachment VersionIds are sidecar pins captured by the manifest loader's
    # per-attachment HEADs. sort_keys keeps the env-var value byte-deterministic
    # across dispatches so downstream caches / idempotency tests don't churn
    # on dict iteration order. Skip when empty/absent — empty env vars are
    # noise and the runner already special-cases empty/unset.
    if experiment.get("attachment_version_ids"):
        env.append(
            {
                "name": "ATTACHMENT_VERSION_IDS",
                "value": json.dumps(experiment["attachment_version_ids"], sort_keys=True),
            }
        )
    # QA gate version pins (contract G). Mirrors ATTACHMENT_VERSION_IDS exactly:
    # {basename: VersionId}, sort_keys for byte-deterministic output. Skip when
    # empty/absent — on an unversioned bucket every pin is None so the map is
    # empty, the env var is omitted, and the runner fetches qa 'latest'. This
    # keeps the no-qa containerOverrides byte-identical to a pre-gate dispatch.
    if experiment.get("qa_version_ids"):
        env.append(
            {
                "name": "QA_VERSION_IDS",
                "value": json.dumps(experiment["qa_version_ids"], sort_keys=True),
            }
        )
    # When the dispatch carries enumerated input-file refs (e.g. user-uploaded
    # agenda PDFs), the runner pre-fetches each via the broker's /inputs/read
    # endpoint before invoking the agent. Refs travel as a JSON-encoded env var
    # — refs are small (a few hundred bytes each, capped at 10 entries).
    if message.get("_input_files"):
        env.append(
            {
                "name": "INPUT_FILES_JSON",
                "value": json.dumps(message["_input_files"]),
            }
        )
    # Write-action manifest fields (system_prompt, permission_mode,
    # allowed_external_tools — ENG-10128) are not forwarded as env vars on
    # purpose: the runner fetches the full manifest itself via
    # runner/manifest_loader.load_from_broker (pinned by MANIFEST_VERSION_ID
    # above) and reads them directly. Duplicating them here would create a
    # second source of truth and risk env-var size limits for system_prompt.
    return {"containerOverrides": [{"name": container_name, "environment": env}]}


def launch_run(
    *,
    experiment: dict,
    message: dict,
    scope: dict,
    params_json: str,
) -> dict:
    """Mint a broker token and launch the Fargate task. Returns
    {"status": "launched", "task_arn": ...} on success, or
    {"status": "failed", "error": <user-safe>} when the run could not be
    launched (broker rejection, ECS RunTask failure). Raises on transient
    errors the caller should retry (httpx during mint, ECS RunTask exception).
    """
    experiment_id = message["experiment_type"]
    prior_artifact_versions = message.get("prior_artifact_versions")
    # User-input prefetch (develop): the scheduler threads `_input_files` from
    # the QueuedJob into this message; mint's MintRequest field is `input_files`
    # (no leading underscore at the API boundary).
    input_files = message.get("_input_files")
    try:
        broker = get_broker_client()
        mint_result = broker.mint_run_token(
            run_id=message["run_id"],
            organization_slug=message["organization_slug"],
            experiment_id=experiment_id,
            scope=scope,
            params=message["params"],
            clerk_user_id=message.get("clerk_user_id"),
            exp_ttl_seconds=experiment.get("timeout_seconds", 3600) + 300,
            prior_artifact_versions=prior_artifact_versions,
            input_files=input_files,
        )
    except BrokerError as e:
        logger.warning(f"Broker rejected {experiment_id} (run={message['run_id']}): {e.status_code} {e.detail}")
        emit_dispatch_metric("BrokerRejected", experiment_id)
        return {"status": "failed", "error": e.user_safe_message or "Broker rejected the request"}
    except httpx.HTTPError as e:
        logger.warning(f"Transient network error during mint for run {message.get('run_id')}: {e}")
        emit_dispatch_metric("MintTransient", experiment_id)
        raise
    except Exception as e:
        logger.exception(f"Unexpected error during mint for run {message.get('run_id')}: {e}")
        emit_dispatch_metric("MintUnexpected", experiment_id)
        return {"status": "failed", "error": f"Unexpected dispatch error: {type(e).__name__}"}

    overrides = build_container_overrides(
        experiment=experiment,
        message=message,
        broker_token=mint_result["broker_token"],
        broker_url=BROKER_URL,
        container_name=CONTAINER_NAME,
        params_json=params_json,
    )

    logger.info(
        f"Dispatching experiment '{experiment_id}' for organization "
        f"'{message['organization_slug']}' (run: {message['run_id']})"
    )

    minted_broker_token = mint_result["broker_token"]

    try:
        response = get_ecs_client().run_task(
            cluster=ECS_CLUSTER_ARN,
            taskDefinition=ECS_TASK_DEFINITION,
            launchType="FARGATE",
            # Tag the task with the run_id (uuid7, 36 chars — within the 36-char
            # startedBy limit) so the stuck-LAUNCHING sweep can tell whether a
            # LAUNCHING job has a live task before it fails the row.
            startedBy=message["run_id"],
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": ECS_SUBNET_IDS,
                    "securityGroups": [ECS_SECURITY_GROUP_ID],
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides=overrides,
        )

        failures = response.get("failures", [])
        tasks = response.get("tasks", [])

        if failures or not tasks:
            failure_reasons = [f.get("reason", "unknown") for f in failures]
            logger.error(
                f"ECS RunTask failed (experiment={experiment_id}, " f"run={message['run_id']}): {failure_reasons}"
            )
            _cleanup_minted_token(broker, minted_broker_token, message["run_id"])
            safe_summary = _classify_ecs_failure_reasons(failure_reasons)
            kind = _classify_ecs_failure_kind(failure_reasons)
            emit_dispatch_metric(f"ECSRunTaskFailed_{kind}", experiment_id)
            return {"status": "failed", "error": f"ECS RunTask failed: {safe_summary}"}

        task_arn = tasks[0]["taskArn"]
        logger.info(f"Started Fargate task: {task_arn}")
        return {"status": "launched", "task_arn": task_arn}

    except Exception as e:
        logger.exception(
            f"ECS RunTask exception (experiment={experiment_id}, "
            f"run={message['run_id']}, exception_type={type(e).__name__}): {e}"
        )
        emit_dispatch_metric("ECSRunTaskException", experiment_id)
        _cleanup_minted_token(broker, minted_broker_token, message["run_id"])
        raise


def handler(event: dict, context) -> dict:
    batch_item_failures = []
    missing_config = _missing_critical_config()

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        body = record.get("body", "")

        # When the Lambda is misconfigured (e.g. ENVIRONMENT unset), the strict
        # parse below will raise on dispatches that exercise envelope validation
        # — and that ValueError would otherwise be caught as InvalidDispatchPayload,
        # masking the underlying misconfig. Check config FIRST and route through
        # the dispatch-misconfig callback path. A minimal lenient parse extracts
        # just enough message identity to make the callback useful; the strict
        # parse below validates the envelope only after config is healthy.
        if missing_config:
            try:
                partial = json.loads(body) if body else {}
                shallow: dict = partial if isinstance(partial, dict) else {}
            except (json.JSONDecodeError, TypeError):
                shallow = {}
            shallow.setdefault("run_id", "unknown")
            shallow.setdefault("experiment_type", "_unknown")
            shallow.setdefault("organization_slug", "unknown")
            logger.error(
                f"Dispatch Lambda misconfigured: missing required env vars "
                f"{missing_config} (run: {shallow['run_id']}). "
                f"Message will be retried via SQS until operator fixes config."
            )
            send_error_callback(
                shallow,
                f"Dispatch Lambda misconfigured: missing required env vars {missing_config}",
                RESULTS_QUEUE_URL,
                dedup_id=f"dispatch-misconfig-{shallow['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        try:
            message = parse_dispatch_message(body)
        except ValueError as e:
            logger.error(f"Invalid message {message_id}: {e}")
            emit_dispatch_metric("InvalidDispatchPayload", "_unknown")
            # gp-api creates the run row as QUEUED and its stale sweep is
            # RUNNING-only, so a malformed message orphans that row until the
            # slow 6h backstop. Best-effort recover run_id and notify so gp-api
            # can fail the row now. Mirror the dispatch-misconfig callback path.
            try:
                partial = json.loads(body) if isinstance(body, str) else {}
            except Exception:
                partial = {}
            if partial.get("run_id") and RESULTS_QUEUE_URL:
                # Same pattern as the other permanent-fault paths: only retry the
                # SQS message (toward the DLQ) if the callback did NOT reach gp-api.
                # Retrying after a successful callback is pointless churn, and a
                # later retry whose callback fails would re-orphan the QUEUED row.
                sent = send_error_callback(
                    partial,
                    f"Malformed dispatch message: {e}",
                    RESULTS_QUEUE_URL,
                    dedup_id=f"invalid-payload-{partial['run_id']}",
                )
                if not sent:
                    batch_item_failures.append({"itemIdentifier": message_id})
            else:
                # No run_id or no queue URL — can't notify gp-api; send to the DLQ
                # for operator alarms.
                batch_item_failures.append({"itemIdentifier": message_id})
            continue

        experiment_id = message["experiment_type"]
        try:
            experiment, known_ids = _resolve_routing(experiment_id, run_id=message["run_id"])
        except ManifestLoaderTransientError:
            # SQS retry — usually self-heals during AWS weather. No callback;
            # leave gp-api's run row in PENDING so the next attempt updates it.
            batch_item_failures.append({"itemIdentifier": message_id})
            continue
        except ManifestLoaderMalformedError as e:
            # Publish-pipeline bug. Don't retry forever — surface to gp-api.
            send_error_callback(
                message,
                f"Experiment manifest is malformed: {e}. Operator action required.",
                RESULTS_QUEUE_URL,
                dedup_id=f"manifest-malformed-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        if experiment is None:
            logger.error(
                f"Unknown experiment '{experiment_id}' in message {message_id}. Known experiments: {known_ids}"
            )
            emit_dispatch_metric("UnknownExperiment", experiment_id)
            # Design choice (A): send error callback AND add to batch_item_failures.
            # gp-api gets immediate PENDING->FAILED feedback; SQS retries the message
            # so it eventually lands in the DLQ for operator alarms. We pass a
            # stable dedup_id keyed on run_id so retries within FIFO's 5-minute
            # dedup window do NOT generate duplicate callbacks to gp-api.
            send_error_callback(
                message,
                f"Unknown experiment: {experiment_id}",
                RESULTS_QUEUE_URL,
                dedup_id=f"unknown-experiment-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        if not isinstance(message["params"], dict):
            type_name = type(message["params"]).__name__
            logger.error(
                f"Invalid params type for {experiment_id} "
                f"(run: {message['run_id']}, organization: {message['organization_slug']}): "
                f"got {type_name}, expected object"
            )
            emit_dispatch_metric("InvalidParamsType", experiment_id)
            sent = send_error_callback(
                message,
                f"params must be a JSON object, got {type_name}",
                RESULTS_QUEUE_URL,
                dedup_id=f"invalid-params-type-{message['run_id']}",
            )
            if not sent:
                batch_item_failures.append({"itemIdentifier": message_id})
            continue

        params_json = json.dumps(message["params"])
        params_bytes = len(params_json.encode("utf-8"))
        if params_bytes > MAX_PARAMS_JSON_BYTES:
            logger.error(
                f"Params too large for {experiment_id} "
                f"(run: {message['run_id']}, organization: {message['organization_slug']}): "
                f"{params_bytes} bytes > {MAX_PARAMS_JSON_BYTES}"
            )
            emit_dispatch_metric("ParamsTooLarge", experiment_id)
            sent = send_error_callback(
                message,
                f"Experiment parameters exceed size limit ({params_bytes} > {MAX_PARAMS_JSON_BYTES} bytes)",
                RESULTS_QUEUE_URL,
                dedup_id=f"params-too-large-{message['run_id']}",
            )
            if not sent:
                batch_item_failures.append({"itemIdentifier": message_id})
            continue

        if message.get("_input_files"):
            input_files_json = json.dumps(message["_input_files"])
            input_files_bytes = len(input_files_json.encode("utf-8"))
            if input_files_bytes > MAX_INPUT_FILES_JSON_BYTES:
                logger.error(
                    f"_input_files too large for {experiment_id} "
                    f"(run: {message['run_id']}, organization: {message['organization_slug']}): "
                    f"{input_files_bytes} bytes > {MAX_INPUT_FILES_JSON_BYTES}"
                )
                emit_dispatch_metric("InputFilesJsonTooLarge", experiment_id)
                sent = send_error_callback(
                    message,
                    f"_input_files serialized size exceeds limit "
                    f"({input_files_bytes} > {MAX_INPUT_FILES_JSON_BYTES} bytes)",
                    RESULTS_QUEUE_URL,
                    dedup_id=f"input-files-too-large-{message['run_id']}",
                )
                if not sent:
                    batch_item_failures.append({"itemIdentifier": message_id})
                continue

        # Validate the dispatch message's params against the manifest's
        # input_schema (JSON Schema Draft-07). The meta-schema makes
        # input_schema required — an empty/missing one here means a
        # publish-pipeline bug, treat it as malformed.
        input_schema = experiment.get("input_schema") or {}
        if not input_schema:
            logger.error(
                f"manifest for {experiment_id} has no input_schema (run: {message['run_id']}). Treating as malformed."
            )
            send_error_callback(
                message,
                f"Experiment manifest is malformed: {experiment_id} has no input_schema.",
                RESULTS_QUEUE_URL,
                dedup_id=f"manifest-no-input-schema-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        violations = format_validation_errors(
            _input_validator(experiment_id, experiment.get("manifest_version_id"), input_schema),
            message["params"],
        )
        if violations:
            logger.error(
                f"input_schema validation failed for {experiment_id} "
                f"(run: {message['run_id']}, organization: {message['organization_slug']}): "
                f"{violations}"
            )
            emit_dispatch_metric("InputSchemaViolation", experiment_id)
            sent = send_error_callback(
                message,
                f"Params for {experiment_id} failed input_schema: {violations}",
                RESULTS_QUEUE_URL,
                dedup_id=f"input-schema-{message['run_id']}",
            )
            if not sent:
                batch_item_failures.append({"itemIdentifier": message_id})
            continue

        # Write-action experiments (ENG-10128) get an empty scope dict. The
        # broker creates the Clerk actor token from MintRequest.clerk_user_id
        # and stores the resulting clerk_session_id on the ScopeTicket; it
        # then mints fresh ~60s JWTs for each MCP call the runner makes to
        # /agent/mcp. No per-experiment allowlist is enforced today — every
        # @McpTool-decorated endpoint on gp-api is exposed to every agent
        # run; a real allowlist is future work.
        #
        # Discriminator: `system_prompt` OR `permission_mode` present in the
        # projected routing dict. Both are Claude Agent SDK signals that only
        # appear on write-action manifests. `allowed_external_tools` is NOT a
        # discriminator — a future read-action experiment could plausibly
        # declare extra non-gp-api tools (e.g. WebFetch) without being
        # write-action. The manifest loader validates each write-action field
        # independently, so we mirror its any-of pattern here for the fields
        # that actually signal write-action semantics.
        #
        # `derive_scope` raises ValueError when read-experiment params slip
        # past `input_schema` but still violate stricter checks (state/city/
        # district control characters). An uncaught ValueError here would
        # crash the Lambda invocation — no batchItemFailures, no error
        # callback, every remaining record in the SQS batch unprocessed.
        # Treat the same as an input_schema violation: client-fault, surface
        # to gp-api with a stable dedup so FIFO retries don't duplicate.
        try:
            if _is_write_action(experiment):
                scope: dict = {}
            else:
                scope = derive_scope(
                    experiment_id,
                    message["params"],
                    manifest_scope=experiment.get("scope"),
                )
        except ValueError as e:
            logger.error(
                f"Scope derivation failed for {experiment_id} "
                f"(run: {message['run_id']}, organization: {message['organization_slug']}): {e}"
            )
            emit_dispatch_metric("ScopeDerivationError", experiment_id)
            sent = send_error_callback(
                message,
                f"Params for {experiment_id} failed scope derivation: {e}",
                RESULTS_QUEUE_URL,
                dedup_id=f"scope-derivation-{message['run_id']}",
            )
            if not sent:
                batch_item_failures.append({"itemIdentifier": message_id})
            continue
        import time as _time

        try:
            from .job_store import QueuedJob
        except ImportError:
            from job_store import QueuedJob  # type: ignore[no-redef]

        routing = {
            "model": experiment["model"],
            "timeout_seconds": experiment.get("timeout_seconds", 600),
            "manifest_version_id": experiment.get("manifest_version_id"),
            "instruction_version_id": experiment.get("instruction_version_id"),
            "attachment_version_ids": experiment.get("attachment_version_ids"),
            "scope": scope,
        }
        try:
            get_job_store().put_queued_job(
                QueuedJob(
                    run_id=message["run_id"],
                    experiment_type=experiment_id,
                    organization_slug=message["organization_slug"],
                    clerk_user_id=message.get("clerk_user_id"),
                    priority=message["priority"],
                    params=message["params"],
                    routing=routing,
                    prior_artifact_versions=message.get("prior_artifact_versions"),
                    # User-input prefetch (develop): the broker MintRequest field
                    # is `input_files`; the dispatch envelope carries it as
                    # `_input_files` (extracted out of params). Persist it on the
                    # job so the scheduler can thread it into launch_run's mint.
                    input_files=message.get("_input_files"),
                    created_at_ms=int(_time.time() * 1000),
                )
            )
        except Exception as e:
            logger.exception(f"Failed to enqueue job for run {message['run_id']}: {e}")
            emit_dispatch_metric("JobEnqueueFailed", experiment_id)
            batch_item_failures.append({"itemIdentifier": message_id})
            continue
        emit_dispatch_metric("JobEnqueued", experiment_id)
        # Arrival is picked up by the scheduler via the table's DynamoDB stream;
        # no explicit invoke needed here.

    return {"batchItemFailures": batch_item_failures}


_ECS_FAILURE_KIND_TO_USER_MESSAGE = {
    "Capacity": "capacity exhausted (see server logs for detail)",
    "IAM": "permission error (see server logs for detail)",
    "Throttled": "throttled by AWS (see server logs for detail)",
    "Other": "capacity or permission error (see server logs for detail)",
}


def _classify_ecs_failure_kind(reasons: list[str]) -> str:
    """Classify ECS RunTask failure reasons into a stable kind tag used for
    BOTH the user-facing message (via the table above) AND the CloudWatch
    metric dimension. Single source of truth so the two never drift."""
    joined_upper = " ".join(str(r).upper() for r in reasons)
    if "CAPACITY" in joined_upper or "RESOURCE:" in joined_upper:
        return "Capacity"
    if "ACCESSDENIED" in joined_upper or "NOT AUTHORIZED" in joined_upper or "IAM" in joined_upper:
        return "IAM"
    if "THROTTL" in joined_upper:
        return "Throttled"
    return "Other"


def _classify_ecs_failure_reasons(reasons: list[str]) -> str:
    return _ECS_FAILURE_KIND_TO_USER_MESSAGE[_classify_ecs_failure_kind(reasons)]


def _cleanup_minted_token(broker, broker_token: str, run_id: str) -> None:
    try:
        broker.delete_run_token(broker_token=broker_token, run_id=run_id)
    except Exception as e:
        logger.warning(
            f"Failed to delete run-token for run {run_id} after ECS failure: "
            f"{type(e).__name__}: {e}. Ticket + run-lock will expire via TTL."
        )
