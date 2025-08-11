import asyncio
from datetime import date

from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo
from shared.llm import LLMClient
from shared.logger import get_logger
from shared.tavily_client import SharedTavilyClient


class CampaignTimelineGenerator:
    """
    A class to generate the 'Campaign Timeline' section of a campaign plan.
    This section focuses on planning events, milestones, and key election dates only.
    It excludes voter contact tactics and includes ballot mail and return dates.
    """

    def __init__(self):
        self.logger = get_logger(__name__)
        self.llm_client = LLMClient()
        self.tavily_client = SharedTavilyClient()
        self.logger.info("CampaignTimelineGenerator initialized")

    async def _fetch_ballot_dates(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Fetch ballot mail and return dates for the campaign location.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            str: Ballot mail and return dates information
        """
        self.logger.info(f"Fetching ballot dates for {cleaned_campaign_info.city}, {cleaned_campaign_info.state_full}")

        location = f"{cleaned_campaign_info.city}, {cleaned_campaign_info.state_full}"
        election_year = cleaned_campaign_info.election_date.year

        try:
            ballot_context = await self.tavily_client.get_search_context(
                query=f"ballot mail return dates {location} {election_year} election absentee voting deadlines",
                max_results=8
            )

            self.logger.debug(f"Ballot context: {ballot_context}")
            return ballot_context

        except Exception as e:
            self.logger.error(f"Failed to fetch ballot dates: {e!s}")
            return f"Error fetching ballot dates: {e!s}"


    async def generate_section(self, cleaned_campaign_info: CleanedCampaignInfo,
                             community_section: str,
                             voter_contact_section: str) -> str:
        """
        Generate the complete 'Campaign Timeline' section.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            community_section: Generated community section content
            voter_contact_section: Generated voter contact section content
            
        Returns:
            str: Generated complete campaign timeline section
        """
        self.logger.info(f"Starting campaign timeline generation for candidate: {cleaned_campaign_info.candidate_name}")

        try:
            ballot_dates = await self._fetch_ballot_dates(cleaned_campaign_info)

            timeline_content = await self._generate_timeline_content(
                cleaned_campaign_info,
                community_section,
                voter_contact_section,
                ballot_dates
            )

            complete_section = f"""## 3. CAMPAIGN TIMELINE

{timeline_content}

*Note: Verify all dates for accuracy. Community event dates may change.*"""

            self.logger.info("Successfully generated campaign timeline section")
            return complete_section

        except Exception as e:
            self.logger.error(f"Failed to generate campaign timeline: {e!s}")
            return "## 3. CAMPAIGN TIMELINE\n\n[Error generating campaign timeline]"

    async def _generate_timeline_content(self, cleaned_campaign_info: CleanedCampaignInfo,
                                       community_events: str,
                                       voter_contact_section: str,
                                       ballot_dates: str) -> str:
        """
        Generate the main timeline content using AI.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            community_events: Extracted community events
            voter_contact_section: Generated voter contact section content
            ballot_dates: Ballot mail and return dates
            
        Returns:
            str: Generated timeline content
        """
        self.logger.info("Generating timeline content with AI")

        has_primary = cleaned_campaign_info.has_primary
        today = date.today()

        timeline_prompt = f"""
You are an expert campaign strategist. Generate a chronological campaign timeline focusing on planning events, milestones, and key election dates ONLY.

CAMPAIGN CONTEXT:
- Candidate: {cleaned_campaign_info.candidate_name}
- Office: {cleaned_campaign_info.office_and_jurisdiction}
- Today's Date: {today}
- Election Date: {cleaned_campaign_info.election_date}
- Primary Date: {cleaned_campaign_info.primary_date if has_primary else "No Primary"}
- Has Primary: {has_primary}

COMMUNITY EVENTS:
{community_events}

VOTER CONTACT SECTION:
{voter_contact_section}

BALLOT DATES INFORMATION:
{ballot_dates}

CRITICAL RULES:
- DO NOT include voter contact tactics (texts, robocalls, etc.)
- Include dates where 25%, 50% and 75% of the TXT campaign will be completed. to do this, count the number of lines that have the word "TEXT" in the VOTER CONTACT SECTION and find the 25%, 50% and 75% of the total number of lines. the date should correspond to the date of the text.
- Include dates where 25%, 50% and 75% of the ROBOCALL campaign will be completed. to do this, count the number of lines that have the word "ROBOCALL" in the VOTER CONTACT SECTIONand find the 25%, 50% and 75% of the total number of lines. the date should correspond to the date of the robocall.
- ONLY include planning events, milestones, and key election dates
- Include ballot mail and return dates
- Include community events where candidate can appear
- Include campaign milestones (launch, fundraising deadlines, etc.)
- Include filing deadlines and other administrative dates
- Sort everything chronologically from today through election day

REQUIRED FORMAT:
md bullet points with the following format:
 - Full-Month DD | Event | Purpose
 - Full-Month DD | Event | Purpose
 - Full-Month DD | Event | Purpose

EXAMPLES:
 - July 15 | Campaign Launch Event | Official campaign announcement
 - August 1 | Ballot Request Deadline | Last day to request absentee ballot
 - August 10 | Town Hall Meeting | Community engagement and visibility
 - September 15 | Ballot Return Deadline | Last day to return mail-in ballots
 - October 1 | Candidate Forum | Voter education and comparison
 - November 5 | Election Day | Final day of voting

Generate the timeline in the exact format shown above. Start each line with the date, then |, then event, then |, then purpose.
"""

        try:
            response = self.llm_client.create_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert campaign strategist. Generate a chronological campaign timeline focusing ONLY on planning events, milestones, and key election dates. Do NOT include voter contact tactics."
                    },
                    {
                        "role": "user",
                        "content": timeline_prompt
                    }
                ],
                max_tokens=20000,
                temperature=0.1
            )

            timeline_content = response.choices[0].message.content
            self.logger.info("Successfully generated timeline content")
            return timeline_content

        except Exception as e:
            self.logger.error(f"Failed to generate timeline content: {e!s}")
            return "Error generating timeline content"


if __name__ == "__main__":
    from ai_generated_campaign_plan.schema.models import (
        CampaignInfo,
        IncumbentStatus,
        RaceType,
    )
    from ai_generated_campaign_plan.utils.utils import CampaignUtils

    logger = get_logger(__name__)
    logger.info("Starting campaign timeline generator test")

    try:
        campaign_info = CampaignInfo(
            candidate_name="Sarah Johnson",
            primary_date=date(2025, 9, 15),
            election_date=date(2025, 11, 5),
            office_and_jurisdiction="School Board, At-Large, Chicopee, MA",
            incumbent_status=IncumbentStatus.NOT_APPLICABLE,
            race_type=RaceType.NONPARTISAN,
            seats_available=3,
            number_of_opponents=7,
            win_number=2500,
            total_likely_voters=8500,
            available_cell_phones=1200,
            available_landlines=300,
            additional_race_context="Focus on education funding and infrastructure improvements"
        )

        utils = CampaignUtils()
        cleaned_campaign_info = utils.clean_campaign_info(campaign_info)

        community_section = """
 - Ambulance Commission Meeting (July 8, 2025): visibility with local emergency services.  
 - Chicopee Clean Sweep (2025 date TBD): community engagement and volunteer opportunity.  
 - Chamber 101 & Coffee & Conversation at Haven Teen Center (2025 date TBD): networking with local leaders.  
 - Career Fair: Exclusive Tech Hiring Event (2025 date TBD): connect with parents and professionals.  
 - Project Management Techniques Training (2025 date TBD): engage with local professionals.  
 - LGBTQIA+ Community Event (2025 date TBD): outreach to diverse voter groups.  
 - Chicopee School Committee Budget Hearing (2025 date TBD): discuss fiscal priorities for schools.
 """
        voter_contact_section = """
[JULY 15] – P2P Text #1: Candidate intro and vote-by-mail awareness if applicable  
- [JULY 25] – Robocall #1: Ballot arrival and early voting prompt  
- [AUGUST 10] – P2P Text #2: Experience and contrast message  
- [AUGUST 25] – Robocall #2: Vote return and community message  
- [SEPTEMBER 5] – P2P Text #3: Persuasion and vote planning  
- [SEPTEMBER 10] – Robocall #3: Final GOTV push and polling info  
- [SEPTEMBER 12] – P2P Text #4: Final GOTV reminder  
- [SEPTEMBER 15] – Primary Election Day  
- [OCTOBER 1] – P2P Text #1: Reintroduction and contrast message  
- [OCTOBER 10] – Robocall #1: Early voting alert  
- [OCTOBER 20] – P2P Text #2: Key issues and voter education  
- [OCTOBER 25] – Robocall #2: Final persuasion  
- [NOVEMBER 1] – P2P Text #3: Vote-by-mail deadline and GOTV push  
- [NOVEMBER 3] – Robocall #3: Election Day GOTV  
- [NOVEMBER 4] – P2P Text #4: Final reminder and polling location link  
- [NOVEMBER 5] – General Election Day
"""

        timeline_generator = CampaignTimelineGenerator()
        timeline_section = asyncio.run(timeline_generator.generate_section(cleaned_campaign_info, community_section, voter_contact_section))

        print("Generated Campaign Timeline:")
        print(timeline_section)

    except Exception as e:
        logger.error(f"Test failed: {e!s}")
        raise
