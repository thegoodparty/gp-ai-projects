"""Discover handler — Fargate task that processes cities from the discover SQS queue.

Polls discover-queue for city slugs, runs source discovery, writes source.json.
Runs as a long-lived Fargate task (not Lambda) because discovery needs Playwright.

Usage (local): python -m meeting_pipeline.lambda_handlers.discover
Usage (Fargate): ENTRYPOINT in Dockerfile.discover
"""

import asyncio
import json
import os
import time

import boto3

from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage, city_to_slug
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
    """Long-poll SQS queue and process cities until queue is empty."""
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    if not DISCOVER_QUEUE_URL:
        print("DISCOVER_QUEUE_URL not set — exiting")
        return

    print(f"Polling {DISCOVER_QUEUE_URL}...")
    idle_count = 0

    while idle_count < 3:
        resp = sqs.receive_message(
            QueueUrl=DISCOVER_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
        )
        messages = resp.get("Messages", [])

        if not messages:
            idle_count += 1
            print(f"No messages (idle {idle_count}/3)")
            continue

        idle_count = 0
        for msg in messages:
            body = json.loads(msg["Body"])
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
            except Exception as e:
                print(f"ERROR: {e} ({time.time()-t:.0f}s)")

            sqs.delete_message(QueueUrl=DISCOVER_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    print("Queue empty — exiting")


if __name__ == "__main__":
    poll_loop()
