"""
orchestrator.py — Pipeline orchestration using stage entry points.

Provides run_pipeline() which calls each stage's process_one_city()
or process_one_meeting() in sequence. Used for local development and testing.
In production, AWS Step Functions replaces this with event-driven invocations.

Stages:
    1. Discover  — find URL + platform for each city
    2. Scan      — check for upcoming meetings
    3. Collect   — download PDFs (only for cities with posted agendas)
    4. Extract   — PDF → normalized JSON
    5. Briefing  — normalized → briefing

Usage:
    from meeting_pipeline.stages.orchestrator import run_pipeline
    asyncio.run(run_pipeline(phases=["scan", "collect"], city_slugs=["chapel-hill-NC"]))
"""

import asyncio
import csv
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from meeting_pipeline.shared.config import AgentConfig, get_storage, city_to_slug


# ── City loading ─────────────────────────────────────────────────────────────

def load_cities_from_sources(cfg: AgentConfig, storage=None) -> list[dict]:
    """Load all cities that have a source.json in storage."""
    if storage is None:
        storage = get_storage(cfg)
    source_keys = [k for k in storage.list_keys(cfg.sources_prefix) if k.endswith("/source.json")]
    cities = []
    for key in source_keys:
        slug = key.split("/")[-2]
        try:
            source = storage.read_json(key)
            cities.append({
                "slug": slug,
                "city": source.get("city", slug),
                "state": source.get("state", ""),
                "platform": (source.get("best_source") or {}).get("platform", ""),
                "source_key": key,
            })
        except Exception:
            pass
    return cities


def load_cities_from_csv(csv_path: str | Path) -> list[dict]:
    """Load cities from a CSV file. Supports multiple column formats."""
    rows = list(csv.DictReader(open(csv_path)))
    if not rows:
        return []

    first = rows[0]
    seen = set()
    cities = []

    for row in rows:
        # Support multiple CSV column formats
        if "city" in first:
            city = row.get("city", "").strip()
            state = row.get("state", "").strip().upper()
        elif "City" in first:
            city = row.get("City", "").strip()
            state_raw = row.get("State", row.get("State/Region", "")).strip()
            state = state_raw.upper() if len(state_raw) == 2 else state_raw[:2].upper()
        else:
            continue

        if not city or not state:
            continue

        slug = city_to_slug(city, state)
        if slug in seen:
            continue
        seen.add(slug)
        cities.append({"city": city, "state": state, "slug": slug})

    return cities


def filter_cities(
    cities: list[dict],
    city_slugs: list[str] | None = None,
) -> list[dict]:
    """Filter city list by slug(s)."""
    if not city_slugs:
        return cities
    slug_set = set(s.lower() for s in city_slugs)
    return [c for c in cities if c["slug"].lower() in slug_set]


# ── Stage runners ────────────────────────────────────────────────────────────

async def run_discover(cities: list[dict], cfg: AgentConfig):
    """Run discovery for a list of cities."""
    from meeting_pipeline.stages.discover.process import process_one_city
    import httpx
    import os

    storage = get_storage(cfg)
    tavily = None
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if tavily_key:
        from tavily import TavilyClient
        tavily = TavilyClient(api_key=tavily_key)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True, timeout=20,
    ) as http:
        for i, city_info in enumerate(cities):
            city = city_info["city"]
            state = city_info["state"]
            expected_body = city_info.get("expected_body", "")
            slug = city_info.get("slug") or city_to_slug(city, state)

            print(f"[{i+1}/{len(cities)}] Discovering {city}, {state}...", end=" ", flush=True)
            try:
                result = await process_one_city(
                    city, state, expected_body=expected_body,
                    tavily_client=tavily, http_client=http,
                )
                platform = result.get("best_source", {}).get("platform", "?")
                freshness = result.get("best_source", {}).get("freshness", "?")
                storage.write_json(f"{cfg.sources_prefix}/{slug}/source.json", result)
                print(f"[{platform}/{freshness}]")
            except Exception as e:
                print(f"ERROR: {str(e)[:60]}")


