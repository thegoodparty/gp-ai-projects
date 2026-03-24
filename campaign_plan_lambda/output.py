"""
S3 result storage and SQS completion messaging for campaign plan Lambda.

Writes result JSON to S3 and sends completion/error messages to gp-api's
existing SQS FIFO queue.
"""

import json
import os
import uuid
from datetime import datetime, timezone

from shared.logger import get_logger

logger = get_logger(__name__)

_s3_client = None
_sqs_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client("s3")
    return _s3_client


def _get_sqs():
    global _sqs_client
    if _sqs_client is None:
        import boto3

        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _send_to_sqs(campaign_id: int, message: dict):
    """Send a message to gp-api's SQS FIFO queue."""
    queue_url = os.environ["OUTPUT_SQS_QUEUE_URL"]
    _get_sqs().send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(message),
        MessageGroupId=f"gp-queue-campaign-plan-{campaign_id}",
        MessageDeduplicationId=str(uuid.uuid4()),
    )


def write_result_to_s3(campaign_id: int, result: dict) -> str:
    """
    Write campaign plan result JSON to S3.

    Returns the S3 key for the stored object.
    """
    bucket = os.environ["S3_RESULTS_BUCKET"]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    unique_id = uuid.uuid4().hex[:8]
    s3_key = f"results/{campaign_id}/{timestamp}-{unique_id}.json"

    _get_s3().put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=json.dumps(result, default=str),
        ContentType="application/json",
    )

    logger.info(f"Wrote result to s3://{bucket}/{s3_key}")
    return s3_key


def send_completion_message(
    campaign_id: int, s3_key: str, task_count: int, timestamp: str
):
    """Send success completion message to gp-api's SQS FIFO queue."""
    message = {
        "type": "campaignPlanComplete",
        "data": {
            "campaignId": campaign_id,
            "status": "completed",
            "s3Key": s3_key,
            "taskCount": task_count,
            "generationTimestamp": timestamp,
        },
    }

    _send_to_sqs(campaign_id, message)
    logger.info(f"Sent completion message for campaign {campaign_id}")


def send_error_message(campaign_id: int, error: str):
    """Send error message to gp-api's SQS FIFO queue."""
    message = {
        "type": "campaignPlanComplete",
        "data": {
            "campaignId": campaign_id,
            "status": "error",
            "error": error,
        },
    }

    _send_to_sqs(campaign_id, message)
    logger.warning(f"Sent error message for campaign {campaign_id}")
