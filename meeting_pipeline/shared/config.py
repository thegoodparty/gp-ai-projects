"""
config.py — Re-export of pipeline configuration from collection_agent.

This shim provides the clean import path `from meeting_pipeline.shared.config import ...`
while the actual implementation stays in collection_agent/config.py.
New code should import from here. Old imports from collection_agent.config still work.
"""

from meeting_pipeline.collection_agent.config import (  # noqa: F401
    AgentConfig,
    get_storage,
    city_to_slug,
    find_city_slug,
)