async def run_scan(cities: list[dict], cfg: AgentConfig, force: bool = False):
    """Run scan for cities with source.json."""
    from meeting_pipeline.stages.scan.process import process_one_city
    import httpx

    storage = get_storage(cfg)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True, timeout=20,
    ) as http:
        for i, city_info in enumerate(cities):
            slug = city_info["slug"]
            source_key = f"{cfg.sources_prefix}/{slug}/source.json"

            if not storage.exists(source_key):
                print(f"[{i+1}/{len(cities)}] {slug}: no source.json, skip")
                continue

            um_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
            if not force and storage.exists(um_key):
                print(f"[{i+1}/{len(cities)}] {slug}: already scanned, skip (--force to re-scan)")
                continue

            try:
                source = storage.read_json(source_key)
                platform = (source.get("best_source") or {}).get("platform", "?")
                print(f"[{i+1}/{len(cities)}] Scanning {slug} ({platform})...", end=" ", flush=True)

                record = await process_one_city(slug, source, source_key, http_client=http, storage=storage)
                if record:
                    storage.write_json(um_key, record)
                    meetings = record.get("upcoming", [])
                    future = [m for m in meetings if m.get("status") != "past"]
                    posted = [m for m in future if m.get("agenda_posted")]
                    print(f"{len(meetings)} meetings ({len(future)} future, {len(posted)} posted)")
                else:
                    print("no result")
            except Exception as e:
                print(f"ERROR: {str(e)[:60]}")


async def run_collect(cities: list[dict], cfg: AgentConfig, posted_only: bool = True):
    """Run collection for cities (download PDFs)."""
    from meeting_pipeline.stages.collect.process import process_one_city

    storage = get_storage(cfg)

    cities_to_collect = []
    for city_info in cities:
        slug = city_info["slug"]
        city = city_info.get("city", slug)
        state = city_info.get("state", "")

        if posted_only:
            um_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
            if storage.exists(um_key):
                um = storage.read_json(um_key)
                has_posted = any(
                    m.get("agenda_posted") and m.get("status") != "past"
                    for m in um.get("upcoming", [])
                )
                if not has_posted:
                    continue
            else:
                continue

        cities_to_collect.append({"city": city, "state": state, "slug": slug})

    if not cities_to_collect:
        print("  No cities to collect (none with posted agendas)")
        return

    print(f"Collecting {len(cities_to_collect)} cities...")
    for i, c in enumerate(cities_to_collect):
        print(f"[{i+1}/{len(cities_to_collect)}] {c['city']}, {c['state']}...", end=" ", flush=True)
        try:
            result = await process_one_city(c["city"], c["state"], cfg=cfg, storage=storage)
            if isinstance(result, dict):
                ok = result.get("ok", False)
                pdfs = result.get("pdfs_downloaded", 0)
                print(f"{'OK' if ok else 'FAILED'} ({pdfs} PDFs)")
            else:
                # CollectionResult object
                ok = not result.error
                print(f"{'OK' if ok else 'FAILED'} ({result.events_found} events, {result.pdfs_downloaded} PDFs)")
        except Exception as e:
            print(f"ERROR: {str(e)[:60]}")


