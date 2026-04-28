"""
models.py — Backward-compat shim. Implementation moved to shared/models.py.

Existing imports like `from meeting_pipeline.collection_agent.models import ...` still work.
New code should import from `meeting_pipeline.shared.models`.
"""

from meeting_pipeline.shared.models import (  # noqa: F401
    CollectionResult,
    NavConfig,
    HealthCheckResult,
)
