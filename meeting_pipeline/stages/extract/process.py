"""
process.py — Single-meeting extraction entry point.

Provides process_one_meeting() which downloads the agenda PDF,
extracts text, runs Gemini structured extraction, and returns
a normalized meeting JSON dict.
"""

from typing import Optional

from meeting_pipeline.shared.config import AgentConfig, get_storage


def process_one_meeting(
    city_slug: str,
    date: str,
    meeting_entry: dict,
    cfg: Optional[AgentConfig] = None,
    storage=None,
) -> dict | None:
    """
    Extract and normalize one meeting's agenda.

    Args:
        city_slug: e.g. "chapel-hill-NC"
        date: e.g. "2026-04-28"
        meeting_entry: dict from meeting_queue.json with city, state, platform, etc.
        cfg: AgentConfig (created from env if not provided)
        storage: StorageBackend (created from cfg if not provided)

    Returns:
        Normalized meeting dict, or None if extraction failed.
    """
    from meeting_pipeline.scripts.extract_and_normalize import (
        find_best_pdf, extract_pdf_text, extract_with_gemini, normalize_meeting,
    )

    if cfg is None:
        cfg = AgentConfig.from_env()
    if storage is None:
        storage = get_storage(cfg)

    city = meeting_entry.get("city", city_slug.rsplit("-", 1)[0].replace("-", " ").title())
    state = meeting_entry.get("state", city_slug.rsplit("-", 1)[1] if "-" in city_slug else "")
    platform = meeting_entry.get("platform", "unknown")

    # Find and download the PDF
    pdf_key, pdf_url = find_best_pdf(city_slug, date, platform, storage, cfg.sources_prefix)
    if not pdf_key:
        print(f"  No PDF found for {city_slug} {date}")
        return None

    try:
        pdf_bytes = storage.read_bytes(pdf_key)
    except Exception as e:
        print(f"  Failed to read PDF {pdf_key}: {e}")
        return None

    # Extract text
    text = extract_pdf_text(pdf_bytes)
    if not text or len(text) < 100:
        print(f"  PDF text too short ({len(text)} chars) for {city_slug} {date}")
        return None

    # LLM extraction
    import os
    from google import genai
    gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

    extraction = extract_with_gemini(text, city, state, date, gemini)
    if not extraction or not extraction.items:
        print(f"  Gemini extraction returned no items for {city_slug} {date}")
        return None

    # Normalize
    normalized = normalize_meeting(extraction, city, state, date, platform, pdf_url or "")
    return normalized
