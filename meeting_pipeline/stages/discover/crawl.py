"""
crawl.py — Domain validation and multi-hop page crawling for discovery.

Validates that a URL belongs to the right city/state, and crawls through
sub-pages to find the actual agenda listing page when the initial URL
is a landing page.
"""

import os
import re
from urllib.parse import urlparse

import httpx

from meeting_pipeline.shared.constants import (
    CITY_NAME_PREFIXES,
    CITY_NAME_SUFFIXES,
    FETCH_BLOCKLIST,
    STATE_NAMES,
)


def validate_domain_for_city(domain: str, city: str, state: str) -> tuple[bool, str]:
    """
    Fetch a domain's root page and check it mentions the city and state.
    Catches wrong-city confusions (e.g. Clear Lake CA vs IA, North Logan vs Logan).
    Returns (valid: bool, reason: str).
    """
    try:
        state_full = STATE_NAMES.get(state.upper(), state)

        if domain in FETCH_BLOCKLIST:
            return False, "blocklisted_domain→rejected"

        # Domain-level wrong-state check
        domain_lower = domain.lower()
        first_segment = domain_lower.split(".")[0]
        correct_state_full = state_full.lower()
        city_clean = city.lower().replace(" ", "")

        for abbrev in STATE_NAMES:
            if abbrev == state.upper():
                continue
            ab = abbrev.lower()
            # Pattern 1: state abbrev as suffix (camdennj → nj)
            if first_segment.endswith(ab) and len(first_segment) > len(ab) and not correct_state_full.endswith(ab) and not city_clean.endswith(ab):
                return False, f"domain_encodes_wrong_state:{abbrev}→rejected"
            # Pattern 2: delimited segment (clearlake-ca.municode → ca)
            if re.search(r'(?:^|[-.])' + ab + r'(?:[.-]|$)', domain_lower):
                return False, f"domain_encodes_wrong_state:{abbrev}→rejected"

        # Wrong-city check (North Logan ≠ Logan, Loganville ≠ Logan)
        domain_clean = domain_lower.replace("-", "").replace(".", "")
        first_seg_clean = first_segment.replace("-", "")

        for prefix in CITY_NAME_PREFIXES:
            if prefix + city_clean in domain_clean and prefix not in city_clean:
                return False, f"domain_is_different_city:{prefix}{city_clean}→rejected"
        for suffix in CITY_NAME_SUFFIXES:
            if city_clean + suffix in first_seg_clean and suffix not in city_clean:
                return False, f"domain_is_different_city:{city_clean}{suffix}→rejected"

        # Fetch and check page content
        for url in [f"https://www.{domain}", f"https://{domain}"]:
            try:
                with httpx.Client(follow_redirects=True, timeout=8.0) as hc:
                    r = hc.get(url)
                    if r.status_code == 200 and len(r.text) > 500:
                        text = r.text.lower()
                        city_words = city.lower().split()
                        city_found = all(w in text for w in city_words)
                        state_found = (
                            bool(re.search(r'\b' + re.escape(state.lower()) + r'\b', text))
                            or state_full.lower() in text
                        )

                        if city_found and state_found:
                            return True, "city+state_found→accepted"
                        if city_found and not state_found:
                            if city_clean in domain_clean:
                                return True, "city_in_domain→accepted"
                            return False, f"state_not_found({state}/{state_full})→rejected"
                        if not city_found:
                            missing = [w for w in city_words if w not in text]
                            return False, f"city_words_missing:{missing}→rejected"
            except Exception:
                continue

        return True, "fetch_failed→optimistic"
    except Exception as e:
        return True, f"exception→optimistic:{str(e)[:60]}"


def firecrawl_map_agenda(base_url: str) -> str | None:
    """
    Run Firecrawl map_url() on a domain to find the agenda sub-page.
    Returns the best agenda URL, or None.
    """
    fc_key = os.environ.get("FIRECRAWL_API_KEY")
    if not fc_key:
        return None
    try:
        from firecrawl import V1FirecrawlApp
        fc = V1FirecrawlApp(api_key=fc_key)
        result = fc.map_url(base_url, search="city council agenda")
        links = getattr(result, "links", None) or []

        # Filter out non-agenda URLs
        _non_agenda = {"facebook.com", "twitter.com", "youtube.com", "wikipedia.org"}
        links = [
            link for link in links
            if urlparse(link).netloc.lower().removeprefix("www.") not in _non_agenda
            and not link.split("?")[0].lower().endswith(".pdf")
        ]

        # Find agenda links
        agenda_links = [
            link for link in links
            if "agenda" in link.lower() and ("council" in link.lower() or "meeting" in link.lower())
        ]
        if not agenda_links:
            agenda_links = [link for link in links if "agenda" in link.lower()]
        if not agenda_links:
            return None

        # Score: prefer listing pages over specific meetings
        _date_re = re.compile(
            r'\d{4}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}'
            r'|\bjanuary\b|\bfebruary\b|\bmarch\b|\bapril\b|\bmay\b|\bjune\b'
            r'|\bjuly\b|\baugust\b|\bseptember\b|\boctober\b|\bnovember\b|\bdecember\b',
            re.I
        )

        def _score(url: str) -> tuple:
            path = url.split("?")[0].rstrip("/")
            last = path.split("/")[-1].lower()
            return (last.endswith(".pdf"), bool(_date_re.search(last)), len(path))

        agenda_links.sort(key=_score)
        best_url = agenda_links[0]

        # Validate the page looks like an agenda listing
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0) as hc:
                r = hc.get(best_url)
                if r.status_code == 200:
                    text = r.text.lower()
                    if text.count("agenda") >= 3 or text.count(".pdf") >= 2:
                        return best_url
                    date_count = len(re.findall(
                        r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+20\d{2}',
                        text, re.I
                    ))
                    if date_count >= 2:
                        return best_url
                    return None
        except Exception:
            return best_url

        return best_url
    except Exception:
        return None


