"""
meeting_pipeline.shared — Shared utilities for the meeting data pipeline.

Modules:
    constants          — Platform patterns, scoring, state names, thresholds
    url_utils          — URL validation, platform detection, city_to_slug
    date_utils         — Date extraction, freshness classification
    config             — AgentConfig, get_storage (re-export from collection_agent)
    storage            — StorageBackend (re-export from collection_agent)
    body_validation    — Body matching, REJECT/GOVERNING keywords (re-export)
    firecrawl_client   — Firecrawl API wrappers (re-export from collection_agent)
    generic_agenda_scanner — Three-tier scan for non-platform cities
"""
