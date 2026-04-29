"""Scan handler — list cities or scan one city for upcoming meetings.

Sends each newly posted meeting to the process queue (one message per meeting).
"""

import asyncio
import json
import os
from datetime import date

import boto3
import httpx

from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.stages.scan.process import process_one_city

sqs = boto3.client("sqs")
PROCESS_QUEUE_URL = os.environ.get("PROCESS_QUEUE_URL", "")
VERIFIED_STATUSES = {"verified", "verified_ocr_needed", "verified_non_pdf"}


def handler(event, context=None):
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    if event.get("action") == "list_cities":
        # Return only verified cities
        slugs = []
        for k in storage.list_keys(cfg.sources_prefix):
            if not k.endswith("/source.json"):
                continue
            slug = k.split("/")[-2]
            try:
                src = storage.read_json(k)
                v = (src.get("best_source") or {}).get("verification", {})
                if v.get("status") in VERIFIED_STATUSES:
                    slugs.append({"slug": slug})
            except Exception:
                pass
        return {"cities": slugs}

    slug = event.get("slug")
    if not slug:
        return {"error": "slug required"}

    source_key = f"{cfg.sources_prefix}/{slug}/source.json"
    source = storage.read_json(source_key)

    result = asyncio.run(_scan(slug, source, source_key, storage))
    if result is None:
        return {"status": "error", "slug": slug}

    upcoming_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"

    previous = None
    if storage.exists(upcoming_key):
        try:
            previous = storage.read_json(upcoming_key)
        except Exception:
            pass

    storage.write_json(upcoming_key, result)

    # Send each newly posted future meeting to process queue
    today = date.today().isoformat()
    platform = result.get("platform", "")
    new_posted = _detect_new_posted(previous, result)
    sent = 0

    if new_posted and PROCESS_QUEUE_URL:
        for m in new_posted:
            if m.get("date", "") >= today:
                sqs.send_message(
                    QueueUrl=PROCESS_QUEUE_URL,
                    MessageBody=json.dumps({
                        "slug": slug,
                        "date": m["date"],
                        "platform": platform,
                    }),
                )
                sent += 1

    return {
        "status": "ok",
        "slug": slug,
        "meetings_found": len(result.get("upcoming", [])),
        "new_posted": len(new_posted),
        "sent_to_process": sent,
    }


async def _scan(slug, source, source_key, storage):
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True,
        timeout=20,
    ) as client:
        return await process_one_city(
            slug, source, source_key, http_client=client, storage=storage,
        )


def _detect_new_posted(previous, current):
    if not previous:
        return [m for m in current.get("upcoming", []) if m.get("agenda_posted")]

    prev_dates = {m.get("date") for m in previous.get("upcoming", []) if m.get("agenda_posted")}
    return [
        m for m in current.get("upcoming", [])
        if m.get("agenda_posted") and m.get("date") not in prev_dates
    ]
