"""
verify_agenda_urls.py — Verify that agenda URLs actually serve downloadable agenda PDFs.

For each city with posted agendas, downloads the most recent agenda URL and checks:
  1. Is it reachable? (HTTP 200)
  2. Is it a PDF? (content-type or magic bytes)
  3. Is it substantial? (> 5KB)
  4. Can we extract text? (PyMuPDF)
  5. Does the text look like a meeting agenda? (keyword check)

Usage:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/tools/verify_agenda_urls.py
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/tools/verify_agenda_urls.py --city beverly-MA
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/tools/verify_agenda_urls.py --tier 2  # only unproven cities
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

import httpx

from meeting_pipeline.shared.config import AgentConfig, get_storage


AGENDA_KEYWORDS = [
    "agenda", "meeting", "council", "motion", "approve", "resolution",
    "ordinance", "public hearing", "consent", "roll call", "adjournment",
    "minutes", "item", "action", "discussion",
]


def check_pdf_content(content: bytes) -> dict:
    """Extract text from PDF bytes and check if it looks like an agenda."""
    try:
        import fitz
        doc = fitz.open(stream=content, filetype="pdf")
        pages = min(len(doc), 5)
        text = "\n".join(doc[i].get_text() for i in range(pages))
        word_count = len(text.split())
        text_lower = text.lower()
        keyword_hits = sum(1 for kw in AGENDA_KEYWORDS if kw in text_lower)
        return {
            "pages": len(doc),
            "words": word_count,
            "keyword_hits": keyword_hits,
            "is_agenda": keyword_hits >= 3 and word_count >= 50,
            "sample": text[:200].replace("\n", " "),
        }
    except Exception as e:
        return {"pages": 0, "words": 0, "keyword_hits": 0, "is_agenda": False, "error": str(e)}


async def verify_one(slug: str, url: str, client: httpx.AsyncClient) -> dict:
    """Verify one agenda URL."""
    result = {"slug": slug, "url": url[:80]}

    try:
        resp = await client.get(url, timeout=20)
        result["status_code"] = resp.status_code

        if resp.status_code != 200:
            result["verdict"] = f"HTTP {resp.status_code}"
            return result

        content_type = resp.headers.get("content-type", "")
        is_pdf = "pdf" in content_type or resp.content[:5] == b"%PDF-"
        result["content_type"] = content_type[:50]
        result["size_kb"] = len(resp.content) // 1024

        if not is_pdf:
            result["verdict"] = f"NOT PDF ({content_type[:30]})"
            return result

        if len(resp.content) < 5000:
            result["verdict"] = f"TOO SMALL ({len(resp.content)}B)"
            return result

        pdf_check = check_pdf_content(resp.content)
        result.update(pdf_check)

        if pdf_check["is_agenda"]:
            result["verdict"] = "PASS"
        elif pdf_check["words"] > 50:
            result["verdict"] = f"PDF OK but low agenda signals ({pdf_check['keyword_hits']} keywords)"
        else:
            result["verdict"] = f"PDF but minimal text ({pdf_check['words']} words)"

    except Exception as e:
        result["verdict"] = f"ERROR: {type(e).__name__}: {str(e)[:50]}"

    return result


async def main():
    parser = argparse.ArgumentParser(description="Verify agenda URLs serve real PDFs")
    parser.add_argument("--city", action="append", help="Specific city slug(s)")
    parser.add_argument("--tier", type=int, choices=[1, 2, 3], help="Only verify this tier")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    # Collect briefed slugs for tier classification
    briefed_slugs = set()
    for key in storage.list_keys(f"{cfg.output_prefix}/briefings"):
        if key.endswith("_briefing.json"):
            fn = key.split("/")[-1]
            slug = fn.replace("_briefing.json", "").rsplit("_", 1)[0]
            briefed_slugs.add(slug)

    # Find cities with posted agenda URLs
    to_verify = []
    for key in storage.list_keys(cfg.sources_prefix):
        if not key.endswith("/upcoming_meetings.json"):
            continue
        slug = key.split("/")[-2]

        if args.city and slug not in args.city:
            continue

        um = storage.read_json(key)
        meetings = um.get("upcoming", [])
        posted = [m for m in meetings if m.get("agenda_posted") and isinstance(m.get("agenda_url"), str) and m["agenda_url"].startswith("http")]

        if not posted:
            continue

        has_briefing = slug in briefed_slugs
        has_meetings_only = len(meetings) > 0 and not posted

        if args.tier == 1 and not has_briefing:
            continue
        if args.tier == 2 and has_briefing:
            continue
        if args.tier == 3 and (has_briefing or posted):
            continue

        # Pick most recent posted meeting
        most_recent = sorted(posted, key=lambda m: m.get("date", ""), reverse=True)[0]
        to_verify.append({
            "slug": slug,
            "url": most_recent["agenda_url"],
            "date": most_recent.get("date", ""),
            "platform": um.get("platform", "?"),
            "tier": 1 if has_briefing else 2,
        })

    if not to_verify:
        print("No cities to verify")
        return

    print(f"Verifying {len(to_verify)} cities...\n")

    results = []
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    ) as client:
        for i, item in enumerate(sorted(to_verify, key=lambda x: x["slug"])):
            result = await verify_one(item["slug"], item["url"], client)
            result["date"] = item["date"]
            result["platform"] = item["platform"]
            result["tier"] = item["tier"]
            results.append(result)

            verdict = result.get("verdict", "?")
            size = result.get("size_kb", 0)
            words = result.get("words", 0)
            marker = "PASS" if verdict == "PASS" else "FAIL"
            print(f"  [{i+1}/{len(to_verify)}] {item['slug']:<35} {item['platform']:<12} {marker:<5} {verdict}")

    # Summary
    passed = [r for r in results if r.get("verdict") == "PASS"]
    pdf_ok = [r for r in results if "PDF OK" in r.get("verdict", "")]
    failed = [r for r in results if r not in passed and r not in pdf_ok]

    print(f"\n{'='*60}")
    print(f"VERIFICATION SUMMARY")
    print(f"{'='*60}")
    print(f"  Verified:     {len(results)}")
    print(f"  PASS (agenda): {len(passed)}")
    print(f"  PDF OK:        {len(pdf_ok)}")
    print(f"  FAILED:        {len(failed)}")

    if failed:
        print(f"\n  FAILURES:")
        for r in failed:
            print(f"    {r['slug']:<35} {r.get('verdict', '?')}")


if __name__ == "__main__":
    asyncio.run(main())
