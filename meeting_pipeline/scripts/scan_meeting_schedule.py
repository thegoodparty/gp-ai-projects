"""
scan_meeting_schedule.py — Lightweight scan for upcoming meeting dates.

For each city with a valid source.json, makes a minimal API/scraper call to
discover upcoming meeting dates and whether an agenda has been posted. Writes
per-city upcoming_meetings.json without downloading any PDFs.

This is the first stage of a two-stage pipeline:
  1. scan_meeting_schedule.py (daily, cheap) — discover dates + agenda status
  2. Full collection scripts (triggered only when agenda_posted=true)

Output: sources/{city}/upcoming_meetings.json
  {
    "city_slug": "chapel-hill-NC",
    "city": "Chapel Hill",
    "state": "NC",
    "body": "Town Council",
    "platform": "legistar",
    "scanned_at": "2026-04-15T...",
    "upcoming": [
      {
        "date": "2026-04-22",
        "title": "Town Council Regular Meeting",
        "agenda_posted": true,
        "agenda_url": "https://...",
        "event_id": "12345"
      }
    ]
  }

Usage:
    # Scan all cities with a known source
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py

    # Scan a single city
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --city chapel-hill-NC

    # Dry-run: list what would be scanned
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --dry-run

    # Only show cities where agenda_posted changed from false → true since last scan
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/scan_meeting_schedule.py --report-new
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.body_validation import (
    REJECT_KEYWORDS,
    GOVERNING_KEYWORDS,
    score_body_match,
    best_body_match,
    validate_legistar_body,
    validate_civicplus_body,
    validate_civicclerk_body,
    validate_boarddocs_body,
    apply_body_validation as _apply_body_validation,
    validate_body_for_city,
)

from meeting_pipeline.shared.constants import (
    LOOKAHEAD_DAYS, LOOKBACK_DAYS, SUPPORTED_PLATFORMS,
    IFRAME_PLATFORM_DOMAINS, GENERIC_MEETING_TITLES,
)
from meeting_pipeline.stages.scan.platforms.legistar import scan_legistar
from meeting_pipeline.stages.scan.platforms.civicplus import scan_civicplus
from meeting_pipeline.stages.scan.platforms.boarddocs import scan_boarddocs
from meeting_pipeline.stages.scan.platforms.civicclerk import scan_civicclerk
from meeting_pipeline.stages.scan.platforms.granicus import scan_granicus
from meeting_pipeline.stages.scan.platforms.escribe import scan_escribe


_COST = {
    "firecrawl_scrape_basic": 0,    # basic scrape (~1 credit)
    "firecrawl_scrape_js": 0,       # scrape with JS rendering (~5 credits)
    "firecrawl_llm_extract": 0,     # Firecrawl extract API (~5-30 credits)
    "gemini_extract": 0,            # Gemini LLM calls for meeting extraction
}

# ============================================================================
# GENERIC FIRECRAWL SCANNER (platform=unknown)
# ============================================================================

async def scan_generic_firecrawl(source_url: str, city: str, state: str) -> list[dict]:
    """Scan a non-platform agenda page. Delegates to shared/generic_agenda_scanner.
    See that module for the three-tier approach (basic scrape → JS render → Gemini)."""
    from meeting_pipeline.shared.generic_agenda_scanner import scan_generic, cost as scanner_cost

    meetings = await scan_generic(source_url, city, state)

    # Sync cost tracking
    _COST["firecrawl_scrape_basic"] += scanner_cost["firecrawl_basic"]
    _COST["firecrawl_scrape_js"] += scanner_cost["firecrawl_js"]
    _COST["gemini_extract"] += scanner_cost["gemini_extract"]
    _COST["firecrawl_llm_extract"] += scanner_cost["firecrawl_llm_extract"]
    # Reset scanner costs so they don't double-count on next call
    for k in scanner_cost:
        scanner_cost[k] = 0

    return meetings


# ============================================================================
# MAIN SCAN DISPATCHER
# ============================================================================

async def scan_city(
    slug: str,
    source: dict,
    source_key: str,
    client: httpx.AsyncClient,
    storage,
    skip_body_validation: bool = False,
) -> dict | None:
    """Scan one city's upcoming meetings. Returns the upcoming_meetings record."""
    best = source.get("best_source") or {}
    platform = best.get("platform", "")
    config = best.get("config", {})
    source_url = best.get("url", "")
    city = source.get("city", slug)
    state = source.get("state", "")

    # Derive body name: manifest.json (from CSV) → source.json → empty
    body = best.get("expected_body", config.get("expected_body", ""))
    if not body:
        manifest_key = source_key.replace("/source.json", "/manifest.json")
        try:
            manifest = storage.read_json(manifest_key)
            body = manifest.get("expected_body", "")
        except Exception as e:
            pass  # manifest not found — body stays empty, filter will be lenient

    # ── Stage 1: Body validation (before any PDF downloads) ──────────────
    body_validation: dict = {}
    if not skip_body_validation and platform in SUPPORTED_PLATFORMS:
        body_validation = await validate_body_for_city(slug, source, source_key, client, storage)

        status = body_validation.get("status", "skip")
        validated_body = body_validation.get("validated_body")

        if status == "unresolved":
            # Try next-ranked supported candidate from all_candidates before giving up
            print(f"\n      ⚠ BODY MISMATCH ({platform}): {body_validation.get('reason')}")
            from meeting_pipeline.shared.constants import PLATFORM_TIER, COLLECTION_METHODS
            for alt in (source.get("all_candidates") or [])[1:]:
                alt_platform = alt.get("platform", "")
                if alt_platform not in SUPPORTED_PLATFORMS:
                    continue
                alt_source = {
                    **source,
                    "best_source": {
                        "platform": alt_platform,
                        "url": alt.get("url", ""),
                        "display_url": alt.get("url", ""),
                        "freshness": alt.get("freshness"),
                        "most_recent_date": alt.get("most_recent_date"),
                        "collection_method": COLLECTION_METHODS.get(alt_platform, "fetch_and_parse"),
                        "config": alt.get("config") or {},
                        "notes": alt.get("notes") or "",
                    },
                }
                try:
                    alt_bv = await validate_body_for_city(slug, alt_source, source_key, client, storage)
                except Exception:
                    continue
                if alt_bv.get("status") in ("ok", "corrected"):
                    print(f"\n      ↳ Switched to {alt_platform} ({alt.get('url', '')})")
                    source = alt_source
                    best = alt_source["best_source"]
                    platform = alt_platform
                    config = best.get("config", {})
                    source_url = best.get("url", "")
                    body_validation = alt_bv
                    status = alt_bv.get("status", "ok")
                    # Persist the better source so future scans use it
                    try:
                        storage.write_json(source_key, alt_source)
                    except Exception:
                        pass
                    break
            else:
                print(f"\n      No fallback candidate resolved body — scan may return wrong body")
        elif status == "corrected":
            print(f"\n      ✓ BODY CORRECTED: {body_validation.get('correction_note')}")
            # Re-read updated config (source.json was patched in-place)
            try:
                source = storage.read_json(source_key)
                best = source.get("best_source") or {}
                config = best.get("config") or {}
            except Exception:
                pass
            # Use the validated body name
            if validated_body:
                body = validated_body
        elif status == "ok" and validated_body:
            body = validated_body

    # ── Stage 2: Scan for upcoming meetings ──────────────────────────────
    upcoming: list[dict] = []

    if platform == "legistar":
        upcoming = await scan_legistar(city, config, client, source_url=source_url)
    elif platform == "civicplus":
        upcoming = await scan_civicplus(city, config, source_url, client)
    elif platform == "boarddocs":
        upcoming = await scan_boarddocs(city, config, source_url, client)
    elif platform == "civicclerk":
        upcoming = await scan_civicclerk(city, config, source_url, client)
    elif platform == "escribe":
        upcoming = await scan_escribe(city, config, source_url, client)
    elif platform == "granicus":
        upcoming = await scan_granicus(city, config, source_url, client)
    elif platform in ("unknown", "generic_html"):
        # Generic Firecrawl scan: scrape the agenda page (1 credit), extract PDF
        # links, parse meeting dates from filenames. Falls back to LLM extract
        # (~5-30 credits) when filename parsing finds nothing. No browser-use needed.
        if source_url and os.environ.get("FIRECRAWL_API_KEY"):
            upcoming = await scan_generic_firecrawl(source_url, city, state)
        else:
            upcoming = []
    else:
        # Unsupported platform — record that we know it exists but can't scan
        pass

    # ── Fallback for platform failures: try the public_agenda_url via generic scanner.
    # This handles CivicPlus 403s, SSL errors, etc. where the platform scanner
    # failed but the city's public website might still have agenda content.
    if not upcoming and os.environ.get("FIRECRAWL_API_KEY"):
        public_url = source.get("public_agenda_url", "")
        fallback_url = public_url or source_url
        if fallback_url and platform not in ("unknown", "generic_html"):
            upcoming = await scan_generic_firecrawl(fallback_url, city, state)

    # ── Body filter: drop events that don't match the governing body ────────
    # Uses score_body_match against the expected body from manifest.json.
    # Events scoring < 0 (REJECT_KEYWORDS hit) are always dropped.
    # Events scoring 0 (no match) are dropped when body is known.
    # Events with no title are kept.
    if body and upcoming:
        filtered = []
        dropped = []
        for m in upcoming:
            title = m.get("title", "")
            if not title:
                filtered.append(m)
                continue
            sc = score_body_match(title, body)
            if sc < 0:
                # Hard reject — advisory board, planning commission, etc.
                dropped.append(title)
            elif sc > 0:
                filtered.append(m)
            else:
                # score == 0: no match. Keep only if title is very generic
                # (e.g. "Regular Meeting", "Work Session") which likely IS the
                # governing body meeting but uses a generic title.
                title_lower = title.lower()
                is_generic = any(kw in title_lower for kw in GENERIC_MEETING_TITLES)
                if is_generic:
                    filtered.append(m)
                else:
                    dropped.append(title)
        if dropped:
            unique_dropped = sorted(set(dropped))
            print(f"    Body filter dropped {len(dropped)} events: {unique_dropped[:5]}{'...' if len(unique_dropped) > 5 else ''}")
        upcoming = filtered

    return {
        "city_slug": slug,
        "city": city,
        "state": state,
        "body": body,
        "platform": platform,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "body_validation": body_validation,
        "upcoming": upcoming,
    }


