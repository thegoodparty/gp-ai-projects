"""
extract_and_normalize.py — Extract agenda items from PDFs and produce normalized meeting JSON.

For each agenda-ready meeting in meeting_queue.json:
  1. Extract text from the PDF (packet preferred over agenda-only)
  2. Use Gemini to extract structured agenda items
  3. Produce normalized meeting JSON with source URLs for QA

Storage:
    Reads/writes via STORAGE_BACKEND (local or s3). Set S3_BUCKET + STORAGE_BACKEND=s3 in .env for S3.
    Output: {output_prefix}/normalized/{city-slug}_{date}.json

Usage:
    uv run python meeting_pipeline/scripts/extract_and_normalize.py
    uv run python meeting_pipeline/scripts/extract_and_normalize.py --dry-run
    uv run python meeting_pipeline/scripts/extract_and_normalize.py --force
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from meeting_pipeline.prompts.extraction import build_extraction_prompt

load_dotenv()

_ROOT = Path(__file__).resolve().parent.parent

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage


# Implementation moved to stages/extract/normalize.py
from meeting_pipeline.stages.extract.normalize import (
    AgendaItem, MeetingExtraction,
    extract_pdf_text, find_best_pdf, extract_with_gemini, normalize_meeting,
)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM extraction, just show what would be processed")
    parser.add_argument("--force", action="store_true", help="Re-extract even if normalized file already exists")
    parser.add_argument("--city", action="append", metavar="SLUG",
                        help="Only process this city slug (repeatable, e.g. --city hampton-GA --city davidson-NC)")
    parser.add_argument("--queue", metavar="S3_KEY",
                        help="Override the default queue S3 key (e.g. to use a pilot queue instead of serve_users queue)")
    args = parser.parse_args()

    from shared.llm_gemini import GeminiClient, GeminiModelType  # lazy — shared has heavy deps

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    gemini = GeminiClient(default_model=GeminiModelType.FLASH_LITE)

    queue_key = args.queue if args.queue else f"{cfg.output_prefix}/meeting_queue.json"
    if not storage.exists(queue_key):
        print(f"Queue file not found: {queue_key}")
        print("Run generate_meeting_queue.py first.")
        sys.exit(1)

    queue = storage.read_json(queue_key)
    normalized_prefix = f"{cfg.output_prefix}/normalized"

    all_normalized = []
    errors = []

    city_filter = set(args.city) if args.city else None

    for entry in queue["queue"]:
        official = entry["official"]
        platform = entry["platform"]
        city_slug = entry["city_slug"]

        if city_filter and city_slug not in city_filter:
            continue

        # Include agenda_posted_no_files if a PDF exists in storage for that date
        ready = []
        for m in entry["upcoming_meetings"]:
            if m["status"] == "agenda_ready":
                ready.append(m)
            elif m["status"] == "agenda_posted_no_files":
                pdf_key, _ = find_best_pdf(city_slug, m["date"], platform, storage, cfg.sources_prefix)
                if pdf_key:
                    ready.append(m)
        if not ready:
            continue

        for meeting in ready:
            date = meeting["date"]
            label = f"{official['name']} — {official['city']}, {official['state']} — {date}"
            print(f"\nProcessing: {label}")

            pdf_key, pdf_label = find_best_pdf(city_slug, date, platform, storage, cfg.sources_prefix)
            if not pdf_key:
                print(f"  ⚠ No PDF found for {city_slug} {date} — skipping")
                errors.append({"label": label, "error": "no PDF found"})
                continue

            pdf_size = storage.get_size(pdf_key)
            print(f"  PDF: {pdf_key.split('/')[-1]} ({pdf_size // 1024}KB, label={pdf_label})")

            if args.dry_run:
                print(f"  [dry-run] would extract from {pdf_key}")
                continue

            # Skip if already normalized (use --force to re-run)
            out_key = f"{normalized_prefix}/{city_slug}_{date}.json"
            if storage.exists(out_key) and not args.force:
                print(f"  ↩ Already normalized — skipping (--force to re-run)")
                all_normalized.append(storage.read_json(out_key))
                continue

            # Extract text from PDF bytes
            pdf_bytes = storage.read_bytes(pdf_key)
            text = extract_pdf_text(pdf_bytes)
            word_count = len(text.split())
            print(f"  Extracted {word_count} words from PDF")

            truncation_warning = None
            if len(text) > 100_000:
                truncated_chars = len(text) - 100_000
                truncation_warning = f"Text truncated: {len(text):,} chars → 100,000 ({truncated_chars:,} chars dropped — tail agenda items may be missing)"
                print(f"  ⚠ {truncation_warning}")

            if len(text.strip()) < 500 and pdf_size > 5000:
                print(f"  ⚠ PDF appears scanned ({len(text.strip())} chars from {pdf_size // 1024}KB) — trying Firecrawl OCR")
                try:
                    from meeting_pipeline.collection_agent.firecrawl_utils import scrape_pdf_text
                    presigned = storage.get_presigned_url(pdf_key, expiry_seconds=300)
                    fc_text = scrape_pdf_text(presigned)
                    if fc_text and len(fc_text.strip()) > 200:
                        text = fc_text
                        print(f"  ✓ Firecrawl OCR succeeded: {len(text.split())} words")
                    else:
                        raise ValueError("Firecrawl returned insufficient text")
                except Exception as fc_err:
                    err = f"PDF appears to be scanned/image-only and OCR failed: {fc_err}"
                    print(f"  ✗ {err}")
                    errors.append({"label": label, "error": err})
                    continue

            # LLM extraction with exponential backoff retry
            import time as _time
            extraction = None
            for attempt in range(3):
                try:
                    extraction = extract_with_gemini(text, official["city"], official["state"], date, gemini)
                    print(f"  Extracted {len(extraction.items)} agenda items")
                    break
                except Exception as e:
                    if attempt < 2:
                        wait = 2 ** attempt
                        print(f"  ✗ LLM extraction attempt {attempt + 1} failed: {e} — retrying in {wait}s")
                        _time.sleep(wait)
                    else:
                        print(f"  ✗ LLM extraction failed after 3 attempts: {e}")
                        errors.append({"label": label, "error": str(e)})
            if extraction is None:
                continue

            # Normalize and save
            normalized = normalize_meeting(
                official=official,
                meeting=meeting,
                extraction=extraction,
                pdf_key=pdf_key,
                pdf_label=pdf_label,
                city_slug=city_slug,
                platform=platform,
            )
            if truncation_warning:
                normalized.setdefault("agenda", {})["truncation_warning"] = truncation_warning
            all_normalized.append(normalized)
            storage.write_json(out_key, normalized)
            print(f"  ✓ Saved: {out_key}")

    if not args.dry_run:
        combined_key = f"{cfg.output_prefix}/normalized_meetings.json"
        storage.write_json(combined_key, {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_meetings": len(all_normalized),
            "meetings": all_normalized,
        })

        stats = gemini.get_usage_stats()
        print(f"\n{'='*60}")
        print(f"EXTRACTION SUMMARY")
        print(f"{'='*60}")
        print(f"  Normalized: {len(all_normalized)} meetings")
        print(f"  Errors:     {len(errors)}")
        for e in errors:
            print(f"    {e['label']}: {e['error']}")
        print(f"  LLM cost:   ${stats.get('total_cost', 0):.4f} ({stats.get('api_call_count', 0)} calls)")
        print(f"\nOutput: {normalized_prefix}/")

        # Write cost report so the pipeline orchestrator can aggregate it
        try:
            cost_report = {
                "phase": "normalize",
                "gemini_calls": stats.get("api_call_count", 0),
                "input_tokens": stats.get("total_input_tokens", 0),
                "output_tokens": stats.get("total_output_tokens", 0),
                "estimated_usd": round(stats.get("total_cost", 0.0), 6),
                "meetings_normalized": len(all_normalized),
            }
            output_prefix = normalized_prefix.rsplit("/", 1)[0]
            storage.write_json(f"{output_prefix}/cost_reports/normalize.json", cost_report)
        except Exception:
            pass


if __name__ == "__main__":
    main()