async def run_extract(
    cities: list[dict],
    cfg: AgentConfig,
    force: bool = False,
    dry_run: bool = False,
):
    """Extract agenda items from PDFs for cities with posted agendas.

    Reads upcoming_meetings.json per city, finds PDFs, runs LLM extraction.
    Replaces the old meeting_queue.json-based approach.
    """
    from meeting_pipeline.stages.extract.normalize import (
        extract_pdf_text, find_best_pdf, extract_with_gemini, normalize_meeting,
    )

    storage = get_storage(cfg)
    normalized_prefix = f"{cfg.output_prefix}/normalized"

    # Lazy import — heavy deps
    gemini = None
    if not dry_run:
        from shared.llm_gemini import GeminiClient, GeminiModelType
        gemini = GeminiClient(default_model=GeminiModelType.FLASH_LITE)

    extracted = 0
    skipped = 0
    errors = []

    for city_info in cities:
        slug = city_info["slug"]
        um_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"

        if not storage.exists(um_key):
            continue

        um = storage.read_json(um_key)
        platform = um.get("platform", "")
        city = um.get("city", slug)
        state = um.get("state", "")
        body = um.get("body", "")

        # Find meetings with posted agendas
        posted = [
            m for m in um.get("upcoming", [])
            if m.get("agenda_posted") and m.get("status") != "past"
        ]
        if not posted:
            continue

        for meeting in posted:
            meeting_date = meeting.get("date", "")
            out_key = f"{normalized_prefix}/{slug}_{meeting_date}.json"
            label = f"{city}, {state} — {meeting_date}"

            if not force and storage.exists(out_key):
                skipped += 1
                continue

            # Find PDF
            pdf_key, pdf_label = find_best_pdf(slug, meeting_date, platform, storage, cfg.sources_prefix)
            if not pdf_key:
                continue

            if dry_run:
                pdf_size = storage.get_size(pdf_key)
                print(f"  [dry-run] {label}: would extract from {pdf_key.split('/')[-1]} ({pdf_size // 1024}KB)")
                continue

            print(f"\n  Extracting: {label}")
            pdf_size = storage.get_size(pdf_key)
            print(f"    PDF: {pdf_key.split('/')[-1]} ({pdf_size // 1024}KB)")

            try:
                pdf_bytes = storage.read_bytes(pdf_key)
                text = extract_pdf_text(pdf_bytes)
                print(f"    {len(text.split())} words extracted")

                truncation_warning = None
                if len(text) > 100_000:
                    truncation_warning = f"Text truncated: {len(text):,} chars -> 100,000"
                    print(f"    Warning: {truncation_warning}")

                if len(text.strip()) < 500 and pdf_size > 5000:
                    print(f"    PDF appears scanned — trying Firecrawl OCR")
                    try:
                        from meeting_pipeline.collection_agent.firecrawl_utils import scrape_pdf_text
                        presigned = storage.get_presigned_url(pdf_key, expiry_seconds=300)
                        fc_text = scrape_pdf_text(presigned)
                        if fc_text and len(fc_text.strip()) > 200:
                            text = fc_text
                            print(f"    Firecrawl OCR: {len(text.split())} words")
                        else:
                            raise ValueError("Insufficient text from OCR")
                    except Exception as e:
                        errors.append({"label": label, "error": f"OCR failed: {e}"})
                        continue

                # LLM extraction with retry
                import time as _time
                extraction = None
                for attempt in range(3):
                    try:
                        extraction = extract_with_gemini(text, city, state, meeting_date, gemini)
                        print(f"    {len(extraction.items)} agenda items extracted")
                        break
                    except Exception as e:
                        if attempt < 2:
                            wait = 2 ** attempt
                            print(f"    Attempt {attempt + 1} failed: {e} — retrying in {wait}s")
                            _time.sleep(wait)
                        else:
                            errors.append({"label": label, "error": str(e)})

                if extraction is None:
                    continue

                # Build official dict from scan data (no CSV dependency)
                official = {"name": "", "city": city, "state": state, "role": body or "City Council"}

                # Build meeting dict compatible with normalize_meeting
                meeting_for_norm = {
                    "date": meeting_date,
                    "title": meeting.get("title", ""),
                    "body": body,
                    "source_url": meeting.get("agenda_url", ""),
                    "agenda_files": [],
                }
                if meeting.get("agenda_url"):
                    meeting_for_norm["agenda_files"] = [
                        {"name": "Agenda", "type": "Agenda", "url": meeting["agenda_url"]}
                    ]

                normalized = normalize_meeting(
                    official=official,
                    meeting=meeting_for_norm,
                    extraction=extraction,
                    pdf_key=pdf_key,
                    pdf_label=pdf_label,
                    city_slug=slug,
                    platform=platform,
                )
                if truncation_warning:
                    normalized.setdefault("agenda", {})["truncation_warning"] = truncation_warning

                storage.write_json(out_key, normalized)
                print(f"    Saved: {out_key.split('/')[-1]}")
                extracted += 1

            except Exception as e:
                errors.append({"label": label, "error": str(e)})

    print(f"\n  Extract: {extracted} normalized, {skipped} skipped, {len(errors)} errors")
    if errors:
        for e in errors:
            print(f"    {e['label']}: {e['error']}")

    if gemini and not dry_run:
        stats = gemini.get_usage_stats()
        print(f"  LLM cost: ${stats.get('total_cost', 0):.4f} ({stats.get('api_call_count', 0)} calls)")


