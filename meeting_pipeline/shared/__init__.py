"""
meeting_pipeline.shared — Shared utilities and infrastructure for the pipeline.

Modules:
    config             — AgentConfig, get_storage, city_to_slug
    storage            — StorageBackend protocol, S3StorageBackend
    models             — CollectionResult, NavConfig, HealthCheckResult
    constants          — Platform patterns, scoring, state names, thresholds
    url_utils          — URL validation, platform detection
    date_utils         — Date extraction, freshness classification
    manifest           — Manifest reader and body validation
    notification_log   — Structured event logging (stderr → CloudWatch)
    firecrawl_client   — Firecrawl API wrappers
    body_validation    — Body matching, REJECT/GOVERNING keywords
    generic_agenda_scanner — Three-tier scan for non-platform cities
    discovery_helpers  — make_candidate, safe_fetch
"""
