"""
router.py — Platform dispatcher: routes each city to the correct collector.

Reads source.json via the storage backend, extracts the platform, and calls
the appropriate collector function. Falls back to misc/replay then misc/reason
for unknown platforms.

Platform map:
    legistar   → collect_legistar
    civicplus  → collect_civicplus
    civicclerk → collect_civicclerk
    granicus   → collect_granicus (auto-detects classic vs swagit from URL)
    swagit     → collect_granicus (NEW_SWAGIT)
    diligent   → misc/reason (Playwright required)
    escribe    → collect_escribe
    boarddocs  → collect_boarddocs
    novus      → collect_novus
    municode   → collect_municode
    generic    → misc/replay → misc/reason
    unknown    → misc/replay → misc/reason
    unknown_spa→ misc/reason (Playwright required)
"""

import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent.parent

from .models import CollectionResult
from .storage import StorageBackend
from .config import AgentConfig, city_to_slug, find_city_slug
from . import notification_log
from .manifest import load_manifest, validate_against_manifest
from .misc.replay import collect_with_replay, ReplayFailed
from .misc.reason import collect_with_reason, ReasonFailed


# ── Dedicated collector adapters ──────────────────────────────────────────────

async def _collect_legistar(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.legistar import LegistarConfig, collect_legistar

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]

    # Load manifest before collection so expected_body can guide filtering
    manifest = load_manifest(city_slug, storage, cfg.sources_prefix)
    expected_body = (manifest or {}).get("expected_body", "")
    if expected_body:
        print(f"  [manifest] expected_body={expected_body!r}")

    legistar_slug = best.get("config", {}).get("legistar_slug", "")
    if not legistar_slug:
        url = best.get("url", "")
        parsed_url = urlparse(url)
        # Pattern 1: https://webapi.legistar.com/v1/{slug}/events
        parts = parsed_url.path.strip("/").split("/")
        if len(parts) > 1 and parts[0] == "v1":
            legistar_slug = parts[1]
        # Pattern 2: https://{slug}.legistar.com/...
        elif "legistar.com" in parsed_url.netloc:
            subdomain = parsed_url.netloc.split(".")[0]
            if subdomain and subdomain != "webapi":
                legistar_slug = subdomain

    if not legistar_slug:
        return CollectionResult.error_result(city, state, "legistar", "Could not determine Legistar slug")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/legistar"

    legistar_cfg = LegistarConfig(
        base_url=f"https://webapi.legistar.com/v1/{legistar_slug}",
        city_name=city,
        output_prefix=output_prefix,
        storage=storage,
        lookback_days=cfg.lookback_days,
        expected_body=expected_body,
        agendas_only=cfg.agendas_only,
    )

    result = await collect_legistar(legistar_cfg)

    collection_result = CollectionResult(
        city=city,
        state=state,
        platform="legistar",
        events_found=result.events_count,
        pdfs_downloaded=result.pdf_count,
        events=[],
    )

    # Manifest validation (best-effort) — reuse manifest loaded above
    if manifest:
        bodies_key = f"{cfg.sources_prefix}/{city_slug}/data/legistar/bodies.json"
        try:
            bodies = storage.read_json(bodies_key)
            body_names = [b.get("BodyName", "") for b in bodies if b.get("BodyName")]
            is_valid, reason = validate_against_manifest(manifest, body_names)
            if not is_valid:
                print(f"  [manifest] VALIDATION FAILED for {city}: {reason}")
                notification_log.log_event(
                    notification_log.COLLECTION_FAILED, city, state,
                    storage=storage, logs_prefix=cfg.logs_prefix,
                    platform="legistar", error=f"manifest_mismatch: {reason}",
                )
                return CollectionResult.error_result(city, state, "legistar", f"manifest_mismatch: {reason}")
        except Exception:
            pass  # manifest validation is best-effort

    return collection_result


async def _collect_civicplus(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.civicplus_scraper import CivicPlusConfig, collect_civicplus

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]

    # Load manifest before collection so expected_body can guide category selection
    manifest = load_manifest(city_slug, storage, cfg.sources_prefix)
    expected_body = (manifest or {}).get("expected_body", "")
    if expected_body:
        print(f"  [manifest] expected_body={expected_body!r}")

    url = best.get("url", "")
    domain = best.get("config", {}).get("domain", "")
    if not domain:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")

    if not domain:
        return CollectionResult.error_result(city, state, "civicplus", "Could not determine CivicPlus domain")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/civicplus"

    council_category_id = best.get("config", {}).get("council_category_id")
    civicplus_cfg = CivicPlusConfig(
        domain=domain,
        city_name=city,
        output_prefix=output_prefix,
        storage=storage,
        download_pdfs=cfg.download_pdfs,
        council_category_id=council_category_id,
        expected_body=expected_body,
    )

    result = await collect_civicplus(civicplus_cfg)

    return CollectionResult(
        city=city,
        state=state,
        platform="civicplus",
        events_found=result.meetings_found,
        pdfs_downloaded=result.pdfs_downloaded,
        events=[],
    )


