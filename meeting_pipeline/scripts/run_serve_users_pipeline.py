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
    load_generic_meetings, format_generic_meeting,
    _civicclerk_tenant,
)

SERVE_CSV = _ROOT / "serve_users_unified.csv"
# Fall back through known CSV names
for _csv_candidate in [_ROOT / "serve_users.csv", _ROOT / "Terry Users2.csv"]:
    if not SERVE_CSV.exists():
        SERVE_CSV = _csv_candidate

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
# Platforms the router can now collect from (includes generic unknown-URL cities)
LOADABLE_PLATFORMS = DEDICATED_PLATFORMS | {"unknown", "generic_html"}
FRESHNESS_OK = {"fresh", "stale"}
CONCURRENCY = 10


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
            if platform in LOADABLE_PLATFORMS and freshness in FRESHNESS_OK:
                slug = k.split("/")[-2]
                capable[slug] = platform
        except Exception:
            pass

    rows = list(csv.DictReader(SERVE_CSV.open()))
    officials = []
    seen_slugs: set[str] = set()
    cities = []

    # Support unified format (lowercase columns), new format (State abbrev), and old format (State/Region)
    first_row = rows[0] if rows else {}
    unified_format = "city" in first_row  # serve_users_unified.csv uses lowercase
    new_format = not unified_format and "State" in first_row and "State/Region" not in first_row

    for row in rows:
        if unified_format:
            city = row.get("city", "").strip()
            state_raw = row.get("state", "").strip()
            state = state_raw.upper() if len(state_raw) <= 2 else normalize_state(state_raw)
            name = row.get("name", "").strip()
            role = row.get("office", "City Council Member").strip()
        elif new_format:
            city = row.get("City", "").strip()
            state_raw = row.get("State", "").strip()
            state = state_raw.upper() if len(state_raw) <= 2 else normalize_state(state_raw)
            name = row.get("Name", "").strip()
            role = row.get("Role", row.get("Office", "City Council Member")).strip()
        else:
            city = row.get("City", "").strip()
            state_raw = row.get("State/Region", "").strip()
            state = normalize_state(state_raw)
            name = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
            role = row.get("Candidate Office", "City Council Member")
        if not city or not state:
            continue
        slug = make_slug(city, state)
        if slug not in capable:
            continue
        officials.append({
            "name": name,
            "city": city,
            "state": state,
            "role": role,
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


def load_all_csv_cities() -> list[dict]:
    """
    Load ALL cities from the CSV (regardless of source status).
    Used by Phase 0 to determine which cities need source discovery.
    """
    rows = list(csv.DictReader(SERVE_CSV.open()))
    seen_slugs: set[str] = set()
    cities = []
    first_row = rows[0] if rows else {}
    unified_format = "city" in first_row
    new_format = not unified_format and "State" in first_row and "State/Region" not in first_row
    for row in rows:
        if unified_format:
            city = row.get("city", "").strip()
            state_raw = row.get("state", "").strip()
            state = state_raw.upper() if len(state_raw) <= 2 else normalize_state(state_raw)
        elif new_format:
            city = row.get("City", "").strip()
            state_raw = row.get("State", "").strip()
            state = state_raw.upper() if len(state_raw) <= 2 else normalize_state(state_raw)
        else:
            city = row.get("City", "").strip()
            state_raw = row.get("State/Region", "").strip()
            state = normalize_state(state_raw)
        if not city or not state:
            continue
        slug = make_slug(city, state)
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            cities.append({"city": city, "state": state, "slug": slug})
    return cities


def _slug_to_city_state(slug: str, all_cities: list[dict]) -> tuple[str, str] | None:
    """Reverse-lookup a slug to (city, state) from the CSV city list."""
    for c in all_cities:
        if c["slug"] == slug:
            return c["city"], c["state"]
    return None


# ── Phase 0: Source Discovery ─────────────────────────────────────────────────

def phase_discover(cfg: AgentConfig) -> None:
    """
    Phase 0: Run source_discover for all cities in the CSV that lack a good source.

    Skips cities that already have a DEDICATED_PLATFORMS source with fresh/stale freshness.
    Re-runs cities with unknown/generic_html/broken/missing sources.
    Uses --skip-existing flag which now also re-runs unknown-platform cities.
    """
    print(f"\n{'='*60}")
    print("PHASE 0: Source discovery (missing/broken/unknown-platform cities)")
    print(f"{'='*60}")
    ok = run_script("source_discover.py", ["--from-csv", "--skip-existing", "--csv", str(SERVE_CSV)])
    if not ok:
        print("  source_discover.py failed — check output above")


# ── Phase 5: Verify + Re-discover ────────────────────────────────────────────

def phase_verify(cfg: AgentConfig) -> list[str]:
    """
    Phase 5: Verify all generated briefings and re-discover sources for cities
    with CRITICAL issues (wrong body, content contamination, wrong BoardDocs entity).

    Returns list of city slugs that had critical issues and were queued for re-discovery.
    """
    print(f"\n{'='*60}")
    print("PHASE 5: Briefing verification + re-discovery for bad sources")
    print(f"{'='*60}")

    from meeting_pipeline.scripts.verify_briefings import verify_briefings as _verify, print_report

    storage = get_storage(cfg)
    report = _verify(cfg, storage)
    print_report(report)

    # Write verification report to S3
    output_key = f"{cfg.output_prefix}/briefing_verification.json"
    storage.write_json(output_key, report)

    # Find cities with CRITICAL issues that indicate a wrong source
    # Check A = wrong body, F = content contamination, G = wrong BoardDocs entity
    bad_slugs: set[str] = set()
    for issue in report["issues"]:
        if issue["level"] == "CRITICAL" and issue["check"] in ("A", "F", "G"):
            bad_slugs.add(issue["slug"])

    if not bad_slugs:
        print("  All briefings verified — no re-discovery needed.")
        return []

    print(f"\n  {len(bad_slugs)} city/cities with CRITICAL source issues — re-running source discovery:")

    all_cities = load_all_csv_cities()
    rediscovered: list[str] = []

    for slug in sorted(bad_slugs):
        city_state = _slug_to_city_state(slug, all_cities)
        if not city_state:
            print(f"  [SKIP] {slug} — not found in CSV")
            continue
        city_name, state = city_state
        print(f"  Re-discovering: {city_name}, {state} ({slug})")
        ok = run_script("source_discover.py", ["--city", city_name, "--state", state])
        if ok:
            # Check whether re-discovery actually resolved the source or still wrong_entity.
            # If wrong_entity, delete old contaminated briefings so verify_briefings doesn't
            # keep flagging them in an infinite loop.
            s3_key = f"{cfg.sources_prefix}/{slug}/source.json"
            try:
                source = storage.read_json(s3_key)
                bs_freshness = (source.get("best_source") or {}).get("freshness", "")
                if bs_freshness == "wrong_entity":
                    print(
                        f"  [warn] {slug}: re-discovery still wrong_entity — "
                        f"deleting contaminated briefings to stop verify loop"
                    )
                    briefings_prefix = f"{cfg.output_prefix}/briefings/"
                    deleted = 0
                    for key in storage.list_keys(briefings_prefix):
                        fname = key.split("/")[-1]
                        if fname.startswith(slug + "_"):
                            try:
                                storage.delete(key)
                                deleted += 1
                            except Exception as del_e:
                                print(f"    [warn] could not delete {fname}: {del_e}")
                    if deleted:
                        print(f"  [warn] {slug}: deleted {deleted} contaminated briefing(s)")
                else:
                    rediscovered.append(slug)
            except Exception as e:
                print(f"  [WARN] could not read source.json for {slug} after re-discovery: {e}")
                rediscovered.append(slug)
        else:
            print(f"  [WARN] source_discover failed for {slug}")

    if rediscovered:
        print(f"\n  Re-discovery complete for {len(rediscovered)} cities.")
        print("  Run --phase collect + --phase queue + --phase normalize + --phase brief")
        print("  (or run the full pipeline again) to regenerate briefings for these cities.")

    return rediscovered


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

def phase_queue(officials: list[dict], cfg: AgentConfig, to_date: str | None = None) -> int:
    storage = get_storage(cfg)
    # Look back 30 days so recently-collected PDFs for past meetings are included.
    # The collector gathers meetings within a lookback window; using today as the
    # cutoff would silently drop any meeting that occurred before today even if
    # a PDF was just downloaded for it.
    from datetime import timedelta
    from_date = (date.today() - timedelta(days=30)).isoformat()
    print(f"\n{'='*60}")
    print(f"PHASE 2: Queue build — {len(officials)} officials from {from_date}" + (f" to {to_date}" if to_date else ""))
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
        elif platform in ("unknown", "generic_html"):
            for m in load_generic_meetings(city_slug, from_date, storage, cfg.sources_prefix):
                meetings.append(format_generic_meeting(m))

        # Apply to_date window — only brief meetings on or before the cutoff
        if to_date:
            meetings = [m for m in meetings if m.get("date", "") <= to_date]

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

    # ── Cost summary ──────────────────────────────────────────────────────────
    print()
    print(f"  {'─'*60}")
    print(f"  COST SUMMARY")
    print(f"  {'─'*60}")
    known_usd = 0.0
    firecrawl_calls = 0
    firecrawl_unknown = False

    phase_labels = {
        "discovery": "Discovery  (Exa + Tavily)",
        "scan":      "Scan       (no paid API)",
        "normalize": "Normalize  (Gemini)",
        "briefing":  "Briefing   (Gemini)",
    }
    for phase_key, label in phase_labels.items():
        report_key = f"{cfg.output_prefix}/cost_reports/{phase_key}.json"
        try:
            r = storage.read_json(report_key)
            usd = r.get("estimated_usd", 0.0)
            known_usd += usd
            # Accumulate Firecrawl call counts
            for k in ("firecrawl_scrape_basic", "firecrawl_scrape_actions", "firecrawl_scrapes"):
                firecrawl_calls += r.get(k, 0)
            if r.get("firecrawl_usd_unknown"):
                firecrawl_unknown = True
            print(f"    {label:<38} ${usd:.4f}")
        except Exception:
            print(f"    {label:<38} (no data)")

    if firecrawl_calls:
        print(f"    {'Firecrawl (all phases)':<38} {firecrawl_calls} calls  — see firecrawl.dev/app for credits")

    print(f"  {'─'*60}")
    print(f"    {'Total (Exa + Tavily + Gemini)':<38} ${known_usd:.4f}")
    if briefing_keys and known_usd > 0:
        per_briefing = known_usd / len(briefing_keys)
        print(f"    {'Cost per briefing (excl. Firecrawl)':<38} ${per_briefing:.4f}")
    print(f"  {'─'*60}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Full pipeline for serve_users.csv briefing-capable officials")
    parser.add_argument("--phase",
                        choices=["discover", "collect", "queue", "normalize", "haystaq", "brief", "verify", "summary"],
                        help="Run only one phase (default: all)")
    parser.add_argument("--agendas-only", action="store_true",
                        help="Legistar only: skip matter histories/attachments (much faster)")
    parser.add_argument("--posted-only", action="store_true",
                        help="Collect only cities that have upcoming meetings with agenda_posted=true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to normalize and brief phases")
    parser.add_argument("--csv", metavar="PATH",
                        help="Alternate CSV file (must have City, State, Name, Role columns)")
    parser.add_argument("--to-date", metavar="YYYY-MM-DD",
                        help="Only queue/normalize/brief meetings on or before this date (default: no limit)")
    args = parser.parse_args()

    if args.csv:
        global SERVE_CSV
        SERVE_CSV = Path(args.csv)

    cfg = AgentConfig.from_env()
    cfg.agendas_only = args.agendas_only

    run_all = args.phase is None
    to_date = args.to_date  # YYYY-MM-DD or None

    t_start = time.time()

    # Phase 0: Source discovery (runs before loading capable officials so new platforms are found)
    if run_all or args.phase == "discover":
        phase_discover(cfg)
        # After discovery, re-load officials with any newly-found platforms
        if args.phase == "discover":
            return

    # Load collection-capable officials (after discovery, so new platforms are included)
    officials, cities = load_briefing_capable_officials()

    print(f"\nServe users pipeline")
    print(f"  Officials: {len(officials)}")
    print(f"  Unique cities: {len(cities)}")

    # Phase 1: Collection
    if run_all or args.phase == "collect":
        collect_cities = cities
        if args.posted_only:
            # Filter to only cities with upcoming agenda_posted=true meetings
            storage = get_storage(cfg)
            posted_slugs = set()
            for k in storage.list_keys(cfg.sources_prefix):
                if not k.endswith("/upcoming_meetings.json"):
                    continue
                try:
                    um = storage.read_json(k)
                    for m in um.get("upcoming", []):
                        if m.get("agenda_posted") and m.get("status") != "past":
                            slug = um.get("city_slug", k.split("/")[-2])
                            posted_slugs.add(slug)
                            break
                except Exception:
                    pass
            collect_cities = [c for c in cities if city_to_slug(c["city"], c["state"]) in posted_slugs]
            print(f"\n  --posted-only: {len(collect_cities)} cities with posted agendas (of {len(cities)} total)")
        asyncio.run(phase_collect(collect_cities, cfg))

    # Phase 2: Queue
    if run_all or args.phase == "queue":
        ready = phase_queue(officials, cfg, to_date=to_date)
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

    # Phase 5: Verify briefings + re-discover bad sources
    if run_all or args.phase == "verify":
        bad = phase_verify(cfg)
        if bad and run_all:
            # Re-collect + re-brief only the bad cities (one pass — no infinite loop)
            print(f"\n  Re-collecting {len(bad)} cities with bad sources ...")
            bad_city_infos = [c for c in cities if c["slug"] in set(bad)]
            if bad_city_infos:
                asyncio.run(phase_collect(bad_city_infos, cfg))
                # Re-queue, re-normalize, re-brief
                phase_queue(officials, cfg, to_date=to_date)
                run_script("extract_and_normalize.py", ["--force"] + (["--dry-run"] if args.dry_run else []))
                run_script("generate_briefing.py", ["--batch", "--force"] + (["--dry-run"] if args.dry_run else []))

    # Summary
    print_summary(officials, cities, cfg)
    print(f"\n  Total wall time: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
