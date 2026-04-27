"""
firecrawl_client.py — Re-export of Firecrawl utilities from collection_agent.

Clean import path: `from meeting_pipeline.shared.firecrawl_client import validate_agenda_page`
"""

from meeting_pipeline.collection_agent.firecrawl_utils import (  # noqa: F401
    validate_agenda_page,
    extract_meeting_links,
    scrape_pdf_text,
    scrape_civicclerk_event_files,
)
