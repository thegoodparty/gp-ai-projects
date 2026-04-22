"""
Generates community event tasks using Gemini Google Search grounding.

2 AI calls:
1. Google Search: find real community events in the candidate's area
2. Filter, rank, and structure events as tasks

Prompts are loaded from Braintrust (with hardcoded fallbacks if unavailable).
"""

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from shared.llm_gemini_3 import Gemini3Client
from shared.braintrust import load_prompt_from_braintrust
from shared.logger import get_logger

logger = get_logger(__name__)

SEARCH_PROMPT_FALLBACK = """Find community events where a political candidate can connect with voters.

Location: {city}, {state}
Date range: {today} to {election_date}

For each event, include the direct URL to the event page if available."""

FILTER_PROMPT_FALLBACK = """Select the best 5-8 community events from the data below for a candidate in {city}, {state}.

RULES:
- Only events between {today} and {election_date}
- Prioritize events where the candidate can speak to or meet voters
- Include a mix of formal meetings and community events
- Dates must be in YYYY-MM-DD format
- Title should be the event name in sentence case: capitalize only the first word and proper nouns (city names, organization names, named events/festivals). Examples: "Boston Pride Festival" (entire title is a named event, all words capitalized); "Community town hall in Cambridge" (generic event — only the first word and the proper noun "Cambridge" are capitalized).
- Description should explain why this event helps the campaign (one sentence)
- Include the direct URL to the event page if one is present in the data

COMMUNITY EVENTS DATA:
{raw_events}"""


class LlmEventResult(BaseModel):
    """Schema for Gemini structured output — what the LLM returns."""
    title: str = Field(..., description="Event name")
    description: str = Field(..., description="Why this event matters for the campaign")
    date: str = Field(..., description="Event date in YYYY-MM-DD format")
    url: Optional[str] = Field(None, description="Direct URL to the event page")

    @field_validator("url", mode="before")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        if not isinstance(v, str):
            logger.warning(f"Dropping invalid URL (not a string): {repr(v)}")
            return None
        v = v.strip()
        if not v:
            return None
        if not v.startswith(("https://", "http://")):
            logger.warning(f"Dropping invalid URL (not http/https): {repr(v)}")
            return None
        return v


class LlmEventResultList(BaseModel):
    events: List[LlmEventResult]


class CampaignEventTask(BaseModel):
    """Final task shape matching gp-api's CampaignTask schema."""
    title: str
    description: str
    cta: str
    flowType: str
    week: int
    date: str
    url: Optional[str] = None


async def generate_event_tasks(
    election_date: date,
    city: str,
    state: str,
    llm_client: Optional[Gemini3Client] = None,
) -> List[CampaignEventTask]:
    llm_client = llm_client or Gemini3Client()

    today = date.today()
    logger.info(f"Generating events for {city}, {state} (election: {election_date})")

    raw_events = await _search_community_events(llm_client, city, state, election_date, today)
    event_tasks = await _filter_and_structure_events(llm_client, city, state, election_date, today, raw_events)

    stats = llm_client.get_usage_stats()
    logger.info(f"Generated {len(event_tasks)} event tasks | Gemini: {stats['api_calls']} calls, ${stats['total_cost']:.4f}")
    return event_tasks


async def _search_community_events(llm_client: Gemini3Client, city: str, state: str, election_date: date, today: date) -> str:
    logger.info(f"Searching for community events in {city}, {state}")

    prompt = load_prompt_from_braintrust(
        prompt_name="search-community-events",
        fallback_prompt=SEARCH_PROMPT_FALLBACK,
        variables={
            "city": city,
            "state": state,
            "today": str(today),
            "election_date": str(election_date),
        },
    )

    response = llm_client.generate_with_search(prompt)
    return response.text


async def _filter_and_structure_events(
    llm_client: Gemini3Client,
    city: str,
    state: str,
    election_date: date,
    today: date,
    raw_events: str,
) -> List[CampaignEventTask]:
    logger.info("Filtering and structuring events as tasks")

    prompt = load_prompt_from_braintrust(
        prompt_name="filter-and-structure-events",
        fallback_prompt=FILTER_PROMPT_FALLBACK,
        variables={
            "city": city,
            "state": state,
            "today": str(today),
            "election_date": str(election_date),
            "raw_events": raw_events,
        },
    )

    raw_response = llm_client.generate_structured_content(
        prompt=prompt,
        response_schema=LlmEventResultList,
        system_instruction="Select community events and return them as structured data. Dates must be YYYY-MM-DD format.",
        temperature=0.0,
    )
    if not isinstance(raw_response, LlmEventResultList):
        raise TypeError(
            f"Expected LlmEventResultList, got {type(raw_response).__name__}"
        )

    tasks: List[CampaignEventTask] = []
    for event in raw_response.events:
        try:
            event_date = date.fromisoformat(event.date)
        except ValueError:
            logger.warning(f"Skipping event with invalid date: {event.date}")
            continue

        if event_date < today or event_date > election_date:
            logger.warning(f"Skipping out-of-range event: {event.title} ({event.date})")
            continue

        # Countdown convention: week 1 = election week, higher = further out
        week = max(1, ((election_date - event_date).days // 7) + 1)

        tasks.append(CampaignEventTask(
            title=event.title,
            description=event.description,
            cta="Attend event",
            flowType="events",
            week=week,
            date=event.date,
            url=event.url,
        ))

    # If the LLM returned events but none survived validation, that's a quality
    # issue worth retrying. If the LLM returned nothing, the area genuinely has
    # no events — return empty as success.
    if raw_response.events and not tasks:
        raise RuntimeError(
            f"LLM returned {len(raw_response.events)} events but none had valid dates"
        )

    tasks.sort(key=lambda t: t.date)
    return tasks
