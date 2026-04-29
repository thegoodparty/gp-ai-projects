"""
process.py — Single-city discovery entry point.

Provides process_one_city() which is the function a Lambda handler
or the orchestrator calls. Handles the full discovery lifecycle:
  1. Run source discovery (search, crawl, probe, rank)
  2. Source stability guard (don't downgrade working sources)
  3. Body validation with fallback to alternate candidates
  4. Source verification (download and check a real agenda PDF)
  5. Write result to S3
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

from meeting_pipeline.shared.config import AgentConfig, get_storage, city_to_slug
from meeting_pipeline.shared.constants import COLLECTION_METHODS

# Platforms that support body validation
_VALIDATABLE_PLATFORMS = {"legistar", "civicplus", "civicclerk", "boarddocs"}

# Freshness values that should NOT be replaced by a new discovery result
_NON_WORKING = {"empty", "blocked", "wrong_entity"}


async def process_one_city(
    city: str,
    state: str,
    expected_body: str = "",
    known_sources: Optional[dict] = None,
    tavily_client=None,
    http_client: Optional[httpx.AsyncClient] = None,
    cfg: Optional[AgentConfig] = None,
    storage=None,
) -> dict:
    """
    Run source discovery for a single city. Returns source.json dict.

    Handles the full lifecycle: discovery → stability guard → body
    validation → verification → S3 write.

    Args:
        city: City name (e.g. "Chapel Hill")
        state: 2-letter state abbreviation (e.g. "NC")
        expected_body: Expected governing body name from manifest
        known_sources: Pre-existing known source config
        tavily_client: Shared Tavily client (created if not provided)
        http_client: Shared httpx client (created if not provided)
        cfg: AgentConfig (created from env if not provided)
        storage: StorageBackend (created from cfg if not provided)

    Returns:
        source.json dict with best_source, verification, etc.
    """
    from meeting_pipeline.stages.discover.main_flow import run_source_discover

    if cfg is None:
        cfg = AgentConfig.from_env()
    if storage is None:
        storage = get_storage(cfg)

    # Create clients if not provided
    if tavily_client is None:
        tavily_key = os.environ.get("TAVILY_API_KEY", "")
        if tavily_key:
            from tavily import TavilyClient
            tavily_client = TavilyClient(api_key=tavily_key)

    owns_http = http_client is None
    if owns_http:
        http_client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
            follow_redirects=True,
            timeout=20,
        )

    slug = f"{city_to_slug(city)}-{state}"
    source_key = f"{cfg.sources_prefix}/{slug}/source.json"

    # Load expected_body from manifest if not provided
    if not expected_body:
        expected_body = _load_expected_body(slug, cfg, storage)

    try:
        # ── Step 1: Run discovery ────────────────────────────────────
        result = await run_source_discover(
            city=city,
            state=state,
            known_sources=known_sources or {},
            tavily=tavily_client,
            http=http_client,
            expected_body=expected_body,
        )

        # ── Step 2: Source stability guard ────────────────────────────
        result = _apply_stability_guard(result, source_key, storage)

        # ── Step 3: Write to S3 ──────────────────────────────────────
        try:
            storage.write_json(source_key, result)
        except Exception as e:
            print(f"  [warn] Could not write source.json for {city}, {state}: {e}")

        # ── Step 4: Body validation ──────────────────────────────────
        result = await _run_body_validation(
            result, slug, source_key, http_client, storage,
        )

        # ── Step 5: Verification ─────────────────────────────────────
        result = await _run_verification(result, http_client)

        # Write final result (with validation + verification)
        try:
            storage.write_json(source_key, result)
        except Exception:
            pass

        return result
    finally:
        if owns_http:
            await http_client.aclose()


def _load_expected_body(slug: str, cfg: AgentConfig, storage) -> str:
    """Load expected_body from manifest.json if it exists."""
    manifest_key = f"{cfg.sources_prefix}/{slug}/manifest.json"
    try:
        if storage.exists(manifest_key):
            manifest = storage.read_json(manifest_key)
            return (manifest or {}).get("expected_body", "")
    except Exception:
        pass
    return ""


def _apply_stability_guard(result: dict, source_key: str, storage) -> dict:
    """Don't downgrade from a working source to a broken one."""
    from meeting_pipeline.stages.discover.scoring import PLATFORM_TIER

    new_bs = result.get("best_source") or {}
    new_platform = new_bs.get("platform", "unknown")
    new_tier = PLATFORM_TIER.get(new_platform, 4)
    new_freshness = new_bs.get("freshness", "unknown")

    try:
        if not storage.exists(source_key):
            return result

        existing = storage.read_json(source_key)
        old_bs = existing.get("best_source") or {}
        old_platform = old_bs.get("platform", "unknown")
        old_tier = PLATFORM_TIER.get(old_platform, 4)
        old_freshness = old_bs.get("freshness", "unknown")

        old_is_working = old_freshness not in _NON_WORKING
        if not old_is_working:
            return result  # existing is broken, accept anything new

        # Don't replace working source with broken result
        if new_freshness in _NON_WORKING:
            print(f"  [guard] keeping existing {old_platform} ({old_freshness}) "
                  f"over new {new_platform} ({new_freshness})")
            return existing

        # Don't downgrade platform tier
        if new_tier < old_tier:
            print(f"  [guard] keeping existing {old_platform} (tier={old_tier}) "
                  f"over new {new_platform} (tier={new_tier})")
            return existing
    except Exception:
        pass

    return result


