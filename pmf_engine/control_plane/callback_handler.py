from __future__ import annotations

import json
import os

import boto3
from botocore.exceptions import ClientError

try:
    from shared.logger import get_logger
    logger = get_logger(__name__)
except (ImportError, OSError):
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

_s3_client = None
_sqs_client = None
_cloudwatch_client = None

RESULTS_QUEUE_URL = os.environ.get("RESULTS_QUEUE_URL", "")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
GOD_QUEUE_MESSAGE_GROUP_ID = "gp-queue-agentExperiments"

if not ARTIFACT_BUCKET:
    raise RuntimeError(
        "ARTIFACT_BUCKET environment variable is required but was empty or unset. "
        "Refusing to load callback_handler with an unvalidated artifact bucket."
    )


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def get_cloudwatch_client():
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = boto3.client("cloudwatch")
    return _cloudwatch_client


def _emit_bucket_mismatch_metric(experiment_id: str) -> None:
    try:
        get_cloudwatch_client().put_metric_data(
            Namespace="PMFEngine/Callback",
            MetricData=[
                {
                    "MetricName": "BucketMismatch",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Environment", "Value": ENVIRONMENT},
                        {"Name": "ExperimentId", "Value": experiment_id},
                    ],
                }
            ],
        )
    except Exception:
        logger.exception("Failed to emit BucketMismatch metric")


def parse_callback_message(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Invalid message body: {e}")

    for field in ("experiment_id", "run_id", "candidate_id", "status"):
        if not data.get(field):
            raise ValueError(f"Missing required field: {field}")

    return data


def artifact_exists(s3_client, bucket: str, key: str) -> bool:
    if not key or not bucket:
        return False

    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def format_gp_api_result_message(message: dict) -> dict:
    data = {
        "experimentId": message["experiment_id"],
        "runId": message["run_id"],
        "candidateId": message["candidate_id"],
        "status": message["status"],
    }

    if message.get("artifact_key"):
        data["artifactKey"] = message["artifact_key"]
    if message.get("artifact_bucket"):
        data["artifactBucket"] = message["artifact_bucket"]
    if message.get("duration_seconds") is not None:
        data["durationSeconds"] = message["duration_seconds"]
    if message.get("error"):
        data["error"] = message["error"]

    return {"type": "agentExperimentResult", "data": data}


def handler(event: dict, context) -> dict:
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        body = record.get("body", "")

        try:
            message = parse_callback_message(body)
        except ValueError as e:
            logger.error(f"Invalid callback message {message_id}: {e} | body={body[:500]}")
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        status = message["status"]
        experiment_id = message["experiment_id"]
        run_id = message["run_id"]
        candidate_id = message["candidate_id"]

        if status == "success":
            artifact_key = message.get("artifact_key", "")
            artifact_bucket = message.get("artifact_bucket", "")

            if artifact_bucket and artifact_bucket != ARTIFACT_BUCKET:
                logger.error(
                    f"Unexpected artifact bucket for experiment={experiment_id} "
                    f"run={run_id} candidate={candidate_id} message_id={message_id}: "
                    f"got={artifact_bucket} expected={ARTIFACT_BUCKET}"
                )
                _emit_bucket_mismatch_metric(experiment_id)
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            try:
                exists = artifact_exists(
                    s3_client=get_s3_client(),
                    bucket=artifact_bucket,
                    key=artifact_key,
                )
            except Exception:
                logger.exception(
                    f"Error checking artifact for experiment={experiment_id} "
                    f"run={run_id} candidate={candidate_id} message_id={message_id} "
                    f"bucket={artifact_bucket} key={artifact_key}"
                )
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            if not exists:
                logger.error(
                    f"Artifact missing from S3 for experiment={experiment_id} "
                    f"run={run_id} candidate={candidate_id}: "
                    f"s3://{artifact_bucket}/{artifact_key}"
                )
                message["status"] = "contract_violation"
                message["error"] = f"Artifact missing from S3 at s3://{artifact_bucket}/{artifact_key}"

        envelope = format_gp_api_result_message(message)

        logger.info(f"Forwarding {message['status']} result for experiment={experiment_id} run={run_id} candidate={candidate_id}")

        try:
            get_sqs_client().send_message(
                QueueUrl=RESULTS_QUEUE_URL,
                MessageBody=json.dumps(envelope),
                MessageGroupId=GOD_QUEUE_MESSAGE_GROUP_ID,
                MessageDeduplicationId=f"{run_id}-result",
            )
        except Exception:
            logger.exception(
                f"Failed to forward result for experiment={experiment_id} "
                f"run={run_id} candidate={candidate_id} message_id={message_id}"
            )
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
