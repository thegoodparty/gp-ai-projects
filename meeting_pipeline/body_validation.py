"""
body_validation.py — Shared body validation logic for the meeting pipeline.

Used by both scan_meeting_schedule.py (pre-collection validation) and
source_discover.py (post-discovery validation for new cities).

Prevents wrong-body collection by scoring candidate body names against
the expected governing body and auto-patching source.json / manifest.json.
"""

import re
from datetime import datetime

import httpx


# ============================================================================
# SCORING CONSTANTS
# ============================================================================

# Keywords that definitively indicate a non-governing body — reject immediately
REJECT_KEYWORDS = [
    "advisory", "planning", "zoning", "historical", "library", "parks",
    "recreation", "school", "utility", "housing authority", "charter review",
    "bza", "pzc", "board of appeals", "board of adjustment", "board of review",
    "board of zoning", "ethics", "civil service", "merit", "pension",
    "beautification", "arts", "senior", "youth", "animal", "cemetery",
    "cultural", "sustainability", "tree committee", "museum",
    "pedestrian", "bicycle", "design review",
]

# Keywords that indicate a governing body — positive signal
GOVERNING_KEYWORDS = [
    "city council", "town council", "village council", "board of aldermen",
    "city commission", "town commission", "village commission",
    "municipal council", "common council", "board of commissioners",
    "board of trustees", "city board", "council of the city",
    # Additional governing body forms used across states
    "town board",               # NY townships (e.g. "Town Board of Horseheads")
    "select board",             # New England towns (MA, VT, NH)
    "borough council",          # PA boroughs (e.g. "Borough Council of West Mifflin")
    "board of mayor",           # Manchester NH uses "Board of Mayor and Aldermen"
    "aldermanic",               # "Aldermanic Council", "Board of Mayor and Aldermen"
    "board of alderpersons",    # gender-neutral variant
    "board of selectmen",       # older New England form
]

# Platforms that support body validation
VALIDATABLE_PLATFORMS = {"legistar", "civicplus", "civicclerk", "boarddocs"}


# ============================================================================
# SCORING FUNCTIONS
# ============================================================================

def score_body_match(candidate: str, expected_body: str) -> int:
    """
    Score how well a candidate body name matches the expected body.

    Returns:
        -1  — rejected (clearly a non-governing body)
        0   — no match
        30  — any governing keyword in candidate
        50  — candidate and expected share a governing keyword
        70  — partial match (candidate contained in expected or vice versa)
        80  — strong match (expected is a substring of candidate)
        100 — exact match (case-insensitive)
    """
    c_lower = candidate.lower().strip()
    e_lower = expected_body.lower().strip()

    if any(kw in c_lower for kw in REJECT_KEYWORDS):
        return -1

    if c_lower == e_lower:
        return 100

    if e_lower in c_lower:
        return 80

    if c_lower in e_lower:
        return 70

    for kw in GOVERNING_KEYWORDS:
        if kw in c_lower and kw in e_lower:
            return 50

    for kw in GOVERNING_KEYWORDS:
        if kw in c_lower:
            return 30

    return 0


def best_body_match(candidates: list[str], expected_body: str) -> tuple[str | None, int]:
    """
    Pick the best-matching body name from a list of candidates.
    Returns (best_candidate, score). Returns (None, -1) if all are rejected.
    """
    scored = [(c, score_body_match(c, expected_body)) for c in candidates if c]
    valid = [(c, s) for c, s in scored if s >= 0]
    if not valid:
        return None, -1
    valid.sort(key=lambda x: x[1], reverse=True)
    return valid[0]


# ============================================================================
# PER-PLATFORM VALIDATORS
# ============================================================================