# ============================================================================
# BATCH RUNNER
# ============================================================================

async def run_batch(
    city_slug: str | None,
    dry_run: bool,
    report_new: bool,
    skip_body_validation: bool,
    cfg: AgentConfig,
    storage,
):
    # Load all city source.json files
    all_source_keys = storage.list_keys(cfg.sources_prefix)
    source_keys = [k for k in all_source_keys if k.endswith("/source.json")]

    if city_slug:
        source_keys = [k for k in source_keys if f"/{city_slug}/" in k]

    print(f"Schedule Scanner: {len(source_keys)} cities")
    print()

    if dry_run:
        for k in source_keys:
            slug = k.split("/")[-2]
            try:
                src = storage.read_json(k)
                platform = (src.get("best_source") or {}).get("platform", "?")
            except Exception:
                platform = "?"
            print(f"  {slug:<35} [{platform}]")
        return

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True,
        timeout=20,
    ) as client:

        results = {
            "scanned": 0, "skipped": 0, "errors": 0,
            "new_agendas": [],
            "body_corrections": [], "body_unresolved": [],
        }

        for i, key in enumerate(source_keys, 1):
            slug = key.split("/")[-2]
            try:
                source = storage.read_json(key)
            except Exception:
                results["errors"] += 1
                continue

            if not source:
                results["errors"] += 1
                continue

            platform = (source.get("best_source") or {}).get("platform", "")
            if platform not in SUPPORTED_PLATFORMS:
                print(f"[{i}/{len(source_keys)}] {slug} — skip ({platform} not supported)")
                results["skipped"] += 1
                continue

            # Load previous scan to detect agenda_posted changes
            prev_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
            prev = {}
            if storage.exists(prev_key):
                try:
                    prev = storage.read_json(prev_key)
                except Exception:
                    pass

            prev_posted = {m["date"]: m.get("agenda_posted", False)
                          for m in prev.get("upcoming", [])}

            print(f"[{i}/{len(source_keys)}] {slug} ({platform})...", end=" ", flush=True)
            try:
                record = await scan_city(
                    slug, source, key, client, storage,
                    skip_body_validation=skip_body_validation,
                )
                storage.write_json(prev_key, record)

                all_meetings = record.get("upcoming", [])
                past = [m for m in all_meetings if m.get("status") == "past"]
                upcoming = [m for m in all_meetings if m.get("status") != "past"]
                posted = [m for m in upcoming if m.get("agenda_posted")]
                unposted = [m for m in upcoming if not m.get("agenda_posted")]

                bv = record.get("body_validation", {})
                bv_status = bv.get("status", "")
                bv_note = ""
                if bv_status == "corrected":
                    bv_note = f" [BODY CORRECTED → '{bv.get('validated_body')}']"
                    results["body_corrections"].append({
                        "city": slug, "note": bv.get("correction_note"), "patch": bv.get("config_patch"),
                    })
                elif bv_status == "unresolved":
                    bv_note = f" [BODY UNRESOLVED ⚠]"
                    results["body_unresolved"].append({"city": slug, "reason": bv.get("reason")})

                past_note = f", last={past[-1]['date']}" if past else ""
                print(f"{len(upcoming)} upcoming ({len(posted)} posted, {len(unposted)} pending{past_note}){bv_note}")

                # Detect newly-posted agendas (only future meetings)
                for m in upcoming:
                    if m.get("agenda_posted") and not prev_posted.get(m["date"], False):
                        results["new_agendas"].append({"city": slug, "date": m["date"], "title": m["title"]})

                results["scanned"] += 1

            except Exception as e:
                print(f"ERROR: {e}")
                results["errors"] += 1

        print()
        print("=" * 60)
        print(f"SUMMARY: {results['scanned']} scanned, {results['skipped']} skipped, {results['errors']} errors")

        if results["body_corrections"]:
            print(f"\nBODY CORRECTIONS APPLIED ({len(results['body_corrections'])}):")
            for item in results["body_corrections"]:
                print(f"  {item['city']:<35} {item['note']}")

        if results["body_unresolved"]:
            print(f"\nBODY UNRESOLVED — COLLECTION BLOCKED ({len(results['body_unresolved'])}):")
            for item in results["body_unresolved"]:
                print(f"  {item['city']:<35} {item['reason']}")

        if results["new_agendas"]:
            print(f"\nNEW AGENDAS POSTED ({len(results['new_agendas'])}):")
            for item in results["new_agendas"]:
                print(f"  {item['city']:<35} {item['date']}  {item['title']}")
        elif report_new:
            print("\nNo newly-posted agendas detected.")

        print("=" * 60)

        # Cost report — Firecrawl calls counted but credit cost varies by call type;
        # see firecrawl.dev/app for actual credit consumption.
        fc_basic = _COST["firecrawl_scrape_basic"]
        fc_js = _COST["firecrawl_scrape_js"]
        fc_llm = _COST["firecrawl_llm_extract"]
        gemini = _COST["gemini_extract"]
        print(f"\n  SCAN COST:")
        print(f"    Firecrawl: {fc_basic:3d} basic, {fc_js:3d} JS renders, {fc_llm:3d} LLM extracts")
        print(f"    Gemini:    {gemini:3d} extraction calls")

        cost_report = {
            "phase": "scan",
            "firecrawl_scrape_basic": fc_basic,
            "firecrawl_scrape_js": fc_js,
            "firecrawl_llm_extract": fc_llm,
            "gemini_extract": gemini,
            "cities_scanned": results["scanned"],
        }
        try:
            storage.write_json(f"{cfg.output_prefix}/cost_reports/scan.json", cost_report)
        except Exception:
            pass


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Scan upcoming meeting schedules for all cities")
    parser.add_argument("--city", help="Scan a single city slug (e.g. chapel-hill-NC)")
    parser.add_argument("--dry-run", action="store_true", help="List cities without making HTTP requests")
    parser.add_argument("--report-new", action="store_true",
                        help="Highlight cities where agenda_posted flipped true since last scan")
    parser.add_argument("--skip-body-validation", action="store_true",
                        help="Skip pre-scan body validation (faster, but may collect wrong body)")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    asyncio.run(run_batch(
        args.city, args.dry_run, args.report_new,
        args.skip_body_validation, cfg, storage,
    ))


if __name__ == "__main__":
    main()
