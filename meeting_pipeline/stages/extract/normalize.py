"""
normalize.py — PDF text extraction, Gemini structured extraction, and normalization.

Contains the core logic for converting agenda PDFs into structured meeting JSON.
Used by stages/extract/process.py.
"""

from datetime import UTC, datetime

from pydantic import BaseModel

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


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 60) -> str:
    """Extract text from PDF bytes using PyMuPDF. Returns full text with [PAGE N] markers."""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = min(len(doc), max_pages)
    return "\n".join(f"[PAGE {i+1}]\n{doc[i].get_text()}" for i in range(pages))


# ── Find best PDF ─────────────────────────────────────────────────────────────

def find_best_pdf(
    city_slug: str, date: str, platform: str, storage, sources_prefix: str,
) -> tuple[str | None, str | None]:
    """
    Find the best PDF for extraction: prefer packet over agenda-only.
    Returns (storage_key, pdf_label) or (None, None).
    """
    data_prefix = f"{sources_prefix}/{city_slug}/data"
    all_keys = storage.list_keys(data_prefix)
    pdf_keys = [k for k in all_keys if k.lower().endswith(".pdf")]

    if not pdf_keys:
        return None, None

    def platform_order(key: str) -> int:
        parts = key.split("/")
        idx = parts.index("data") if "data" in parts else -1
        plat = parts[idx + 1] if idx >= 0 and idx + 1 < len(parts) else ""
        return 0 if plat == platform else 1

    date_compact = date.replace("-", "")
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
        if platform == "legistar":
            legistar_pdf = _download_legistar_agenda_pdf(city_slug, date, storage, sources_prefix)
            if legistar_pdf:
                return legistar_pdf, "agenda"
        return None, None

    def sort_key(item):
        key, size = item
        return (platform_order(key), 0 if "packet" in key.lower() else 1, -size)

    matching.sort(key=sort_key)
    best_key, _ = matching[0]
    filename = best_key.split("/")[-1]
    label = "packet" if "packet" in filename.lower() else "agenda"
    return best_key, label


def _download_legistar_agenda_pdf(
    city_slug: str, date: str, storage, sources_prefix: str,
) -> str | None:
    """For Legistar: download EventAgendaFile from events.json."""
    import requests

    save_key = f"{sources_prefix}/{city_slug}/data/legistar/pdfs/{date}_agenda.pdf"
    if storage.exists(save_key):
        return save_key

    events_key = f"{sources_prefix}/{city_slug}/data/legistar/events.json"
    if not storage.exists(events_key):
        return None

    try:
        events = storage.read_json(events_key)
    except Exception:
        return None

    agenda_url = None
    for event in events:
        event_date = (event.get("EventDate") or "")[:10]
        if event_date == date and event.get("EventAgendaFile"):
            agenda_url = event["EventAgendaFile"]
            break

    if not agenda_url:
        return None

    try:
        resp = requests.get(agenda_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and len(resp.content) > 5000:
            storage.write_bytes(save_key, resp.content)
            return save_key
    except Exception:
        pass
    return None


# ── LLM extraction ───────────────────────────────────────────────────────────

def extract_with_gemini(text: str, city: str, state: str, date: str, gemini) -> MeetingExtraction:
    """Use Gemini to extract structured agenda items from PDF text."""
    from meeting_pipeline.prompts.extraction import build_extraction_prompt

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
    """Convert LLM extraction + metadata into the normalized meeting JSON format."""
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
        "generated_at": datetime.now(UTC).isoformat(),
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
