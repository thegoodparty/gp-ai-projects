"""
storage.py — Backward-compat shim. Implementation moved to shared/storage.py.

Existing imports like `from meeting_pipeline.collection_agent.storage import ...` still work.
New code should import from `meeting_pipeline.shared.storage`.
"""

from meeting_pipeline.shared.storage import (  # noqa: F401
    StorageBackend,
    S3StorageBackend,
)