async def _collect_civicclerk(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.civicclerk import CivicClerkConfig, collect_civicclerk

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]

    url = best.get("config", {}).get("civicclerk_url") or best.get("url", "")
    # Extract tenant from https://{tenant}.portal.civicclerk.com
    host = urlparse(url).netloc  # e.g. "claytonnc.portal.civicclerk.com"
    tenant = host.split(".")[0] if host else ""

    if not tenant:
        return CollectionResult.error_result(city, state, "civicclerk", "Could not determine CivicClerk tenant")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/civicclerk"

    council_categories = best.get("config", {}).get("council_categories")
    cc_cfg = CivicClerkConfig(
        tenant=tenant,
        city_name=city,
        output_prefix=output_prefix,
        storage=storage,
        lookback_days=cfg.lookback_days,
        download_pdfs=cfg.download_pdfs,
        **({"council_categories": council_categories} if council_categories else {}),
    )

    result = await collect_civicclerk(cc_cfg)

    collection_result = CollectionResult(
        city=city,
        state=state,
        platform="civicclerk",
        events_found=result.council_events,
        pdfs_downloaded=result.pdfs_downloaded,
        events=[],
    )

    # Manifest validation (best-effort)
    manifest = load_manifest(city_slug, storage, cfg.sources_prefix)
    if manifest:
        events_key = f"{cfg.sources_prefix}/{city_slug}/data/civicclerk/events.json"
        try:
            events = storage.read_json(events_key)
            categories = list({e.get("categoryName", "") for e in events if e.get("categoryName")})
            is_valid, reason = validate_against_manifest(manifest, categories)
            if not is_valid:
                print(f"  [manifest] VALIDATION FAILED for {city}: {reason}")
                notification_log.log_event(
                    notification_log.COLLECTION_FAILED, city, state,
                    storage=storage, logs_prefix=cfg.logs_prefix,
                    platform="civicclerk", error=f"manifest_mismatch: {reason}",
                )
                return CollectionResult.error_result(city, state, "civicclerk", f"manifest_mismatch: {reason}")
        except Exception:
            pass  # manifest validation is best-effort

    return collection_result


async def _collect_granicus(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.granicus_scraper import (
        GranicusConfig, collect_granicus, CLASSIC_GRANICUS, NEW_SWAGIT
    )

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]

    url = best.get("config", {}).get("granicus_url") or best.get("url", "")
    parsed = urlparse(url)
    host = parsed.netloc  # e.g. "greenville.granicus.com" or "beaumonttx.new.swagit.com"

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/granicus"

    if "new.swagit.com" in host:
        # New Swagit: subdomain from host
        subdomain = host.split(".")[0]
        granicus_cfg = GranicusConfig(
            platform=NEW_SWAGIT,
            subdomain=subdomain,
            city_name=city,
            output_prefix=output_prefix,
            storage=storage,
            lookback_days=cfg.lookback_days,
            download_pdfs=cfg.download_pdfs,
        )
    else:
        # Classic Granicus: extract subdomain and view_id from URL
        subdomain = host.split(".")[0]  # e.g. "greenville"
        qs = parse_qs(parsed.query)
        view_id = int(qs.get("view_id", ["1"])[0])

        granicus_cfg = GranicusConfig(
            platform=CLASSIC_GRANICUS,
            subdomain=subdomain,
            city_name=city,
            output_prefix=output_prefix,
            storage=storage,
            view_id=view_id,
            lookback_days=cfg.lookback_days,
            download_pdfs=cfg.download_pdfs,
        )

    result = await collect_granicus(granicus_cfg)

    return CollectionResult(
        city=city,
        state=state,
        platform="granicus",
        events_found=result.council_events,
        pdfs_downloaded=result.pdfs_downloaded,
        events=[],
    )