async def validate_legistar_body(
    slug: str, config: dict, expected_body: str, client: httpx.AsyncClient
) -> dict:
    """Query /bodies endpoint and score against expected_body."""
    legistar_slug = config.get("legistar_slug", "")
    if not legistar_slug:
        return {"status": "skip", "reason": "no legistar_slug"}

    try:
        resp = await client.get(
            f"https://webapi.legistar.com/v1/{legistar_slug}/bodies",
            timeout=15,
        )
        resp.raise_for_status()
        bodies = resp.json()
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    body_names = [b.get("BodyName", "") for b in bodies if b.get("BodyName")]
    best, score = best_body_match(body_names, expected_body)

    if score == 100:
        return {"status": "ok", "validated_body": best, "score": score, "candidates": body_names[:10]}
    elif score >= 50:
        if best and best.lower() != expected_body.lower():
            return {
                "status": "corrected",
                "validated_body": best,
                "score": score,
                "correction_note": f"Platform uses '{best}' (expected '{expected_body}')",
                "config_patch": {"manifest_expected_body": best},
                "candidates": body_names[:10],
            }
        return {"status": "ok", "validated_body": best, "score": score}
    elif best is None:
        return {
            "status": "unresolved",
            "reason": f"All bodies rejected as non-governing. Found: {body_names[:10]}",
            "candidates": body_names[:10],
        }
    else:
        result = {
            "status": "unresolved",
            "reason": f"Best match '{best}' (score={score}) too low for '{expected_body}'. All: {body_names[:10]}",
            "candidates": body_names[:10],
        }
        # Self-heal: if the best candidate is a governing body (score >= 30),
        # update the manifest so the next scan uses the correct expected_body.
        if score >= 30:
            result["config_patch"] = {"manifest_expected_body": best}
        return result


async def validate_civicplus_body(
    slug: str, config: dict, source_url: str, expected_body: str, client: httpx.AsyncClient
) -> dict:
    """Fetch AgendaCenter categories via AJAX aria-labels and score."""
    from urllib.parse import urlparse
    from meeting_pipeline.collectors.civicplus_scraper import discover_categories, _ensure_www

    domain = config.get("domain", "") or urlparse(source_url).netloc.replace("www.", "")
    if not domain:
        return {"status": "skip", "reason": "no domain"}

    try:
        categories = await discover_categories(client, domain)
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    if not categories:
        return {"status": "error", "reason": "no categories found"}

    www_domain = _ensure_www(domain)
    verified = []
    for cat in categories:
        try:
            resp = await client.post(
                f"https://{www_domain}/AgendaCenter/UpdateCategoryList",
                data={"year": str(datetime.now().year), "catID": str(cat["id"])},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=10,
            )
            if resp.status_code == 404:
                break
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            year_link = soup.find("a", attrs={"aria-label": True})
            if year_link:
                label = year_link.get("aria-label", "")
                actual_name = re.sub(r"\s*\d{4}\s*$", "", label).strip()
                verified.append({"id": cat["id"], "name": actual_name})
        except Exception:
            break

    search_cats = verified if verified else categories
    cat_names = [c["name"] for c in search_cats]
    current_cat_id = config.get("council_category_id") or config.get("category_id")

    current_cat_name = None
    if current_cat_id:
        for c in search_cats:
            if c["id"] == int(current_cat_id):
                current_cat_name = c["name"]
                break

    scored = [(c["id"], c["name"], score_body_match(c["name"], expected_body)) for c in search_cats]
    valid = [(cid, name, s) for cid, name, s in scored if s >= 0]
    if not valid:
        return {"status": "unresolved", "reason": f"All categories rejected. Found: {cat_names}", "candidates": cat_names}

    valid.sort(key=lambda x: x[2], reverse=True)
    best_id, best_name, best_score = valid[0]

    if current_cat_id and int(current_cat_id) == best_id:
        return {"status": "ok", "validated_body": best_name, "score": best_score, "candidates": cat_names}

    if best_score >= 50:
        patch = {}
        if not current_cat_id or int(current_cat_id) != best_id:
            patch["council_category_id"] = best_id
        return {
            "status": "corrected" if patch else "ok",
            "validated_body": best_name,
            "score": best_score,
            "correction_note": (
                f"Switching from cat {current_cat_id} ('{current_cat_name}') to cat {best_id} ('{best_name}')"
                if patch else None
            ),
            "config_patch": patch if patch else None,
            "candidates": cat_names,
        }

    result = {
        "status": "unresolved",
        "reason": f"Best match '{best_name}' (score={best_score}) too low for '{expected_body}'",
        "candidates": cat_names,
    }
    if best_score >= 30:
        result["config_patch"] = {"manifest_expected_body": best_name}
    return result