async def run_briefing(
    cities: list[dict],
    cfg: AgentConfig,
    force: bool = False,
    dry_run: bool = False,
):
    """Generate briefings for cities with normalized meeting data."""
    from meeting_pipeline.stages.briefing.generate import generate_briefing_for_meeting

    storage = get_storage(cfg)
    normalized_prefix = f"{cfg.output_prefix}/normalized"
    briefing_prefix = f"{cfg.output_prefix}/briefings"

    # Find normalized files for the target cities
    all_norm_keys = storage.list_keys(normalized_prefix)
    norm_keys = sorted(
        k for k in all_norm_keys
        if re.search(r"[^/]+_\d{4}-\d{2}-\d{2}\.json$", k)
    )

    # Filter to target cities if specified
    if cities:
        slug_set = set(c["slug"] for c in cities)
        norm_keys = [
            k for k in norm_keys
            if any(k.split("/")[-1].startswith(slug) for slug in slug_set)
        ]

    # Skip existing briefings unless --force
    if not force:
        existing = set(storage.list_keys(briefing_prefix))
        before = len(norm_keys)
        norm_keys = [
            k for k in norm_keys
            if not any(k.split("/")[-1].replace(".json", "") in bk for bk in existing)
        ]
        skipped = before - len(norm_keys)
        if skipped:
            print(f"  Skipping {skipped} with existing briefings (--force to regenerate)")

    if not norm_keys:
        print("  No normalized meetings to brief")
        return

    results = []
    for key in norm_keys:
        filename = key.split("/")[-1]
        print(f"\n  Briefing: {filename}")
        try:
            result = generate_briefing_for_meeting(key, storage, cfg, dry_run=dry_run)
        except Exception as e:
            print(f"    Failed: {e}")
            result = {"status": "error", "error": str(e), "cost": 0}
        result["file"] = filename
        results.append(result)

    ok = sum(1 for r in results if r.get("status") == "ok")
    errs = sum(1 for r in results if r.get("status") == "error")
    cost = sum(r.get("cost", 0) for r in results)
    print(f"\n  Briefing: {ok}/{len(results)} generated, {errs} errors, ${cost:.4f}")


# ── Full pipeline ────────────────────────────────────────────────────────────

ALL_PHASES = ["discover", "scan", "collect", "extract", "briefing"]


