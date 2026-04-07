"""
discovery_agent.py — Proactive health checking + migration detection.

Probes cached source URLs with HTTP HEAD requests. If a URL returns 4xx or
redirects to a different domain, it flags MIGRATION_DETECTED and triggers
a re-run of source discovery for that city.

Design:
  - Stateless: accepts a list of city slugs, returns a list of events
  - Lambda-safe: no Playwright (pure httpx)
  - Runs independently from collection — usually scheduled daily
"""

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from .models import HealthCheckResult
from .storage import StorageBackend
from .config import AgentConfig
from . import notification_log


# ── HTTP HEAD probe ───────────────────────────────────────────────────────────

async def _probe_url(
    client: httpx.AsyncClient,
    url: str,
    timeout: int = 10,
) -> tuple[int | None, str | None, str | None]:
    """
    Probe a URL with HEAD, following redirects.

    Returns:
        (status_code, final_url, error)
        status_code: HTTP status code of final response (or None on error)
        final_url:   URL after redirects (or None)
        error:       Error message string (or None)
    """
    try:
        resp = await client.head(url, follow_redirects=True, timeout=timeout)
        return resp.status_code, str(resp.url), None
    except httpx.RequestError as e:
        return None, None, str(e)
    except Exception as e:
        return None, None, str(e)


def _domain_changed(original_url: str, final_url: str) -> bool:
    """Return True if final_url has a different domain than original_url."""
    orig_host = urlparse(original_url).netloc.replace("www.", "").lower()
    final_host = urlparse(final_url).netloc.replace("www.", "").lower()
    return orig_host != final_host and bool(final_host)


# ── Per-city health check ─────────────────────────────────────────────────────

async def _check_city(
    client: httpx.AsyncClient,
    city_slug: str,
    source: dict,
    cfg: AgentConfig,
) -> HealthCheckResult:
    """Probe the best_source URL for one city and return a HealthCheckResult."""
    best = source.get("best_source", {})
    url = best.get("url", "")
    city = source.get("city", city_slug)
    state = source.get("state", "")

    if not url:
        return HealthCheckResult(
            city=city, state=state, city_slug=city_slug,
            url="", status_code=None, redirected_to=None,
            migration_detected=False, error="No URL in source.json",
        )

    status, final_url, error = await _probe_url(client, url)

    migration = False
    if status is not None and status >= 400:
        migration = True
    elif final_url and _domain_changed(url, final_url):
        migration = True

    return HealthCheckResult(
        city=city, state=state, city_slug=city_slug,
        url=url,
        status_code=status,
        redirected_to=final_url if (final_url and final_url != url) else None,
        migration_detected=migration,
        error=error,
    )


# ── Batch health check ────────────────────────────────────────────────────────

async def run_health_check(
    storage: StorageBackend,
    cfg: AgentConfig,
    city_slugs: list[str] | None = None,
    concurrency: int = 10,
    re_discover: bool = False,
) -> list[HealthCheckResult]:
    """
    Probe all (or specified) cities for URL health.

    Args:
        storage:      Storage backend for reading source.json files
        cfg:          Agent configuration
        city_slugs:   Specific slugs to check (default: all cities)
        concurrency:  Max concurrent HTTP requests
        re_discover:  If True, trigger source discovery for migrated cities

    Returns:
        List of HealthCheckResult, one per city checked.
    """
    # Discover city slugs if not provided
    if city_slugs is None:
        all_keys = storage.list_keys(cfg.sources_prefix)
        city_slugs = list({
            k.split("/")[-2]    # "meeting_pipeline/sources/{slug}/source.json" → slug
            for k in all_keys
            if k.endswith("source.json") and len(k.split("/")) >= 4
        })
        city_slugs.sort()

    notification_log.log_event(
        notification_log.HEALTH_CHECK_STARTED, "batch", "ALL",
        storage=storage, logs_prefix=cfg.logs_prefix,
        city_count=len(city_slugs),
    )

    # Load all source.json files
    sources: dict[str, dict] = {}
    for slug in city_slugs:
        key = f"{cfg.sources_prefix}/{slug}/source.json"
        if storage.exists(key):
            try:
                sources[slug] = storage.read_json(key)
            except Exception as e:
                print(f"[discovery_agent] Could not load {key}: {e}")

    # Run probes concurrently
    semaphore = asyncio.Semaphore(concurrency)
    results: list[HealthCheckResult] = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15,
        headers=headers,
        verify=False,  # some cities have expired SSL certs
    ) as client:

        async def _probe_with_semaphore(slug: str, source: dict) -> HealthCheckResult:
            async with semaphore:
                await asyncio.sleep(0.1)  # gentle rate limiting
                return await _check_city(client, slug, source, cfg)

        tasks = [
            _probe_with_semaphore(slug, source)
            for slug, source in sources.items()
        ]
        results = list(await asyncio.gather(*tasks, return_exceptions=False))

    # Log migrations
    migrations = [r for r in results if r.migration_detected]
    for r in migrations:
        notification_log.log_event(
            notification_log.MIGRATION_DETECTED, r.city, r.state,
            storage=storage, logs_prefix=cfg.logs_prefix,
            url=r.url,
            status_code=r.status_code,
            redirected_to=r.redirected_to,
            error=r.error,
        )

    notification_log.log_event(
        notification_log.HEALTH_CHECK_COMPLETE, "batch", "ALL",
        storage=storage, logs_prefix=cfg.logs_prefix,
        cities_checked=len(results),
        migrations_detected=len(migrations),
    )

    # Optionally trigger re-discovery for migrated cities
    if re_discover and migrations:
        await _re_discover_cities(migrations, storage, cfg)

    return results


async def _re_discover_cities(
    migrations: list[HealthCheckResult],
    storage: StorageBackend,
    cfg: AgentConfig,
) -> None:
    """
    Trigger source discovery for cities with detected migrations.

    Imports and calls the existing source_discover.py logic.
    """
    try:
        from meeting_pipeline.scripts.source_discover import run_discovery_for_city
    except ImportError:
        print("[discovery_agent] WARNING: source_discover not importable, skipping re-discovery")
        return

    for r in migrations:
        print(f"[discovery_agent] Re-discovering {r.city}, {r.state} (migration detected)")
        try:
            await run_discovery_for_city(r.city, r.state)
        except Exception as e:
            print(f"[discovery_agent] Re-discovery failed for {r.city}: {e}")
