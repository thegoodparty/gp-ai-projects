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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from typing import TypedDict, Optional

from shared.logger import get_logger
from campaign_plan_lambda.output import CampaignPlanResult

logger = get_logger(__name__)


class SqsAttributes(TypedDict, total=False):
    ApproximateReceiveCount: str


class SqsRecord(TypedDict):
    body: str
    messageId: str
    attributes: SqsAttributes


class LambdaEvent(TypedDict):
    Records: list[SqsRecord]


class Secrets(BaseModel):
    GEMINI_API_KEY: str
    BRAINTRUST_API_KEY: Optional[str] = None


_secrets_cache: Optional[Secrets] = None
MAX_RECEIVE_COUNT = 3


class SqsMessageBody(BaseModel):
    # populate_by_name lets the `electionDate` field accept either alias below.
    # extra="ignore" means gp-api can add new fields without breaking the Lambda.
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    campaignId: int
    electionDate: str = Field(
        validation_alias=AliasChoices("electionDate", "election_date"),
    )
    state: Optional[str] = None
    city: Optional[str] = None
    officeName: Optional[str] = None
    officeLevel: Optional[str] = None
    primaryElectionDate: Optional[str] = None

    # Blank optional strings from gp-api collapse to None so the "not available"
    # fallback fires uniformly downstream. Whitespace-only strings count as
    # blank. electionDate is excluded — it's required, so blank input is a real
    # validation error.
    @field_validator(
        "state", "city", "officeName", "officeLevel", "primaryElectionDate",
        mode="before",
    )
    @classmethod
    def _blank_string_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("electionDate")
    @classmethod
    def validate_election_date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @field_validator("primaryElectionDate")
    @classmethod
    def validate_primary_election_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        date.fromisoformat(v)
        return v


# Throws an error via Pydantic if a key is missing.
def _load_secrets() -> Secrets:
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    import boto3

    environment = os.environ.get("ENVIRONMENT", "dev").upper()
    secret_id = f"AI_SECRETS_{environment}"

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    _secrets_cache = Secrets(**json.loads(response["SecretString"]))
    logger.info(f"Loaded secrets from {secret_id}")
    return _secrets_cache


def _inject_secrets() -> None:
    secrets = _load_secrets()
    os.environ["GEMINI_API_KEY"] = secrets.GEMINI_API_KEY
    if secrets.BRAINTRUST_API_KEY:
        os.environ["BRAINTRUST_API_KEY"] = secrets.BRAINTRUST_API_KEY


def handler(event: LambdaEvent, context=None) -> None:
    _inject_secrets()

    from shared.braintrust import init_braintrust
    init_braintrust(project="campaign-plan")

    for record in event.get("Records", []):
        receive_count = int(
            record.get("attributes", {}).get("ApproximateReceiveCount", "1")
        )

        try:
            message_body = SqsMessageBody(**json.loads(record["body"]))
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error(f"Invalid SQS message: {e}", exc_info=True)
            # On final retry, try to notify gp-api if we can extract a campaignId
            if receive_count >= MAX_RECEIVE_COUNT:
                try:
                    raw = json.loads(record.get("body", "{}"))
                    if isinstance(raw, dict) and "campaignId" in raw:
                        from campaign_plan_lambda.output import send_error_message
                        send_error_message(int(raw["campaignId"]), "Invalid message format")
                except Exception as notify_err:
                    logger.warning(f"Failed to send error notification: {notify_err}")
            # Raise so the message goes to DLQ and Slack alarm fires
            raise

        campaign_id = message_body.campaignId

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


async def _generate(campaign_id: int, msg: SqsMessageBody) -> CampaignPlanResult:
    from campaign_plan_lambda.event_generator import CampaignContext, generate_event_tasks
    from campaign_plan_lambda.output import write_result_to_s3, send_completion_message

    ctx = CampaignContext(
        electionDate=msg.electionDate,
        state=msg.state,
        city=msg.city,
        officeName=msg.officeName,
        officeLevel=msg.officeLevel,
        primaryElectionDate=msg.primaryElectionDate,
    )
    event_tasks = await generate_event_tasks(ctx)
    logger.info(f"Generated {len(event_tasks)} event tasks")

    generation_timestamp = datetime.now(timezone.utc).isoformat()

    from campaign_plan_lambda.output import TaskDict

    task_dicts: list[TaskDict] = []
    for task in event_tasks:
        d: TaskDict = {
            "title": task.title,
            "description": task.description,
            "cta": task.cta,
            "flowType": task.flowType,
            "week": task.week,
            "date": task.date,
        }
        if task.url:
            d["url"] = task.url
        task_dicts.append(d)

    result: CampaignPlanResult = {
        "campaignId": campaign_id,
        "tasks": task_dicts,
        "taskCount": len(event_tasks),
        "generationTimestamp": generation_timestamp,
    }

    s3_key = write_result_to_s3(campaign_id, result)
    send_completion_message(campaign_id, s3_key, len(event_tasks), generation_timestamp)

    return result
