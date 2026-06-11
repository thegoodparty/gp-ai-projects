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


def get_broker_client() -> BrokerClient:
    """Process-cached BrokerClient. Safe across threads — BrokerClient holds
    only the URL + service token; httpx is invoked at module-level per call.
    """
    global _broker_client
    if _broker_client is None:
        _broker_client = BrokerClient(BROKER_URL, SERVICE_TOKEN)
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


# Maximum number of concurrently RUNNING agent Fargate tasks. 0 (or unset)
# disables the cap. At-cap messages are deferred — returned to the queue with
# an extended visibility timeout so each SQS receive buys a long wait instead
# of burning maxReceiveCount every 120s.
MAX_CONCURRENT_AGENTS = int(os.environ.get("MAX_CONCURRENT_AGENTS", "0") or 0)
CAP_DEFERRAL_VISIBILITY_SECONDS = 600

ECS_CLUSTER_ARN = os.environ.get("ECS_CLUSTER_ARN", "")
ECS_TASK_DEFINITION = os.environ.get("ECS_TASK_DEFINITION", "")
ECS_SUBNET_IDS = [s for s in os.environ.get("ECS_SUBNET_IDS", "").split(",") if s]
ECS_SECURITY_GROUP_ID = os.environ.get("ECS_SECURITY_GROUP_ID", "")
RESULTS_QUEUE_URL = os.environ.get("RESULTS_QUEUE_URL", "")
BROKER_URL = os.environ.get("BROKER_URL", "")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")
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
    if not SERVICE_TOKEN:
        missing.append("SERVICE_TOKEN")
    if not os.environ.get("EXPERIMENT_METADATA_BUCKET", "").strip():
        missing.append("EXPERIMENT_METADATA_BUCKET")
    return missing


MAX_PARAMS_JSON_BYTES = 6000

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

    if data.get("params") is None:
        data["params"] = {}

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
    # Write-action manifest fields (system_prompt, permission_mode,
    # allowed_external_tools — ENG-10128) are not forwarded as env vars on
    # purpose: the runner fetches the full manifest itself via
    # runner/manifest_loader.load_from_broker (pinned by MANIFEST_VERSION_ID
    # above) and reads them directly. Duplicating them here would create a
    # second source of truth and risk env-var size limits for system_prompt.
    return {"containerOverrides": [{"name": container_name, "environment": env}]}


def _count_active_agent_tasks() -> int:
    """Live RUNNING task count on the agent cluster — the source of truth for
    "concurrent active agents". desiredStatus=RUNNING includes tasks still
    PROVISIONING/PENDING, so just-launched tasks count immediately.
    list_tasks returns at most 100 ARNs per page; paginate or caps >= 100
    silently never bind.
    """
    paginator = get_ecs_client().get_paginator("list_tasks")
    count = 0
    for page in paginator.paginate(cluster=ECS_CLUSTER_ARN, desiredStatus="RUNNING"):
        count += len(page.get("taskArns", []))
    return count


def _queue_url_from_arn(queue_arn: str) -> str:
    _, _, _, region, account, name = queue_arn.split(":")
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


def _defer_for_concurrency_cap(record: dict, message: dict, active: int) -> None:
    """Return an at-cap message to the queue for a later retry.

    Extends the message's visibility timeout so the retry comes in
    CAP_DEFERRAL_VISIBILITY_SECONDS instead of the queue's 120s default —
    each receive consumes one of maxReceiveCount, so longer waits mean a
    cap-blocked message survives longer saturation before dead-lettering.
    No error callback: deferral is not a failure; gp-api's run row stays
    RUNNING until the agent actually executes.
    """
    logger.info(
        f"Concurrency cap reached ({active}/{MAX_CONCURRENT_AGENTS}); deferring run {message['run_id']} "
        f"(experiment={message['experiment_type']}, organization={message['organization_slug']})"
    )
    emit_dispatch_metric("ConcurrencyCapDeferred", message["experiment_type"])
    try:
        get_sqs_client().change_message_visibility(
            QueueUrl=_queue_url_from_arn(record["eventSourceARN"]),
            ReceiptHandle=record["receiptHandle"],
            VisibilityTimeout=CAP_DEFERRAL_VISIBILITY_SECONDS,
        )
    except Exception:
        logger.warning(
            f"change_message_visibility failed for deferred run {message['run_id']}; "
            f"message will retry after the queue's default visibility timeout",
            exc_info=True,
        )


