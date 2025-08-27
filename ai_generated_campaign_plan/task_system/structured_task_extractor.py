"""
Structured task extraction system using LLM structured outputs instead of regex parsing.
This approach is more robust and can handle variations in AI-generated text format.
"""

import re
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field

try:
    # Relative imports for package usage
    from .task_models import (
        CampaignTask, 
        CampaignTaskType, 
        TaskExtractionResult,
        TemplateTheme
    )
except ImportError:
    # Absolute imports for standalone execution
    import sys
    from pathlib import Path
    current_dir = Path(__file__).parent
    project_root = current_dir.parent.parent
    sys.path.insert(0, str(project_root))
    
    from ai_generated_campaign_plan.task_system.task_models import (
        CampaignTask, 
        CampaignTaskType, 
        TaskExtractionResult,
        TemplateTheme
    )

from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo
from shared.llm_gemini import GeminiClient
from shared.logger import get_logger

logger = get_logger(__name__)


class ExtractedContact(BaseModel):
    """Structured model for extracted voter contact tasks"""
    date_string: str = Field(..., description="Date as it appears in text (e.g., 'JULY 15', 'July 15')")
    contact_type: str = Field(..., description="Type of contact: 'text' or 'robocall'")
    sequence_number: int = Field(..., description="Sequence number (1, 2, 3, etc.)")
    message_theme: str = Field(..., description="Theme or description of the message")
    template_id: Optional[str] = Field(None, description="Selected AI template ID for this contact")
    raw_line: str = Field(..., description="Original text line")


class ExtractedTimelineEvent(BaseModel):
    """Structured model for extracted timeline events"""
    date_string: str = Field(..., description="Date as it appears in text (e.g., 'July 15', 'August 1')")
    event_name: str = Field(..., description="Name of the event or milestone")
    event_purpose: str = Field(..., description="Purpose or description of the event")
    template_id: Optional[str] = Field(None, description="Selected AI template ID for this event")
    raw_line: str = Field(..., description="Original text line")


class VoterContactExtraction(BaseModel):
    """Model for Section 6 voter contact extraction results"""
    contacts: List[ExtractedContact] = Field(default=[], description="Extracted contact tasks")
    election_dates: List[str] = Field(default=[], description="Election day mentions")
    

class TimelineExtraction(BaseModel):
    """Model for Section 3 timeline extraction results"""
    events: List[ExtractedTimelineEvent] = Field(default=[], description="Extracted timeline events")
    milestones: List[str] = Field(default=[], description="Important milestones mentioned")