async def validate_civicclerk_body(
    slug: str, config: dict, source_url: str, expected_body: str, client: httpx.AsyncClient
) -> dict:
    """Fetch distinct event names from CivicClerk and score."""
    match = re.search(r"https://(\w+)\.(?:api\.|portal\.)?civicclerk\.com", source_url)
    tenant = match.group(1) if match else config.get("tenant", "")
    if not tenant:
        return {"status": "skip", "reason": "no tenant"}

    is_portal = "portal.civicclerk.com" in source_url

    try:
        if is_portal:
            # Portal uses eventName field; $select doesn't filter portal API cleanly
            resp = await client.get(
                f"https://{tenant}.api.civicclerk.com/v1/EventCategories",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            categories = data.get("value", data) if isinstance(data, dict) else data
            names_seen = set()
            for cat in categories:
                n = cat.get("categoryDesc") or cat.get("categoryName") or ""
                if n:
                    names_seen.add(n.strip())
        else:
            resp = await client.get(
                f"https://{tenant}.api.civicclerk.com/v1/Events/",
                params={"$select": "Name,EventName", "$top": 50},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            events = data.get("value", data) if isinstance(data, dict) else data
            names_seen = set()
            for ev in events:
                n = ev.get("Name") or ev.get("EventName") or ""
                if n:
                    names_seen.add(n.strip())
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    candidates = list(names_seen)
    best, score = best_body_match(candidates, expected_body)

    if score >= 50:
        if best and best.lower() != expected_body.lower():
            return {
                "status": "corrected",
                "validated_body": best,
                "score": score,
                "correction_note": f"Platform uses '{best}' (expected '{expected_body}')",
                "config_patch": {"manifest_expected_body": best},
                "candidates": candidates,
            }
        return {"status": "ok", "validated_body": best, "score": score, "candidates": candidates}

    result = {
        "status": "unresolved",
        "reason": f"No match for '{expected_body}'. Found: {candidates}",
        "candidates": candidates,
    }
    if best is not None and score >= 30:
        result["config_patch"] = {"manifest_expected_body": best}
    return result


async def validate_boarddocs_body(
    slug: str, config: dict, source_url: str, expected_body: str, client: httpx.AsyncClient
) -> dict:
    """Fetch committees from BoardDocs and score."""
    from meeting_pipeline.collectors.boarddocs import BoardDocsConfig, _fetch_committees

    match = re.search(r"(https://go\.boarddocs\.com/\w+/\w+/Board\.nsf)", source_url)
    base_url = match.group(1) if match else None
    if not base_url:
        return {"status": "skip", "reason": "no boarddocs base_url"}

    bd_config = BoardDocsConfig(
        base_url=base_url,
        city_name=slug,
        output_prefix="",
        storage=None,
        committee_id=config.get("committee_id", ""),
        expected_body=expected_body,
    )
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://go.boarddocs.com",
        "Referer": f"{base_url}/Public",
        "User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)",
    }

    try:
        committees = await _fetch_committees(client, bd_config, headers)
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    candidates = [c["name"] for c in committees if c.get("name")]
    best, score = best_body_match(candidates, expected_body)

    if score >= 50:
        best_committee = next((c for c in committees if c.get("name") == best), None)
        patch = {}
        current_committee_id = str(config.get("committee_id", ""))
        if best_committee and str(best_committee.get("id", "")) != current_committee_id:
            patch["committee_id"] = str(best_committee.get("id", ""))
        return {
            "status": "corrected" if patch else "ok",
            "validated_body": best,
            "score": score,
            "correction_note": (
                f"Switching committee_id to {patch.get('committee_id')} ('{best}')" if patch else None
            ),
            "config_patch": patch if patch else None,
            "candidates": candidates,
        }

    result = {
        "status": "unresolved",
        "reason": f"No committee matched '{expected_body}'. Found: {candidates}",
        "candidates": candidates,
    }
    if best is not None and score >= 30:
        result["config_patch"] = {"manifest_expected_body": best}
    return result


# ============================================================================
# APPLY CORRECTIONS
# ============================================================================

def apply_body_validation(
    source_key: str,
    source: dict,
    validation: dict,
    storage,
) -> dict:
    """
    Apply config_patch from a validation result to source.json and/or manifest.json.
    Returns the updated source dict.
    """
    patch = validation.get("config_patch") or {}
    if not patch:
        return source

    best_source = source.get("best_source") or {}
    config = best_source.get("config") or {}
    changed = False

    for k, v in patch.items():
        if k == "manifest_expected_body":
            slug = source_key.split("/")[-2]
            sources_prefix = "/".join(source_key.split("/")[:-2])
            manifest_key = f"{sources_prefix}/{slug}/manifest.json"
            try:
                manifest = storage.read_json(manifest_key) if storage.exists(manifest_key) else {}
                manifest["expected_body"] = v
                storage.write_json(manifest_key, manifest)
                print(f"      → Updated manifest.json expected_body: '{v}'")
            except Exception as e:
                print(f"      → WARNING: Could not update manifest.json: {e}")
        elif k in ("council_category_id", "category_id", "committee_id", "tenant"):
            config[k] = v
            changed = True

    if changed:
        best_source["config"] = config
        source["best_source"] = best_source
        try:
            storage.write_json(source_key, source)
            print(f"      → Updated source.json config: {patch}")
        except Exception as e:
            print(f"      → WARNING: Could not update source.json: {e}")

    return source


# ============================================================================
# ORCHESTRATOR
# ============================================================================

async def validate_body_for_city(
    slug: str,
    source: dict,
    source_key: str,
    client: httpx.AsyncClient,
    storage,
) -> dict:
    """
    Run body validation for a single city. Applies corrections to source.json/manifest.json.
    Returns the validation result dict.

    Reads expected_body from manifest.json. If none is set, returns status='skip'.
    """
    best = source.get("best_source") or {}
    platform = best.get("platform", "")
    config = best.get("config") or {}
    source_url = best.get("url", "")

    source_key_parts = source_key.split("/")
    sources_prefix = "/".join(source_key_parts[:-2])
    manifest_key = f"{sources_prefix}/{slug}/manifest.json"

    expected_body = ""
    if storage.exists(manifest_key):
        try:
            manifest = storage.read_json(manifest_key)
            expected_body = manifest.get("expected_body", "")
        except Exception:
            pass

    if not expected_body:
        return {"status": "skip", "reason": "no expected_body in manifest.json"}

    if platform == "legistar":
        validation = await validate_legistar_body(slug, config, expected_body, client)
    elif platform == "civicplus":
        validation = await validate_civicplus_body(slug, config, source_url, expected_body, client)
    elif platform == "civicclerk":
        validation = await validate_civicclerk_body(slug, config, source_url, expected_body, client)
    elif platform == "boarddocs":
        validation = await validate_boarddocs_body(slug, config, source_url, expected_body, client)
    else:
        return {"status": "skip", "reason": f"platform '{platform}' not validated"}

    validation["expected_body"] = expected_body

    if validation.get("status") == "corrected":
        apply_body_validation(source_key, source, validation, storage)
    elif validation.get("status") == "unresolved":
        manifest_body = (validation.get("config_patch") or {}).get("manifest_expected_body")
        if manifest_body:
            # Self-heal: the platform has a governing body that doesn't match the
            # manifest's expected_body. Update the manifest now so the next scan
            # uses the correct name and passes body validation.
            apply_body_validation(source_key, source, validation, storage)
            print(f"      → Manifest self-healed: '{expected_body}' → '{manifest_body}'")

    return validation
