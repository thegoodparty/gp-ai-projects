"""
search.py — Web search backends for source discovery.

Each function wraps a different search API and returns (candidates, query_used).
Candidates are standardized dicts (via make_candidate) that feed into scoring.

Search backends:
  - Serper.dev (real Google results — primary)
  - Firecrawl (search + extract — last resort)
  - PDF search (filetype:pdf Google search — platform detection)
"""

import os
from datetime import date
from urllib.parse import urlparse

import httpx

from meeting_pipeline.shared.constants import (
    PDF_PLATFORM_SIGNALS,
)
from meeting_pipeline.shared.date_utils import extract_dates
from meeting_pipeline.shared.discovery_helpers import make_candidate
from meeting_pipeline.shared.url_utils import (
    detect_platform,
    is_non_agenda_url,
    is_wrong_city,
    normalize_platform_url,
)

# Cost tracking — shared module-level dict, updated by callers
_COST = {
    "serper_searches": 0,
}


# ── Result normalization ─────────────────────────────────────────────────────

def search_results_to_candidates(
    raw_results: list[dict],
    city: str,
    source_label: str,
    state: str = "",
) -> list[dict]:
    """
    Convert {url, title, content} dicts to verified candidate dicts.

    Shared by all search backends. Applies wrong-city/entity filtering,
    platform detection, and snippet date extraction.
    """
    candidates = []
    for r in raw_results:
        url = r.get("url", "")
        title = r.get("title", "")
        content = r.get("content", "")
        if not url:
            continue

        # Wrong city/entity check
        if is_wrong_city(url, f"{title} {content}", city, state=state):
            continue
        if is_non_agenda_url(url):
            continue

        platform = detect_platform(url)
        url = normalize_platform_url(url, platform)

        # Path-detected platforms: verify city name in domain
        _PATH_PLATFORMS = {"civicplus", "novus", "primegov", "diligent"}
        if platform in _PATH_PLATFORMS and city:
            netloc = urlparse(url).netloc.lower().replace("www.", "")
            domain_clean = netloc.replace("-", "").replace(".", "")
            city_slug = city.lower().replace(" ", "")
            if city_slug not in domain_clean and city.lower() not in domain_clean:
                continue

        # Foreign TLD rejection for US cities
        _FOREIGN_TLDS = {".ca", ".uk", ".au", ".nz", ".ie", ".in", ".de", ".fr"}
        if state:
            netloc_raw = urlparse(url).netloc.lower()
            if any(netloc_raw.endswith(tld) for tld in _FOREIGN_TLDS):
                continue

        # CivicClerk tenant validation
        if platform == "civicclerk" and city:
            cc_netloc = urlparse(url).netloc.lower()
            if ".portal.civicclerk.com" in cc_netloc:
                tenant = cc_netloc.split(".portal.civicclerk.com")[0]
                city_tokens = [t for t in city.lower().split() if len(t) > 2]
                if city_tokens and not any(tok in tenant for tok in city_tokens):
                    continue

        # County/township domain rejection
        if city:
            netloc_check = urlparse(url).netloc.lower()
            city_lower = city.lower()
            if "county" in netloc_check and "county" not in city_lower:
                continue
            if ("township" in netloc_check or "twp" in netloc_check.split(".")[0]) and \
               "township" not in city_lower and "twp" not in city_lower:
                continue

        # Unknown platform: require city in domain or title
        _MULTITENANT = {"legistar", "boarddocs", "granicus", "municode", "civicclerk", "escribe"}
        if platform == "unknown" and city:
            netloc = urlparse(url).netloc.lower().replace("www.", "")
            domain_clean = netloc.replace("-", "").replace(".", "")
            city_slug = city.lower().replace(" ", "")
            title_lower = title.lower()
            city_in_domain = city_slug in domain_clean
            city_in_title = city_slug in title_lower.replace(" ", "") or city.lower() in title_lower
            if not city_in_domain and not city_in_title:
                continue

        # Build candidate
        note_parts = [title[:80]]
        if content:
            note_parts.append(content[:100])
        c = make_candidate(
            url=url, platform=platform, source=source_label,
            notes=" | ".join(p for p in note_parts if p).strip(" |")[:200],
        )

        # Pre-populate freshness from snippet
        if content:
            snippet_dates = extract_dates(content)
            if snippet_dates:
                c["_snippet_date"] = snippet_dates[0].isoformat()
            content_lower = content.lower()
            if any(kw in content_lower for kw in (
                "moved to", "migrated to", "new portal", "new website",
            )):
                c["notes"] += " [migration_hint_in_snippet]"

        candidates.append(c)
    return candidates


