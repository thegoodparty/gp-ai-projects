"""
process.py — Single-city scan entry point.

Provides process_one_city() which is the function a Lambda handler
would call. Currently delegates to scan_city() in
scripts/scan_meeting_schedule.py.
"""

import httpx

from meeting_pipeline.shared.config import AgentConfig, get_storage


async def process_one_city(
    slug: str,
    source: dict,
    source_key: str,
    http_client: httpx.AsyncClient | None = None,
    storage=None,
    skip_body_validation: bool = False,
) -> dict | None:
    """
    Scan one city for upcoming meetings. Returns upcoming_meetings dict.

    This is the entry point for both:
    - Batch processing (called in a loop)
    - Lambda handler (called per invocation)
    """
    from meeting_pipeline.scripts.scan_meeting_schedule import scan_city

    if storage is None:
        cfg = AgentConfig.from_env()
        storage = get_storage(cfg)

    owns_client = http_client is None
    if owns_client:
        http_client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
            follow_redirects=True, timeout=20,
        )

    try:
        return await scan_city(
            slug, source, source_key, http_client, storage,
            skip_body_validation=skip_body_validation,
        )
    finally:
        if owns_client:
            await http_client.aclose()
