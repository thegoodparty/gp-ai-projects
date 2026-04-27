"""
process.py — Single-city discovery entry point.

Provides process_one_city() which is the function a Lambda handler
would call. Currently delegates to run_source_discover() in
scripts/source_discover.py. As we continue extracting modules,
the delegation will be replaced with direct calls.
"""

from typing import Optional

import httpx
from tavily import TavilyClient

from meeting_pipeline.shared.config import AgentConfig, get_storage


async def process_one_city(
    city: str,
    state: str,
    expected_body: str = "",
    known_sources: Optional[dict] = None,
    tavily_client: Optional[TavilyClient] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """
    Run source discovery for a single city. Returns source.json dict.

    This is the entry point for both:
    - Batch processing (called in a loop by the batch runner)
    - Lambda handler (called once per invocation with event payload)

    Args:
        city: City name (e.g. "Chapel Hill")
        state: 2-letter state abbreviation (e.g. "NC")
        expected_body: Expected governing body name from manifest (e.g. "Town Council")
        known_sources: Pre-existing known source config (e.g. domain, meeting URL from CSV)
        tavily_client: Shared Tavily client (created if not provided)
        http_client: Shared httpx client (created if not provided)

    Returns:
        source.json dict with best_source, all_candidates, public_agenda_url, etc.
    """
    import os
    from meeting_pipeline.scripts.source_discover import run_source_discover

    # Create clients if not provided
    if tavily_client is None:
        tavily_key = os.environ.get("TAVILY_API_KEY", "")
        tavily_client = TavilyClient(api_key=tavily_key) if tavily_key else None

    owns_http = http_client is None
    if owns_http:
        http_client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
            follow_redirects=True,
            timeout=20,
        )

    try:
        result = await run_source_discover(
            city=city,
            state=state,
            known_sources=known_sources or {},
            tavily=tavily_client,
            http=http_client,
            expected_body=expected_body,
        )
        return result
    finally:
        if owns_http:
            await http_client.aclose()