async def _collect_escribe(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.escribemeetings import EscribeConfig, collect_escribemeetings

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]
    url = best.get("url", "")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/escribemeetings"

    escribe_cfg = EscribeConfig(
        base_url=url,
        city_name=city,
        output_prefix=output_prefix,
        storage=storage,
        lookback_days=cfg.lookback_days,
    )

    result = await collect_escribemeetings(escribe_cfg)

    return CollectionResult(
        city=city,
        state=state,
        platform="escribe",
        events_found=result.events_count,
        pdfs_downloaded=result.pdf_count,
        events=[],
    )


async def _collect_municode(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.municode import MunicodeConfig, collect_municode

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]

    url = best.get("config", {}).get("municode_url") or best.get("url", "")
    if not url:
        return CollectionResult.error_result(city, state, "municode", "Could not determine Municode portal URL")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/municode"

    mc_cfg = MunicodeConfig(
        portal_url=url,
        city_name=city,
        output_prefix=output_prefix,
        storage=storage,
        lookback_days=cfg.lookback_days,
        download_pdfs=cfg.download_pdfs,
    )

    result = await collect_municode(mc_cfg)

    return CollectionResult(
        city=city,
        state=state,
        platform="municode",
        events_found=result.meetings_found,
        pdfs_downloaded=result.pdfs_downloaded,
        events=[],
    )


async def _collect_novus(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.novus_scraper import NovusConfig, collect_novus

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]

    url = best.get("config", {}).get("novus_url") or best.get("url", "")
    if not url:
        return CollectionResult.error_result(city, state, "novus", "Could not determine Novus portal URL")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/novus"

    novus_cfg = NovusConfig(
        portal_url=url,
        city_name=city,
        output_prefix=output_prefix,
        storage=storage,
        lookback_days=cfg.lookback_days,
        download_pdfs=cfg.download_pdfs,
    )

    result = await collect_novus(novus_cfg)

    return CollectionResult(
        city=city,
        state=state,
        platform="novus",
        events_found=result.meetings_found,
        pdfs_downloaded=result.pdfs_downloaded,
        events=[],
    )


async def _collect_boarddocs(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    from meeting_pipeline.collectors.boarddocs import BoardDocsConfig, collect_boarddocs

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)
    best = source["best_source"]

    # Load manifest before collection so expected_body can guide committee filtering
    manifest = load_manifest(city_slug, storage, cfg.sources_prefix)
    expected_body = (manifest or {}).get("expected_body", "")
    if expected_body:
        print(f"  [manifest] expected_body={expected_body!r}")

    url = best.get("url", "").removesuffix("/Public").removesuffix("/public")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/boarddocs"

    bd_cfg = BoardDocsConfig(
        base_url=url,
        city_name=city,
        output_prefix=output_prefix,
        storage=storage,
        lookback_days=cfg.lookback_days,
        download_pdfs=cfg.download_pdfs,
        expected_body=expected_body,
    )

    result = await collect_boarddocs(bd_cfg)

    return CollectionResult(
        city=city,
        state=state,
        platform="boarddocs",
        events_found=result.events_count,
        pdfs_downloaded=result.pdf_count,
        events=[],
    )


# ── Generic direct-download collector (platform=unknown with PDF URLs) ────────

