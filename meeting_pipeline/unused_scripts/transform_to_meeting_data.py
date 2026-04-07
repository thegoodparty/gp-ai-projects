"""
transform_to_meeting_data.py — Transform collector output to standardized MeetingData JSON.

Reads raw collector output (Legistar events + event items, etc.) and transforms
it into the standard MeetingData schema defined in rapid-pilot-plan.md.

Each source type has its own transform function. All produce the same output.

Usage:
    uv run python meeting_pipeline/scripts/transform_to_meeting_data.py --source-dir meeting_pipeline/sources/chapel-hill-NC/data/legistar --source-type legistar --city "Chapel Hill" --state NC
    uv run python meeting_pipeline/scripts/transform_to_meeting_data.py --batch-legistar
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ============================================================================
# TOPIC CLASSIFICATION — keyword dictionary, no LLM needed
# ============================================================================

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "zoning": [
        "rezone", "rezoning", "zoning", "variance", "conditional use",
        "land use", "subdivision", "plat", "annexation", "special use permit",
    ],
    "budget": [
        "budget", "appropriation", "fiscal", "tax rate", "fee schedule",
        "revenue", "grant", "bond", "capital improvement",
    ],
    "infrastructure": [
        "water", "sewer", "street", "road", "sidewalk", "bridge",
        "stormwater", "utility", "paving", "traffic signal",
    ],
    "housing": [
        "affordable housing", "housing trust", "cdbg", "home funds",
        "workforce housing", "low-income housing", "section 8",
    ],
    "public_safety": [
        "police", "fire", "ems", "code enforcement", "public safety",
        "crime", "emergency", "sheriff",
    ],
    "economic_dev": [
        "incentive", "tif", "enterprise zone", "tax abatement",
        "economic development", "business district",
    ],
    "governance": [
        "appointment", "board member", "committee assignment", "charter",
        "proclamation", "resolution of recognition",
    ],
    "environment": [
        "park", "greenway", "tree", "sustainability", "conservation",
        "recycling", "solar", "environmental",
    ],
    "procedural": [
        "call to order", "roll call", "minutes", "adjournment",
        "consent agenda", "invocation", "pledge", "welcome",
        "petitions by the public", "public comment", "presentations",
    ],
}


def classify_topic(title: str) -> str:
    """Classify an agenda item title into a topic using keyword matching."""
    lower = title.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return topic
    return "other"


# ============================================================================
# SECTION INFERENCE — map Legistar matter types + consent flags to sections
# ============================================================================

SECTION_MAP: dict[str, str] = {
    "ordinance": "action",
    "resolution": "action",
    "motion": "action",
    "public hearing": "public_hearing",
    "hearing": "public_hearing",
    "discussion item": "discussion",
    "discussion": "discussion",
    "presentation": "presentation",
    "proclamation": "procedural",
    "minutes": "procedural",
    "report": "discussion",
    "appointment": "action",
    "contract": "action",
    "agreement": "action",
}


def infer_section(matter_type: str | None, title: str, is_consent: bool) -> str:
    """Infer the section from Legistar matter type, title, and consent flag."""
    if is_consent:
        return "consent"

    if matter_type:
        lower_type = matter_type.lower()
        for key, section in SECTION_MAP.items():
            if key in lower_type:
                return section

    # Fallback: check title for clues
    lower_title = title.lower()
    if any(kw in lower_title for kw in ["public hearing", "hearing on"]):
        return "public_hearing"
    if any(kw in lower_title for kw in ["call to order", "roll call", "adjournment", "welcome", "pledge", "invocation"]):
        return "procedural"
    if any(kw in lower_title for kw in ["presentation", "update", "report"]):
        return "presentation"

    return "other"


# ============================================================================
# FISCAL AMOUNT EXTRACTION
# ============================================================================

DOLLAR_PATTERN = re.compile(r'\$[\d,]+\.?\d*')


def extract_fiscal_amounts(title: str, description: str | None = None) -> list[dict] | None:
    """Extract dollar amounts from title and description."""
    text = title
    if description:
        text += " " + description

    amounts = []
    for match in DOLLAR_PATTERN.findall(text):
        try:
            amount = float(match.replace("$", "").replace(",", ""))
            amounts.append({"amount": amount, "context": title[:100]})
        except ValueError:
            continue

    return amounts if amounts else None


# ============================================================================
# LEGISTAR TRANSFORM
# ============================================================================

def transform_legistar(source_dir: Path, city_name: str, state: str, city_slug: str) -> list[dict]:
    """
    Transform Legistar collector output into MeetingData records.

    Returns a list of meeting records, each with the full MeetingData shape.
    """
    events_file = source_dir / "events.json"
    if not events_file.exists():
        print(f"  No events.json found in {source_dir}")
        return []

    with open(events_file) as f:
        events = json.load(f)

    # Filter to council-only bodies using bodies.json
    # BodyTypeId 42 = primary legislative body (most cities)
    # Also match by name for cities that use different type IDs (e.g. Cleveland = 56)
    _COUNCIL_PREFIXES = ("city council", "town council")
    _COUNCIL_EXCLUDES = ("advisory", "youth", "americorps", "committee")

    bodies_file = source_dir / "bodies.json"
    council_body_ids: set[int] = set()
    if bodies_file.exists():
        with open(bodies_file) as f:
            bodies = json.load(f)
        for body in bodies:
            name = (body.get("BodyName") or "").lower()
            is_primary = body.get("BodyTypeId") == 42
            is_council_named = (
                any(name.startswith(p) for p in _COUNCIL_PREFIXES)
                and not any(ex in name for ex in _COUNCIL_EXCLUDES)
            )
            if is_primary or is_council_named:
                council_body_ids.add(body["BodyId"])
                print(f"  Council body: {body.get('BodyName')} (BodyId={body['BodyId']})")
    if council_body_ids:
        before = len(events)
        events = [e for e in events if e.get("EventBodyId") in council_body_ids]
        print(f"  Filtered events: {before} → {len(events)} (council-only)")

    meetings = []

    for event in events:
        event_id = event["EventId"]
        items_file = source_dir / f"event_items/{event_id}.json"

        if not items_file.exists():
            continue

        with open(items_file) as f:
            raw_items = json.load(f)

        # Transform each event item to AgendaItem
        agenda_items = []
        for item in raw_items:
            title = (item.get("EventItemTitle") or "").strip()
            if not title:
                continue

            matter_type = item.get("EventItemMatterType")
            is_consent = bool(item.get("EventItemConsent"))
            section = infer_section(matter_type, title, is_consent)
            topic = classify_topic(title)
            is_public_hearing = section == "public_hearing"

            agenda_item = {
                "number": item.get("EventItemAgendaNumber"),
                "title": title,
                "section": section,
                "topic": topic,
                "isPublicHearing": is_public_hearing,
            }

            # Optional fields — only include if present
            fiscal = extract_fiscal_amounts(title)
            if fiscal:
                agenda_item["fiscalAmounts"] = fiscal

            if item.get("EventItemActionText"):
                agenda_item["staffRecommendation"] = item["EventItemActionText"]

            if matter_type:
                agenda_item["matterType"] = matter_type

            # Per-item attachment URLs from matter_attachments/{matterId}.json
            matter_id = item.get("EventItemMatterId")
            if matter_id:
                att_file = source_dir / f"matter_attachments/{matter_id}.json"
                if att_file.exists():
                    try:
                        with open(att_file) as f:
                            attachments = json.load(f)
                        docs = [
                            {"name": a["MatterAttachmentName"], "url": a["MatterAttachmentHyperlink"]}
                            for a in attachments
                            if a.get("MatterAttachmentHyperlink")
                            and a.get("MatterAttachmentShowOnInternetPage", True)
                        ]
                        if docs:
                            agenda_item["attachments"] = docs
                    except (json.JSONDecodeError, KeyError):
                        pass

            # Vote data (from EventItemPassedFlag / EventItemTally)
            if item.get("EventItemPassedFlagName"):
                agenda_item["voteResult"] = {
                    "outcome": item["EventItemPassedFlagName"],
                    "yeas": 0,
                    "nays": 0,
                }

            agenda_items.append(agenda_item)

        # Build summary
        public_hearings = sum(1 for i in agenda_items if i.get("isPublicHearing"))
        consent_items = sum(1 for i in agenda_items if i.get("section") == "consent")
        action_items = sum(1 for i in agenda_items if i.get("section") == "action")
        total_fiscal = sum(
            fa["amount"]
            for i in agenda_items
            if i.get("fiscalAmounts")
            for fa in i["fiscalAmounts"]
        ) or None

        # Top topics
        topic_counts: dict[str, int] = {}
        for i in agenda_items:
            t = i.get("topic", "other")
            if t != "procedural":
                topic_counts[t] = topic_counts.get(t, 0) + 1
        top_topics = sorted(topic_counts, key=topic_counts.get, reverse=True)[:5]

        # Parse event date
        event_date_str = event.get("EventDate", "")
        try:
            event_date = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
            date_iso = event_date.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            date_iso = event_date_str[:10] if len(event_date_str) >= 10 else event_date_str

        # Determine meeting status
        try:
            event_date_obj = datetime.strptime(date_iso, "%Y-%m-%d")
            if event_date_obj.date() > datetime.now().date():
                status = "UPCOMING"
            else:
                status = "COMPLETED"
        except ValueError:
            status = "UPCOMING"

        meeting_data = {
            "version": "1.0",
            "agendaItems": agenda_items,
            "summary": {
                "totalItems": len(agenda_items),
                "publicHearings": public_hearings,
                "consentItems": consent_items,
                "actionItems": action_items,
                "totalFiscalImpact": total_fiscal,
                "topTopics": top_topics,
            },
            "source": {
                "type": "legistar",
                "collectedAt": datetime.now().isoformat(),
                "legistarEventId": event_id,
            },
        }

        meeting_record = {
            "citySlug": city_slug,
            "cityName": city_name,
            "state": state,
            "date": date_iso,
            "time": event.get("EventTime"),
            "body": event.get("EventBodyName", "City Council"),
            "title": event.get("EventComment") or f"{event.get('EventBodyName', 'City Council')} Meeting",
            "status": status,
            "sourceType": "legistar",
            "sourceUrl": event.get("EventInSiteURL"),
            "data": meeting_data,
        }

        meetings.append(meeting_record)

    return meetings


# ============================================================================
# BATCH TRANSFORM FOR ALL LEGISTAR CITIES
# ============================================================================

SOURCES_DIR = Path(__file__).resolve().parent.parent / "sources"


def batch_transform_legistar() -> dict:
    """Transform all collected Legistar data from sources/ directory."""
    summary_file = SOURCES_DIR / "discovery-summary.json"
    if not summary_file.exists():
        print("No discovery-summary.json found")
        return {}

    with open(summary_file) as f:
        summary = json.load(f)

    all_meetings = {}
    total = 0

    for city_entry in summary["cities"]:
        if city_entry["platform"] != "legistar":
            continue

        city = city_entry["city"]
        state = city_entry["state"]
        city_slug = f"{city.lower().replace(' ', '-')}-{state}"
        source_dir = SOURCES_DIR / city_slug / "data" / "legistar"

        if not source_dir.exists():
            print(f"  {city_slug}: no collected data yet, skipping")
            continue

        meetings = transform_legistar(source_dir, city, state, city_slug)
        if meetings:
            # Save per-city
            output_file = SOURCES_DIR / city_slug / "meetings.json"
            with open(output_file, "w") as f:
                json.dump(meetings, f, indent=2)
            print(f"  {city_slug}: {len(meetings)} meetings -> {output_file.name}")
            all_meetings[city_slug] = meetings
            total += len(meetings)
        else:
            print(f"  {city_slug}: no meetings found")

    print(f"\nTotal: {total} meetings across {len(all_meetings)} cities")
    return all_meetings


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Transform collector output to MeetingData JSON")
    parser.add_argument("--source-dir", help="Path to collector output directory")
    parser.add_argument("--source-type", choices=["legistar"], default="legistar", help="Collector type")
    parser.add_argument("--city", help="City name (e.g. 'Chapel Hill')")
    parser.add_argument("--state", help="State code (e.g. 'NC')")
    parser.add_argument("--batch-legistar", action="store_true", help="Transform all collected Legistar data")
    parser.add_argument("--output", help="Output file path (default: meetings.json in source dir)")
    args = parser.parse_args()

    if args.batch_legistar:
        batch_transform_legistar()
        return

    if not args.source_dir:
        parser.error("--source-dir required (or use --batch-legistar)")

    source_dir = Path(args.source_dir)
    city = args.city or source_dir.parent.parent.name.split("-")[0].title()
    state = args.state or source_dir.parent.parent.name.split("-")[-1]
    city_slug = f"{city.lower().replace(' ', '-')}-{state}"

    if args.source_type == "legistar":
        meetings = transform_legistar(source_dir, city, state, city_slug)
    else:
        print(f"Unknown source type: {args.source_type}")
        sys.exit(1)

    if not meetings:
        print("No meetings found to transform")
        sys.exit(1)

    output_path = Path(args.output) if args.output else source_dir.parent / "meetings.json"
    with open(output_path, "w") as f:
        json.dump(meetings, f, indent=2)

    print(f"Transformed {len(meetings)} meetings -> {output_path}")


if __name__ == "__main__":
    main()