async def _run_body_validation(
    result: dict, slug: str, source_key: str,
    http_client: httpx.AsyncClient, storage,
) -> dict:
    """Validate governing body and auto-correct or fallback to alternate candidates."""
    from meeting_pipeline.shared.body_validation import validate_body_for_city

    bs = result.get("best_source") or {}
    platform = bs.get("platform", "")

    if platform not in _VALIDATABLE_PLATFORMS:
        return result

    try:
        bv = await validate_body_for_city(slug, result, source_key, http_client, storage)
        status = bv.get("status", "skip")

        if status == "corrected":
            print(f"  [body] corrected → {bv.get('correction_note', '')}")
        elif status == "unresolved":
            print(f"  [body] UNRESOLVED for {platform} — {bv.get('reason', '')}")
            result = _try_fallback_candidates(result, slug, source_key, http_client, storage, bv)
        elif status not in ("skip", "ok"):
            print(f"  [body] {status}: {bv.get('reason', '')}")
    except Exception as e:
        print(f"  [body] validation error: {e}")

    return result


def _try_fallback_candidates(
    result: dict, slug: str, source_key: str,
    http_client: httpx.AsyncClient, storage, bv: dict,
) -> dict:
    """When body validation fails, try alternate candidates from discovery."""
    import asyncio
    from meeting_pipeline.shared.body_validation import validate_body_for_city

    candidates = (result.get("all_candidates") or [])[1:]

    for alt in candidates:
        alt_platform = alt.get("platform", "")
        alt_freshness = alt.get("freshness", "")

        alt_source = {
            **result,
            "best_source": {
                "platform": alt_platform,
                "url": alt.get("url", ""),
                "display_url": alt.get("url", ""),
                "freshness": alt_freshness,
                "most_recent_date": alt.get("most_recent_date"),
                "days_since_update": alt.get("days_since_update"),
                "date_source": alt.get("date_source"),
                "collection_method": COLLECTION_METHODS.get(alt_platform, "fetch_and_parse"),
                "config": alt.get("config") or {},
                "notes": alt.get("notes") or "",
            },
        }

        if alt_platform not in _VALIDATABLE_PLATFORMS:
            # Non-validatable but fresh → accept as fallback
            if alt_freshness in ("fresh", "stale"):
                print(f"  [body] fallback: switched to {alt_platform} ({alt_freshness})")
                _safe_write(storage, source_key, alt_source)
                return alt_source
            continue

        try:
            alt_bv = asyncio.get_event_loop().run_until_complete(
                validate_body_for_city(slug, alt_source, source_key, http_client, storage)
            )
        except Exception:
            continue

        if alt_bv.get("status") in ("ok", "corrected"):
            print(f"  [body] fallback: switched to {alt_platform} ({alt.get('url', '')})")
            _safe_write(storage, source_key, alt_source)
            return alt_source

    # No fallback worked — mark as wrong_entity
    print(f"  [body] no fallback resolved — marking as wrong_entity")
    if result.get("best_source"):
        result["best_source"]["freshness"] = "wrong_entity"
        result["best_source"]["wrong_entity_reason"] = bv.get(
            "reason", "body unresolved — no matching governing body found"
        )
        _safe_write(storage, source_key, result)

    return result


async def _run_verification(result: dict, http_client: httpx.AsyncClient) -> dict:
    """Download and verify a real agenda PDF from the discovered source."""
    from meeting_pipeline.shared.verify_source import (
        verify_agenda_url, _find_past_agenda_from_platform, _find_pdf_links_on_page,
    )
    from datetime import datetime, timezone

    best = result.get("best_source", {})
    if not best.get("url"):
        return result
    if best.get("freshness") in ("wrong_entity", "wrong_city", "blocked", "empty"):
        return result

    # Try source URL if it's a PDF
    verify_url = result.get("public_agenda_url") or best.get("url", "")
    agenda_url = None

    if verify_url and ".pdf" in verify_url.lower():
        agenda_url = verify_url
    else:
        # Try platform API for past agendas
        platform = best.get("platform", "unknown")
        config = best.get("config", {})
        if platform not in ("unknown", "generic_html"):
            agenda_url = await _find_past_agenda_from_platform(
                platform, config, best.get("url", ""), http_client,
            )
        # Try scraping the page for PDF links
        if not agenda_url:
            source_url = best.get("url", "")
            if source_url:
                pdf_links = await _find_pdf_links_on_page(source_url, http_client)
                if pdf_links:
                    agenda_url = pdf_links[0]

    if agenda_url:
        verification = await verify_agenda_url(agenda_url, client=http_client)
    else:
        verification = {
            "status": "unverified",
            "reason": "No agenda URL found to verify during discovery",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    best["verification"] = verification
    result["best_source"] = best
    return result


def _safe_write(storage, key: str, data: dict):
    """Write to S3, swallowing errors."""
    try:
        storage.write_json(key, data)
    except Exception:
        pass
