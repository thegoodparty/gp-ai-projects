import asyncio
from datetime import date, timedelta
from typing import Dict, Any, List, Tuple
from pydantic import BaseModel, Field
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo
from shared.logger import get_logger
from shared.llm import LLMClient
from shared.llm_gemini import GeminiClient
from shared.tavily_client import SharedTavilyClient

# Structured task models for timeline generation
class TimelineTask(BaseModel):
    """Single timeline task with proper categorization"""
    date: str = Field(description="Date string (e.g., 'Aug 19, 2025')")
    title: str = Field(description="Task/event title")
    description: str = Field(description="Task description/purpose")
    cta: str = Field(description="Call to action: Schedule, develop strategy, Visit in person, Write post, Learn More, etc.")
    type: str = Field(description="Task type: outreach, externalLink, event, general, compliance")
    category: str = Field(description="Task category: text, robocall, doorKnocking, phoneBanking, socialMedia, link, general")
    deadline: str = Field(description="Last effective date for this task (e.g., 'Aug 25, 2025')")
    link: str = Field(default="", description="External link for events (real URLs only)")

class TimelineResponse(BaseModel):
    """Timeline generation response with structured tasks"""
    markdown_content: str = Field(description="Formatted markdown timeline content")
    tasks: List[TimelineTask] = Field(description="List of structured timeline tasks")

class CampaignTimelineGenerator:
    """
    A class to generate the 'Campaign Timeline' section of a campaign plan.
    This section focuses on planning events, milestones, and key election dates only.
    It excludes voter contact tactics and includes ballot mail and return dates.
    """
    
    def __init__(self):
        self.logger = get_logger(__name__)
        self.llm_client = LLMClient()
        self.gemini_client = GeminiClient()
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
            self.logger.error(f"Failed to fetch ballot dates: {str(e)}")
            return f"Error fetching ballot dates: {str(e)}"


    async def generate_section(self, cleaned_campaign_info: CleanedCampaignInfo, 
                             community_section: str, 
                             voter_contact_section: str) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Generate the complete 'Campaign Timeline' section.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            community_section: Generated community section content
            voter_contact_section: Generated voter contact section content
            
        Returns:
            Tuple[str, List[Dict[str, Any]]]: (markdown_section, structured_tasks)
        """
        self.logger.info(f"Starting campaign timeline generation for candidate: {cleaned_campaign_info.candidate_name}")
        
        try:            
            ballot_dates = await self._fetch_ballot_dates(cleaned_campaign_info)
            
            timeline_response = await self._generate_structured_timeline(
                cleaned_campaign_info, 
                community_section, 
                voter_contact_section,
                ballot_dates
            )
            
            complete_section = f"""## 3. CAMPAIGN TIMELINE

{timeline_response.markdown_content}

*Note: Verify all dates for accuracy. Community event dates may change.*"""
            
            # Convert tasks to dict format expected by json_extractor
            structured_tasks = []
            for task in timeline_response.tasks:
                task_dict = {
                    "date": task.date,
                    "title": task.title,
                    "description": task.description,
                    "cta": task.cta,
                    "type": task.type,
                    "category": task.category,
                    "deadline": task.deadline
                }
                if task.link:
                    task_dict["link"] = task.link
                structured_tasks.append(task_dict)
            
            self.logger.info("Successfully generated campaign timeline section with structured tasks")
            return complete_section, structured_tasks
            
        except Exception as e:
            self.logger.error(f"Failed to generate campaign timeline: {str(e)}")
            return "## 3. CAMPAIGN TIMELINE\n\n[Error generating campaign timeline]", []

    async def _generate_structured_timeline(self, cleaned_campaign_info: CleanedCampaignInfo,
                                          community_events: str, 
                                          voter_contact_section: str,
                                          ballot_dates: str) -> TimelineResponse:
        """
        Generate structured timeline content with proper categorization and links using AI.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            community_events: Extracted community events
            voter_contact_section: Generated voter contact section content
            ballot_dates: Ballot mail and return dates
            
        Returns:
            TimelineResponse: Structured timeline with markdown and tasks
        """
        self.logger.info("Generating structured timeline content with AI")
        
        has_primary = cleaned_campaign_info.has_primary
        today = date.today()
        location = f"{cleaned_campaign_info.city}, {cleaned_campaign_info.state_full}"
        
        timeline_prompt = f"""