def handler(event: dict, context) -> dict:
    batch_item_failures = []
    missing_config = _missing_critical_config()

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        body = record.get("body", "")

        try:
            message = parse_dispatch_message(body)
        except ValueError as e:
            logger.error(f"Invalid message {message_id}: {e}")
            emit_dispatch_metric("InvalidDispatchPayload", "_unknown")
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        if missing_config:
            logger.error(
                f"Dispatch Lambda misconfigured: missing required env vars "
                f"{missing_config} (run: {message['run_id']}). "
                f"Message will be retried via SQS until operator fixes config."
            )
            send_error_callback(
                message,
                f"Dispatch Lambda misconfigured: missing required env vars {missing_config}",
                RESULTS_QUEUE_URL,
                dedup_id=f"dispatch-misconfig-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        # Concurrency cap gate — before routing/mint so a deferred message
        # does no work and leaks no broker token. Fail open on count errors:
        # an ECS API hiccup or IAM regression must not wedge all dispatch.
        if MAX_CONCURRENT_AGENTS > 0:
            try:
                active = _count_active_agent_tasks()
            except Exception as e:
                logger.warning(
                    f"Concurrency cap check failed ({type(e).__name__}: {e}); "
                    f"dispatching run {message['run_id']} without cap enforcement"
                )
                active = -1
            if active >= MAX_CONCURRENT_AGENTS:
                _defer_for_concurrency_cap(record, message, active)
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
        prior_artifact_versions = message.get("prior_artifact_versions")
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
            )
        except BrokerError as e:
            logger.warning(f"Broker rejected {experiment_id} (run={message['run_id']}): {e.status_code} {e.detail}")
            emit_dispatch_metric("BrokerRejected", experiment_id)
            sent = send_error_callback(
                message,
                e.user_safe_message or "Broker rejected the request",
                RESULTS_QUEUE_URL,
                dedup_id=f"broker-rejected-{message['run_id']}",
            )
            if not sent:
                batch_item_failures.append({"itemIdentifier": message_id})
            continue
        except httpx.HTTPError as e:
            logger.warning(f"Transient network error during mint for run {message.get('run_id')}: {e}")
            emit_dispatch_metric("MintTransient", experiment_id)
            batch_item_failures.append({"itemIdentifier": message_id})
            continue
        except Exception as e:
            logger.exception(f"Unexpected error during mint for run {message.get('run_id')}: {e}")
            emit_dispatch_metric("MintUnexpected", experiment_id)
            send_error_callback(
                message,
                f"Unexpected dispatch error: {type(e).__name__}",
                RESULTS_QUEUE_URL,
                dedup_id=f"mint-exception-{message.get('run_id', 'unknown')}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        overrides = build_container_overrides(
            experiment=experiment,
            message=message,
            broker_token=mint_result["broker_token"],
            broker_url=BROKER_URL,
            container_name=CONTAINER_NAME,
            params_json=params_json,
        )

        logger.info(
            f"Dispatching experiment '{experiment_id}' for organization '{message['organization_slug']}' (run: {message['run_id']})"
        )

        minted_broker_token = mint_result["broker_token"]

        try:
            response = get_ecs_client().run_task(
                cluster=ECS_CLUSTER_ARN,
                taskDefinition=ECS_TASK_DEFINITION,
                launchType="FARGATE",
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
                    f"ECS RunTask failed for {message_id} "
                    f"(experiment={experiment_id}, run={message['run_id']}): "
                    f"{failure_reasons}"
                )
                _cleanup_minted_token(broker, minted_broker_token, message["run_id"])
                safe_summary = _classify_ecs_failure_reasons(failure_reasons)
                kind = _classify_ecs_failure_kind(failure_reasons)
                emit_dispatch_metric(f"ECSRunTaskFailed_{kind}", experiment_id)
                send_error_callback(
                    message,
                    f"ECS RunTask failed: {safe_summary}",
                    RESULTS_QUEUE_URL,
                    dedup_id=f"runtask-failed-{message['run_id']}",
                )
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            task_arn = tasks[0]["taskArn"]
            logger.info(f"Started Fargate task: {task_arn}")

        except Exception as e:
            logger.exception(
                f"ECS RunTask exception for {message_id} "
                f"(experiment={experiment_id}, run={message['run_id']}, "
                f"exception_type={type(e).__name__}): {e}"
            )
            emit_dispatch_metric("ECSRunTaskException", experiment_id)
            _cleanup_minted_token(broker, minted_broker_token, message["run_id"])
            send_error_callback(
                message,
                f"ECS RunTask exception: {type(e).__name__}",
                RESULTS_QUEUE_URL,
                dedup_id=f"runtask-exception-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})

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
