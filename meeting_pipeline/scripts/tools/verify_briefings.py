"""
verify_briefings.py — Quality verification for generated meeting briefings.

Reads all briefings from S3, runs sanity checks, and emits a structured report.

Checks:
  A — Body name validation (non-governing body)
  B — Future meeting stub (>14 days out, <5 items)
  C — Wrong-city detection (city name / state mismatch)
  D — Item count sanity (<3 items)
  E — Duplicate body/date for the same city slug
  F — Agenda content contamination (school/non-city keywords in item titles)
  G — BoardDocs account name validation (confirm city government, not school/district)

Usage:
    uv run python meeting_pipeline/scripts/verify_briefings.py
    uv run python meeting_pipeline/scripts/verify_briefings.py --fix   (stub — not yet implemented)
    uv run python meeting_pipeline/scripts/verify_briefings.py --city johnstown-OH

Output:
    Console report
    s3://meeting-pipeline-dev/meeting_pipeline/output/briefing_verification.json
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent

from meeting_pipeline.shared.config import AgentConfig, get_storage

# ============================================================================
# CHECK F — CONTENT CONTAMINATION KEYWORDS
# ============================================================================

SCHOOL_CONTENT_KEYWORDS = [
    "staffing proposal", "elementary", "school board", "curriculum",
    "superintendent", "principal", "enrollment", "instructional", "pupil",
    "teacher", "cafeteria", "school district", "isd", "usd", "student",
    "grade level", "phy ed", "k-5", "k-8", "k-12", "special education",
]

OTHER_WRONG_ENTITY_KEYWORDS = [
    "library board", "fire district", "water district", "overdue fines",
    "book collection", "fire apparatus",
]

CONTENT_CONTAMINATION_KEYWORDS = SCHOOL_CONTENT_KEYWORDS + OTHER_WRONG_ENTITY_KEYWORDS

# ============================================================================
# CHECK G — BOARDDOCS PAGE TITLE KEYWORDS
# ============================================================================

BOARDDOCS_WRONG_ENTITY_KEYWORDS = [
    "school", "district", "isd", "library", "fire", "water",
    "board of education", "education board",
]

# Cache: BoardDocs slug → (is_wrong: bool, page_title: str)
_boarddocs_cache: dict[str, tuple[bool, str]] = {}

# ============================================================================
# BODY NAME VALIDATION PATTERNS
# ============================================================================

# Bodies that ARE governing bodies — these are allowed.
ALLOWED_BODY_PATTERNS = [
    r"city council",
    r"common council",
    r"town council",
    r"village council",
    r"board of aldermen",
    r"board of alderpersons",
    r"board of mayor and aldermen",
    r"aldermanic council",
    r"board of commissioners",
    r"city commission",
    r"board of trustees",
    r"municipal council",
    r"metro council",
    r"city manager",           # Sometimes presents to council
]

# Keywords that suggest a non-governing body — flag as suspicious.
SUSPICIOUS_BODY_KEYWORDS = [
    "planning",
    "zoning",
    "committee",
    "commission",
    "school",
    "library",
    "fire",
    "water",
    "utilities",
    "parks",
    "housing",
    "economic development",
    "redevelopment",
    "historic",
    "ethics",
    "civil service",
    "personnel",
    "budget",   # budget committee ≠ council
    "finance",  # finance committee ≠ council
]

# ============================================================================
# PILOT CITY REGISTRY — slug → expected city name + state
# ============================================================================

def _build_registry(cfg: AgentConfig = None, storage=None) -> dict[str, dict]:
    """Build a slug→{city, state} lookup from source.json files in storage."""
    if cfg is None:
        cfg = AgentConfig.from_env()
    if storage is None:
        storage = get_storage(cfg)
    registry: dict[str, dict] = {}
    for key in storage.list_keys(cfg.sources_prefix):
        if not key.endswith("/source.json"):
            continue
        slug = key.split("/")[-2]
        try:
            source = storage.read_json(key)
            registry[slug] = {"city": source.get("city", ""), "state": source.get("state", "")}
        except Exception:
            pass
    return registry


# Lazy-loaded on first use (needs AWS credentials at runtime, not import time)
PILOT_REGISTRY: dict[str, dict] | None = None


def _get_registry() -> dict[str, dict]:
    global PILOT_REGISTRY
    if PILOT_REGISTRY is None:
        PILOT_REGISTRY = _build_registry()
    return PILOT_REGISTRY


# ============================================================================
# HELPERS
# ============================================================================

def _body_is_governing(body: str) -> bool:
    """Return True if the body name matches a known governing body pattern."""
    body_lower = body.lower()
    return any(re.search(pat, body_lower) for pat in ALLOWED_BODY_PATTERNS)


def _body_suspicious_keywords(body: str) -> list[str]:
    """Return list of suspicious keywords found in the body name."""
    body_lower = body.lower()
    return [kw for kw in SUSPICIOUS_BODY_KEYWORDS if kw in body_lower]


def _days_from_today(date_str: str) -> Optional[int]:
    """Return days from today to the meeting date (positive = future)."""
    try:
        meeting_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (meeting_date - date.today()).days
    except (ValueError, TypeError):
        return None


def _total_items(briefing: dict) -> int:
    """Return the total number of agenda items in the briefing."""
    return briefing.get("executiveSummary", {}).get("totalAgendaItems", 0)


def _slug_from_key(key: str) -> str:
    """Extract city slug from an S3 key like .../briefings/johnstown-OH_2026-04-07_briefing.json."""
    filename = key.split("/")[-1]                   # johnstown-OH_2026-04-07_briefing.json
    return filename.split("_")[0]                   # johnstown-OH


def _date_from_key(key: str) -> str:
    """Extract date portion from an S3 key filename."""
    filename = key.split("/")[-1]
    parts = filename.split("_")
    return parts[1] if len(parts) >= 2 else ""


# ============================================================================
# CHECKS
# ============================================================================

def check_a_body(slug: str, briefing: dict) -> Optional[dict]:
    """Check A: body name validation."""
    meeting = briefing.get("meeting", {})
    body = meeting.get("body", "")
    if not body:
        return {
            "level": "WARNING",
            "check": "A",
            "slug": slug,
            "message": f'body is empty/missing',
            "detail": {},
        }

    if not _body_is_governing(body):
        suspicious = _body_suspicious_keywords(body)
        return {
            "level": "CRITICAL",
            "check": "A",
            "slug": slug,
            "message": f'body="{body}" — not a recognized governing body',
            "detail": {"body": body, "suspicious_keywords": suspicious},
        }

    # Body IS allowed — but also check for suspicious keywords as a secondary warning.
    suspicious = _body_suspicious_keywords(body)
    if suspicious:
        return {
            "level": "WARNING",
            "check": "A",
            "slug": slug,
            "message": f'body="{body}" — contains suspicious keywords: {suspicious}',
            "detail": {"body": body, "suspicious_keywords": suspicious},
        }

    return None


def check_b_future_stub(slug: str, briefing: dict) -> Optional[dict]:
    """Check B: future meeting with very few items."""
    meeting = briefing.get("meeting", {})
    date_str = meeting.get("date", "")
    days_out = _days_from_today(date_str)
    total = _total_items(briefing)

    if days_out is not None and days_out > 14 and total < 5:
        return {
            "level": "WARNING",
            "check": "B",
            "slug": slug,
            "message": f"{total} items, {days_out} days out — likely stub agenda",
            "detail": {"date": date_str, "days_out": days_out, "total_items": total},
        }
    return None


def check_c_wrong_city(slug: str, briefing: dict) -> Optional[dict]:
    """Check C: city name or state in briefing doesn't match the slug."""
    expected = _get_registry().get(slug)
    if not expected:
        # Slug not in registry — skip city check but note it
        return None

    meeting = briefing.get("meeting", {})
    briefing_city = (meeting.get("cityName") or "").strip()
    briefing_state = (meeting.get("state") or "").strip().upper()

    expected_city = expected["city"]
    expected_state = expected["state"].upper()

    city_matches = briefing_city.lower() == expected_city.lower()
    state_matches = briefing_state == expected_state

    if not city_matches or not state_matches:
        return {
            "level": "CRITICAL",
            "check": "C",
            "slug": slug,
            "message": (
                f'city mismatch — briefing has "{briefing_city}, {briefing_state}" '
                f'but slug {slug} expects "{expected_city}, {expected_state}"'
            ),
            "detail": {
                "briefing_city": briefing_city,
                "briefing_state": briefing_state,
                "expected_city": expected_city,
                "expected_state": expected_state,
            },
        }
    return None


