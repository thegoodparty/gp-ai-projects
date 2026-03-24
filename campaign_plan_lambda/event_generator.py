"""
Generates community event tasks using Gemini Google Search grounding.

2 AI calls:
1. Google Search: find real community events in the candidate's area
2. Filter, rank, and structure events as tasks
"""

from datetime import date
from typing import List

from pydantic import BaseModel, Field

from shared.llm_gemini_3 import Gemini3Client
from shared.logger import get_logger

logger = get_logger(__name__)


class EventTask(BaseModel):
    title: str = Field(..., description="Event name")
    description: str = Field(..., description="Why this event matters for the campaign")
    date: str = Field(..., description="Event date in YYYY-MM-DD format")


class EventTaskList(BaseModel):
    events: List[EventTask]


async def generate_event_tasks(
    election_date: date,
    city: str,
    state: str,
    llm_client: Gemini3Client = None,
) -> List[dict]:
    llm_client = llm_client or Gemini3Client()

    logger.info(f"Generating events for {city}, {state} (election: {election_date})")

    raw_events = await _search_community_events(llm_client, city, state, election_date)
    event_tasks = await _filter_and_structure_events(llm_client, city, state, election_date, raw_events)

    logger.info(f"Generated {len(event_tasks)} event tasks")
    return event_tasks


async def _search_community_events(llm_client: Gemini3Client, city: str, state: str, election_date: date) -> str:
    logger.info(f"Searching for community events in {city}, {state}")

    prompt = f"""Find community events where a political candidate can connect with voters.

Location: {city}, {state}
Date range: {date.today()} to {election_date}"""

    response = llm_client.generate_with_search(prompt)
    return response.text


async def _filter_and_structure_events(
    llm_client: Gemini3Client,
    city: str,
    state: str,
    election_date: date,
    raw_events: str,
) -> List[dict]:
    logger.info("Filtering and structuring events as tasks")

    today = date.today()

    prompt = f"""Select the best 5-8 community events from the data below for a candidate in {city}, {state}.

RULES:
- Only events between {today} and {election_date}
- Prioritize events where the candidate can speak to or meet voters
- Include a mix of formal meetings and community events
- Dates must be in YYYY-MM-DD format
- Title should be the event name
- Description should explain why this event helps the campaign (one sentence)

COMMUNITY EVENTS DATA:
{raw_events}"""

    response = llm_client.generate_structured_content(
        prompt=prompt,
        response_schema=EventTaskList,
        system_instruction="Select community events and return them as structured data. Dates must be YYYY-MM-DD format.",
        temperature=0.0,
    )

    tasks = []
    for event in response.events:
        try:
            event_date = date.fromisoformat(event.date)
        except ValueError:
            logger.warning(f"Skipping event with invalid date: {event.date}")
            continue

        if event_date < today or event_date > election_date:
            logger.warning(f"Skipping out-of-range event: {event.title} ({event.date})")
            continue

        week = max(1, ((election_date - event_date).days // 7) + 1)

        tasks.append({
            "title": event.title,
            "description": event.description,
            "cta": "Attend event",
            "flowType": "events",
            "week": week,
            "date": event.date,
        })

    tasks.sort(key=lambda t: t["date"])
    return tasks
