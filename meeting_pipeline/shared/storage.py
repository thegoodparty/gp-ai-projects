"""
storage.py — Re-export of storage backend from collection_agent.

Clean import path: `from meeting_pipeline.shared.storage import StorageBackend`
"""

from meeting_pipeline.collection_agent.storage import (  # noqa: F401
    StorageBackend,
    S3StorageBackend,
)
