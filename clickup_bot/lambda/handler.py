import hashlib
import hmac
import json
import os
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import boto3


CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"
_secrets_cache = None


def get_secrets() -> dict:
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    environment = os.environ.get("ENVIRONMENT", "prod").upper()
    secret_id = f"AI_SECRETS_{environment}"

    try:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_id)
        _secrets_cache = json.loads(response["SecretString"])
        print(f"Loaded secrets from {secret_id}")
    except Exception as e:
        print(f"Failed to load secrets from {secret_id}: {e}")
        _secrets_cache = {}

    return _secrets_cache


def get_clickup_api_key() -> str:
    return get_secrets().get("CLICKUP_API_KEY", "")


def get_webhook_secret() -> str:
    return get_secrets().get("CLICKUP_WEBHOOK_SECRET", "")


def verify_webhook_signature(body: str, signature: str) -> bool:
    secret = get_webhook_secret()
    if not secret:
        print("ERROR: No CLICKUP_WEBHOOK_SECRET configured, rejecting request")
        return False

    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    is_valid = hmac.compare_digest(expected, signature)

    if not is_valid:
        print(f"Signature mismatch: expected={expected}, got={signature}")

    return is_valid

BOT_PREFIX = "[GP-Bot]"

ANALYZE_INSTRUCTION = """## YOUR TASK: Analyze and Report

**Approach this ticket with healthy skepticism.** It may be out of date - the issue
could have been fixed, the data may have changed, the description may be incomplete,
or the reporter may have been incorrect.

Post your analysis to ClickUp when done. Be concise.
"""

IMPLEMENT_INSTRUCTION = """## YOUR TASK: Implement and Create PR

**Approach this ticket with healthy skepticism.** It may be out of date - the issue
could have been fixed, the data may have changed, the description may be incomplete,
or the reporter may have been incorrect.

**BEFORE writing any code**, you MUST:
1. Find all files that use/import the function or component you plan to modify
2. Read each of those files to understand how they depend on it
3. Consider if your change will break any of those usages

If you're unsure about the solution or the impact is too broad, post a comment explaining
your findings and recommend a human handle the implementation.

Branch naming: `<custom_id>/gp-bot_<description-slug>` (use the task's custom_id like ENG-1234, not the internal ID)
PR title format: `[GP-Bot] <description>`

Post the PR link to ClickUp when done.
"""

TAG_CONFIG = {
    "gpbot-analyze": {"instruction": ANALYZE_INSTRUCTION, "label": "analyze", "model": "opus"},
    "gpbot-work": {"instruction": IMPLEMENT_INSTRUCTION, "label": "implement", "model": "opus"},
}


def clickup_request(method: str, endpoint: str, data: dict | None = None) -> dict:
    url = f"{CLICKUP_BASE_URL}{endpoint}"
    headers = {
        "Authorization": get_clickup_api_key(),
        "Content-Type": "application/json",
    }

    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)

    with urlopen(req) as response:
        return json.loads(response.read().decode())


def get_task_comments(task_id: str) -> list[dict]:
    result = clickup_request("GET", f"/task/{task_id}/comment")
    return result.get("comments", [])


def has_bot_comment(comments: list[dict]) -> bool:
    for comment in comments:
        comment_text = ""
        for item in comment.get("comment", []):
            if item.get("type") == "text":
                comment_text += item.get("text", "")
        if comment_text.startswith(BOT_PREFIX):
            return True
    return False


def post_comment(task_id: str, text: str) -> None:
    clickup_request("POST", f"/task/{task_id}/comment", {
        "comment_text": text,
        "notify_all": False,
    })


