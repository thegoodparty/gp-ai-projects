import asyncio
from datetime import date, timedelta
from typing import Dict, Any, List, Tuple
from pydantic import BaseModel, Field
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo, ContactOptimization, IncumbentStatus
from shared.logger import get_logger
from shared.llm import LLMClient
from shared.llm_gemini import GeminiClient
from shared.ai_template_client import ai_template_client

# Structured task models for voter contact generation
class VoterContactTask(BaseModel):
    """Single voter contact task with proper categorization"""
    date: str = Field(description="Date string (e.g., 'Aug 19, 2025')")
    title: str = Field(description="Contact method/type title")
    description: str = Field(description="Message theme or purpose")
    cta: str = Field(description="Call to action: Schedule, develop strategy, Visit in person, Write post, Learn More, etc.")
    type: str = Field(description="Task type: outreach, externalLink, event, general")
    category: str = Field(description="Task category: text, robocall, doorKnocking, phoneBanking, socialMedia, events, education, compliance, general")
    deadline: str = Field(description="Last effective date for this task (e.g., 'Aug 25, 2025')")
    link: str = Field(default="", description="External link if applicable")
    week: int = Field(description="Campaign week number (1-9, where 1 = election week)")
    defaultAiTemplateId: str = Field(default="", description="AI template ID for text/robocall/doorKnocking/phoneBanking/socialMedia tasks")
    proRequired: bool = Field(description="Whether task requires pro subscription")

class VoterContactResponse(BaseModel):
    """Voter contact generation response with structured tasks"""
    markdown_content: str = Field(description="Formatted markdown voter contact content")
    tasks: List[VoterContactTask] = Field(description="List of structured voter contact tasks")

