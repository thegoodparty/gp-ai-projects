"""
process.py — Single-city collection entry point.

Routes to the correct platform collector based on source.json platform.
Downloads agenda PDFs and platform-specific data to S3.
"""

from typing import Optional

from meeting_pipeline.shared.config import AgentConfig, get_storage


async def process_one_city(
    city: str,
    state: str,
    cfg: Optional[AgentConfig] = None,
    storage=None,
) -> dict:
    """
    Collect meeting data for one city. Returns result dict.

    Reads source.json to determine platform, then routes to the
    appropriate collector (Legistar API, CivicPlus scraper, etc.).

    Args:
        city: City name
        state: 2-letter state abbreviation
        cfg: AgentConfig (created from env if not provided)
        storage: StorageBackend (created from cfg if not provided)

    Returns:
        CollectionResult from route_city()
    """
    from meeting_pipeline.stages.collect.router import route_city

    if cfg is None:
        cfg = AgentConfig.from_env()
    if storage is None:
        storage = get_storage(cfg)

    event = {"city": city, "state": state}
    return await route_city(event, storage, cfg)
