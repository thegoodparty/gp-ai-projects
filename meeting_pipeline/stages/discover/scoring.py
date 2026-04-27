"""
scoring.py — Candidate scoring and ranking for source discovery.

Scores candidates based on freshness, platform tier, domain trust,
URL authority signals, and body match. Used to select the best source
from multiple candidates found during discovery.
"""

from urllib.parse import urlparse

from meeting_pipeline.shared.constants import (
    FRESHNESS_SCORE,
    PLATFORM_TIER,
    SOURCE_BONUS,
    STATE_NAMES,
    GOV_PLATFORM_DOMAINS,
    NEWS_DOMAIN_SUFFIXES,
)

# ── Domain trust classification ───────────────────────────────────────────────

# Domain keyword fragments indicating content/media sites (not government)
_CONTENT_SITE_DOMAIN_KEYWORDS = [
    "news", "times", "herald", "journal", "tribune", "reporter",
    "chronicle", "gazette", "press", "dispatch", "daily", "weekly",
    "channel", "radio", "media", "broadcast", "network",
    "hotel", "hotels", "agoda", "expedia", "kayak", "booking",
    "apartments", "realty", "homes", "travel", "airbnb", "vrbo",
    "weather", "accuweather",
    "tiktok", "youtube", "facebook", "instagram", "twitter",
    "forum", "patch", "blog",
    "directory", "yellowpages", "whitepages", "yelp", "mapquest",
]

# URL path fragments indicating content/media pages
_CONTENT_SITE_PATH_PATTERNS = [
    "/article/", "/articles/", "/story/", "/stories/",
    "/news/local/", "/watch?", "/channel/", "/@", "/video/",
]


def classify_domain_trust(url: str, city: str = "", state: str = "") -> float:
    """
    Return a trust multiplier (0.1–1.0) for how likely this URL is a gov source.

    1.0 = verified government (.gov TLD) or known meeting platform
    0.7 = city name found in domain (likely official city site)
    0.4 = generic unknown domain (no government signal)
    0.1 = structural content-site signals (news, TV, travel, social media)
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().replace("www.", "")
    url_lower = url.lower()

    # Known government meeting platforms → full trust
    for plat_domain in GOV_PLATFORM_DOMAINS:
        if plat_domain in netloc:
            return 1.0

    # .gov TLD → full trust
    if netloc.endswith(".gov") or ".gov/" in url_lower:
        return 1.0

    # Content-site domain keywords → penalty
    domain_base = netloc.split(".")[0] if "." in netloc else netloc
    for kw in _CONTENT_SITE_DOMAIN_KEYWORDS:
        if kw in domain_base or kw in netloc:
            return 0.1

    # Content-site URL path patterns
    for pat in _CONTENT_SITE_PATH_PATTERNS:
        if pat in url_lower:
            return 0.1

    # .tv TLD or "tv" in SLD → TV station
    if netloc.endswith(".tv") or domain_base.endswith("tv") or domain_base.startswith("tv"):
        return 0.1

    # "online" suffix → news/media blog
    if domain_base.endswith("online"):
        return 0.1

    # County government → reduced trust when searching for a city
    _parts = netloc.split(".")
    if (len(_parts) >= 4 and _parts[0] == "co" and _parts[-1] in ("us", "gov")) or \
       ("county" in _parts[0] and netloc.endswith((".gov", ".us"))) or \
       (len(_parts) >= 2 and _parts[0] == "county"):
        return 0.3

    # City name in domain → likely official
    if city:
        city_slug = city.lower().replace(" ", "").replace("-", "").replace("'", "")
        domain_clean = netloc.replace("-", "").replace(".", "")
        if city_slug in domain_clean:
            return 0.7
        if state:
            state_lower = state.lower()
            state_name_slug = STATE_NAMES.get(state.upper(), "").lower().replace(" ", "")
            if (city_slug + state_lower) in domain_clean:
                return 0.7
            if state_name_slug and (city_slug + state_name_slug[:4]) in domain_clean:
                return 0.7

    return 0.4


# ── Agenda authority scoring ──────────────────────────────────────────────────

def agenda_authority_score(c: dict) -> int:
    """
    Bonus points for URL/content signals proving this is an agenda source.
    Max ~65 points. Rewards government agenda pages over unrelated fresh pages.
    """
    url_lower = (c.get("url") or "").lower()
    notes_lower = (c.get("notes") or "").lower()
    platform = c.get("platform") or "unknown"
    score = 0

    # API platforms get a strong bonus
    if platform in ("legistar", "civicclerk", "civicplus", "escribe", "boarddocs"):
        score += 25

    # URL path signals
    path = url_lower.split("?")[0]

    def _in_path(kw: str) -> bool:
        return f"/{kw}" in path or f"-{kw}" in path or path.endswith(f"/{kw}")

    if _in_path("agendacenter") or _in_path("agenda-center"):
        score += 20
    elif _in_path("agendas") or _in_path("agenda"):
        score += 15
    if _in_path("minutes"):
        score += 10
    if _in_path("citycouncil") or _in_path("city-council"):
        score += 8
    elif _in_path("council"):
        score += 5
    if _in_path("meetings") or _in_path("meeting"):
        score += 5

    # Content signals from search snippet / notes
    if "agenda" in notes_lower:
        score += 5
    if "minutes" in notes_lower:
        score += 3

    return score


# ── Candidate scoring and ranking ─────────────────────────────────────────────

def candidate_score(c: dict, city: str = "", state: str = "") -> int:
    """Score a discovery candidate. Higher = better source."""
    f = FRESHNESS_SCORE.get(c.get("freshness") or "", 0)
    p = PLATFORM_TIER.get(c.get("platform") or "unknown", 4)
    s = SOURCE_BONUS.get(c.get("source") or "probe", 0)
    b = 10 if c.get("body_match") else 0
    a = agenda_authority_score(c)

    trust = classify_domain_trust(c.get("url") or "", city, state)

    # Gov-domain floor: treat unverified .gov URLs with strong agenda signals
    # as stale_warning (35) instead of unknown (5) for ranking purposes.
    _unverified = c.get("freshness") in ("unknown", "unknown_spa")
    if trust >= 1.0 and _unverified and f < FRESHNESS_SCORE["stale_warning"] and a >= 20:
        f = FRESHNESS_SCORE["stale_warning"]

    return int(f * trust) + p + s + b + a


def rank_candidates(candidates: list[dict], city: str = "", state: str = "") -> list[dict]:
    """Sort candidates by score (highest first) and assign rank numbers."""
    ranked = sorted(candidates, key=lambda c: candidate_score(c, city, state), reverse=True)
    for i, c in enumerate(ranked):
        c["rank"] = i + 1
    return ranked
