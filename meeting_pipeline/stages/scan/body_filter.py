"""
body_filter.py — Filter meeting events to the expected governing body.

Drops events from planning commissions, school boards, library boards, etc.
Uses score_body_match against the expected_body from the city's manifest.
"""

from meeting_pipeline.body_validation import score_body_match
from meeting_pipeline.shared.constants import GENERIC_MEETING_TITLES


def filter_by_body(meetings: list[dict], body: str) -> list[dict]:
    """
    Filter meetings to only include the expected governing body.

    Args:
        meetings: list of meeting dicts with 'title' field
        body: expected body name from manifest (e.g. "City Council")

    Returns:
        Filtered list. If body is empty, returns all meetings unchanged.
    """
    if not body or not meetings:
        return meetings

    filtered = []
    dropped = []

    for m in meetings:
        title = m.get("title", "")
        if not title:
            filtered.append(m)
            continue

        sc = score_body_match(title, body)
        if sc < 0:
            # Hard reject — advisory board, planning commission, etc.
            dropped.append(title)
        elif sc > 0:
            filtered.append(m)
        else:
            # score == 0: no match. Keep if title is generic enough to
            # likely be the governing body (e.g. "Regular Meeting")
            title_lower = title.lower()
            is_generic = any(kw in title_lower for kw in GENERIC_MEETING_TITLES)
            if is_generic:
                filtered.append(m)
            else:
                dropped.append(title)

    if dropped:
        unique_dropped = sorted(set(dropped))
        suffix = "..." if len(unique_dropped) > 5 else ""
        print(f"    Body filter dropped {len(dropped)} events: {unique_dropped[:5]}{suffix}")

    return filtered
