"""
config.py — AgentConfig + storage backend factory.

All paths come from env vars or this config object — never hardcoded.
This mirrors the Lambda handler pattern where environment variables
configure the function at deploy time.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from .storage import StorageBackend, LocalStorageBackend, S3StorageBackend

# Auto-load .env from the meeting_pipeline directory
load_dotenv(Path(__file__).parent.parent / ".env")


@dataclass
class AgentConfig:
    """
    Central configuration for the collection agent.

    Defaults work for local development. Override via env vars for cloud.
    """
    sources_prefix: str = "meeting_pipeline/sources"
    logs_prefix: str = "meeting_pipeline/logs"
    output_prefix: str = "meeting_pipeline/output"
    storage_backend: str = "local"   # "local" | "s3"
    s3_bucket: str | None = None
    lookback_days: int = 90
    download_pdfs: bool = True

    @classmethod
    def from_env(cls) -> "AgentConfig":
        return cls(
            sources_prefix=os.getenv("SOURCES_PREFIX", "meeting_pipeline/sources"),
            logs_prefix=os.getenv("LOGS_PREFIX", "meeting_pipeline/logs"),
            output_prefix=os.getenv("OUTPUT_PREFIX", "meeting_pipeline/output"),
            storage_backend=os.getenv("STORAGE_BACKEND", "local"),
            s3_bucket=os.getenv("S3_BUCKET"),
            lookback_days=int(os.getenv("LOOKBACK_DAYS", "90")),
            download_pdfs=os.getenv("DOWNLOAD_PDFS", "true").lower() == "true",
        )


def get_storage(cfg: AgentConfig) -> StorageBackend:
    """
    Factory: return the appropriate StorageBackend for this config.

    Local: base_dir = repo root (so keys like "meeting_pipeline/sources/..." resolve correctly)
    S3:    reads/writes directly to S3_BUCKET — set STORAGE_BACKEND=s3 and S3_BUCKET=meeting-pipeline-dev
    """
    if cfg.storage_backend == "local":
        # Repo root is 3 levels up from this file:
        # meeting_pipeline/collection_agent/config.py → meeting_pipeline → repo_root
        repo_root = Path(__file__).resolve().parent.parent.parent
        return LocalStorageBackend(repo_root)
    if cfg.storage_backend == "s3":
        if not cfg.s3_bucket:
            raise ValueError("S3_BUCKET must be set when STORAGE_BACKEND=s3")
        profile = os.getenv("AWS_PROFILE")
        return S3StorageBackend(bucket=cfg.s3_bucket, profile=profile)
    raise NotImplementedError(f"Storage backend '{cfg.storage_backend}' not supported")


def city_to_slug(city: str, state: str) -> str:
    """
    Convert city name + state to directory slug.

    Matches source_discover.py convention:
        "Canal Winchester", "OH" → "canal-winchester-OH"
    """
    city_slug = city.lower().replace(" ", "-").replace(".", "").replace("'", "")
    return f"{city_slug}-{state.upper()}"


def find_city_slug(city: str, state: str, cfg: AgentConfig, storage: StorageBackend) -> str | None:
    """
    Find the actual slug for a city by scanning the sources directory.

    Returns the slug if found, None otherwise.
    Tries the canonical form first, then scans the directory.
    """
    canonical = city_to_slug(city, state)
    source_key = f"{cfg.sources_prefix}/{canonical}/source.json"
    if storage.exists(source_key):
        return canonical

    # Fallback: scan the directory for a matching city+state
    all_keys = storage.list_keys(cfg.sources_prefix)
    city_lower = city.lower().replace(" ", "-").replace(".", "").replace("'", "")
    state_upper = state.upper()

    for key in all_keys:
        if not key.endswith("source.json"):
            continue
        parts = key.split("/")
        if len(parts) < 2:
            continue
        slug = parts[-2]  # e.g. "loveland-OH"
        if slug.endswith(f"-{state_upper}") and slug.startswith(city_lower):
            return slug

    return None
