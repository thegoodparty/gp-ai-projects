"""
orchestrator.py — Pipeline orchestration using stage entry points.

Provides run_pipeline() which calls each stage's process_one_city()
or process_one_meeting() in sequence. Used for local development.
In production, AWS Step Functions replaces this with event-driven invocations.

Stages:
    1. Discover  — find URL + platform for each city
    2. Scan      — check for upcoming meetings
    3. Collect   — download PDFs (only for cities with posted agendas)
    4. Extract   — PDF → normalized JSON
    5. Briefing  — normalized → briefing

Usage:
    from meeting_pipeline.stages.orchestrator import run_pipeline
    run_pipeline(cities=[...], phases=["scan", "collect"])
"""

import asyncio
from typing import Optional

from meeting_pipeline.shared.config import AgentConfig, get_storage


async def run_discover(cities: list[dict], cfg: AgentConfig):
    """Run discovery for a list of cities."""
    from meeting_pipeline.stages.discover.process import process_one_city
    import httpx
    from tavily import TavilyClient
    import os

    tavily = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY", ""))
    storage = get_storage(cfg)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True, timeout=20,
    ) as http:
        for i, city_info in enumerate(cities):
            city = city_info["city"]
            state = city_info["state"]
            expected_body = city_info.get("expected_body", "")
            slug = f"{city.lower().replace(' ', '-')}-{state}"

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


async def run_scan(cfg: AgentConfig):
    """Run scan for all cities with source.json."""
    from meeting_pipeline.stages.scan.process import process_one_city
    import httpx

    storage = get_storage(cfg)
    source_keys = [k for k in storage.list_keys(cfg.sources_prefix) if k.endswith("/source.json")]

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
        follow_redirects=True, timeout=20,
    ) as http:
        for i, key in enumerate(source_keys):
            slug = key.split("/")[-2]
            try:
                source = storage.read_json(key)
                platform = source.get("best_source", {}).get("platform", "?")
                print(f"[{i+1}/{len(source_keys)}] Scanning {slug} ({platform})...", end=" ", flush=True)

                record = await process_one_city(slug, source, key, http_client=http, storage=storage)
                if record:
                    storage.write_json(f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json", record)
                    meetings = record.get("upcoming", [])
                    future = [m for m in meetings if m.get("status") != "past"]
                    posted = [m for m in future if m.get("agenda_posted")]
                    print(f"{len(meetings)} meetings ({len(future)} future, {len(posted)} posted)")
                else:
                    print("no result")
            except Exception as e:
                print(f"ERROR: {str(e)[:60]}")


async def run_collect(cfg: AgentConfig, posted_only: bool = True):
    """Run collection for cities with posted agendas."""
    from meeting_pipeline.stages.collect.process import process_one_city

    storage = get_storage(cfg)
    source_keys = [k for k in storage.list_keys(cfg.sources_prefix) if k.endswith("/source.json")]

    cities_to_collect = []
    for key in source_keys:
        slug = key.split("/")[-2]
        source = storage.read_json(key)
        city = source.get("city", "")
        state = source.get("state", "")

        if posted_only:
            um_key = key.replace("/source.json", "/upcoming_meetings.json")
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

        cities_to_collect.append({"city": city, "state": state})

    print(f"Collecting {len(cities_to_collect)} cities...")
    for i, c in enumerate(cities_to_collect):
        print(f"[{i+1}/{len(cities_to_collect)}] {c['city']}, {c['state']}...", end=" ", flush=True)
        try:
            result = await process_one_city(c["city"], c["state"], cfg=cfg, storage=storage)
            ok = result.get("ok", False)
            pdfs = result.get("pdfs_downloaded", 0)
            print(f"{'OK' if ok else 'FAILED'} ({pdfs} PDFs)")
        except Exception as e:
            print(f"ERROR: {str(e)[:60]}")
