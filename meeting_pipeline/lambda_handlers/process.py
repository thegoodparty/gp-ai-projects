"""Process handler — collect PDF, extract, generate briefing, and QA for one meeting.

SQS triggered. Each message is one meeting that needs a briefing.
Runs collect → extract → briefing → QA in a single Lambda invocation.

Message format: {"slug": "chapel-hill-NC", "date": "2026-04-29", "platform": "legistar"}
"""

import asyncio
import json
import os
import sys

import boto3

from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage

# Ensure project root on path for shared.llm_gemini
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sqs = boto3.client("sqs")
QA_QUEUE_URL = os.environ.get("QA_QUEUE_URL", "")


def handler(event, context=None):
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    records = event.get("Records", [event])
    results = []

    for record in records:
        body = json.loads(record["body"]) if isinstance(record.get("body"), str) else record
        slug = body["slug"]
        meeting_date = body["date"]
        platform = body.get("platform", "")

        result = _process_meeting(slug, meeting_date, platform, cfg, storage)
        results.append(result)

    return {"results": results}


def _process_meeting(slug, meeting_date, platform, cfg, storage):
    """Collect → Extract → Brief → QA for one meeting."""

    # ── Step 1: Collect PDF ──────────────────────────────────────────────
    # Lazy imports: these pull in heavy dependencies (Gemini, Firecrawl,
    # Playwright) that are expensive to load at module level in Lambda.
    from meeting_pipeline.stages.extract.normalize import (
        extract_pdf_text,
        extract_with_gemini,
        find_best_pdf,
        normalize_meeting,
    )

    pdf_key, pdf_label = find_best_pdf(slug, meeting_date, platform, storage, cfg.sources_prefix)

    if not pdf_key:
        # Try collecting first
        try:
            source = storage.read_json(f"{cfg.sources_prefix}/{slug}/source.json")
            city = source.get("city", slug)
            state = source.get("state", "")
            from meeting_pipeline.stages.collect.process import process_one_city
            asyncio.run(process_one_city(city, state, cfg=cfg, storage=storage))
            # Retry finding PDF
            pdf_key, pdf_label = find_best_pdf(slug, meeting_date, platform, storage, cfg.sources_prefix)
        except Exception as e:
            return {"status": "collect_failed", "slug": slug, "date": meeting_date, "error": str(e)}

    if not pdf_key:
        return {"status": "no_pdf", "slug": slug, "date": meeting_date}

    # ── Step 2: Extract ──────────────────────────────────────────────────
    try:
        pdf_bytes = storage.read_bytes(pdf_key)
        text = extract_pdf_text(pdf_bytes)

        if len(text.strip()) < 500 and storage.get_size(pdf_key) > 5000:
            try:
                from meeting_pipeline.shared.firecrawl_client import scrape_pdf_text
                presigned = storage.get_presigned_url(pdf_key, expiry_seconds=300)
                fc_text = scrape_pdf_text(presigned)
                if fc_text and len(fc_text.strip()) > 200:
                    text = fc_text
            except Exception:
                pass

        if not text or len(text.strip()) < 100:
            return {"status": "text_too_short", "slug": slug, "date": meeting_date}

        from shared.llm_gemini import GeminiClient, GeminiModelType
        gemini = GeminiClient(default_model=GeminiModelType.FLASH_LITE)
        extraction = extract_with_gemini(text, slug.rsplit("-", 1)[0].replace("-", " ").title(),
                                         slug.rsplit("-", 1)[1] if "-" in slug else "", meeting_date, gemini)

        if not extraction or not extraction.items:
            return {"status": "no_items", "slug": slug, "date": meeting_date}

        # Get city info from upcoming_meetings
        um_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
        um = storage.read_json(um_key) if storage.exists(um_key) else {}
        city = um.get("city", slug)
        state = um.get("state", "")
        body = um.get("body", "")
        meeting = next((m for m in um.get("upcoming", []) if m.get("date") == meeting_date), {})

        official = {"name": "", "city": city, "state": state, "role": body or "City Council"}
        meeting_for_norm = {
            "date": meeting_date,
            "title": meeting.get("title", ""),
            "body": body,
            "source_url": meeting.get("agenda_url", ""),
            "agenda_files": [{"name": "Agenda", "type": "Agenda", "url": meeting.get("agenda_url", "")}] if meeting.get("agenda_url") else [],
        }
        normalized = normalize_meeting(official, meeting_for_norm, extraction, pdf_key, pdf_label, slug, platform)
        normalized_key = f"{cfg.output_prefix}/normalized/{slug}_{meeting_date}.json"
        storage.write_json(normalized_key, normalized)

    except Exception as e:
        return {"status": "extract_failed", "slug": slug, "date": meeting_date, "error": str(e)}

    # ── Step 3: Briefing ─────────────────────────────────────────────────
    try:
        from meeting_pipeline.stages.briefing.generate import generate_briefing_for_meeting
        brief_result = generate_briefing_for_meeting(normalized_key, storage, cfg)
        if brief_result.get("status") != "ok":
            return {"status": "briefing_failed", "slug": slug, "date": meeting_date, "error": brief_result.get("error", "unknown")}
        briefing_key = brief_result.get("output", "")
    except Exception as e:
        return {"status": "briefing_failed", "slug": slug, "date": meeting_date, "error": str(e)}

    # ── Step 4: Send to QA queue ────────────────────────────────────────
    if QA_QUEUE_URL:
        sqs.send_message(
            QueueUrl=QA_QUEUE_URL,
            MessageBody=json.dumps({"briefing_key": briefing_key}),
        )

    return {
        "status": "ok",
        "slug": slug,
        "date": meeting_date,
        "briefing_key": briefing_key,
    }