async def _collect_generic_direct(
    event: dict, source: dict, storage: StorageBackend, cfg: AgentConfig
) -> CollectionResult:
    """
    Direct httpx PDF downloader for platform=unknown cities.

    Reads upcoming_meetings.json (produced by scan_generic_firecrawl during scan),
    downloads each agenda PDF directly, and writes meetings.json in the same
    shape as other collectors so extract_and_normalize can process them.

    Only runs when:
      - platform == "unknown"
      - upcoming_meetings.json exists with at least one meeting having an agenda_url
    """
    import httpx
    from datetime import datetime, timezone

    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)

    upcoming_key = f"{cfg.sources_prefix}/{city_slug}/upcoming_meetings.json"
    if not storage.exists(upcoming_key):
        return CollectionResult.error_result(city, state, "generic_direct", "No upcoming_meetings.json found")

    try:
        upcoming_data = storage.read_json(upcoming_key)
    except Exception as e:
        return CollectionResult.error_result(city, state, "generic_direct", f"Could not read upcoming_meetings.json: {e}")

    meetings_with_pdfs = [
        m for m in upcoming_data.get("upcoming", [])
        if m.get("agenda_url") and m["agenda_url"].lower().endswith(".pdf")
    ]

    if not meetings_with_pdfs:
        return CollectionResult.error_result(city, state, "generic_direct", "No PDF agenda URLs in upcoming_meetings.json")

    output_prefix = f"{cfg.sources_prefix}/{city_slug}/data/generic"
    pdf_count = 0
    meetings_out = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for m in meetings_with_pdfs:
            date = m.get("date", "")
            pdf_url = m["agenda_url"]
            filename = f"{date}_agenda.pdf"
            pdf_key = f"{output_prefix}/pdfs/{filename}"

            if storage.exists(pdf_key):
                print(f"  [generic_direct] Already downloaded: {filename}")
                pdf_count += 1
            else:
                try:
                    resp = await client.get(pdf_url)
                    resp.raise_for_status()
                    if len(resp.content) < 5000:
                        print(f"  [generic_direct] PDF too small ({len(resp.content)}B), skipping: {pdf_url}")
                        continue
                    storage.write_bytes(pdf_key, resp.content)
                    pdf_count += 1
                    print(f"  [generic_direct] Downloaded: {filename} ({len(resp.content) // 1024}KB)")
                except Exception as e:
                    print(f"  [generic_direct] Failed to download {pdf_url}: {e}")
                    continue

            meetings_out.append({
                "date": date,
                "title": m.get("title", "City Council Meeting"),
                "body": "City Council",
                "hasAgenda": True,
                "agenda_files": [{"name": filename, "type": "Agenda", "url": pdf_url}],
                "source_url": source.get("best_source", {}).get("url", ""),
                "platform": "generic_direct",
                "collected_at": datetime.now(timezone.utc).isoformat(),
            })

    if not meetings_out:
        return CollectionResult.error_result(city, state, "generic_direct", "All PDF downloads failed")

    storage.write_json(f"{output_prefix}/meetings.json", meetings_out)
    print(f"  [generic_direct] Saved {len(meetings_out)} meetings, {pdf_count} PDFs")

    return CollectionResult(
        city=city,
        state=state,
        platform="generic_direct",
        events_found=len(meetings_out),
        pdfs_downloaded=pdf_count,
        events=[],
    )


# ── Platform → collector function map ────────────────────────────────────────

# Platforms that have dedicated Lambda-safe collectors
DEDICATED_COLLECTORS = {
    "legistar":   _collect_legistar,
    "civicplus":  _collect_civicplus,
    "civicclerk": _collect_civicclerk,
    "granicus":   _collect_granicus,
    "swagit":     _collect_granicus,   # same collector, detects from URL
    "escribe":    _collect_escribe,
    "boarddocs":  _collect_boarddocs,
    "municode":   _collect_municode,
    "novus":      _collect_novus,
}

# Platforms that skip replay and go straight to reason (Playwright required)
REASON_ONLY_PLATFORMS = {"diligent", "unknown_spa"}

# Platforms that route through misc (replay → reason)
MISC_PLATFORMS = {"generic", "unknown", "destinyhosted"}


# ── Main dispatch function ────────────────────────────────────────────────────