def check_d_low_items(slug: str, briefing: dict) -> Optional[dict]:
    """Check D: total agenda items < 3 (extraction failure or empty stub)."""
    total = _total_items(briefing)
    if total < 3:
        meeting = briefing.get("meeting", {})
        return {
            "level": "INFO",
            "check": "D",
            "slug": slug,
            "message": f"only {total} total agenda items — possible extraction failure",
            "detail": {"total_items": total, "date": meeting.get("date", "")},
        }
    return None


def check_e_duplicates(slug_date_pairs: list[tuple[str, str]]) -> list[dict]:
    """Check E: multiple briefings for the same city slug on the same date."""
    seen: defaultdict[tuple, list] = defaultdict(list)
    for slug, date_str, key in slug_date_pairs:
        seen[(slug, date_str)].append(key)

    issues = []
    for (slug, date_str), keys in seen.items():
        if len(keys) > 1:
            issues.append({
                "level": "WARNING",
                "check": "E",
                "slug": slug,
                "message": f"duplicate briefings for {slug} on {date_str}: {keys}",
                "detail": {"keys": keys, "date": date_str},
            })
    return issues


def check_f_content_contamination(
    slug: str, briefing: dict, normalized_json: Optional[dict] = None
) -> Optional[dict]:
    """Check F: scan agenda item titles for school/non-city content keywords."""
    # Collect all item titles from the briefing
    titles: list[str] = []

    # Top-level agenda items
    for item in briefing.get("agendaItems", []):
        title = item.get("title", "")
        if title:
            titles.append(title)
        # Sub-items
        for sub in item.get("subItems", []):
            sub_title = sub.get("title", "")
            if sub_title:
                titles.append(sub_title)

    # Also check inside keyTopics / highlightedItems if present
    exec_summary = briefing.get("executiveSummary", {})
    for topic in exec_summary.get("keyTopics", []):
        t = topic.get("title", "") or topic.get("topic", "") or ""
        if t:
            titles.append(t)

    # Also scan priorityIssues titles and descriptions
    for issue in briefing.get("priorityIssues", []):
        t = issue.get("title", "") or issue.get("topic", "") or ""
        if t:
            titles.append(t)
        d = issue.get("description", "") or issue.get("context", "") or ""
        if d:
            titles.append(d)

    # Fallback: scan normalized JSON agenda items if briefing has no agenda items
    if not titles and normalized_json:
        agenda = normalized_json.get("agenda", {})
        for item in agenda.get("items", []):
            t = item.get("title", "") or ""
            if t:
                titles.append(t)
            d = item.get("description", "") or ""
            if d:
                titles.append(d)

    matched_keywords: list[str] = []
    matched_titles: list[str] = []

    for title in titles:
        title_lower = title.lower()
        for kw in CONTENT_CONTAMINATION_KEYWORDS:
            if kw in title_lower and kw not in matched_keywords:
                matched_keywords.append(kw)
                matched_titles.append(title)
                break  # only count each title once

    if len(matched_keywords) >= 2:
        return {
            "level": "CRITICAL",
            "check": "F",
            "slug": slug,
            "message": "content_contamination: agenda items contain school/non-city content",
            "detail": {
                "matched_keywords": matched_keywords,
                "matched_titles": matched_titles[:5],  # cap for readability
            },
        }
    return None


