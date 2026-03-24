"""
AWS Lambda handler for campaign event task generation.

Triggered by SQS FIFO queue. Finds community events via Google Search,
writes results to S3, and sends a completion message to gp-api's SQS queue.
"""

import asyncio
import json
import os
import time
from datetime import date, datetime, timezone

from pydantic import BaseModel, ValidationError

from shared.logger import get_logger

logger = get_logger(__name__)

_secrets_cache = None
MAX_RECEIVE_COUNT = 3


class SqsMessageBody(BaseModel):
    campaignId: int
    election_date: str
    city: str
    state: str


def _load_secrets():
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    import boto3

    environment = os.environ.get("ENVIRONMENT", "dev").upper()
    secret_id = f"AI_SECRETS_{environment}"

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    _secrets_cache = json.loads(response["SecretString"])
    logger.info(f"Loaded secrets from {secret_id}")
    return _secrets_cache


def _inject_secrets():
    secrets = _load_secrets()
    os.environ["GEMINI_API_KEY"] = secrets.get("GEMINI_API_KEY", "")


def handler(event, context):
    _inject_secrets()
    os.environ.setdefault("ENVIRONMENT", "dev")

    for record in event.get("Records", []):
        try:
            message_body = SqsMessageBody(**json.loads(record["body"]))
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error(f"Invalid SQS message: {e}", exc_info=True)
            continue

        campaign_id = message_body.campaignId
        receive_count = int(
            record.get("attributes", {}).get("ApproximateReceiveCount", "1")
        )

        logger.info(f"Processing campaign {campaign_id} (attempt {receive_count}/{MAX_RECEIVE_COUNT})")
        start_time = time.time()

        try:
            result = asyncio.run(_generate(campaign_id, message_body))
            elapsed = time.time() - start_time
            logger.info(f"Campaign {campaign_id} completed in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"Campaign {campaign_id} failed after {elapsed:.1f}s: {e}",
                exc_info=True,
            )

            if receive_count >= MAX_RECEIVE_COUNT:
                from campaign_plan_lambda.output import send_error_message

                try:
                    send_error_message(campaign_id, "Campaign plan generation failed")
                except Exception as send_err:
                    logger.error(f"Failed to send error message for campaign {campaign_id}: {send_err}")

            raise


async def _generate(campaign_id: int, msg: SqsMessageBody):
    from campaign_plan_lambda.event_generator import generate_event_tasks
    from campaign_plan_lambda.output import write_result_to_s3, send_completion_message

    event_tasks = await generate_event_tasks(
        election_date=date.fromisoformat(msg.election_date),
        city=msg.city,
        state=msg.state,
    )
    logger.info(f"Generated {len(event_tasks)} event tasks")

    generation_timestamp = datetime.now(timezone.utc).isoformat()

    result = {
        "campaignId": campaign_id,
        "tasks": event_tasks,
        "taskCount": len(event_tasks),
        "generationTimestamp": generation_timestamp,
    }

    s3_key = write_result_to_s3(campaign_id, result)
    send_completion_message(campaign_id, s3_key, len(event_tasks), generation_timestamp)

    return result
