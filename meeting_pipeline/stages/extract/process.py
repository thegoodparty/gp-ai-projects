"""
process.py — Single-meeting extraction entry point.

Downloads the agenda PDF, extracts text, runs Gemini structured extraction,
and returns a normalized meeting JSON dict.
"""

from typing import Optional

from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.stages.extract.normalize import (
    find_best_pdf, extract_pdf_text, extract_with_gemini, normalize_meeting,
)


def process_one_meeting(
    official: dict,
    meeting: dict,
    city_slug: str,
    platform: str,
    cfg: Optional[AgentConfig] = None,
    storage=None,
) -> dict | None:
    """
    Extract and normalize one meeting's agenda.

    Args:
        official: dict with name, city, state, role
        meeting: dict from meeting_queue with date, source_url, agenda_files, etc.
        city_slug: e.g. "chapel-hill-NC"
        platform: e.g. "legistar"
        cfg: AgentConfig (created from env if not provided)
        storage: StorageBackend (created if not provided)

    Returns:
        Normalized meeting dict, or None if extraction failed.
    """
    if cfg is None:
        cfg = AgentConfig.from_env()
    if storage is None:
        storage = get_storage(cfg)

    date = meeting.get("date", "")
    city = official.get("city", "")
    state = official.get("state", "")

    # Find the best PDF
    pdf_key, pdf_label = find_best_pdf(city_slug, date, platform, storage, cfg.sources_prefix)
    if not pdf_key:
        print(f"  No PDF found for {city_slug} {date}")
        return None

    # Extract text
    try:
        pdf_bytes = storage.read_bytes(pdf_key)
    except Exception as e:
        print(f"  Failed to read PDF {pdf_key}: {e}")
        return None

    text = extract_pdf_text(pdf_bytes)
    if not text or len(text) < 100:
        print(f"  PDF text too short ({len(text or '')} chars) for {city_slug} {date}")
        return None

    # LLM extraction
    from shared.llm_gemini import GeminiClient
    gemini = GeminiClient()

    try:
        extraction = extract_with_gemini(text, city, state, date, gemini)
    except Exception as e:
        print(f"  Gemini extraction failed for {city_slug} {date}: {e}")
        return None

    if not extraction or not extraction.items:
        print(f"  No items extracted for {city_slug} {date}")
        return None

    # Normalize
    return normalize_meeting(official, meeting, extraction, pdf_key, pdf_label, city_slug, platform)
