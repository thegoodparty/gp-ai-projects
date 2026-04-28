"""
config.py — Backward-compat shim. Implementation moved to shared/config.py.

Existing imports like `from meeting_pipeline.collection_agent.config import ...` still work.
New code should import from `meeting_pipeline.shared.config`.
"""

from meeting_pipeline.shared.config import (  # noqa: F401
    AgentConfig,
    get_storage,
    city_to_slug,
    find_city_slug,
)
