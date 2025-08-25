import asyncio
from datetime import date, timedelta
from typing import Dict, Any, List, Tuple
from pydantic import BaseModel, Field
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo
from shared.logger import get_logger
from shared.llm import LLMClient
from shared.llm_gemini import GeminiClient
from shared.tavily_client import SharedTavilyClient
from shared.ai_template_client import ai_template_client

# Structured task models for timeline generation
class TimelineTask(BaseModel):
    """Single timeline task with proper categorization"""
    date: str = Field(description="Exact date string (e.g., 'Aug 19, 2025')")
    title: str = Field(description="Task/event title")
    description: str = Field(description="Task description/purpose")
    cta: str = Field(description="Call to action: Schedule, develop strategy, Visit in person, Write post, Learn More, etc.")
    type: str = Field(description="Task type: outreach, externalLink, event, general, compliance")
    category: str = Field(description="Task category: text, robocall, doorKnocking, phoneBanking, socialMedia, events, education, compliance, general")
    deadline: int = Field(description="Weeks from election date when this task becomes ineffective (e.g., 2 = 2 weeks before election)")
    link: str = Field(default="", description="External link for events (real URLs only)")
    week: int = Field(description="Campaign week number (1-9, where 1 = election week)")
    defaultAiTemplateId: str = Field(default="", description="AI template ID for text/robocall/doorKnocking/phoneBanking/socialMedia tasks")
    proRequired: bool = Field(description="Whether task requires pro subscription")

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
                    "deadline": task.deadline,
                    "week": task.week,
                    "proRequired": task.proRequired
                }
                if task.link:
                    task_dict["link"] = task.link
                
                # Prioritize AI-generated template ID, fallback to template service
                if task.category in ["text", "robocall", "doorKnocking", "phoneBanking", "socialMedia"]:
                    if task.defaultAiTemplateId:
                        # Use AI-generated template ID if available (prioritized)
                        task_dict["defaultAiTemplateId"] = task.defaultAiTemplateId
                    else:
                        # Fallback to template service
                        template_id = ai_template_client.get_template_id_for_task(
                            task.category, task.week, task.description
                        )
                        if template_id:
                            task_dict["defaultAiTemplateId"] = template_id
                
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

CAMPAIGN TASK GENERATION GUIDELINES:
The campaign timeline runs from Week 9 (campaign start) to Week 1 (election week). Generate tasks following these week-specific priorities:

WEEK 1 (Election Week - Final GOTV Push):
- Focus: Get Out The Vote (GOTV)
- Required Task Types: text, robocall, doorKnocking, phoneBanking, socialMedia, events
- Messaging Theme: "reminding people to vote", "election day reminders"
- Events: Going to polls, celebrating campaign efforts

WEEK 2 (1 Week Before Election):
- Focus: GOTV + Final Persuasion
- Required Task Types: text, robocall, doorKnocking, phoneBanking, socialMedia, events
- Messaging Theme: Mix of persuasive content and vote reminders, "answering common questions"

WEEKS 3-6 (Persuasion Phase - 2-5 Weeks Before Election):
- Focus: Voter Persuasion & Trust Building
- Required Task Types: doorKnocking, phoneBanking, socialMedia, events, education (week 4+)
- Special: Week 4 reintroduces text/robocalls with "1 month to election" messaging
- Messaging Theme: "persuade voters", "build trust", discussing "top voter issues"

WEEKS 7-8 (Voter Identification Phase - 6-7 Weeks Before Election):
- Focus: Getting to Know Voters
- Required Task Types: doorKnocking, phoneBanking, socialMedia, events, education
- Messaging Theme: "get to know your voters", "learn about their top issues"

WEEK 9 (Campaign Foundation):
- Focus: Education & Platform Building
- Required Task Types: education only
- Content: Profile completion, community joining, campaign education

TASK CATEGORIES (use exact values):
- "text" - Text messaging campaigns (requires AI template ID)
- "robocall" - Robocall campaigns (requires AI template ID)
- "doorKnocking" - Door-to-door canvassing (requires AI template ID)
- "phoneBanking" - Phone banking campaigns (requires AI template ID)
- "socialMedia" - Social media campaigns (requires AI template ID)
- "events" - Campaign events, community activities (use link instead)
- "education" - Educational content, platform building (use link instead)
- "compliance" - Filing deadlines, ballot dates, administrative tasks
- "general" - General campaign activities, planning, fundraising

