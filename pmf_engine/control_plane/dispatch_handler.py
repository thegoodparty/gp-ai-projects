from __future__ import annotations

import json
import os

import boto3
import httpx

try:
    from shared.logger import get_logger
    logger = get_logger(__name__)
except (ImportError, OSError):
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    from .dispatch_registry import DISPATCH_REGISTRY
    from .broker_client import BrokerClient, BrokerError
    from .scope_derivation import derive_scope
except ImportError:
    from dispatch_registry import DISPATCH_REGISTRY
    from broker_client import BrokerClient, BrokerError
    from scope_derivation import derive_scope

_ecs_client = None
_sqs_client = None
_cw_client = None


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
        logger.warning(f"Failed to emit metric {metric_name}: {e}")


def emit_dispatch_metric(metric_name: str, experiment_id: str):
    _emit_metric(metric_name, [
        {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
        {"Name": "ExperimentId", "Value": experiment_id},
    ])


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
        body = json.dumps({
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
        })
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
    return missing


MAX_PARAMS_JSON_BYTES = 6000

import re

_PRIOR_ARTIFACT_VALUE_RE = re.compile(
    r"^[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/artifact\.json$"
)


_PRIOR_ARTIFACT_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_prior_artifact_versions(versions) -> None:
    if versions is None:
        return
    if not isinstance(versions, dict):
        raise ValueError(
            f"prior_artifact_versions must be an object, got {type(versions).__name__}"
        )
    for key, value in versions.items():
        if not isinstance(key, str) or not _PRIOR_ARTIFACT_KEY_RE.fullmatch(key):
            raise ValueError(
                f"prior_artifact_versions key must match "
                f"[A-Za-z0-9_-]{{1,64}}: got {key!r}"
            )
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
        raise ValueError(f"Invalid message body: {e}")

    for field in ("experiment_type", "organization_slug", "run_id"):
        if not data.get(field):
            raise ValueError(f"Missing required field: {field}")

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
    return {
        "containerOverrides": [
            {
                "name": container_name,
                "environment": [
                    {"name": "EXPERIMENT_ID", "value": message["experiment_type"]},
                    {"name": "RUN_ID", "value": message["run_id"]},
                    {"name": "ORGANIZATION_SLUG", "value": message["organization_slug"]},
                    {"name": "HARNESS", "value": experiment["harness"]},
                    {"name": "AGENT_MODEL", "value": experiment["model"]},
                    {"name": "BROKER_TOKEN", "value": broker_token},
                    {"name": "BROKER_URL", "value": broker_url},
                    {"name": "ANTHROPIC_BASE_URL", "value": f"{broker_url}/anthropic"},
                    {"name": "ANTHROPIC_API_KEY", "value": broker_token},
                    {"name": "PARAMS_JSON", "value": params_json},
                    {"name": "TIMEOUT_SECONDS", "value": str(experiment.get("timeout_seconds", 600))},
                ],
            }
        ]
    }


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

        experiment_id = message["experiment_type"]
        experiment = DISPATCH_REGISTRY.get(experiment_id)

        if experiment is None:
            known_ids = sorted(DISPATCH_REGISTRY.keys())
            logger.error(
                f"Unknown experiment '{experiment_id}' in message {message_id}. "
                f"Known experiments: {known_ids}"
            )
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

        required = experiment.get("required_params", [])
        missing = [p for p in required if not message["params"].get(p)]
        if missing:
            logger.error(
                f"Missing required params for {experiment_id} "
                f"(run: {message['run_id']}, organization: {message['organization_slug']}): "
                f"{missing}"
            )
            emit_dispatch_metric("MissingRequiredParams", experiment_id)
            sent = send_error_callback(
                message,
                f"Missing required params for {experiment_id}: {missing}",
                RESULTS_QUEUE_URL,
                dedup_id=f"missing-params-{message['run_id']}",
            )
            if not sent:
                batch_item_failures.append({"itemIdentifier": message_id})
            continue

        scope = derive_scope(experiment_id, message["params"])
        prior_artifact_versions = message.get("prior_artifact_versions")
        try:
            broker = BrokerClient(BROKER_URL, SERVICE_TOKEN)
            mint_result = broker.mint_run_token(
                run_id=message["run_id"],
                organization_slug=message["organization_slug"],
                experiment_id=experiment_id,
                scope=scope,
                params=message["params"],
                exp_ttl_seconds=experiment.get("timeout_seconds", 3600) + 300,
                prior_artifact_versions=prior_artifact_versions,
            )
        except BrokerError as e:
            logger.warning(f"Broker rejected {experiment_id} (run={message['run_id']}): {e.status_code} {e.detail}")
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
            logger.warning(
                f"Transient network error during mint for run {message.get('run_id')}: {e}"
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue
        except Exception as e:
            logger.exception(
                f"Unexpected error during mint for run {message.get('run_id')}: {e}"
            )
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

        logger.info(f"Dispatching experiment '{experiment_id}' for organization '{message['organization_slug']}' (run: {message['run_id']})")

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
            _cleanup_minted_token(broker, minted_broker_token, message["run_id"])
            send_error_callback(
                message,
                f"ECS RunTask exception: {type(e).__name__}",
                RESULTS_QUEUE_URL,
                dedup_id=f"runtask-exception-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


def _classify_ecs_failure_reasons(reasons: list[str]) -> str:
    joined_upper = " ".join(str(r).upper() for r in reasons)
    if "CAPACITY" in joined_upper or "RESOURCE:" in joined_upper:
        return "capacity exhausted (see server logs for detail)"
    if "ACCESSDENIED" in joined_upper or "NOT AUTHORIZED" in joined_upper or "IAM" in joined_upper:
        return "permission error (see server logs for detail)"
    if "THROTTL" in joined_upper:
        return "throttled by AWS (see server logs for detail)"
    return "capacity or permission error (see server logs for detail)"


def _cleanup_minted_token(broker, broker_token: str, run_id: str) -> None:
    try:
        broker.delete_run_token(broker_token=broker_token, run_id=run_id)
    except Exception as e:
        logger.warning(
            f"Failed to delete run-token for run {run_id} after ECS failure: "
            f"{type(e).__name__}: {e}. Ticket + run-lock will expire via TTL."
        )