class StructuredTaskExtractor:
    """
    Extracts tasks from AI-generated campaign plan sections using structured LLM outputs.
    Much more robust than regex parsing and can handle format variations.
    """
    
    def __init__(self, llm_client: GeminiClient = None):
        self.llm_client = llm_client or GeminiClient()
        self.logger = logger
        self._template_cache = None
    
    async def _get_ai_templates(self) -> Dict[str, str]:
        """Get AI templates name->ID mapping for LLM to use during task generation."""
        if self._template_cache is not None:
            return self._template_cache
            
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get("https://gp-api.goodparty.org/v1/content/type/aiContentTemplate")
                response.raise_for_status()
                
                templates_data = response.json()
                template_mappings = {}
                
                if isinstance(templates_data, list):
                    for template_data in templates_data:
                        template_id = template_data.get('id')
                        template_name = template_data.get('data', {}).get('name')
                        
                        if template_id and template_name:
                            template_mappings[template_name] = template_id
                
                self._template_cache = template_mappings
                self.logger.info(f"Loaded {len(template_mappings)} AI templates for LLM selection")
                return template_mappings
                
        except Exception as e:
            self.logger.warning(f"Failed to load AI templates: {e}")
            self._template_cache = {}
            return {}
        
    async def extract_tasks_from_sections(
        self, 
        timeline_section: str, 
        voter_contact_section: str, 
        campaign_info: CleanedCampaignInfo
    ) -> TaskExtractionResult:
        """
        Main extraction method that processes both sections and returns structured tasks.
        
        Args:
            timeline_section: Section 3 timeline content
            voter_contact_section: Section 6 voter contact content  
            campaign_info: Campaign information for date processing
            
        Returns:
            TaskExtractionResult with extracted tasks and metadata
        """
        self.logger.info("Starting structured task extraction from campaign plan sections")
        
        try:
            # Get available AI templates for LLM to use during extraction
            template_mappings = await self._get_ai_templates()
            
            # Extract from both sections in parallel
            import asyncio
            
            section_6_extraction, section_3_extraction = await asyncio.gather(
                self._extract_voter_contact_tasks(voter_contact_section, template_mappings),
                self._extract_timeline_events(timeline_section, template_mappings)
            )
            
            # Convert extractions to CampaignTask objects
            tasks = []
            errors = []
            
            # Process voter contact tasks
            for contact in section_6_extraction.contacts:
                try:
                    task = self._create_contact_task(contact, campaign_info)
                    tasks.append(task)
                except Exception as e:
                    errors.append(f"Failed to process contact task: {str(e)}")
                    self.logger.warning(f"Failed to process contact: {contact.raw_line}, error: {e}")
            
            # Process timeline events
            for event in section_3_extraction.events:
                try:
                    task = self._create_timeline_task(event, campaign_info)
                    tasks.append(task)
                except Exception as e:
                    errors.append(f"Failed to process timeline event: {str(e)}")
                    self.logger.warning(f"Failed to process event: {event.raw_line}, error: {e}")
            
            # Sort tasks by scheduled date
            tasks = sorted(tasks, key=lambda x: x.date or "9999-12-31")
            
            self.logger.info(f"Extracted {len(tasks)} tasks successfully with {len(errors)} errors")
            
            return TaskExtractionResult(
                success=True,
                tasks=tasks,
                errors=errors,
                extracted_count=len(tasks),
                skipped_count=len(errors)
            )
            
        except Exception as e:
            self.logger.error(f"Task extraction failed: {str(e)}")
            return TaskExtractionResult(
                success=False,
                tasks=[],
                errors=[f"Extraction failed: {str(e)}"],
                extracted_count=0,
                skipped_count=0
            )
    
    async def _extract_voter_contact_tasks(self, section_text: str, template_mappings: Dict[str, str]) -> VoterContactExtraction:
        """Extract voter contact tasks using structured LLM output with AI template selection"""
        
        # Create template list for LLM reference
        template_list = "\n".join([f"- {name}: {template_id}" for name, template_id in template_mappings.items()])
        
        extraction_prompt = f"""
Analyze this voter contact plan section and extract all contact tasks in structured format.

SECTION TEXT:
{section_text}

AVAILABLE AI TEMPLATES:
{template_list}

Extract each line that contains a scheduled contact (text message or robocall) with:
- Date information (like [JULY 15] or similar)  
- Contact type (P2P Text, Robocall, etc.)
- Sequence number (#1, #2, etc.)
- Message theme/description
- Select the most appropriate AI template ID from the list above based on the contact type and theme

For each contact task, ONLY assign a template ID if you are highly confident it's a good match:
- For text messages (P2P Text, SMS): choose from SMS templates IF theme matches well
- For robocalls: choose from robocall templates IF theme matches well  
- Match both the contact type AND the specific theme/purpose
- If uncertain or no good match exists, leave template_id as null

IMPORTANT: Only assign template IDs when you're confident the template purpose matches the task purpose. Better to have no template than a wrong template.

ONLY extract lines that represent actual scheduled voter contact tasks.
Do NOT extract election day lines or general information.

Examples of what TO extract:
- [JULY 15] – P2P Text #1: Candidate intro and vote-by-mail awareness
- [AUGUST 10] – Robocall #2: Vote return and community message

Examples of what NOT to extract:
- [NOVEMBER 5] – General Election Day
- General information without specific contact tasks
"""
        
        try:
            response = self.llm_client.generate_structured_content(
                prompt=extraction_prompt,
                response_schema=VoterContactExtraction,
                temperature=0.1,
                system_instruction="You are an expert at extracting structured data from campaign plans. Extract only scheduled voter contact tasks (texts and robocalls) with specific dates and themes."
            )
            
            self.logger.info(f"Extracted {len(response.contacts)} voter contact tasks")
            return response
            
        except Exception as e:
            self.logger.error(f"Failed to extract voter contact tasks: {str(e)}")
            return VoterContactExtraction(contacts=[])
    
    async def _extract_timeline_events(self, section_text: str, template_mappings: Dict[str, str]) -> TimelineExtraction:
        """Extract timeline events using structured LLM output"""
        
        # Create template list for LLM reference
        template_list = "\n".join([f"- {name}: {template_id}" for name, template_id in template_mappings.items()])
        
        extraction_prompt = f"""
Analyze this campaign timeline section and extract all events and milestones in structured format.

SECTION TEXT:
{section_text}

AVAILABLE AI TEMPLATES:
{template_list}

Extract each line that contains a scheduled event or milestone with:
- Date information
- Event name/title
- Event purpose or description  
- Select the most appropriate AI template ID from the list above based on the event type

For each event, ONLY assign a template ID if you are highly confident it's a good match:
- For launch events: choose launch templates IF it's clearly a launch
- For community events/meetings: choose community or event templates IF it's clearly community-focused
- For deadlines/reminders: choose appropriate templates IF it's a communication task (not just a deadline)
- For speeches: choose speech templates IF it's clearly a speech event
- If uncertain or no good match exists, leave template_id as null

IMPORTANT: Only assign template IDs when you're confident the template purpose matches the event purpose. Many events may not need AI templates at all.

Look for patterns like:
- July 15 | Campaign Launch Event | Official campaign announcement
- August 1 | Ballot Request Deadline | Last day to request absentee ballot

ONLY extract actionable events, deadlines, and milestones.
Do NOT extract general text or section headers.
"""
        
        try:
            response = self.llm_client.generate_structured_content(
                prompt=extraction_prompt,
                response_schema=TimelineExtraction,
                temperature=0.1,
                system_instruction="You are an expert at extracting structured data from campaign timelines. Extract only specific events, milestones, and deadlines with dates."
            )
            
            self.logger.info(f"Extracted {len(response.events)} timeline events")
            return response
            
        except Exception as e:
            self.logger.error(f"Failed to extract timeline events: {str(e)}")
            return TimelineExtraction(events=[])
    
    def _create_contact_task(self, contact: ExtractedContact, campaign_info: CleanedCampaignInfo) -> CampaignTask:
        """Convert extracted contact to CampaignTask"""
        
        # Parse the date
        parsed_date = self._parse_campaign_date(contact.date_string, campaign_info.election_date)
        
        # Determine task properties
        flow_type = CampaignTaskType.TEXT if contact.contact_type.lower() == "text" else CampaignTaskType.ROBOCALL
        template_theme = self._determine_template_theme(contact.message_theme.lower())
        
        # Calculate week number
        week_number = self._calculate_week_number(parsed_date, campaign_info.election_date)
        
        # Generate appropriate title
        title = self._generate_contact_task_title(contact.contact_type, template_theme, contact.sequence_number)
        
        return CampaignTask(
            title=title,
            description=contact.message_theme,
            cta="Schedule",
            flowType=flow_type,
            week=week_number,
            proRequired=True,
            date=parsed_date.isoformat(),
            deadline=self._calculate_deadline(parsed_date, campaign_info.election_date),
            defaultAiTemplateId=contact.template_id  # Use LLM-selected template ID
        )
    
    def _create_timeline_task(self, event: ExtractedTimelineEvent, campaign_info: CleanedCampaignInfo) -> CampaignTask:
        """Convert extracted timeline event to CampaignTask"""
        
        # Parse the date
        parsed_date = self._parse_campaign_date(event.date_string, campaign_info.election_date)
        
        # Categorize event type
        flow_type, cta, template_theme = self._categorize_event(event.event_name)
        
        # Calculate week number
        week_number = self._calculate_week_number(parsed_date, campaign_info.election_date)
        
        # Generate appropriate title
        title = self._generate_timeline_task_title(event.event_name)
        
        return CampaignTask(
            title=title,
            description=event.event_purpose,
            cta=cta,
            flowType=flow_type,
            week=week_number,
            proRequired=flow_type in [CampaignTaskType.TEXT, CampaignTaskType.ROBOCALL],
            date=parsed_date.isoformat(),
            deadline=self._calculate_deadline(parsed_date, campaign_info.election_date) if flow_type != CampaignTaskType.EVENTS else None,
            defaultAiTemplateId=event.template_id  # Use LLM-selected template ID
        )
    
    def _parse_campaign_date(self, date_string: str, election_date: date) -> date:
        """Parse date string in context of campaign election date"""
        # Clean the date string
        date_str = date_string.strip().upper().replace('[', '').replace(']', '')
        
        # Try different date formats
        formats = [
            "%B %d",      # JULY 15
            "%b %d",      # JUL 15
            "%m/%d",      # 7/15
            "%B %d, %Y",  # JULY 15, 2025
        ]
        
        for fmt in formats:
            try:
                parsed = datetime.strptime(date_str, fmt)
                # If no year specified, use election year
                if parsed.year == 1900:
                    parsed = parsed.replace(year=election_date.year)
                
                # If parsed date is significantly before today, assume next year
                parsed_date = parsed.date()
                if parsed_date < (date.today() - timedelta(days=30)):
                    parsed_date = parsed_date.replace(year=election_date.year)
                
                return parsed_date
                
            except ValueError:
                continue
        
        # Fallback: return a date relative to election
        self.logger.warning(f"Could not parse date: {date_string}, using election date - 30 days")
        return election_date - timedelta(days=30)
    
    def _determine_template_theme(self, description: str) -> TemplateTheme:
        """Determine template theme from task description"""
        description_lower = description.lower()
        
        if any(word in description_lower for word in ["intro", "introduction", "candidate intro"]):
            return TemplateTheme.INTRO
        elif any(word in description_lower for word in ["gotv", "vote", "election day", "reminder", "polling"]):
            return TemplateTheme.GOTV
        elif any(word in description_lower for word in ["persuasion", "persuasive", "contrast", "experience"]):
            return TemplateTheme.PERSUASIVE
        elif any(word in description_lower for word in ["ballot", "early voting", "mail", "absentee"]):
            return TemplateTheme.EARLY_VOTING
        else:
            return TemplateTheme.PERSUASIVE  # Default
    
    def _categorize_event(self, event_name: str) -> Tuple[CampaignTaskType, str, TemplateTheme]:
        """Categorize timeline event and return (flow_type, cta, template_theme)"""
        event_lower = event_name.lower()
        
        if "launch" in event_lower:
            return CampaignTaskType.EVENTS, "Plan event", TemplateTheme.LAUNCH
        elif any(word in event_lower for word in ["social", "post", "media"]):
            return CampaignTaskType.SOCIAL_MEDIA, "Write post", TemplateTheme.EVENT_PROMOTION
        elif any(word in event_lower for word in ["meeting", "town hall", "forum", "debate"]):
            return CampaignTaskType.EVENTS, "Attend event", TemplateTheme.COMMUNITY_ENGAGEMENT
        elif "deadline" in event_lower:
            return CampaignTaskType.EDUCATION, "Get reminder", TemplateTheme.DEADLINE_REMINDER
        else:
            return CampaignTaskType.EVENTS, "Plan event", TemplateTheme.COMMUNITY_ENGAGEMENT
    
    def _generate_contact_task_title(self, contact_type: str, theme: TemplateTheme, sequence: int) -> str:
        """Generate appropriate task title for contact tasks"""
        contact_word = "text message" if "text" in contact_type.lower() else "robocall"
        
        theme_map = {
            TemplateTheme.INTRO: "introduction",
            TemplateTheme.PERSUASIVE: "persuasive", 
            TemplateTheme.GOTV: "election day reminder",
            TemplateTheme.EARLY_VOTING: "early voting"
        }
        
        theme_word = theme_map.get(theme, "campaign")
        return f"Schedule your {theme_word} {contact_word}"
    
    def _generate_timeline_task_title(self, event_name: str) -> str:
        """Generate appropriate task title for timeline events"""
        if "launch" in event_name.lower():
            return "Host campaign launch event"
        elif "deadline" in event_name.lower():
            return f"Note: {event_name}"
        elif any(word in event_name.lower() for word in ["meeting", "town hall", "forum"]):
            return f"Attend {event_name.lower()}"
        else:
            return f"Plan for {event_name.lower()}"
    
    def _calculate_week_number(self, task_date: date, election_date: date) -> int:
        """
        Calculate week number based on election date using deterministic math.
        
        Week 1 = Election week (0-6 days before election)  
        Week 2 = 7-13 days before election
        Week 3 = 14-20 days before election, etc.
        """
        days_until = (election_date - task_date).days
        
        # Handle past dates (shouldn't happen but be safe)
        if days_until < 0:
            return 1
            
        # Calculate week: Week 1 = 0-6 days, Week 2 = 7-13 days, etc.
        return (days_until // 7) + 1
    
    def _calculate_deadline(self, task_date: date, election_date: date) -> Optional[int]:
        """Calculate deadline (days before election when task expires)"""
        days_until = (election_date - task_date).days
        return max(0, days_until) if days_until >= 0 else None


# Import uuid at module level

if __name__ == "__main__":
    import asyncio
    from ai_generated_campaign_plan.schema.models import CampaignInfo, RaceType, IncumbentStatus, CleanedCampaignInfo
    
    async def test_structured_extractor():
        """Test the structured extractor with sample campaign content"""
        
        print("🧪 Testing AI Task Structured Extractor")
        print("=" * 50)
        
        # Sample campaign info
        campaign_info = CleanedCampaignInfo(
            candidate_name="Sarah Johnson",
            primary_date=None,
            election_date=date(2025, 11, 5),
            office_and_jurisdiction="School Board, At-Large, Chicopee, MA",
            incumbent_status=IncumbentStatus.NOT_APPLICABLE,
            race_type=RaceType.NONPARTISAN,
            seats_available=3,
            number_of_opponents=5,
            win_number=2500,
            total_likely_voters=8500,
            available_cell_phones=1200,
            available_landlines=300,
            city="Chicopee",
            state="MA",
            state_full="Massachusetts",
            election_date_formatted="2025-11-05",
            has_primary=False,
            primary_date_formatted=None
        )
        
        # Sample timeline section (Section 3)
        timeline_section = """## 3. CAMPAIGN TIMELINE

- July 15 | Campaign Launch Event | Official campaign announcement
- August 1 | Ballot Request Deadline | Last day to request absentee ballot  
- August 10 | Town Hall Meeting | Community engagement and visibility
- September 15 | Primary Election Day | Primary voting day
- October 1 | Candidate Forum | Voter education and comparison
- November 5 | Election Day | Final day of voting"""
        
        # Sample voter contact section (Section 6)
        voter_contact_section = """## 6. VOTER CONTACT PLAN

### Core Tactics (Chronological)

- [JULY 15] – P2P Text #1: Candidate intro and vote-by-mail awareness
- [JULY 25] – Robocall #1: Ballot arrival and early voting prompt  
- [AUGUST 10] – P2P Text #2: Experience and contrast message
- [AUGUST 25] – Robocall #2: Vote return and community message
- [SEPTEMBER 5] – P2P Text #3: Persuasion and vote planning
- [SEPTEMBER 10] – Robocall #3: Final GOTV push and polling info
- [SEPTEMBER 12] – P2P Text #4: Final GOTV reminder
- [NOVEMBER 4] – P2P Text #5: Final reminder and polling location link"""
        
        # Initialize extractor
        extractor = StructuredTaskExtractor()
        
        try:
            print("📝 Extracting tasks from campaign plan sections...")
            
            # Extract tasks
            result = await extractor.extract_tasks_from_sections(
                timeline_section=timeline_section,
                voter_contact_section=voter_contact_section,
                campaign_info=campaign_info
            )
            
            # Display results
            print(f"\n✅ Extraction {'SUCCESS' if result.success else 'FAILED'}")
            print(f"📊 Generated {result.extracted_count} tasks, skipped {result.skipped_count}")
            
            if result.errors:
                print(f"⚠️  Errors: {result.errors}")
            
            print(f"\n📋 Generated Tasks ({len(result.tasks)} total):")
            print("=" * 90)
            
            for i, task in enumerate(result.tasks, 1):
                print(f"\n{i:2d}. {task.title}")
                print(f"    Title: {task.title}")
                print(f"    Description: {task.description}")
                print(f"    CTA: {task.cta}")
                print(f"    Flow Type: {task.flowType.value}")
                print(f"    Week: {task.week}")
                print(f"    Date: {task.date}")
                print(f"    Default AI Template ID: {task.defaultAiTemplateId}")
                print("-" * 90)
            
            # Show breakdown by type
            type_counts = {}
            for task in result.tasks:
                type_counts[task.flowType.value] = type_counts.get(task.flowType.value, 0) + 1
            
            print("📈 Task Breakdown:")
            for task_type, count in sorted(type_counts.items()):
                print(f"  • {task_type}: {count} tasks")
            
            # Show template mapping results (from LLM-based selection)
            mapped_count = sum(1 for task in result.tasks if task.defaultAiTemplateId)
            mapping_rate = mapped_count / len(result.tasks) if result.tasks else 0
            print(f"\n🔗 LLM Template Selection Results:")
            print(f"✅ Template Selection: {mapped_count}/{len(result.tasks)} tasks ({mapping_rate:.1%} success rate)")
            
            print(f"\n🔍 Complete JSON Output (first 3 tasks without templates):")
            print("=" * 90)
            import json
            for i, task in enumerate(result.tasks, 1):
                task_dict = task.model_dump()
                print(f"\nTask {i} JSON:")
                print(json.dumps(task_dict, indent=2, default=str))
                
            return result.success
            
        except Exception as e:
            print(f"❌ Error during extraction: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    # Run the test
    print("Starting structured extractor test...")
    success = asyncio.run(test_structured_extractor())
    
    if success:
        print("\n🎉 Test completed successfully!")
    else:
        print("\n💥 Test failed!")
    
    exit(0 if success else 1)