from __future__ import annotations

import json
import os

import boto3

try:
    from shared.logger import get_logger
    logger = get_logger(__name__)
except (ImportError, OSError):
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    from .dispatch_registry import DISPATCH_REGISTRY
    from .param_screening import screen_params
except ImportError:
    from dispatch_registry import DISPATCH_REGISTRY
    from param_screening import screen_params

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


def emit_screening_rejected_metric(experiment_id: str, candidate_id: str, reason: str):
    _emit_metric("ParamScreeningRejected", [
        {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
        {"Name": "ExperimentId", "Value": experiment_id},
        {"Name": "Reason", "Value": reason},
    ])


def send_error_callback(
    message: dict,
    error: str,
    callback_queue_url: str,
    dedup_id: str | None = None,
):
    if not callback_queue_url:
        return
    try:
        import uuid
        body = json.dumps({
            "experiment_id": message.get("experiment_id", "unknown"),
            "run_id": message.get("run_id", "unknown"),
            "candidate_id": message.get("candidate_id", "unknown"),
            "status": "failed",
            "error": error,
        })
        get_sqs_client().send_message(
            QueueUrl=callback_queue_url,
            MessageBody=body,
            MessageGroupId=f"callback-{message.get('candidate_id', 'unknown')}",
            MessageDeduplicationId=dedup_id or str(uuid.uuid4()),
        )
        logger.info(f"Sent error callback for run {message.get('run_id')}: {error}")
    except Exception as e:
        logger.exception(f"Failed to send error callback: {e}")

ECS_CLUSTER_ARN = os.environ.get("ECS_CLUSTER_ARN", "")
ECS_TASK_DEFINITION = os.environ.get("ECS_TASK_DEFINITION", "")
ECS_SUBNET_IDS = [s for s in os.environ.get("ECS_SUBNET_IDS", "").split(",") if s]
ECS_SECURITY_GROUP_ID = os.environ.get("ECS_SECURITY_GROUP_ID", "")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "")
CALLBACK_QUEUE_URL = os.environ.get("CALLBACK_QUEUE_URL", "")
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
    if not CALLBACK_QUEUE_URL:
        missing.append("CALLBACK_QUEUE_URL")
    if not ARTIFACT_BUCKET:
        missing.append("ARTIFACT_BUCKET")
    return missing


MAX_PARAMS_JSON_BYTES = 6000
SCREENER_INFRA_REASONS = (
    "screener_not_configured",
    "screener_invalid_response",
)


def parse_dispatch_message(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Invalid message body: {e}")

    for field in ("experiment_id", "candidate_id", "run_id"):
        if not data.get(field):
            raise ValueError(f"Missing required field: {field}")

    if data.get("params") is None:
        data["params"] = {}
    return data


def build_container_overrides(
    experiment: dict,
    message: dict,
    artifact_bucket: str,
    callback_queue_url: str,
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
                    {"name": "EXPERIMENT_ID", "value": message["experiment_id"]},
                    {"name": "RUN_ID", "value": message["run_id"]},
                    {"name": "CANDIDATE_ID", "value": message["candidate_id"]},
                    {"name": "HARNESS", "value": experiment["harness"]},
                    {"name": "AGENT_MODEL", "value": experiment["model"]},
                    {"name": "ARTIFACT_BUCKET", "value": artifact_bucket},
                    {"name": "ARTIFACT_KEY_TEMPLATE", "value": experiment["contract"]["s3_key_template"]},
                    {"name": "CALLBACK_QUEUE_URL", "value": callback_queue_url},
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
                CALLBACK_QUEUE_URL,
                dedup_id=f"dispatch-misconfig-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        experiment_id = message["experiment_id"]
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
                CALLBACK_QUEUE_URL,
                dedup_id=f"unknown-experiment-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        if not isinstance(message["params"], dict):
            type_name = type(message["params"]).__name__
            logger.error(
                f"Invalid params type for {experiment_id} "
                f"(run: {message['run_id']}, candidate: {message['candidate_id']}): "
                f"got {type_name}, expected object"
            )
            emit_screening_rejected_metric(
                experiment_id, message["candidate_id"], "invalid_params_type"
            )
            send_error_callback(
                message,
                f"params must be a JSON object, got {type_name}",
                CALLBACK_QUEUE_URL,
                dedup_id=f"invalid-params-type-{message['run_id']}",
            )
            continue

        params_json = json.dumps(message["params"])
        params_bytes = len(params_json.encode("utf-8"))
        if params_bytes > MAX_PARAMS_JSON_BYTES:
            logger.error(
                f"Params too large for {experiment_id} "
                f"(run: {message['run_id']}, candidate: {message['candidate_id']}): "
                f"{params_bytes} bytes > {MAX_PARAMS_JSON_BYTES}"
            )
            emit_screening_rejected_metric(
                experiment_id, message["candidate_id"], "params_too_large"
            )
            send_error_callback(
                message,
                f"Experiment parameters exceed size limit ({params_bytes} > {MAX_PARAMS_JSON_BYTES} bytes)",
                CALLBACK_QUEUE_URL,
                dedup_id=f"params-too-large-{message['run_id']}",
            )
            continue

        screening = screen_params(message["params"])
        if not screening.safe:
            reason = screening.reason or "unknown"
            is_infra_failure = (
                reason.startswith("screener_unavailable") or reason in SCREENER_INFRA_REASONS
            )
            if is_infra_failure:
                logger.error(
                    f"Param screener unavailable for {experiment_id} "
                    f"(run: {message['run_id']}, candidate: {message['candidate_id']}, "
                    f"reason: {reason}) — retrying via SQS"
                )
                emit_screening_rejected_metric(experiment_id, message["candidate_id"], reason)
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            logger.warning(
                f"Param screening rejected for {experiment_id} "
                f"(candidate: {message['candidate_id']}, run: {message['run_id']}, "
                f"reason: {reason}, key: {screening.flagged_key})"
            )
            emit_screening_rejected_metric(experiment_id, message["candidate_id"], reason)
            send_error_callback(
                message,
                "Invalid experiment parameters",
                CALLBACK_QUEUE_URL,
                dedup_id=f"screening-rejected-{message['run_id']}",
            )
            continue

        overrides = build_container_overrides(
            experiment=experiment,
            message=message,
            artifact_bucket=ARTIFACT_BUCKET,
            callback_queue_url=CALLBACK_QUEUE_URL,
            container_name=CONTAINER_NAME,
            params_json=params_json,
        )

        logger.info(f"Dispatching experiment '{experiment_id}' for candidate '{message['candidate_id']}' (run: {message['run_id']})")

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
                logger.error(f"ECS RunTask failed for {message_id}: {failure_reasons}")
                send_error_callback(
                    message,
                    f"ECS RunTask failed: {failure_reasons}",
                    CALLBACK_QUEUE_URL,
                    dedup_id=f"runtask-failed-{message['run_id']}",
                )
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            task_arn = tasks[0]["taskArn"]
            logger.info(f"Started Fargate task: {task_arn}")

        except Exception as e:
            logger.exception(f"ECS RunTask exception for {message_id}: {e}")
            send_error_callback(
                message,
                f"ECS RunTask exception: {e}",
                CALLBACK_QUEUE_URL,
                dedup_id=f"runtask-exception-{message['run_id']}",
            )
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