Generate a chronological campaign timeline with both markdown content and structured task data.

CAMPAIGN CONTEXT:
- Candidate: {cleaned_campaign_info.candidate_name}
- Office: {cleaned_campaign_info.office_and_jurisdiction}
- Location: {location}
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

TASK TYPES (use exact values):
- "outreach" - Direct voter contact milestones
- "externalLink" - External community events and activities
- "event" - Campaign events, meetings, forums
- "compliance" - Filing deadlines, ballot dates, administrative tasks
- "general" - General campaign activities, planning, fundraising

TASK CATEGORIES (use exact values):
- "text" - Text messaging campaign milestones
- "robocall" - Robocall campaign milestones
- "doorKnocking" - Canvassing milestones
- "phoneBanking" - Phone banking milestones
- "socialMedia" - Social media campaign milestones
- "link" - External links and community events
- "general" - General activities

CALL TO ACTION EXAMPLES:
- "Schedule" - Schedule the activity/event
- "develop strategy" - Plan and strategize
- "Visit in person" - Attend event or activity
- "Write post" - Create content or materials
- "Learn More" - Research or gather information
- "File paperwork" - Complete compliance tasks
- "Register" - Sign up or register for events

EXTERNAL LINK REQUIREMENTS (for externalLink category):
- Research and provide REAL, specific URLs when possible
- For state fairs: use official state fair websites
- For farmers markets: use local harvest or official market websites
- For community events: find local government or organization websites
- For festivals: find official event websites
- If you can't find a real URL, leave the link field empty

JSON FORMAT REQUIREMENTS:
- Use only standard ASCII characters in JSON
- Escape all quotes and special characters properly
- Avoid apostrophes and smart quotes in text fields
- Use simple punctuation only

CRITICAL RULES:
- Include text/robocall campaign milestones (25%, 50%, 75% completion dates)
- Include ballot mail and return dates from the ballot information
- Include community events from the community section
- Include campaign milestones and administrative deadlines
- Sort everything chronologically from today through election day
- DO NOT include actual voter contact activities (those are separate)

Generate both:
1. markdown_content: Formatted bullet points (- Full-Month DD | Event | Purpose)
2. tasks: Array of structured task objects with ALL required fields:
   - date: Task date in format "Aug 19, 2025" (abbreviated month, day, year)
   - title: Task/event title
   - description: Task description/purpose
   - cta: Call to action from examples above
   - type: Task type from list above
   - category: Task category from list above
   - deadline: Last effective date in format "Aug 25, 2025" (usually same day for events, few days for milestones)
   - link: External link if applicable (real URLs only, empty string if none)

Ensure each task has a realistic deadline that makes sense for the activity type.
"""
        
        try:
            response = self.gemini_client.generate_structured_content(
                prompt=timeline_prompt,
                response_schema=TimelineResponse,
                temperature=0.1
            )
            
            self.logger.info("Successfully generated structured timeline content")
            return response
            
        except Exception as e:
            self.logger.error(f"Failed to generate structured timeline content: {str(e)}")
            
            # Try fallback with regular LLM client for markdown content only
            try:
                self.logger.info("Attempting fallback with regular LLM client")
                fallback_response = self.llm_client.create_completion(
                    messages=[
                        {
                            "role": "system",
                            "content": "You are an expert campaign strategist. Generate a chronological campaign timeline focusing ONLY on planning events, milestones, and key election dates. Do NOT include voter contact tactics."
                        },
                        {
                            "role": "user",
                            "content": timeline_prompt.replace("Generate both:", "Generate timeline content in markdown format:")
                        }
                    ],
                    max_tokens=20000,
                    temperature=0.1
                )
                
                markdown_content = fallback_response.choices[0].message.content
                self.logger.info("Successfully generated fallback timeline content")
                
                return TimelineResponse(
                    markdown_content=markdown_content,
                    tasks=[]  # No structured tasks from fallback
                )
                
            except Exception as fallback_error:
                self.logger.error(f"Fallback timeline generation also failed: {str(fallback_error)}")
                return TimelineResponse(
                    markdown_content="Error generating timeline content",
                    tasks=[]
                )


if __name__ == "__main__":
    from ai_generated_campaign_plan.schema.models import CampaignInfo, IncumbentStatus, RaceType
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
        logger.error(f"Test failed: {str(e)}")
        raise