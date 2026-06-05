"""
process.py — Single-city scan implementation.

Dispatches to platform-specific scanners, applies body filter,
and returns upcoming_meetings dict. This is the function a Lambda
handler would call.
"""

import os
from datetime import UTC, datetime

import httpx

from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.shared.constants import SUPPORTED_PLATFORMS
from meeting_pipeline.shared.generic_agenda_scanner import scan_generic
from meeting_pipeline.stages.scan.body_filter import filter_by_body
from meeting_pipeline.stages.scan.platforms.boarddocs import scan_boarddocs
from meeting_pipeline.stages.scan.platforms.civicclerk import scan_civicclerk
from meeting_pipeline.stages.scan.platforms.civicplus import scan_civicplus
from meeting_pipeline.stages.scan.platforms.escribe import scan_escribe
from meeting_pipeline.stages.scan.platforms.granicus import scan_granicus
from meeting_pipeline.stages.scan.platforms.legistar import scan_legistar
from meeting_pipeline.stages.scan.platforms.municode import scan_municode
from meeting_pipeline.stages.scan.platforms.novus import scan_novus


async def process_one_city(
    slug: str,
    source: dict,
    source_key: str,
    http_client: httpx.AsyncClient | None = None,
    storage=None,
    skip_body_validation: bool = False,
) -> dict | None:
    """
    Scan one city for upcoming meetings.

    Dispatches to the platform-specific scanner based on source.json platform,
    then filters results by governing body.

    Args:
        slug: city slug (e.g. "chapel-hill-NC")
        source: source.json dict
        source_key: S3 key for source.json
        http_client: shared httpx client (created if not provided)
        storage: StorageBackend (created if not provided)
        skip_body_validation: skip CivicPlus/Legistar body validation step

    Returns:
        upcoming_meetings dict, or None on error.
    """
    if storage is None:
        cfg = AgentConfig.from_env()
        storage = get_storage(cfg)

    owns_client = http_client is None
    if owns_client:
        http_client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
            follow_redirects=True, timeout=20,
        )

    try:
        best = source.get("best_source") or {}
        platform = best.get("platform", "")
        config = best.get("config", {})
        source_url = best.get("url", "")
        city = source.get("city", slug)
        state = source.get("state", "")

        # ── Get expected body from manifest ───────────────────────────────
        body = best.get("expected_body", config.get("expected_body", ""))
        if not body:
            manifest_key = source_key.replace("/source.json", "/manifest.json")
            try:
                manifest = storage.read_json(manifest_key)
                body = manifest.get("expected_body", "")
            except Exception:
                pass

        # ── Body validation (CivicPlus category correction etc.) ──────────
        body_validation: dict = {}
        if not skip_body_validation and platform in SUPPORTED_PLATFORMS:
            from meeting_pipeline.shared.body_validation import validate_body_for_city
            body_validation = await validate_body_for_city(
                slug, source, source_key, http_client, storage
            )
            status = body_validation.get("status", "skip")
            validated_body = body_validation.get("validated_body")

            if status == "corrected":
                print(f"\n      ✓ BODY CORRECTED: {body_validation.get('correction_note')}")
                try:
                    source = storage.read_json(source_key)
                    best = source.get("best_source") or {}
                    config = best.get("config") or {}
                except Exception:
                    pass
                if validated_body:
                    body = validated_body
            elif status == "ok" and validated_body:
                body = validated_body
            elif status == "unresolved":
                print(f"\n      ⚠ BODY MISMATCH ({platform}): {body_validation.get('reason')}")

        # ── Platform dispatch ─────────────────────────────────────────────
        upcoming: list[dict] = []

        if platform == "legistar":
            upcoming = await scan_legistar(city, config, http_client, source_url=source_url)
        elif platform == "civicplus":
            upcoming = await scan_civicplus(city, config, source_url, http_client)
        elif platform == "boarddocs":
            upcoming = await scan_boarddocs(city, config, source_url, http_client)
        elif platform == "civicclerk":
            upcoming = await scan_civicclerk(city, config, source_url, http_client)
        elif platform == "escribe":
            upcoming = await scan_escribe(city, config, source_url, http_client)
        elif platform in ("granicus", "swagit"):
            upcoming = await scan_granicus(city, config, source_url, http_client)
        elif platform == "novus":
            upcoming = await scan_novus(city, config, source_url, http_client)
        elif platform == "municode":
            upcoming = await scan_municode(city, config, source_url, http_client)
        elif platform in ("unknown", "generic_html") and source_url and os.environ.get("FIRECRAWL_API_KEY"):
            upcoming = await scan_generic(source_url, city, state)

        # ── Fallback: try public_agenda_url if platform scanner failed ────
        if not upcoming and os.environ.get("FIRECRAWL_API_KEY"):
            public_url = source.get("public_agenda_url", "")
            fallback_url = public_url or source_url
            if fallback_url and platform not in ("unknown", "generic_html"):
                upcoming = await scan_generic(fallback_url, city, state)

        # ── Body filter ───────────────────────────────────────────────────
        upcoming = filter_by_body(upcoming, body)

        return {
            "city_slug": slug,
            "city": city,
            "state": state,
            "body": body,
            "platform": platform,
            "scanned_at": datetime.now(UTC).isoformat(),
            "body_validation": body_validation,
            "upcoming": upcoming,
        }
    finally:
        if owns_client:
            await http_client.aclose()
