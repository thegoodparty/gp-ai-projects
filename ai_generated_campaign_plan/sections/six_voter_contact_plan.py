import asyncio
from datetime import date, timedelta
from typing import Dict, Any, List, Tuple
from pydantic import BaseModel, Field
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo, ContactOptimization, IncumbentStatus
from shared.logger import get_logger
from shared.llm import LLMClient
from shared.llm_gemini import GeminiClient


# Structured task models for voter contact generation
class VoterContactTask(BaseModel):
    """Single voter contact task with proper categorization"""
    date: str = Field(description="Exact date string (e.g., 'Aug 19, 2025')")
    title: str = Field(description="Contact method/type title")
    description: str = Field(description="Message theme or purpose")
    cta: str = Field(description="Call to action: Schedule, develop strategy, Visit in person, Write post, Learn More, etc.")
    type: str = Field(description="Task type: outreach, externalLink, event, general")
    category: str = Field(description="Task category: text, robocall, doorKnocking, phoneBanking, socialMedia, events, education, compliance, general")
    deadline: int = Field(description="Weeks from election date when this task becomes ineffective (e.g., 2 = 2 weeks before election)")
    link: str = Field(default="", description="External link if applicable")
    week: int = Field(description="Campaign week number (1-9, where 1 = election week)")

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

MANDATORY VOTER CONTACT TASK REQUIREMENTS:
You MUST generate tasks with the following specific categories. DO NOT use "general" for voter contact activities:

REQUIRED WEEKLY VOTER CONTACT TASKS (MUST INCLUDE ALL OF THESE):
Week 1 (Election Week):
- text: Election Day Text Reminder (proRequired: true)
- robocall: Election Day Robocall Reminder (proRequired: true)  
- doorKnocking: GOTV Door Knocking (proRequired: true)
- phoneBanking: GOTV Phone Banking (proRequired: true)
- socialMedia: GOTV Social Media Push (proRequired: false)

Week 2:
- text: Persuasive Text Campaign (proRequired: true)
- robocall: Persuasive Robocall Campaign (proRequired: true)
- doorKnocking: Door Knocking Campaign (proRequired: true)
- phoneBanking: Phone Banking Campaign (proRequired: true)
- socialMedia: Social Media Q&A (proRequired: false)

Week 3:
- doorKnocking: Persuasive Door Knocking (proRequired: true)
- phoneBanking: Persuasive Phone Banking (proRequired: true)
- socialMedia: Issue-Focused Social Media (proRequired: false)

Week 4:
- text: 1 Month to Election Text (proRequired: true)
- robocall: 1 Month to Election Robocall (proRequired: true)
- doorKnocking: Persuasive Door Knocking (proRequired: true)
- phoneBanking: Persuasive Phone Banking (proRequired: true)
- socialMedia: Issue-Focused Social Media (proRequired: false)

Week 7-8:
- doorKnocking: Voter ID Door Knocking (proRequired: true)
- phoneBanking: Voter ID Phone Banking (proRequired: true)
- socialMedia: Community Engagement Social Media (proRequired: false)

CATEGORY CONSTRAINTS (MUST use these exact values):
- "text" - Text messaging campaigns (proRequired: true)
- "robocall" - Robocall campaigns (proRequired: true)
- "doorKnocking" - Door-to-door canvassing (proRequired: true)
- "phoneBanking" - Phone banking campaigns (proRequired: true)
- "socialMedia" - Social media campaigns (proRequired: false)
- "events" - Campaign events, voter contact activities
- "education" - Educational voter contact content
- "general" - ONLY for general activities, NOT for voter contact

CALL TO ACTION MAPPING:
- text/robocall/doorKnocking/phoneBanking: "develop strategy"
- socialMedia: "Write post"
- events: "Visit in person"
- education: "Learn More"
- general: "Schedule"

TYPE FIELD MAPPING:
- text/robocall/doorKnocking/phoneBanking/socialMedia: "outreach"
- events: "events" 
- education: "education"
- general: "general"

CRITICAL REQUIREMENTS:
1. You MUST include ALL the required weekly voter contact tasks listed above with their exact template IDs
2. You MUST use the correct category values (NOT "general" for voter contact activities)
3. You MUST set proRequired correctly (true for text/robocall/doorKnocking/phoneBanking, false for others)
4. Include scheduling milestones for text/robocall campaigns (25%, 50%, 75% completion)
5. Schedule all voter contact activities chronologically

