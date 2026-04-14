#!/usr/bin/env python3
"""
run_serve_users_pipeline.py — Full pipeline for the 63 briefing-capable officials
in serve_users.csv.

Phases:
  1. Collection    — parallel async (5 concurrent), one city per task
  2. Queue build   — reads collected data → meeting_queue.json
  3. Normalize     — PDF → Gemini extraction → normalized/{city}_{date}.json
  3b. Haystaq      — collect constituent scores for any cities missing data (--skip-existing)
  4. Briefing      — normalized JSON → briefing JSON

Run: uv run python meeting_pipeline/scripts/run_serve_users_pipeline.py
     uv run python meeting_pipeline/scripts/run_serve_users_pipeline.py --dry-run
     uv run python meeting_pipeline/scripts/run_serve_users_pipeline.py --phase collect
     uv run python meeting_pipeline/scripts/run_serve_users_pipeline.py --phase queue
     uv run python meeting_pipeline/scripts/run_serve_users_pipeline.py --phase normalize
     uv run python meeting_pipeline/scripts/run_serve_users_pipeline.py --phase haystaq
     uv run python meeting_pipeline/scripts/run_serve_users_pipeline.py --phase brief
"""

import argparse
import asyncio
import csv
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
for p in [str(_ROOT), str(_PROJECT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage, city_to_slug
from meeting_pipeline.collection_agent.router import route_city

# Queue generation functions (imported directly so we can pass custom officials)
from meeting_pipeline.scripts.generate_meeting_queue import (
    load_civicclerk_meetings, format_civicclerk_meeting,
    load_legistar_meetings, format_legistar_meeting,
    load_civicplus_meetings, format_civicplus_meeting,
    load_granicus_meetings, format_granicus_meeting,
    load_boarddocs_meetings, format_boarddocs_meeting,
    load_municode_meetings, format_municode_meeting,
    load_novus_meetings, format_novus_meeting,
    _civicclerk_tenant,
)

SERVE_CSV = _ROOT / "serve_users.csv"

STATE_ABBREVS = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}
DEDICATED_PLATFORMS = {
    "legistar", "civicplus", "civicclerk", "granicus", "swagit",
    "escribe", "boarddocs", "municode", "novus",
}
FRESHNESS_OK = {"fresh", "stale"}
CONCURRENCY = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_state(raw: str) -> str:
    raw = raw.strip()
    if len(raw) == 2:
        return raw.upper()
    return STATE_ABBREVS.get(raw, raw[:2].upper())


def make_slug(city: str, state: str) -> str:
    slug = city.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return f"{slug}-{state.upper()}"


def load_briefing_capable_officials() -> tuple[list[dict], list[dict]]:
    """
    Return (officials, cities) for the 63 briefing-capable entries in serve_users.csv.
    officials — one per row (may have duplicates per city)
    cities    — deduplicated {city, state, slug, platform} for collection
    """
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    # Build slug → platform map from sources dir
    capable: dict[str, str] = {}
    all_keys = storage.list_keys(cfg.sources_prefix)
    for k in all_keys:
        if not k.endswith("/source.json"):
            continue
        try:
            src = storage.read_json(k)
            bs = src.get("best_source")
            if not bs:
                continue
            platform = bs.get("platform", "")
            freshness = bs.get("freshness", "")
            if platform in DEDICATED_PLATFORMS and freshness in FRESHNESS_OK:
                slug = k.split("/")[-2]
                capable[slug] = platform
        except Exception:
            pass

    rows = list(csv.DictReader(SERVE_CSV.open()))
    officials = []
    seen_slugs: set[str] = set()
    cities = []

    for row in rows:
        city = row.get("City", "").strip()
        state_raw = row.get("State/Region", "").strip()
        if not city or not state_raw:
            continue
        state = normalize_state(state_raw)
        slug = make_slug(city, state)
        if slug not in capable:
            continue
        officials.append({
            "name": f"{row['First Name']} {row['Last Name']}".strip(),
            "city": city,
            "state": state,
            "role": row.get("Candidate Office", "City Council Member"),
        })
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            cities.append({
                "city": city,
                "state": state,
                "slug": slug,
                "platform": capable[slug],
            })

    return officials, cities


# ── Phase 1: Collection ───────────────────────────────────────────────────────

async def collect_city(city: str, state: str, storage, cfg: AgentConfig, sem: asyncio.Semaphore) -> dict:
    async with sem:
        event = {"city": city, "state": state}
        try:
            result = await route_city(event, storage, cfg)
            return {
                "city": city, "state": state,
                "platform": result.platform,
                "events_found": result.events_found,
                "pdfs_downloaded": result.pdfs_downloaded,
                "error": result.error,
                "ok": not result.error,
            }
        except Exception as e:
            return {"city": city, "state": state, "error": str(e), "ok": False}


async def phase_collect(cities: list[dict], cfg: AgentConfig) -> list[dict]:
    storage = get_storage(cfg)
    sem = asyncio.Semaphore(CONCURRENCY)
    print(f"\n{'='*60}")
    print(f"PHASE 1: Collection — {len(cities)} cities (concurrency={CONCURRENCY})")
    print(f"{'='*60}")

    tasks = [collect_city(c["city"], c["state"], storage, cfg, sem) for c in cities]
    results = []
    for coro in asyncio.as_completed(tasks):
        r = await coro
        status = "OK" if r["ok"] else f"FAILED: {r.get('error', '')}"
        events = r.get("events_found", 0)
        pdfs = r.get("pdfs_downloaded", 0)
        print(f"  {r['city']}, {r['state']:<4} [{r.get('platform','?'):<12}] {status} — {events} events, {pdfs} PDFs")
        results.append(r)

    ok = sum(1 for r in results if r["ok"])
    print(f"\n  Collected: {ok}/{len(cities)} cities succeeded")
    return results


# ── Phase 2: Queue build ──────────────────────────────────────────────────────

def phase_queue(officials: list[dict], cfg: AgentConfig) -> int:
    storage = get_storage(cfg)
    # Look back 30 days so recently-collected PDFs for past meetings are included.
    # The collector gathers meetings within a lookback window; using today as the
    # cutoff would silently drop any meeting that occurred before today even if
    # a PDF was just downloaded for it.
    from datetime import timedelta
    from_date = (date.today() - timedelta(days=30)).isoformat()
    print(f"\n{'='*60}")
    print(f"PHASE 2: Queue build — {len(officials)} officials from {from_date}")
    print(f"{'='*60}")

    # Build slug → {platform, slug} from sources
    city_platform: dict[tuple[str, str], dict] = {}
    all_keys = storage.list_keys(cfg.sources_prefix)
    for k in [k for k in all_keys if k.endswith("/source.json")]:
        try:
            src = storage.read_json(k)
            city = src.get("city", "").lower()
            state = src.get("state", "")
            platform = src.get("best_source", {}).get("platform", "")
            slug = k.split("/")[-2]
            city_platform[(city, state)] = {"platform": platform, "slug": slug}
        except Exception:
            pass

    queue = []
    skipped = []

    for official in officials:
        key = (official["city"].lower(), official["state"])
        src_info = city_platform.get(key)
        if not src_info:
            skipped.append({**official, "reason": "no source"})
            continue

        platform = src_info["platform"]
        city_slug = src_info["slug"]
        meetings = []

        if platform == "civicclerk":
            raw, tenant = load_civicclerk_meetings(city_slug, from_date, storage, cfg.sources_prefix)
            seen: set = set()
            for m in sorted(raw, key=lambda x: x.get("date", "")):
                k2 = (m.get("date"), m.get("title"), m.get("hasAgenda"))
                if k2 not in seen:
                    seen.add(k2)
                    meetings.append(format_civicclerk_meeting(m, tenant))
        elif platform == "legistar":
            for e in load_legistar_meetings(city_slug, from_date, storage, cfg.sources_prefix):
                meetings.append(format_legistar_meeting(e))
        elif platform == "civicplus":
            for m in load_civicplus_meetings(city_slug, from_date, storage, cfg.sources_prefix):
                meetings.append(format_civicplus_meeting(m))
        elif platform in ("granicus", "swagit"):
            for e in load_granicus_meetings(city_slug, from_date, storage, cfg.sources_prefix):
                meetings.append(format_granicus_meeting(e))
        elif platform == "boarddocs":
            raw_events, base_url = load_boarddocs_meetings(city_slug, from_date, storage, cfg.sources_prefix)
            matters_key = f"{cfg.sources_prefix}/{city_slug}/data/boarddocs/matters.json"
            has_matters = storage.exists(matters_key) and bool(storage.read_json(matters_key))
            for e in raw_events:
                meetings.append(format_boarddocs_meeting(e, base_url, has_matters))
        elif platform == "municode":
            for m in load_municode_meetings(city_slug, from_date, storage, cfg.sources_prefix):
                meetings.append(format_municode_meeting(m))
        elif platform == "novus":
            for m in load_novus_meetings(city_slug, from_date, storage, cfg.sources_prefix):
                meetings.append(format_novus_meeting(m))

        agenda_ready = sum(1 for m in meetings if m.get("status") == "agenda_ready")
        print(f"  {official['city']}, {official['state']:<4} [{platform:<12}] {len(meetings)} meetings, {agenda_ready} with agendas")

        queue.append({
            "official": official,
            "platform": platform,
            "city_slug": city_slug,
            "upcoming_meetings": meetings,
        })

    queue_key = f"{cfg.output_prefix}/meeting_queue.json"
    storage.write_json(queue_key, {"queue": queue, "generated_at": from_date, "total": len(queue)})

    total_meetings = sum(len(e["upcoming_meetings"]) for e in queue)
    total_ready = sum(
        sum(1 for m in e["upcoming_meetings"] if m.get("status") == "agenda_ready")
        for e in queue
    )
    # Also count agenda_posted_no_files — extract_and_normalize.py will check S3
    # for PDFs and process those meetings too, so they should not block the pipeline.
    total_with_content = sum(
        sum(1 for m in e["upcoming_meetings"] if m.get("status") in ("agenda_ready", "agenda_posted_no_files"))
        for e in queue
    )
    print(f"\n  Queue saved: {len(queue)} officials, {total_meetings} meetings, {total_ready} with agendas ready")
    if skipped:
        print(f"  Skipped {len(skipped)} officials (no source data collected yet)")
    return total_with_content


# ── Phase 3 & 4: Normalize + Brief (subprocess) ───────────────────────────────

def run_script(script_name: str, extra_args: list[str] = []) -> bool:
    cmd = ["uv", "run", "python", f"meeting_pipeline/scripts/{script_name}"] + extra_args
    print(f"\n  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    return result.returncode == 0


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(officials: list[dict], cities: list[dict], cfg: AgentConfig):
    storage = get_storage(cfg)
    normalized_prefix = f"{cfg.output_prefix}/normalized"
    briefings_prefix = f"{cfg.output_prefix}/briefings"

    normalized_keys = [
        k for k in storage.list_keys(normalized_prefix)
        if re.search(r"[^/]+_\d{4}-\d{2}-\d{2}\.json$", k)
    ]
    briefing_keys = [
        k for k in storage.list_keys(briefings_prefix)
        if k.endswith("_briefing.json")
    ]

    print(f"\n{'='*60}")
    print("PIPELINE SUMMARY")
    print(f"{'='*60}")
    print(f"  Officials targeted:   {len(officials)}")
    print(f"  Unique cities:        {len(cities)}")
    print()

    # Per-platform breakdown
    from collections import Counter
    platform_counts = Counter(c["platform"] for c in cities)
    print("  Cities by platform:")
    for platform, count in platform_counts.most_common():
        print(f"    {platform:<16} {count}")
    print()

    print(f"  Normalized meetings:  {len(normalized_keys)}")
    print(f"  Briefings generated:  {len(briefing_keys)}")
    print()

    if briefing_keys:
        print("  Briefings produced:")
        for k in sorted(briefing_keys):
            filename = k.split("/")[-1]
            # Attempt to read city name from the briefing
            try:
                b = storage.read_json(k)
                city_name = b.get("cityName", filename.split("_")[0])
                date_str = b.get("date", "")
                items = len(b.get("data", {}).get("agendaItems", []))
                priorities = len(b.get("data", {}).get("priorityIssues", []))
                print(f"    {city_name}, — {date_str}  ({items} items, {priorities} priorities)")
            except Exception:
                print(f"    {filename}")

    no_briefing = [c for c in cities if not any(
        c["slug"] in k for k in briefing_keys
    )]
    if no_briefing:
        print()
        print(f"  Cities with no briefing produced ({len(no_briefing)}):")
        for c in no_briefing:
            print(f"    {c['city']}, {c['state']} [{c['platform']}]")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Full pipeline for serve_users.csv briefing-capable officials")
    parser.add_argument("--phase", choices=["collect", "queue", "normalize", "haystaq", "brief", "summary"],
                        help="Run only one phase (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to normalize and brief phases")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    officials, cities = load_briefing_capable_officials()

    print(f"Serve users pipeline")
    print(f"  Officials: {len(officials)}")
    print(f"  Unique cities: {len(cities)}")

    run_all = args.phase is None

    t_start = time.time()

    # Phase 1: Collection
    if run_all or args.phase == "collect":
        asyncio.run(phase_collect(cities, cfg))

    # Phase 2: Queue
    if run_all or args.phase == "queue":
        ready = phase_queue(officials, cfg)
        if ready == 0:
            print("\n  No meetings with agendas (ready or posted-no-files) — stopping here.")
            print("  (Collection may not have found any meetings with PDFs in the last 30 days)")
            if run_all:
                print_summary(officials, cities, cfg)
                return

    # Phase 3: Normalize
    if run_all or args.phase == "normalize":
        print(f"\n{'='*60}")
        print("PHASE 3: Extract + normalize (PDF → Gemini → structured JSON)")
        print(f"{'='*60}")
        extra = ["--dry-run"] if args.dry_run else []
        ok = run_script("extract_and_normalize.py", extra)
        if not ok:
            print("  extract_and_normalize.py failed — check output above")

    # Phase 3b: Haystaq constituent data (collect missing cities before briefing)
    if run_all or args.phase == "haystaq":
        print(f"\n{'='*60}")
        print("PHASE 3b: Haystaq constituent data (missing cities only)")
        print(f"{'='*60}")
        ok = run_script("collect_haystaq_batch.py", ["--from-csv", "--skip-existing"])
        if not ok:
            print("  collect_haystaq_batch.py failed — check output above")

    # Phase 4: Briefings
    if run_all or args.phase == "brief":
        print(f"\n{'='*60}")
        print("PHASE 4: Briefing generation")
        print(f"{'='*60}")
        extra = ["--batch"] + (["--dry-run"] if args.dry_run else [])
        ok = run_script("generate_briefing.py", extra)
        if not ok:
            print("  generate_briefing.py failed — check output above")

    # Summary
    print_summary(officials, cities, cfg)
    print(f"\n  Total wall time: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