def firecrawl_crawl_for_agenda(
    start_url: str, city: str, state: str,
    expected_body: str = "", max_hops: int = 4,
) -> str | None:
    """
    Follow links from start_url up to max_hops to find the actual agenda page.
    At each hop: scrape with JS rendering, check for meeting content.
    Stops as soon as content is found.

    Cost: ~5 Firecrawl credits per hop.
    """
    from datetime import date as _date
    fc_key = os.environ.get("FIRECRAWL_API_KEY")
    if not fc_key:
        return None
    try:
        from firecrawl import FirecrawlApp

        from meeting_pipeline.shared.body_validation import REJECT_KEYWORDS
        fc = FirecrawlApp(api_key=fc_key)

        body_words = [w.lower() for w in expected_body.split() if len(w) > 2] if expected_body else []
        domain = urlparse(start_url).netloc.lower()
        visited = {start_url.rstrip("/")}
        current_url = start_url

        for hop in range(max_hops):
            result = fc.scrape(
                current_url,
                formats=["markdown", "links"],
                actions=[{"type": "wait", "milliseconds": 3000}],
            )
            markdown = getattr(result, "markdown", "") or ""
            links = list(getattr(result, "links", None) or [])
            pdf_links = [link for link in links if ".pdf" in link.lower()]

            # Check: page has meeting content for the right body?
            has_pdfs = len(pdf_links) >= 1
            has_content = len(markdown) > 5000

            if has_pdfs or has_content:
                md_lower = markdown.lower()
                reject_score = sum(1 for kw in REJECT_KEYWORDS if kw in md_lower)
                wrong_body = False
                if expected_body and reject_score >= 2:
                    header_area = md_lower[:2000]
                    wrong_body = not any(w in header_area for w in body_words)

                if wrong_body:
                    print(f"  [crawl] Hop {hop+1}: Wrong body — continuing")
                elif has_pdfs:
                    # Page has actual PDF links — this is the real agenda page
                    print(f"  [crawl] Hop {hop+1}: Found agenda page ({len(pdf_links)} PDFs, {len(markdown)} chars): {current_url[:70]}")
                    return current_url
                else:
                    # Page has content but no PDFs — likely a landing page.
                    # Keep crawling to find the page with actual PDFs.
                    print(f"  [crawl] Hop {hop+1}: Landing page (0 PDFs, {len(markdown)} chars) — following links for PDFs: {current_url[:70]}")

            if not links:
                print(f"  [crawl] Hop {hop+1}: No links on {current_url[:60]}")
                return None

            # Parse link text and score
            link_texts = {}
            for m in re.finditer(r'\[([^\]]+)\]\(([^)]+)\)', markdown):
                link_texts[m.group(2)] = m.group(1).lower()

            scored = []
            for link in links:
                link_lower = link.lower()
                if domain not in urlparse(link).netloc.lower():
                    continue
                if link_lower.endswith(".pdf"):
                    continue
                if link.rstrip("/") in visited:
                    continue
                if "#" in link and link.split("#")[0].rstrip("/") in visited:
                    continue

                anchor = link_texts.get(link, "")
                combined = f"{link_lower} {anchor}"
                score = 0
                if body_words:
                    score += sum(5 for bw in body_words if bw in combined)
                if "agenda" in combined:
                    score += 3
                if "council" in combined or "commission" in combined:
                    score += 2
                if "meeting" in combined:
                    score += 1
                if str(_date.today().year) in combined:
                    score += 2
                if any(rk in combined for rk in REJECT_KEYWORDS):
                    score -= 10
                if score > 0:
                    scored.append((score, link))

            if not scored:
                print(f"  [crawl] Hop {hop+1}: No matching links on {current_url[:60]}")
                return None

            scored.sort(reverse=True)
            next_url = scored[0][1]
            visited.add(next_url.rstrip("/"))
            print(f"  [crawl] Hop {hop+1}: Following → {next_url[:80]}")
            current_url = next_url

        return None
    except Exception as e:
        print(f"  [crawl] Error: {str(e)[:80]}")
        return None