ESSENTIAL WEEKLY TASKS WITH AI TEMPLATE IDS (THESE ARE REQUIRED FOR EACH TASK GENERATION):
Week 1: "5b6W9pYlX796TBI2HV7HlQ" (election day text), "2GMO6bQoQermNhdRmRe1fh" (election day robocall), "2p3mztAVPhuDHOYJetmdWJ" (GOTV door knocking), "1HcpEmwIcXMCSW26ilxQP7" (GOTV phone banking), "GpWsRql46Nif2wYroxj81" (GOTV social media)
Week 2: "5NbCRs4cIhti8pxnI8IM0P" (persuasive text), "6ZH4tMYcZNXshFOcLtjMJB" (persuasive robocall), "2p3mztAVPhuDHOYJetmdWJ" (door knocking), "1HcpEmwIcXMCSW26ilxQP7" (phone banking), "2X5rPGVz0sneUZ06w0ezcl" (social media Q&A)
Week 3: "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue-focused social media)
Week 4: "6Adu3kct9uvZ0YNCXLPUvd" (1-month text), "452l4TPYpWdQZYxHHJsdUb" (1-month robocall), "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue-focused social media)
Week 5-6: "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue social media), "3nr6D5fpYfIfywijoE1ITH" (event calendar social media - week 6)
Week 7-8: "5jrvZCd28PMH4ipYl9DzTB" (voter ID door knocking), "2QCSobc5r6R7gO5hb0i8Ho" (voter ID phone banking), "NogRPt7eIxTU3ZEIw87LA" (community social media)

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
1. markdown_content: Formatted bullet points (- Full-Month DD | Event | Purpose) - DO NOT include template IDs in markdown
2. tasks: Array of structured task objects with ALL required fields:
   - date: Exact task date in format "Aug 19, 2025" (abbreviated month, day, year)
   - title: Task/event title
   - description: Task description/purpose
   - cta: Call to action from examples above
   - type: Task type from list above
   - category: Task category from list above (text, robocall, doorKnocking, phoneBanking, socialMedia, events, education, compliance, general)
   - deadline: Weeks from election date when task becomes ineffective (integer, e.g., 2 = 2 weeks before election)
   - link: External link if applicable (real URLs only, empty string if none)
   - week: Campaign week number (1-9, where 1 = election week)
   - defaultAiTemplateId: Required for text/robocall/doorKnocking/phoneBanking/socialMedia tasks (use IDs from essential tasks list above)
   - proRequired: Boolean - true for text/robocall/doorKnocking/phoneBanking, false for socialMedia/events/education

Ensure each task has a realistic deadline in weeks that makes sense for the activity type (e.g., events = same week, milestones = 1-2 weeks buffer).
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
                
                # Try to extract tasks from any JSON content in the fallback response
                try:
                    import json
                    import re
                    
                    # Look for JSON content in the response
                    json_match = re.search(r'\{.*"tasks":\s*\[(.*?)\]\s*.*\}', fallback_response.choices[0].message.content, re.DOTALL)
                    if json_match:
                        # Try to parse the JSON and extract tasks
                        json_content = json_match.group(0)
                        parsed_json = json.loads(json_content)
                        if 'tasks' in parsed_json and parsed_json['tasks']:
                            self.logger.info(f"Extracted {len(parsed_json['tasks'])} tasks from fallback JSON content")
                            # Convert to TimelineTask objects
                            timeline_tasks = []
                            for task_data in parsed_json['tasks']:
                                try:
                                    timeline_task = TimelineTask(**task_data)
                                    timeline_tasks.append(timeline_task)
                                except Exception as task_error:
                                    self.logger.warning(f"Failed to parse task: {str(task_error)}")
                            
                            return TimelineResponse(
                                markdown_content=parsed_json.get('markdown_content', fallback_response.choices[0].message.content),
                                tasks=timeline_tasks
                            )
                except Exception as extract_error:
                    self.logger.warning(f"Failed to extract tasks from fallback content: {str(extract_error)}")
                
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
[JULY 15]  - text: Candidate intro and vote-by-mail awareness if applicable  
[JULY 25]  - robocall: Ballot arrival and early voting prompt  
[AUGUST 10]  - text: Experience and contrast message  
[AUGUST 25]  - robocall: Vote return and community message  
[SEPTEMBER 5]  - text: Persuasion and vote planning  
[SEPTEMBER 10]  - robocall: Final GOTV push and polling info  
[SEPTEMBER 12]  - text: Final GOTV reminder  
[SEPTEMBER 15]  - Primary Election Day  
[OCTOBER 1]  - text: Reintroduction and contrast message  
[OCTOBER 10]  - robocall: Early voting alert  
[OCTOBER 20]  - text: Key issues and voter education  
[OCTOBER 25]  - robocall: Final persuasion  
[NOVEMBER 1]  - text: Vote-by-mail deadline and GOTV push  
[NOVEMBER 3]  - robocall: Election Day GOTV  
[NOVEMBER 4]  - text: Final reminder and polling location link  
[NOVEMBER 5]  - General Election Day
"""
        
        timeline_generator = CampaignTimelineGenerator()
        timeline_section = asyncio.run(timeline_generator.generate_section(cleaned_campaign_info, community_section, voter_contact_section))
        
        print("Generated Campaign Timeline:")
        print(timeline_section)
        
    except Exception as e:
        logger.error(f"Test failed: {str(e)}")
        raise