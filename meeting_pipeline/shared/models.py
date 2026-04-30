from __future__ import annotations

"""
models.py — Shared dataclasses for the pipeline.

Kept as plain dataclasses (not Pydantic) so they serialize/deserialize cleanly
as dicts without extra dependencies — compatible with Lambda event payloads.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class CollectionResult:
    """
    Outcome of running a collector for one city.

    requires_browser=True signals that Playwright was needed — in a Lambda
    deployment this task should be routed to Fargate instead.
    """
    city: str
    state: str
    platform: str
    events_found: int
    pdfs_downloaded: int
    events: list[dict] = field(default_factory=list)
    requires_browser: bool = False
    error: str | None = None
    nav_config_saved: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def error_result(cls, city: str, state: str, platform: str, error: str) -> CollectionResult:
        return cls(
            city=city,
            state=state,
            platform=platform,
            events_found=0,
            pdfs_downloaded=0,
            error=error,
        )


@dataclass
class NavConfig:
    """
    Reusable navigation configuration saved after a successful reason run.

    Stored as source["best_source"]["nav_config"] in source.json.
    On subsequent runs, replay mode uses this to skip LLM analysis.
    """
    platform_guess: str
    entry_url: str
    strategy: str           # direct_pdf | document_center | rss_feed | two_hop | archive_aspx
    keyword_filter: str
    body_name_hint: str
    recorded_at: str        # ISO date (YYYY-MM-DD)
    selector: str | None = None
    follow_url: str | None = None
    verify_ssl: bool = True
    replay_success_count: int = 0
    last_replay_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> NavConfig:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class HealthCheckResult:
    """Result of a URL health probe for one city."""
    city: str
    state: str
    city_slug: str
    url: str
    status_code: int | None
    redirected_to: str | None
    migration_detected: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