async def route_city(
    event: dict,
    storage: StorageBackend,
    cfg: AgentConfig,
) -> CollectionResult:
    """
    Route a city to the appropriate collector based on source.json platform.

    Fallback chain:
        dedicated collector → misc/replay → misc/reason → COLLECTION_FAILED

    Args:
        event:   {"city": "...", "state": "..."}
        storage: Storage backend
        cfg:     Agent configuration
    """
    city = event["city"]
    state = event["state"]

    # Load source.json
    city_slug = find_city_slug(city, state, cfg, storage)
    if not city_slug:
        msg = f"No source.json found for {city}, {state}"
        notification_log.log_event(
            notification_log.NO_PORTAL, city, state,
            storage=storage, logs_prefix=cfg.logs_prefix, error=msg,
        )
        return CollectionResult.error_result(city, state, "unknown", msg)

    source_key = f"{cfg.sources_prefix}/{city_slug}/source.json"
    try:
        source = storage.read_json(source_key)
    except Exception as e:
        return CollectionResult.error_result(city, state, "unknown", f"Could not read source.json: {e}")

    platform = source.get("best_source", {}).get("platform", "unknown")

    # ── Freshness gate: block wrong_entity / wrong_city before wasting API calls ─
    freshness = source.get("best_source", {}).get("freshness", "")
    if freshness in ("wrong_entity", "wrong_city"):
        msg = f"Source marked {freshness}: {source.get('best_source', {}).get('notes', '')}"
        notification_log.log_event(
            notification_log.COLLECTION_FAILED, city, state,
            storage=storage, logs_prefix=cfg.logs_prefix,
            platform=platform, error=msg,
        )
        return CollectionResult.error_result(city, state, platform, msg)

    # ── Step 1: Dedicated collector (Lambda-safe) ─────────────────────────────
    if platform in DEDICATED_COLLECTORS:
        try:
            result = await DEDICATED_COLLECTORS[platform](event, source, storage, cfg)
            if result.error is None:
                notification_log.log_event(
                    notification_log.COLLECTION_SUCCESS, city, state,
                    storage=storage, logs_prefix=cfg.logs_prefix,
                    platform=platform, events_found=result.events_found,
                    pdfs_downloaded=result.pdfs_downloaded,
                )
                return result
            print(f"[router] Dedicated collector for {city} failed: {result.error}, trying misc fallback")
        except Exception as e:
            print(f"[router] Dedicated collector for {city} raised: {e}, trying misc fallback")
        # Fall through to misc fallback below

    # ── Step 1b: platform=unknown with PDF URLs from scan ────────────────────
    elif platform == "unknown":
        try:
            result = await _collect_generic_direct(event, source, storage, cfg)
            if result.error is None:
                notification_log.log_event(
                    notification_log.COLLECTION_SUCCESS, city, state,
                    storage=storage, logs_prefix=cfg.logs_prefix,
                    platform="generic_direct", events_found=result.events_found,
                    pdfs_downloaded=result.pdfs_downloaded,
                )
                return result
            print(f"[router] generic_direct for {city} failed ({result.error}), trying misc fallback")
        except Exception as e:
            print(f"[router] generic_direct for {city} raised: {e}, trying misc fallback")
        # Fall through to misc fallback below

    # ── Step 1c: Reason-only platforms (Playwright, no replay fallback) ───────
    elif platform in REASON_ONLY_PLATFORMS:
        try:
            result = await collect_with_reason(event, source, storage, cfg)
            notification_log.log_event(
                notification_log.COLLECTION_SUCCESS, city, state,
                storage=storage, logs_prefix=cfg.logs_prefix,
                platform="playwright_llm", events_found=result.events_found,
                pdfs_downloaded=result.pdfs_downloaded,
            )
            return result
        except ReasonFailed as e:
            error_msg = str(e)
            notification_log.log_event(
                notification_log.COLLECTION_FAILED, city, state,
                storage=storage, logs_prefix=cfg.logs_prefix,
                platform=platform, error=error_msg,
            )
            return CollectionResult.error_result(city, state, platform, error_msg)

    # ── Step 2 & 3: Misc fallback — replay → reason ───────────────────────────
    # Reaches here when:
    #   - platform is a misc/unknown platform (no dedicated collector), OR
    #   - a dedicated collector failed and fell through above
    if True:
        # Step 1: Try replay if nav_config exists
        has_nav_config = bool(source.get("best_source", {}).get("nav_config"))
        if has_nav_config:
            try:
                result = await collect_with_replay(event, source, storage, cfg)
                notification_log.log_event(
                    notification_log.COLLECTION_SUCCESS, city, state,
                    storage=storage, logs_prefix=cfg.logs_prefix,
                    platform="replay", events_found=result.events_found,
                    pdfs_downloaded=result.pdfs_downloaded,
                )
                return result
            except (ReplayFailed, ValueError) as e:
                print(f"[router] Replay failed for {city}: {e}, escalating to reason")

        # Step 2: Reason mode (Playwright + LLM)
        notification_log.log_event(
            notification_log.COLLECTOR_NEEDED, city, state,
            storage=storage, logs_prefix=cfg.logs_prefix,
            platform=platform, has_nav_config=has_nav_config,
        )
        try:
            result = await collect_with_reason(event, source, storage, cfg)
            notification_log.log_event(
                notification_log.COLLECTION_SUCCESS, city, state,
                storage=storage, logs_prefix=cfg.logs_prefix,
                platform="playwright_llm", events_found=result.events_found,
                pdfs_downloaded=result.pdfs_downloaded,
            )
            return result
        except ReasonFailed as e:
            error_msg = str(e)
            notification_log.log_event(
                notification_log.COLLECTION_FAILED, city, state,
                storage=storage, logs_prefix=cfg.logs_prefix,
                platform=platform, error=error_msg,
            )
            return CollectionResult.error_result(city, state, platform, error_msg)

    # Should not reach here
    return CollectionResult.error_result(city, state, platform, f"No handler for platform '{platform}'")