# ── Search backends ───────────────────────────────────────────────────────────

def serper_search(query: str, api_key: str) -> list[dict]:
    """Run one Serper.dev search. Returns list of {url, title} dicts (top 5).
    Raises RuntimeError on rate limit."""
    _COST["serper_searches"] += 1
    try:
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "us", "num": 10},
            timeout=15.0,
        )
        resp.raise_for_status()
        organic = resp.json().get("organic", [])
        return [
            {"url": r.get("link", ""), "title": r.get("title", "")}
            for r in organic[:5]
            if r.get("link")
        ]
    except Exception as e:
        msg = str(e)
        if "429" in msg or "quota" in msg.lower():
            raise RuntimeError(f"serper_rate_limited: {msg[:120]}") from e
        return []


async def discover_from_firecrawl(city: str, state: str) -> list[dict]:
    """Use Firecrawl search + extract to find an agenda page. Returns candidates."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return []
    try:
        from meeting_pipeline.shared.firecrawl_client import extract_meeting_links, search_agenda_page
    except ImportError:
        return []

    url = search_agenda_page(city, state)
    if not url:
        return []
    cand = make_candidate(url=url, platform="generic_html", source="firecrawl")
    meetings = extract_meeting_links(url, city, state)
    cand["body_match"] = bool(meetings)
    cand["notes"] = f"firecrawl: {len(meetings)} meetings" if meetings else "firecrawl: no meetings extracted"
    return [cand]


def discover_from_pdf_search(city: str, state: str, body_term: str) -> list[dict]:
    """
    Search Google for agenda PDFs via Serper filetype:pdf.
    Detects platforms from result URLs (Legistar, CivicClerk, etc.).
    Returns candidate dicts for any platforms found.
    """
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        return []

    _COST["serper_searches"] += 1
    today = date.today()
    query = f'"{city}" "{state}" "{body_term}" agenda filetype:pdf {today.year}'

    try:
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "us", "num": 5},
            timeout=15.0,
        )
        results = resp.json().get("organic", [])
    except Exception:
        return []

    candidates = []
    city_lower = city.lower()

    for pr in results[:5]:
        pdf_url = pr.get("link", "")
        pdf_url_lower = pdf_url.lower()

        # Verify city name in result
        if city_lower not in f"{pr.get('title', '')} {pr.get('snippet', '')}".lower():
            continue

        for platform_name, signals in PDF_PLATFORM_SIGNALS.items():
            if not any(sig in pdf_url_lower for sig in signals):
                continue

            parsed = urlparse(pdf_url)
            host = parsed.netloc.lower()

            if platform_name == "legistar" and "legistar" in host:
                slug = host.split(".")[0]
                cand = make_candidate(
                    url=f"https://webapi.legistar.com/v1/{slug}/events?$top=3&$orderby=EventDate+desc",
                    platform="legistar", source="pdf_search",
                    display_url=f"https://{slug}.legistar.com",
                    config={"legistar_slug": slug},
                    notes=f"pdf_search→{slug}.legistar.com",
                )
            elif platform_name == "civicclerk" and "civicclerk" in host:
                slug = host.split(".")[0]
                cand = make_candidate(
                    url=f"https://{slug}.portal.civicclerk.com",
                    platform="civicclerk", source="pdf_search",
                    notes=f"pdf_search→{slug}.civicclerk.com",
                )
            elif platform_name == "granicus":
                slug = host.split(".")[0] if "granicus.com" in host else (
                    parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
                )
                if not slug:
                    continue
                cand = make_candidate(
                    url=f"https://{slug}.granicus.com/ViewPublisher.php?view_id=1",
                    platform="granicus", source="pdf_search",
                    notes=f"pdf_search→{slug}.granicus.com",
                )
            else:
                cand = make_candidate(
                    url=pdf_url, platform=platform_name, source="pdf_search",
                    notes=f"pdf_search→{platform_name}",
                )

            candidates.append(cand)
            print(f"  [pdf_search] Found {platform_name}: {cand['url'][:60]}")
            break

    return candidates