class VoterContactPlanGenerator:
    
    def __init__(self):
        self.llm_client = LLMClient()
        self.gemini_client = GeminiClient()
        self.logger = get_logger(__name__)
    

    
    async def generate_section(self, campaign_info: CleanedCampaignInfo, primary_contact_strategy: ContactOptimization = None, general_contact_strategy: ContactOptimization = None) -> Tuple[str, List[Dict[str, Any]]]:
        self.logger.info(f"Generating voter contact plan for {campaign_info.candidate_name}")
        
        has_primary = campaign_info.has_primary
        
        try:
            if has_primary:
                contact_response = await self._generate_structured_contact_with_primary(
                    campaign_info, primary_contact_strategy, general_contact_strategy
                )
            else:
                contact_response = await self._generate_structured_contact_general_only(
                    campaign_info, general_contact_strategy
                )
            
            complete_section = f"""## 6. VOTER CONTACT PLAN
### Core Tactics (Chronological)
{contact_response.markdown_content}
"""
            
            # Convert tasks to dict format expected by json_extractor
            structured_tasks = []
            for task in contact_response.tasks:
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
                
                # Get template ID dynamically from AI template service
                if task.category in ["text", "robocall", "doorKnocking", "phoneBanking", "socialMedia"]:
                    template_id = ai_template_client.get_template_id_for_task(
                        task.category, task.week, task.description
                    )
                    if template_id:
                        task_dict["defaultAiTemplateId"] = template_id
                    elif task.defaultAiTemplateId:
                        # Fallback to AI-generated template ID if available
                        task_dict["defaultAiTemplateId"] = task.defaultAiTemplateId
                
                structured_tasks.append(task_dict)
            
            self.logger.info("Successfully generated voter contact plan with structured tasks")
            return complete_section, structured_tasks
            
        except Exception as e:
            self.logger.error(f"Error generating voter contact plan: {str(e)}")
            return "## 6. VOTER CONTACT PLAN\n\n[Error generating voter contact plan]", []
    
    async def _generate_structured_contact_with_primary(self, campaign_info: CleanedCampaignInfo, 
                                                       primary_strategy: ContactOptimization, 
                                                       general_strategy: ContactOptimization) -> VoterContactResponse:
        """Generate structured voter contact plan for campaigns with primary."""
        
        prompt = f"""
Generate a voter contact plan with both markdown content and structured task data.

CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Today's Date: {date.today()}
- Primary Date: {campaign_info.primary_date}
- General Election Date: {campaign_info.election_date}

CONTACT LIMITS:
- Primary phase: {primary_strategy.p2p_texts} texts, {primary_strategy.robocalls} robocalls
- General phase: {general_strategy.p2p_texts} texts, {general_strategy.robocalls} robocalls

CAMPAIGN TASK GENERATION GUIDELINES:
The campaign timeline runs from Week 9 (campaign start) to Week 1 (election week). Generate tasks following these week-specific priorities:

WEEK 1 (Election Week - Final GOTV Push):
- Focus: Get Out The Vote (GOTV)
- Required Task Types: text, robocall, doorKnocking, phoneBanking, socialMedia, events
- Messaging Theme: "reminding people to vote", "election day reminders"

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

TASK CATEGORIES (use exact values):
- "text" - Text messaging campaigns (requires AI template ID)
- "robocall" - Robocall campaigns (requires AI template ID)
- "doorKnocking" - Door-to-door canvassing (requires AI template ID)
- "phoneBanking" - Phone banking campaigns (requires AI template ID)
- "socialMedia" - Social media campaigns (requires AI template ID)
- "events" - Campaign events, community activities (use link instead)
- "education" - Educational content, platform building (use link instead)
- "general" - General voter contact activities

ESSENTIAL WEEKLY TASKS WITH AI TEMPLATE IDS:
Week 1: "5b6W9pYlX796TBI2HV7HlQ" (election day text), "2GMO6bQoQermNhdRmRe1fh" (election day robocall), "2p3mztAVPhuDHOYJetmdWJ" (GOTV door knocking), "1HcpEmwIcXMCSW26ilxQP7" (GOTV phone banking), "GpWsRql46Nif2wYroxj81" (GOTV social media)
Week 2: "5NbCRs4cIhti8pxnI8IM0P" (persuasive text), "6ZH4tMYcZNXshFOcLtjMJB" (persuasive robocall), "2p3mztAVPhuDHOYJetmdWJ" (door knocking), "1HcpEmwIcXMCSW26ilxQP7" (phone banking), "2X5rPGVz0sneUZ06w0ezcl" (social media Q&A)
Week 3: "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue-focused social media)
Week 4: "6Adu3kct9uvZ0YNCXLPUvd" (1-month text), "452l4TPYpWdQZYxHHJsdUb" (1-month robocall), "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue-focused social media)
Week 5-6: "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue social media), "3nr6D5fpYfIfywijoE1ITH" (event calendar social media - week 6)
Week 7-8: "5jrvZCd28PMH4ipYl9DzTB" (voter ID door knocking), "2QCSobc5r6R7gO5hb0i8Ho" (voter ID phone banking), "NogRPt7eIxTU3ZEIw87LA" (community social media)

CALL TO ACTION EXAMPLES:
- "Schedule" - Schedule the activity/contact
- "develop strategy" - Plan and strategize
- "Visit in person" - Attend event or canvass
- "Write post" - Create social media content
- "Learn More" - Research or gather information
- "Make calls" - Conduct phone banking
- "Send texts" - Execute text campaigns

JSON FORMAT REQUIREMENTS:
- Use only standard ASCII characters in JSON
- Escape all quotes and special characters properly
- Avoid apostrophes and smart quotes in text fields
- Use simple punctuation only

VOTER CONTACT STRATEGY EXAMPLES:
Primary Phase:
- Text: Candidate intro and vote-by-mail awareness
- Robocall: Ballot arrival and early voting prompt
- DoorKnocking: Weekend canvassing in high-turnout precincts
- PhoneBanking: Volunteer-led voter ID calls
- SocialMedia: Facebook/Instagram targeted ads
- Event: Town hall meetings, coffee hours
- ExternalLink: Election Day polling location link
- General: General voter contact activities

General Phase:
- Text: Reintroduction and contrast message
- Robocall: Early voting alert
- DoorKnocking: Targeted door-to-door in swing precincts
- PhoneBanking: Persuasion calls to undecided voters
- SocialMedia: Digital ad campaigns, social media engagement
- Event: Candidate forums, community events
- Final Text: Final reminder and polling location link
- Final Robocall: Election Day GOTV

Generate both:
1. markdown_content: Formatted bullet points (- [FULL MONTH DD] - Contact Type: Message theme) - DO NOT include template IDs in markdown
2. tasks: Array of structured contact task objects with ALL required fields:
   - date: Task date in format "Aug 19, 2025" (abbreviated month, day, year)
   - title: Contact method/type title
   - description: Message theme or purpose
   - cta: Call to action from examples above
   - type: Task type from list above
   - category: Task category from list above (text, robocall, doorKnocking, phoneBanking, socialMedia, events, education, general)
   - deadline: Last effective date in format "Aug 25, 2025" (usually 1-3 days after task date)
   - link: External link if applicable (empty string if none)
   - week: Campaign week number (1-9, where 1 = election week)
   - defaultAiTemplateId: Required for text/robocall/doorKnocking/phoneBanking/socialMedia tasks (use IDs from essential tasks list above)
   - proRequired: Boolean - true for text/robocall/doorKnocking/phoneBanking, false for socialMedia/events/education

Schedule contacts chronologically from today through election day.
Ensure each task has a realistic deadline that makes sense for the activity type.
"""
        
        try:
            return self.gemini_client.generate_structured_content(
                prompt=prompt,
                response_schema=VoterContactResponse,
                temperature=0.1
            )
        except Exception as e:
            self.logger.error(f"Structured voter contact generation failed (primary): {str(e)}")
            
            # Try fallback with regular LLM client
            try:
                self.logger.info("Attempting fallback with regular LLM client for primary campaign")
                fallback_response = self.llm_client.create_completion(
                    messages=[
                        {"role": "system", "content": "You are a campaign strategist. Generate the voter contact plan in the exact format shown. Do not add thinking or reasoning."},
                        {"role": "user", "content": prompt.replace("Generate both:", "Generate voter contact plan in markdown format:")}
                    ],
                    temperature=0.1,
                    max_tokens=10000
                )
                
                markdown_content = fallback_response.choices[0].message.content
                self.logger.info("Successfully generated fallback voter contact content for primary campaign")
                
                return VoterContactResponse(
                    markdown_content=markdown_content,
                    tasks=[]  # No structured tasks from fallback
                )
                
            except Exception as fallback_error:
                self.logger.error(f"Fallback voter contact generation also failed for primary campaign: {str(fallback_error)}")
                
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
                            self.logger.info(f"Extracted {len(parsed_json['tasks'])} voter contact tasks from primary fallback JSON content")
                            # Convert to VoterContactTask objects
                            contact_tasks = []
                            for task_data in parsed_json['tasks']:
                                try:
                                    contact_task = VoterContactTask(**task_data)
                                    contact_tasks.append(contact_task)
                                except Exception as task_error:
                                    self.logger.warning(f"Failed to parse primary voter contact task: {str(task_error)}")
                            
                            return VoterContactResponse(
                                markdown_content=parsed_json.get('markdown_content', fallback_response.choices[0].message.content),
                                tasks=contact_tasks
                            )
                except Exception as extract_error:
                    self.logger.warning(f"Failed to extract voter contact tasks from primary fallback content: {str(extract_error)}")
                
                return VoterContactResponse(
                    markdown_content="Error generating voter contact plan",
                    tasks=[]
                )
    
    async def _generate_structured_contact_general_only(self, campaign_info: CleanedCampaignInfo, 
                                                       general_strategy: ContactOptimization) -> VoterContactResponse:
        """Generate structured voter contact plan for general election only."""
        
        prompt = f"""
Generate a voter contact plan with both markdown content and structured task data.

CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Today's Date: {date.today()}
- General Election Date: {campaign_info.election_date}

CONTACT LIMITS:
- {general_strategy.p2p_texts} texts and {general_strategy.robocalls} robocalls total

CAMPAIGN TASK GENERATION GUIDELINES:
The campaign timeline runs from Week 9 (campaign start) to Week 1 (election week). Generate tasks following these week-specific priorities:

WEEK 1 (Election Week - Final GOTV Push):
- Focus: Get Out The Vote (GOTV)
- Required Task Types: text, robocall, doorKnocking, phoneBanking, socialMedia, events
- Messaging Theme: "reminding people to vote", "election day reminders"

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

TASK CATEGORIES (use exact values):
- "text" - Text messaging campaigns (requires AI template ID)
- "robocall" - Robocall campaigns (requires AI template ID)
- "doorKnocking" - Door-to-door canvassing (requires AI template ID)
- "phoneBanking" - Phone banking campaigns (requires AI template ID)
- "socialMedia" - Social media campaigns (requires AI template ID)
- "events" - Campaign events, community activities (use link instead)
- "education" - Educational content, platform building (use link instead)
- "general" - General voter contact activities

ESSENTIAL WEEKLY TASKS WITH AI TEMPLATE IDS:
Week 1: "5b6W9pYlX796TBI2HV7HlQ" (election day text), "2GMO6bQoQermNhdRmRe1fh" (election day robocall), "2p3mztAVPhuDHOYJetmdWJ" (GOTV door knocking), "1HcpEmwIcXMCSW26ilxQP7" (GOTV phone banking), "GpWsRql46Nif2wYroxj81" (GOTV social media)
Week 2: "5NbCRs4cIhti8pxnI8IM0P" (persuasive text), "6ZH4tMYcZNXshFOcLtjMJB" (persuasive robocall), "2p3mztAVPhuDHOYJetmdWJ" (door knocking), "1HcpEmwIcXMCSW26ilxQP7" (phone banking), "2X5rPGVz0sneUZ06w0ezcl" (social media Q&A)
Week 3: "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue-focused social media)
Week 4: "6Adu3kct9uvZ0YNCXLPUvd" (1-month text), "452l4TPYpWdQZYxHHJsdUb" (1-month robocall), "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue-focused social media)
Week 5-6: "wgbnDDTxrf8OrresVE1HU" (persuasive door knocking), "5N93cglp3cvq62EIwu1IOa" (persuasive phone banking), "Xboqgh6Ye3SgSwO6moujw" (issue social media), "3nr6D5fpYfIfywijoE1ITH" (event calendar social media - week 6)
Week 7-8: "5jrvZCd28PMH4ipYl9DzTB" (voter ID door knocking), "2QCSobc5r6R7gO5hb0i8Ho" (voter ID phone banking), "NogRPt7eIxTU3ZEIw87LA" (community social media)

JSON FORMAT REQUIREMENTS:
- Use only standard ASCII characters in JSON
- Escape all quotes and special characters properly
- Avoid apostrophes and smart quotes in text fields
- Use simple punctuation only

VOTER CONTACT STRATEGY EXAMPLES:
- Early Text: Voter intro + early voting alert
- Early Robocall: Candidate intro + race message
- Early DoorKnocking: Weekend canvassing in target neighborhoods
- Early PhoneBanking: Voter ID and registration drives
- Early SocialMedia: Digital ad campaigns and social engagement
- Mid Campaign Text: Contrast/persuasion message
- Mid Campaign Robocall: Polling info + persuasion
- Mid Campaign DoorKnocking: Targeted persuasion canvassing
- Mid Campaign Event: Town halls, candidate forums
- Late Campaign PhoneBanking: GOTV calls to supporters
- Late Campaign SocialMedia: Final push digital campaigns
- Final Text: Election Day reminder + poll finder
- Final Robocall: Final GOTV call (morning)

Generate both:
1. markdown_content: Formatted bullet points (- [FULL MONTH DD] - Contact Type: Message theme) - DO NOT include template IDs in markdown
2. tasks: Array of structured contact task objects with ALL required fields:
   - date: Task date in format "Aug 19, 2025" (abbreviated month, day, year)
   - title: Contact method/type title
   - description: Message theme or purpose
   - cta: Call to action from examples above
   - type: Task type from list above
   - category: Task category from list above (text, robocall, doorKnocking, phoneBanking, socialMedia, events, education, general)
   - deadline: Last effective date in format "Aug 25, 2025" (usually 1-3 days after task date)
   - link: External link if applicable (empty string if none)
   - week: Campaign week number (1-9, where 1 = election week)
   - defaultAiTemplateId: Required for text/robocall/doorKnocking/phoneBanking/socialMedia tasks (use IDs from essential tasks list above)
   - proRequired: Boolean - true for text/robocall/doorKnocking/phoneBanking, false for socialMedia/events/education

Schedule contacts chronologically from today through election day.
Ensure each task has a realistic deadline that makes sense for the activity type.
"""
        
        try:
            return self.gemini_client.generate_structured_content(
                prompt=prompt,
                response_schema=VoterContactResponse,
                temperature=0.1
            )
        except Exception as e:
            self.logger.error(f"Structured voter contact generation failed (general): {str(e)}")
            
            # Try fallback with regular LLM client
            try:
                self.logger.info("Attempting fallback with regular LLM client for general campaign")
                fallback_response = self.llm_client.create_completion(
                    messages=[
                        {"role": "system", "content": "You are a campaign strategist. Generate the voter contact plan in the exact format shown. Do not add thinking or reasoning."},
                        {"role": "user", "content": prompt.replace("Generate both:", "Generate voter contact plan in markdown format:")}
                    ],
                    temperature=0.1,
                    max_tokens=10000
                )
                
                markdown_content = fallback_response.choices[0].message.content
                self.logger.info("Successfully generated fallback voter contact content for general campaign")
                
                return VoterContactResponse(
                    markdown_content=markdown_content,
                    tasks=[]  # No structured tasks from fallback
                )
                
            except Exception as fallback_error:
                self.logger.error(f"Fallback voter contact generation also failed for general campaign: {str(fallback_error)}")
                
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
                            self.logger.info(f"Extracted {len(parsed_json['tasks'])} voter contact tasks from fallback JSON content")
                            # Convert to VoterContactTask objects
                            contact_tasks = []
                            for task_data in parsed_json['tasks']:
                                try:
                                    contact_task = VoterContactTask(**task_data)
                                    contact_tasks.append(contact_task)
                                except Exception as task_error:
                                    self.logger.warning(f"Failed to parse voter contact task: {str(task_error)}")
                            
                            return VoterContactResponse(
                                markdown_content=parsed_json.get('markdown_content', fallback_response.choices[0].message.content),
                                tasks=contact_tasks
                            )
                except Exception as extract_error:
                    self.logger.warning(f"Failed to extract voter contact tasks from fallback content: {str(extract_error)}")
                
                return VoterContactResponse(
                    markdown_content="Error generating voter contact plan",
                    tasks=[]
                )

