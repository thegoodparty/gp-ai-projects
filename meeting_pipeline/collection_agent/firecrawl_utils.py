"""
firecrawl_utils.py — Backward-compat shim. Implementation moved to shared/firecrawl_client.py.

Existing imports like `from meeting_pipeline.collection_agent.firecrawl_utils import ...` still work.
New code should import from `meeting_pipeline.shared.firecrawl_client`.
"""

from meeting_pipeline.shared.firecrawl_client import (  # noqa: F401
    get_remaining_credits,
    search_agenda_page,
    extract_meeting_links,
    validate_agenda_page,
    scrape_pdf_text,
    scrape_civicclerk_event_files,
)
