"""Discover handler — long-running Fargate service that consumes the discover SQS queue.

Runs as an ECS service with desired_count=1: long-polls forever, processes cities
as messages arrive, never exits on its own. Producers (e.g. an external API) just
SendMessage to the queue and this service picks them up.

Usage (local): python -m meeting_pipeline.lambda_handlers.discover
Usage (Fargate): ENTRYPOINT in Dockerfile.discover
"""

import asyncio
import json
import os
import time

import boto3

from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, city_to_slug, get_storage
from meeting_pipeline.stages.discover.process import process_one_city

sqs = boto3.client("sqs")
DISCOVER_QUEUE_URL = os.environ.get("DISCOVER_QUEUE_URL", "")


async def discover_one(city: str, state: str, cfg, storage):
    slug = city_to_slug(city, state)
    manifest_key = f"{cfg.sources_prefix}/{slug}/manifest.json"
    expected_body = ""
    if storage.exists(manifest_key):
        try:
            manifest = storage.read_json(manifest_key)
            expected_body = manifest.get("expected_body", "")
        except Exception:
            pass

    result = await process_one_city(city, state, expected_body=expected_body)
    storage.write_json(f"{cfg.sources_prefix}/{slug}/source.json", result)

    platform = result.get("best_source", {}).get("platform", "?")
    freshness = result.get("best_source", {}).get("freshness", "?")
    return {"slug": slug, "platform": platform, "freshness": freshness}


def poll_loop():
    """Long-poll SQS queue forever. ECS keeps the service at desired_count=1."""
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    if not DISCOVER_QUEUE_URL:
        print("DISCOVER_QUEUE_URL not set — exiting")
        return

    print(f"Polling {DISCOVER_QUEUE_URL}...")

    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=DISCOVER_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
            )
        except Exception as e:
            # Don't crash the container on transient SQS/STS/network failures —
            # boto3 already retries internally; if we got here those retries
            # were exhausted. Sleep briefly and reconnect on the next iteration.
            print(f"receive_message error: {e} — backing off 5s")
            time.sleep(5)
            continue

        messages = resp.get("Messages", [])

        if not messages:
            # Idle long-poll cycle — keep waiting. ECS service health is
            # observed via container running, not via queue activity.
            continue

        for msg in messages:
            try:
                body = json.loads(msg["Body"])
            except json.JSONDecodeError as e:
                # Poison-pill message — would crash the loop on every redelivery
                # until DLQ. Delete it so the queue keeps moving and log once.
                print(f"Skipping malformed message: {e} — body: {msg.get('Body', '')[:200]}")
                sqs.delete_message(QueueUrl=DISCOVER_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
                continue

            city = body.get("city", "")
            state = body.get("state", "")
            slug = body.get("slug", "")

            if not city and slug:
                parts = slug.rsplit("-", 1)
                if len(parts) == 2:
                    city = parts[0].replace("-", " ").title()
                    state = parts[1]

            if not city or not state:
                print(f"Skipping invalid message: {body}")
                sqs.delete_message(QueueUrl=DISCOVER_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
                continue

            print(f"Discovering: {city}, {state}...", end=" ", flush=True)
            t = time.time()
            try:
                result = asyncio.run(discover_one(city, state, cfg, storage))
                print(f"[{result['platform']}/{result['freshness']}] ({time.time()-t:.0f}s)")
                # Only delete on success — on exception, leave the message visible
                # so SQS redelivers (counting toward maxReceiveCount → DLQ on persistent failure).
                sqs.delete_message(QueueUrl=DISCOVER_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
            except Exception as e:
                print(f"ERROR: {e} ({time.time()-t:.0f}s) — leaving message for retry")


if __name__ == "__main__":
    poll_loop()