if __name__ == "__main__":
    from ai_generated_campaign_plan.schema.models import CampaignInfo
    from ai_generated_campaign_plan.utils.utils import CampaignUtils
    campaign_utils = CampaignUtils()
    generator = VoterContactPlanGenerator()

    print("=== TEST CASE 1: CAMPAIGN WITH PRIMARY ===")
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=date(2025, 11, 5),
        primary_date=date(2025, 8, 1),
        race_type="Nonpartisan",
        seats_available=1,
        number_of_opponents=2,
        win_number=15000,
        total_likely_voters=100000,
        available_cell_phones=10000,
        available_landlines=1000,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on education funding and infrastructure improvements"
    )

    cleaned_campaign_info_with_primary = campaign_utils.clean_campaign_info(campaign_info_with_primary)
    primary_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_with_primary.primary_date)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(cleaned_campaign_info_with_primary.primary_date, cleaned_campaign_info_with_primary.election_date)
    result_with_primary = asyncio.run(generator.generate_section(cleaned_campaign_info_with_primary, primary_campaign_plan, general_campaign_plan))
    print(result_with_primary)
    
    print("\n" + "="*60 + "\n")
    
    print("=== TEST CASE 2: CAMPAIGN WITHOUT PRIMARY ===")
    campaign_info_no_primary = CampaignInfo(
        candidate_name="Jane Smith",
        office_and_jurisdiction="City Council, District 3, Boston, MA",
        election_date=date(2025, 7, 22),
        primary_date=None,
        race_type="Nonpartisan",
        seats_available=1,
        number_of_opponents=3,
        win_number=8000,
        total_likely_voters=50000,
        available_cell_phones=5000,
        available_landlines=500,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on neighborhood safety and small business support"
    )

    cleaned_campaign_info_no_primary = campaign_utils.clean_campaign_info(campaign_info_no_primary)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_no_primary.election_date)
    result_no_primary = asyncio.run(generator.generate_section(cleaned_campaign_info_no_primary, general_contact_strategy=general_campaign_plan))
    print(result_no_primary)