def _fetch_boarddocs_title(slug_path: str) -> tuple[bool, str]:
    """
    Fetch the BoardDocs page for the given account path (e.g. 'wi/janesville').
    Returns (is_wrong_entity, page_title).
    Results are cached in _boarddocs_cache.
    """
    if slug_path in _boarddocs_cache:
        return _boarddocs_cache[slug_path]

    url = f"https://go.boarddocs.com/{slug_path}/Board.nsf/Public"
    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html = resp.text

        # Extract <title>
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        page_title = title_match.group(1).strip() if title_match else ""

        # Also grab first <h1> if title is generic
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
        h1_text = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip() if h1_match else ""

        # BoardDocs sometimes puts the entity name in meta DESCRIPTION or JS windowTitleNew
        meta_desc_match = re.search(
            r'<meta[^>]+name=["\']DESCRIPTION["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
        meta_desc = meta_desc_match.group(1).strip() if meta_desc_match else ""

        win_title_match = re.search(r'windowTitleNew\s*=\s*["\']([^"\']+)["\']', html)
        win_title = win_title_match.group(1).strip() if win_title_match else ""

        combined = f"{page_title} {h1_text} {meta_desc} {win_title}".lower()
        is_wrong = any(kw in combined for kw in BOARDDOCS_WRONG_ENTITY_KEYWORDS)
        display_title = page_title or h1_text or meta_desc or "(no title)"
        _boarddocs_cache[slug_path] = (is_wrong, display_title)
        return is_wrong, display_title

    except Exception as e:
        # Network failure — don't flag as wrong, just note the error
        error_msg = f"fetch error: {e}"
        _boarddocs_cache[slug_path] = (False, error_msg)
        return False, error_msg


def check_g_boarddocs_account(
    slug: str,
    briefing: dict,
    normalized_json: Optional[dict],
) -> Optional[dict]:
    """Check G: validate BoardDocs account name for city government (not school/district)."""
    # Determine platform from briefing or normalized JSON
    platform = ""
    platform_url = ""

    # Try normalized JSON first (most reliable)
    if normalized_json:
        norm_meeting = normalized_json.get("meeting", {})
        sources = normalized_json.get("sources", {})
        platform = (norm_meeting.get("platform", "") or sources.get("platform", "") or "").lower()
        platform_url = sources.get("platform_meeting_url", "") or ""

    # Fall back to briefing metadata
    if not platform:
        meeting = briefing.get("meeting", {})
        platform = (
            meeting.get("platform", "")
            or meeting.get("sourceType", "")
            or meeting.get("source", "")
            or ""
        ).lower()
        platform_url = (
            meeting.get("platformMeetingUrl", "")
            or meeting.get("sourceUrl", "")
            or ""
        )
    elif not platform_url:
        # platform found in normalized but no URL yet — also try briefing sourceUrl
        meeting = briefing.get("meeting", {})
        platform_url = (
            meeting.get("platformMeetingUrl", "")
            or meeting.get("sourceUrl", "")
            or ""
        )

    if "boarddocs" not in platform and "boarddocs" not in platform_url.lower():
        return None  # Not a BoardDocs briefing

    # Extract account slug from URL
    # e.g. https://go.boarddocs.com/wi/janesville/Board.nsf/...  → "wi/janesville"
    bd_match = re.search(r"go\.boarddocs\.com/([^/]+/[^/]+)", platform_url)
    if not bd_match:
        return {
            "level": "WARNING",
            "check": "G",
            "slug": slug,
            "message": "BoardDocs source detected but could not parse account URL",
            "detail": {"platform_url": platform_url},
        }

    slug_path = bd_match.group(1)  # e.g. "wi/janesville"
    is_wrong, page_title = _fetch_boarddocs_title(slug_path)

    if is_wrong:
        return {
            "level": "CRITICAL",
            "check": "G",
            "slug": slug,
            "message": f"wrong_entity: BoardDocs account '{slug_path}' appears to be a non-city entity",
            "detail": {
                "boarddocs_slug": slug_path,
                "page_title": page_title,
                "platform_url": platform_url,
            },
        }
    return None


def check_h_source_citations(slug: str, briefing: dict) -> Optional[dict]:
    """Check H: source_citations populated in at least some priority issues."""
    # priorityIssues is at top level of briefing JSON
    issues_list = briefing.get("priorityIssues", [])
    if not issues_list:
        # No priority issues — nothing to check
        return None

    total_issues = len(issues_list)
    issues_with_citations = 0

    for issue in issues_list:
        detail = issue.get("detail", {})
        citations = detail.get("sourceCitations", [])
        if citations:
            issues_with_citations += 1

    if issues_with_citations == 0:
        return {
            "level": "WARNING",
            "check": "H",
            "slug": slug,
            "message": f"no source_citations in any of {total_issues} priority issue(s) — old briefing or citation wiring broken",
            "detail": {"total_issues": total_issues, "issues_with_citations": 0},
        }
    return None


def check_i_haystaq_data(slug: str, briefing: dict) -> Optional[dict]:
    """Check I: Haystaq constituent data was used (check constituentData block in briefing)."""
    constituent = briefing.get("constituentData", {})
    available = constituent.get("available", False)
    voter_count = constituent.get("voterCount", 0)

    if not available or not voter_count:
        return {
            "level": "INFO",
            "check": "I",
            "slug": slug,
            "message": "no Haystaq constituent data — briefing generated without voter context",
            "detail": {"available": available, "voterCount": voter_count},
        }
    return None


# ============================================================================
# MAIN VERIFICATION LOOP
# ============================================================================

def _load_normalized_json(slug: str, date_str: str, normalized_prefix: str, storage) -> Optional[dict]:
    """
    Load the normalized JSON for a given slug and date from S3.

    Tries the exact key first: {normalized_prefix}/{slug}_{date}.json
    Falls back to listing the prefix and finding the best match.
    """
    exact_key = f"{normalized_prefix}/{slug}_{date_str}.json"
    try:
        if storage.exists(exact_key):
            return storage.read_json(exact_key)
    except Exception:
        pass

    # Fallback: scan the normalized prefix for any file matching the slug
    try:
        all_keys = storage.list_keys(f"{normalized_prefix}/{slug}")
        # Pick most recent matching key
        candidates = sorted(
            k for k in all_keys
            if re.search(rf"{re.escape(slug)}_\d{{4}}-\d{{2}}-\d{{2}}\.json$", k)
        )
        if candidates:
            return storage.read_json(candidates[-1])
    except Exception:
        pass

    return None


def verify_briefings(cfg: AgentConfig, storage, city_filter: Optional[str] = None) -> dict:
    """Download and verify all briefings, returning a structured report dict."""
    briefing_prefix = f"{cfg.output_prefix}/briefings"
    normalized_prefix = f"{cfg.output_prefix}/normalized"

    print(f"\nScanning briefings at s3://{cfg.s3_bucket}/{briefing_prefix} ...")
    all_keys = storage.list_keys(briefing_prefix)
    briefing_keys = sorted(
        k for k in all_keys
        if re.search(r"[^/]+_\d{4}-\d{2}-\d{2}_briefing\.json$", k)
    )

    if city_filter:
        briefing_keys = [k for k in briefing_keys if city_filter in k]

    print(f"Found {len(briefing_keys)} briefing file(s).")

    issues: list[dict] = []
    slug_date_keys: list[tuple[str, str, str]] = []
    briefings_checked = 0
    clean = 0

    for key in briefing_keys:
        slug = _slug_from_key(key)
        date_str = _date_from_key(key)

        print(f"  Checking {slug}_{date_str} ...", end=" ", flush=True)

        try:
            briefing = storage.read_json(key)
        except Exception as e:
            issues.append({
                "level": "CRITICAL",
                "check": "load",
                "slug": slug,
                "message": f"failed to load briefing JSON: {e}",
                "detail": {"key": key},
            })
            print("LOAD ERROR")
            continue

        briefings_checked += 1
        slug_date_keys.append((slug, date_str, key))

        # Load normalized JSON for checks G (BoardDocs validation)
        normalized_json = _load_normalized_json(slug, date_str, normalized_prefix, storage)

        item_issues: list[dict] = []

        for check_fn in [check_a_body, check_b_future_stub, check_c_wrong_city, check_d_low_items]:
            result = check_fn(slug, briefing)
            if result:
                result["s3_key"] = key
                item_issues.append(result)

        # Check F — content contamination
        result_f = check_f_content_contamination(slug, briefing, normalized_json)
        if result_f:
            result_f["s3_key"] = key
            item_issues.append(result_f)

        # Check G — BoardDocs account validation
        result_g = check_g_boarddocs_account(slug, briefing, normalized_json)
        if result_g:
            result_g["s3_key"] = key
            item_issues.append(result_g)

        # Check H — source citations presence
        result_h = check_h_source_citations(slug, briefing)
        if result_h:
            result_h["s3_key"] = key
            item_issues.append(result_h)

        # Check I — Haystaq constituent data
        result_i = check_i_haystaq_data(slug, briefing)
        if result_i:
            result_i["s3_key"] = key
            item_issues.append(result_i)

        if item_issues:
            print(f"{len(item_issues)} issue(s)")
            issues.extend(item_issues)
        else:
            clean += 1
            print("OK")

    # Check E — duplicates across all briefings
    dup_issues = check_e_duplicates(slug_date_keys)
    for issue in dup_issues:
        issue["s3_key"] = ""  # multiple keys, listed in detail
    issues.extend(dup_issues)

    return {
        "generated_at": datetime.now().isoformat(),
        "total_checked": briefings_checked,
        "total_issues": len(issues),
        "clean": clean,
        "issues": issues,
    }


# ============================================================================
# REPORT PRINTING
# ============================================================================

def print_report(report: dict) -> None:
    issues = report["issues"]

    criticals = [i for i in issues if i["level"] == "CRITICAL"]
    warnings = [i for i in issues if i["level"] == "WARNING"]
    infos = [i for i in issues if i["level"] == "INFO"]

    print("\n" + "=" * 60)
    print("BRIEFING VERIFICATION REPORT")
    print("=" * 60)

    if criticals:
        print(f"\nCRITICAL — Wrong city/body/content ({len(criticals)}):")
        for issue in criticals:
            label = f"{issue['slug']}_{issue['detail'].get('date', '')}" if issue.get('detail', {}).get('date') else issue['slug']
            check_label = f"[{issue['check']}]"
            print(f"  {check_label} {label}: {issue['message']}")
            # Extra detail for contamination checks
            if issue["check"] == "F":
                d = issue["detail"]
                print(f"      keywords: {d.get('matched_keywords', [])}")
                for t in d.get("matched_titles", []):
                    print(f"      title: \"{t}\"")
            if issue["check"] == "G":
                d = issue["detail"]
                print(f"      boarddocs_slug: {d.get('boarddocs_slug', '?')}  page_title: \"{d.get('page_title', '?')}\"")
                print(f"      url: {d.get('platform_url', '?')}")
    else:
        print("\nCRITICAL: none")

    if warnings:
        print(f"\nWARNING ({len(warnings)}):")

        stub_warns = [i for i in warnings if i["check"] == "B"]
        if stub_warns:
            print("  Stub agendas (future + < 5 items):")
            for i in stub_warns:
                d = i["detail"]
                label = f"{i['slug']}_{d.get('date', '')}"
                print(f"    {label}: {d.get('total_items', '?')} items, {d.get('days_out', '?')} days out")

        body_warns = [i for i in warnings if i["check"] == "A"]
        if body_warns:
            print("  Suspicious body name:")
            for i in body_warns:
                d = i["detail"]
                label = f"{i['slug']}"
                print(f"    {label}: body=\"{d.get('body', '?')}\" — {i['message']}")

        dup_warns = [i for i in warnings if i["check"] == "E"]
        if dup_warns:
            print("  Duplicate briefings:")
            for i in dup_warns:
                print(f"    {i['slug']}: {i['message']}")

        citation_warns = [i for i in warnings if i["check"] == "H"]
        if citation_warns:
            print(f"  Missing source_citations ({len(citation_warns)}):")
            for i in citation_warns:
                print(f"    {i['slug']}: {i['message']}")
    else:
        print("\nWARNING: none")

    if infos:
        print(f"\nINFO ({len(infos)}):")
        for i in infos:
            d = i["detail"]
            check = i["check"]
            if check == "D":
                label = f"{i['slug']}_{d.get('date', '')}"
                print(f"  [D] {label}: {i['message']}")
            elif check == "I":
                print(f"  [I] {i['slug']}: {i['message']}")
            else:
                label = f"{i['slug']}_{d.get('date', '')}"
                print(f"  [{check}] {label}: {i['message']}")

    # Summary counts by check
    check_counts: dict[str, int] = {}
    for issue in issues:
        c = issue["check"]
        check_counts[c] = check_counts.get(c, 0) + 1

    print("\n" + "-" * 60)
    print("SUMMARY")
    print(f"  Total briefings checked : {report['total_checked']}")
    print(f"  Critical issues         : {len(criticals)}")
    print(f"  Warnings                : {len(warnings)}")
    print(f"  Info                    : {len(infos)}")
    print(f"  Clean (no issues)       : {report['clean']}")
    if check_counts:
        print("\n  Issues by check:")
        for chk, cnt in sorted(check_counts.items()):
            labels = {
                "A": "Body name",
                "B": "Future stub",
                "C": "Wrong city",
                "D": "Low item count",
                "E": "Duplicate",
                "F": "Content contamination",
                "G": "BoardDocs entity",
                "H": "Missing source citations",
                "I": "Missing Haystaq data",
            }
            print(f"    [{chk}] {labels.get(chk, chk)}: {cnt}")
    print("=" * 60 + "\n")


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Verify generated meeting briefings for quality issues")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Delete CRITICAL briefings from S3 (not yet implemented)",
    )
    parser.add_argument(
        "--city",
        help="Filter to a single city slug (e.g. louisville-OH)",
        default=None,
    )
    args = parser.parse_args()

    if args.fix:
        print("--fix not yet implemented")
        sys.exit(0)

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    report = verify_briefings(cfg, storage, city_filter=args.city)

    print_report(report)

    # Write JSON report to S3
    output_key = f"{cfg.output_prefix}/briefing_verification.json"
    storage.write_json(output_key, report)
    print(f"Report written to s3://{cfg.s3_bucket}/{output_key}")


if __name__ == "__main__":
    main()
