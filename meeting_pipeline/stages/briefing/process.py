"""
process.py — Single-meeting briefing generation entry point.

Takes a normalized meeting dict and generates a full briefing
with provenance tracking.
"""

from typing import Optional

from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.stages.briefing.generate import generate_briefing_for_meeting


def process_one_meeting(
    normalized_meeting: dict,
    cfg: Optional[AgentConfig] = None,
    storage=None,
) -> dict | None:
    """
    Generate a briefing for one normalized meeting.

    Args:
        normalized_meeting: dict from extract stage
        cfg: AgentConfig (created from env if not provided)
        storage: StorageBackend (created from cfg if not provided)

    Returns:
        Briefing dict, or None if generation failed.
    """
    if cfg is None:
        cfg = AgentConfig.from_env()
    if storage is None:
        storage = get_storage(cfg)

    return generate_briefing_for_meeting(normalized_meeting, cfg, storage)
