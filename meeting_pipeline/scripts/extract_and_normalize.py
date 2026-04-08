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
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage


# ── Pydantic schemas for LLM structured output ───────────────────────────────

class AgendaItem(BaseModel):
    number: str | None = None
    title: str
    section: str | None = None
    description: str | None = None
    fiscal_amounts: list[str] = []
    is_public_hearing: bool = False
    staff_recommendation: str | None = None

class MeetingExtraction(BaseModel):
    date: str
    time: str | None = None
    location: str | None = None
    body: str
    meeting_type: str | None = None
    total_items: int
    items: list[AgendaItem]
    extraction_notes: str | None = None


# ── PDF extraction ────────────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 60) -> str:
    """Extract text from PDF bytes using PyMuPDF. Returns full text."""
    import fitz  # PyMuPDF — imported here to keep tests fast (heavy dependency)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = min(len(doc), max_pages)
    return "\n".join(doc[i].get_text() for i in range(pages))


def find_best_pdf(city_slug: str, date: str, platform: str, storage, sources_prefix: str) -> tuple[str | None, str | None]:
    """
    Find the best PDF for extraction: prefer packet over agenda-only.
    Scans all platform subdirs under sources/{city}/data/ so new platforms
    are found automatically without code changes.
    Returns (storage_key, pdf_label).
    """
    data_prefix = f"{sources_prefix}/{city_slug}/data"
    all_keys = storage.list_keys(data_prefix)
    pdf_keys = [k for k in all_keys if k.lower().endswith(".pdf")]

    if not pdf_keys:
        return None, None

    # Organise by platform, primary platform first
    def platform_order(key: str) -> int:
        parts = key.split("/")
        # key structure: .../data/{plat}/pdfs/filename.pdf
        idx = parts.index("data") if "data" in parts else -1
        plat = parts[idx + 1] if idx >= 0 and idx + 1 < len(parts) else ""
        return 0 if plat == platform else 1

    date_compact = date.replace("-", "")

    # Filter to PDFs matching this date
    matching = []
    for key in pdf_keys:
        filename = key.split("/")[-1]
        if date in filename or date_compact in filename:
            try:
                size = storage.get_size(key)
            except Exception:
                size = 0
            if size > 5000:
                matching.append((key, size))

    if not matching:
        return None, None

    # Sort: primary platform first, then packet > agenda, then largest size
    def sort_key(item):
        key, size = item
        return (platform_order(key), 0 if "packet" in key.lower() else 1, -size)

    matching.sort(key=sort_key)
    best_key, _ = matching[0]
    filename = best_key.split("/")[-1]
    label = "packet" if "packet" in filename.lower() else "agenda"
    return best_key, label


# ── LLM extraction ────────────────────────────────────────────────────────────

def extract_with_gemini(text: str, city: str, state: str, date: str, gemini) -> MeetingExtraction:
    large_agenda = len(text.split()) > 8000
    prompt = build_extraction_prompt(text, city, state, date, large_agenda=large_agenda)

    result = gemini.generate_structured_content(
        prompt=prompt,
        response_schema=MeetingExtraction,
        temperature=0.1,
        trace_name="extract_agenda",
    )

    if isinstance(result, MeetingExtraction):
        return result
    return MeetingExtraction.model_validate(result)


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize_meeting(
    official: dict,
    meeting: dict,
    extraction: MeetingExtraction,
    pdf_key: str | None,
    pdf_label: str | None,
    city_slug: str,
    platform: str,
) -> dict:
    items = []
    for item in extraction.items:
        items.append({
            "number": item.number,
            "title": item.title,
            "section": item.section,
            "description": item.description,
            "fiscal_amounts": item.fiscal_amounts,
            "is_public_hearing": item.is_public_hearing,
            "staff_recommendation": item.staff_recommendation,
        })

    agenda_files = []
    for af in meeting.get("agenda_files", []):
        agenda_files.append({
            "name": af.get("name", ""),
            "type": af.get("type", ""),
            "url": af.get("url", ""),
        })
    if pdf_key:
        agenda_files.append({
            "name": pdf_label or "downloaded",
            "type": "storage_pdf",
            "url": pdf_key,
        })

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "official": {
            "name": official["name"],
            "city": official["city"],
            "state": official["state"],
            "role": official["role"],
        },
        "meeting": {
            "date": extraction.date or meeting.get("date", ""),
            "time": extraction.time or meeting.get("time", ""),
            "location": extraction.location or "",
            "body": extraction.body or meeting.get("body", ""),
            "meeting_type": extraction.meeting_type or "",
            "title": meeting.get("title", ""),
            "platform": platform,
            "city_slug": city_slug,
        },
        "sources": {
            "platform_meeting_url": meeting.get("source_url", ""),
            "agenda_files": agenda_files,
        },
        "agenda": {
            "total_items": len(items),
            "items": items,
            "extraction_notes": extraction.extraction_notes,
        },
        "summary": {
            "total_items": len(items),
            "public_hearings": sum(1 for i in items if i.get("is_public_hearing")),
            "consent_items": sum(1 for i in items if i.get("section") == "consent"),
            "action_items": sum(1 for i in items if i.get("section") == "action"),
            "fiscal_items": [
                {"item": i["title"], "amounts": i["fiscal_amounts"]}
                for i in items if i.get("fiscal_amounts")
            ],
        },
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM extraction, just show what would be processed")
    parser.add_argument("--force", action="store_true", help="Re-extract even if normalized file already exists")
    args = parser.parse_args()

    from shared.llm_gemini import GeminiClient, GeminiModelType  # lazy — shared has heavy deps

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    gemini = GeminiClient(default_model=GeminiModelType.FLASH_LITE)

    queue_key = f"{cfg.output_prefix}/meeting_queue.json"
    if not storage.exists(queue_key):
        print(f"Queue file not found: {queue_key}")
        print("Run generate_meeting_queue.py first.")
        sys.exit(1)

    queue = storage.read_json(queue_key)
    normalized_prefix = f"{cfg.output_prefix}/normalized"

    all_normalized = []
    errors = []

    for entry in queue["queue"]:
        official = entry["official"]
        platform = entry["platform"]
        city_slug = entry["city_slug"]

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

            # LLM extraction
            try:
                extraction = extract_with_gemini(text, official["city"], official["state"], date, gemini)
                print(f"  Extracted {len(extraction.items)} agenda items")
            except Exception as e:
                print(f"  ✗ LLM extraction failed: {e}")
                errors.append({"label": label, "error": str(e)})
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
        print(f"  LLM cost:   ${stats.get('estimated_cost_usd', 0):.4f} ({stats.get('api_calls', 0)} calls)")
        print(f"\nOutput: {normalized_prefix}/")


if __name__ == "__main__":
    main()