def handler(event: dict, context: Any) -> dict:
    print(f"Received webhook event: {json.dumps(event)}")

    headers = event.get("headers", {})
    signature = headers.get("x-signature", "")
    raw_body = event.get("body", "{}")

    if not verify_webhook_signature(raw_body, signature):
        print("Webhook signature verification failed")
        return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

    body = raw_body
    if isinstance(body, str):
        body = json.loads(body)

    event_type = body.get("event")
    task_id = body.get("task_id")

    if event_type != "taskTagUpdated":
        print(f"Skipping event type: {event_type}")
        return {"statusCode": 200, "body": json.dumps({"skipped": "not a tag update"})}

    if not task_id:
        print("Missing task_id in webhook payload")
        return {"statusCode": 400, "body": json.dumps({"error": "missing task_id"})}

    history_items = body.get("history_items", [])
    matched_tag = None
    for item in history_items:
        if item.get("field") == "tag" and item.get("after"):
            after_tags = item["after"]
            if isinstance(after_tags, list):
                for tag in after_tags:
                    tag_name = tag.get("name", "").lower()
                    if tag_name in TAG_CONFIG:
                        matched_tag = tag_name
                        break
            if matched_tag:
                break

    if not matched_tag:
        print(f"No target tag found in history_items: {history_items}")
        return {"statusCode": 200, "body": json.dumps({"skipped": "not a target tag"})}

    try:
        comments = get_task_comments(task_id)
    except HTTPError as e:
        print(f"Failed to get comments for task {task_id}: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": "failed to get comments"})}

    if has_bot_comment(comments):
        print(f"Task {task_id} already has [GP-Bot] comment, skipping")
        return {"statusCode": 200, "body": json.dumps({"skipped": "already processed"})}

    config = TAG_CONFIG[matched_tag]
    instruction = config["instruction"].format(task_id=task_id)

    if os.environ.get("ENABLE_FARGATE") == "true":
        return trigger_fargate_task(task_id, instruction, config["label"], config["model"])

    handoff_data = {
        "task_id": task_id,
        "tag": matched_tag,
        "label": config["label"],
        "instruction_preview": instruction[:500] + "..." if len(instruction) > 500 else instruction,
        "instruction_length": len(instruction),
    }
    print(f"HANDOFF_DATA: {json.dumps(handoff_data, indent=2)}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "logged",
            "task_id": task_id,
            "tag": matched_tag,
            "label": config["label"],
            "message": "Handoff data logged to CloudWatch (Fargate trigger not enabled)"
        })
    }


def trigger_fargate_task(task_id: str, instruction: str, label: str, model: str = "sonnet") -> dict:
    ecs_client = boto3.client("ecs")

    cluster_arn = os.environ.get("ECS_CLUSTER_ARN")
    task_definition = os.environ.get("ECS_TASK_DEFINITION")
    subnet_ids = os.environ.get("ECS_SUBNET_IDS", "").split(",")
    security_group_id = os.environ.get("ECS_SECURITY_GROUP_ID")

    if not all([cluster_arn, task_definition, subnet_ids, security_group_id]):
        print("ERROR: Missing ECS configuration")
        return {"statusCode": 500, "body": json.dumps({"error": "missing ECS configuration"})}

    print(f"Triggering Fargate task for {task_id} with model={model}, label={label}")

    try:
        post_comment(task_id, f"{BOT_PREFIX} Processing started ({label}, model: {model})...")
    except HTTPError as e:
        print(f"Failed to post starting comment: {e}")

    try:
        response = ecs_client.run_task(
            cluster=cluster_arn,
            taskDefinition=task_definition,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnet_ids,
                    "securityGroups": [security_group_id],
                    "assignPublicIp": "DISABLED"
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "engineer-agent",
                        "environment": [
                            {"name": "CLICKUP_TASK_ID", "value": task_id},
                            {"name": "INSTRUCTION", "value": instruction},
                            {"name": "AGENT_MODEL", "value": model},
                        ]
                    }
                ]
            }
        )

        task_arn = response["tasks"][0]["taskArn"] if response.get("tasks") else "unknown"
        print(f"Started Fargate task: {task_arn}")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "triggered",
                "task_id": task_id,
                "label": label,
                "fargate_task_arn": task_arn
            })
        }

    except Exception as e:
        print(f"Failed to start Fargate task: {e}")
        try:
            post_comment(task_id, f"{BOT_PREFIX} Failed to start processing: {str(e)}")
        except HTTPError:
            pass
        return {"statusCode": 500, "body": json.dumps({"error": f"failed to start task: {str(e)}"})}

