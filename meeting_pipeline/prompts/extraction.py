"""
extraction.py — Prompt builder for agenda extraction.

Used by scripts/extract_and_normalize.py to extract structured agenda items
from a city council meeting packet PDF via Gemini structured output.

To edit the extraction behavior: change the instructions in build_extraction_prompt().
The Pydantic schema (MeetingExtraction, AgendaItem) is defined in extract_and_normalize.py.
"""


def build_extraction_prompt(
    text: str,
    city: str,
    state: str,
    date: str,
    large_agenda: bool = False,
) -> str:
    """
    Build the Gemini extraction prompt for a city council meeting packet.

    Args:
        text: Extracted PDF text (will be truncated to 50,000 chars)
        city: City name, e.g. "Johnstown"
        state: State abbreviation, e.g. "OH"
        date: Meeting date in YYYY-MM-DD format
        large_agenda: If True, use shorter 1-sentence descriptions to avoid
                      hitting Gemini output token limits on 25+ item agendas.
    """
    if large_agenda:
        description_instruction = (
            "For ANY item that has a staff report, memo, or background section in the packet, "
            "write a 1-sentence description (max 25 words) summarizing what is being decided "
            "and any key dollar amounts."
        )
    else:
        description_instruction = (
            "For ANY item that has a staff report, memo, or background section in the packet, "
            "write a 1-3 sentence description summarizing what is being decided, why, and any "
            "key details (contract terms, project scope, location, etc.)"
        )

    return f"""You are extracting structured data from a city council meeting agenda packet for {city}, {state} on {date}.

This document may contain both the agenda overview AND full staff reports for each item.

Extract ALL agenda items including procedural ones (call to order, roll call, etc).
For each item:
- Use the item number exactly as shown
- Write a clear, concise title (not the full ordinance text if it's very long)
- Classify section: consent, action, public_hearing, discussion, procedural, or other
- {description_instruction}
- Extract ALL dollar amounts mentioned verbatim (e.g. "$1,234,567")
- Note staff recommendation if stated (approve, deny, table, receive and file, etc.)
- Mark is_public_hearing=true if it's a public hearing
- For procedural items (call to order, roll call, adjournment, approval of minutes, pledge of allegiance, invocation) that have no staff report or background section, omit the description field entirely — do not fabricate content for items with nothing substantive to describe

GROUNDING RULE: Distinguish between what the agenda item itself states versus what appears only in supporting documents (staff reports, resolutions, attachments). If a specific name, dollar amount, or detail appears only in a supporting document and not in the agenda item heading, prefix it in the description with "Per staff report:" or "Per resolution text:". If a detail is an inference from context rather than an explicit statement, prefix it with "Inferred:". Never present inferred details as stated facts.

The goal is for someone reading this JSON to understand what is actually being discussed at the meeting without having to open the PDF.

AGENDA PACKET TEXT:
{text[:100000]}
"""