Generate both:
1. markdown_content: Formatted bullet points (- [MONTH DD] - Contact Type: Message theme)
2. tasks: Array of structured voter contact task objects with ALL required fields correctly set

WEEK CALCULATION:
Calculate week numbers where Week 1 = election week. Work backwards from election date.

FORMAT EXAMPLE for tasks array:
{{
  "date": "Oct 15, 2026",
  "title": "Election Day Text Reminder",
  "description": "Final text reminder to vote on Election Day",
  "cta": "develop strategy",
  "type": "outreach", 
  "category": "text",
  "deadline": 1,
  "link": "",
  "week": 1,

  "proRequired": true
}}
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

MANDATORY VOTER CONTACT TASK REQUIREMENTS:
You MUST generate tasks with the following specific categories. DO NOT use "general" for voter contact activities:

REQUIRED WEEKLY VOTER CONTACT TASKS (MUST INCLUDE ALL OF THESE):
Week 1 (Election Week):
- text: Election Day Text Reminder (proRequired: true)
- robocall: Election Day Robocall Reminder (proRequired: true)  
- doorKnocking: GOTV Door Knocking (proRequired: true)
- phoneBanking: GOTV Phone Banking (proRequired: true)
- socialMedia: GOTV Social Media Push (proRequired: false)

Week 2:
- text: Persuasive Text Campaign (proRequired: true)
- robocall: Persuasive Robocall Campaign (proRequired: true)
- doorKnocking: Door Knocking Campaign (proRequired: true)
- phoneBanking: Phone Banking Campaign (proRequired: true)
- socialMedia: Social Media Q&A (proRequired: false)

Week 3:
- doorKnocking: Persuasive Door Knocking (proRequired: true)
- phoneBanking: Persuasive Phone Banking (proRequired: true)
- socialMedia: Issue-Focused Social Media (proRequired: false)

Week 4:
- text: 1 Month to Election Text (proRequired: true)
- robocall: 1 Month to Election Robocall (proRequired: true)
- doorKnocking: Persuasive Door Knocking (proRequired: true)
- phoneBanking: Persuasive Phone Banking (proRequired: true)
- socialMedia: Issue-Focused Social Media (proRequired: false)

Week 7-8:
- doorKnocking: Voter ID Door Knocking (proRequired: true)
- phoneBanking: Voter ID Phone Banking (proRequired: true)
- socialMedia: Community Engagement Social Media (proRequired: false)

CATEGORY CONSTRAINTS (MUST use these exact values):
- "text" - Text messaging campaigns (proRequired: true)
- "robocall" - Robocall campaigns (proRequired: true)
- "doorKnocking" - Door-to-door canvassing (proRequired: true)
- "phoneBanking" - Phone banking campaigns (proRequired: true)
- "socialMedia" - Social media campaigns (proRequired: false)
- "events" - Campaign events, voter contact activities
- "education" - Educational voter contact content
- "general" - ONLY for general activities, NOT for voter contact

CALL TO ACTION MAPPING:
- text/robocall/doorKnocking/phoneBanking: "develop strategy"
- socialMedia: "Write post"
- events: "Visit in person"
- education: "Learn More"
- general: "Schedule"

TYPE FIELD MAPPING:
- text/robocall/doorKnocking/phoneBanking/socialMedia: "outreach"
- events: "events" 
- education: "education"
- general: "general"

CRITICAL REQUIREMENTS:
1. You MUST include ALL the required weekly voter contact tasks listed above with their exact template IDs
2. You MUST use the correct category values (NOT "general" for voter contact activities)
3. You MUST set proRequired correctly (true for text/robocall/doorKnocking/phoneBanking, false for others)
4. Include scheduling milestones for text/robocall campaigns (25%, 50%, 75% completion)
5. Schedule all voter contact activities chronologically

Generate both:
1. markdown_content: Formatted bullet points (- [MONTH DD] - Contact Type: Message theme)
2. tasks: Array of structured voter contact task objects with ALL required fields correctly set

WEEK CALCULATION:
Calculate week numbers where Week 1 = election week. Work backwards from election date.

FORMAT EXAMPLE for tasks array:
{{
  "date": "Oct 15, 2026",
  "title": "Election Day Text Reminder",
  "description": "Final text reminder to vote on Election Day",
  "cta": "develop strategy",
  "type": "outreach", 
  "category": "text",
  "deadline": 1,
  "link": "",
  "week": 1,

  "proRequired": true
}}
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