async def run_pipeline(
    phases: list[str] | None = None,
    cfg: AgentConfig | None = None,
    city_slugs: list[str] | None = None,
    csv_path: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
    skip_existing: bool = True,
):
    """
    Run the full pipeline or selected phases.

    City selection (in priority order):
        1. city_slugs — specific slugs to process
        2. csv_path — load cities from CSV file
        3. All cities with source.json in storage

    Args:
        phases: list of phase names to run (default: all)
        cfg: AgentConfig (created from env if not provided)
        city_slugs: filter to these city slugs
        csv_path: load cities from this CSV
        force: overwrite existing outputs
        dry_run: preview without making changes
        skip_existing: skip cities that already have outputs for each phase
    """
    if cfg is None:
        cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    phases = phases or ALL_PHASES

    # Load cities
    if csv_path:
        cities = load_cities_from_csv(csv_path)
        print(f"Loaded {len(cities)} cities from {csv_path}")
    else:
        cities = load_cities_from_sources(cfg, storage)
        print(f"Loaded {len(cities)} cities from storage")

    # Apply city filter
    if city_slugs:
        cities = filter_cities(cities, city_slugs)
        print(f"Filtered to {len(cities)} cities: {[c['slug'] for c in cities]}")

    if not cities:
        print("No cities to process")
        return

    t_start = time.time()

    if "discover" in phases:
        print(f"\n{'=' * 60}")
        print(f"DISCOVER — {len(cities)} cities")
        print(f"{'=' * 60}")
        await run_discover(cities, cfg)

        # Reload cities after discovery (new source.json files may exist)
        if csv_path:
            enriched = load_cities_from_sources(cfg, storage)
            slug_set = set(c["slug"] for c in cities)
            cities = [c for c in enriched if c["slug"] in slug_set] or cities

    if "scan" in phases:
        print(f"\n{'=' * 60}")
        print(f"SCAN — {len(cities)} cities")
        print(f"{'=' * 60}")
        await run_scan(cities, cfg, force=force)

    if "collect" in phases:
        print(f"\n{'=' * 60}")
        print(f"COLLECT — {len(cities)} cities (posted agendas only)")
        print(f"{'=' * 60}")
        await run_collect(cities, cfg, posted_only=True)

    if "extract" in phases:
        print(f"\n{'=' * 60}")
        print(f"EXTRACT — PDF -> normalized JSON")
        print(f"{'=' * 60}")
        await run_extract(cities, cfg, force=force, dry_run=dry_run)

    if "briefing" in phases:
        print(f"\n{'=' * 60}")
        print(f"BRIEFING — normalized -> briefing")
        print(f"{'=' * 60}")
        await run_briefing(cities, cfg, force=force, dry_run=dry_run)

    elapsed = time.time() - t_start
    print(f"\n{'=' * 60}")
    print(f"PIPELINE COMPLETE — {elapsed:.0f}s")
    print(f"{'=' * 60}")

    # Summary
    _print_summary(cities, cfg, storage)


def _print_summary(cities: list[dict], cfg: AgentConfig, storage):
    """Print pipeline run summary."""
    slug_set = set(c["slug"] for c in cities)

    # Count outputs
    source_count = 0
    scan_count = 0
    posted_count = 0
    norm_count = 0
    brief_count = 0

    for slug in slug_set:
        if storage.exists(f"{cfg.sources_prefix}/{slug}/source.json"):
            source_count += 1
        um_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
        if storage.exists(um_key):
            scan_count += 1
            um = storage.read_json(um_key)
            if any(m.get("agenda_posted") for m in um.get("upcoming", [])):
                posted_count += 1

    norm_keys = storage.list_keys(f"{cfg.output_prefix}/normalized")
    for k in norm_keys:
        if re.search(r"[^/]+_\d{4}-\d{2}-\d{2}\.json$", k):
            fn = k.split("/")[-1]
            slug_part = fn.rsplit("_", 1)[0]
            if slug_part in slug_set:
                norm_count += 1

    brief_keys = storage.list_keys(f"{cfg.output_prefix}/briefings")
    for k in brief_keys:
        if k.endswith("_briefing.json"):
            fn = k.split("/")[-1]
            slug_part = fn.replace("_briefing.json", "").rsplit("_", 1)[0]
            if slug_part in slug_set:
                brief_count += 1

    print(f"  Cities:      {len(cities)}")
    print(f"  Sources:     {source_count}")
    print(f"  Scanned:     {scan_count}")
    print(f"  With agenda: {posted_count}")
    print(f"  Normalized:  {norm_count}")
    print(f"  Briefings:   {brief_count}")
