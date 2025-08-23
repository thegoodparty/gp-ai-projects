import json
from datetime import date, datetime
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from ai_generated_campaign_plan.schema.models import CampaignInfo
from shared.logger import get_logger
from shared.llm_gemini import GeminiClient

logger = get_logger(__name__)

# Pydantic schemas for structured task generation
class TaskCategory:
    """Constants for task categories"""
    TEXT = "text"
    ROBOCALL = "robocall"
    DOOR_KNOCKING = "doorKnocking"
    PHONE_BANKING = "phoneBanking"
    SOCIAL_MEDIA = "socialMedia"
    EXTERNAL_LINK = "externalLink"
    GENERAL = "general"
    WEBSITE = "website"
    COMPLIANCE = "compliance"
    UPGRADE_TO_PRO = "upgradeToPro"
    PROFILE = "profile"

class TaskItem(BaseModel):
    """Single task item with proper categorization"""
    date: str = Field(description="Date string (e.g., 'August 19, 2025')")
    title: str = Field(description="Task title")
    description: str = Field(description="Task description/purpose")
    category: str = Field(description="Task category from the enum: text, robocall, doorKnocking, phoneBanking, socialMedia, externalLink, general, website, compliance, upgradeToPro, profile")
    link: Optional[str] = Field(default=None, description="External link for events/activities (real URLs, not generic searches)")
    contact_method: Optional[str] = Field(default=None, description="For voter contact tasks: text_message, phone_call, direct_mail, door_to_door, digital_outreach, event")

class TaskList(BaseModel):
    """List of structured tasks"""
    tasks: List[TaskItem] = Field(description="List of campaign tasks")

class CampaignPlanJSONExtractor:
    """
    Extracts structured JSON data from campaign plan text. 
    the logic was derived from https://github.com/thegoodparty/gp-ai-projects/blob/cb7b0246d15bf3b42cdd07adb7e0ae16f2e63b95/api_wrapper.py
    we should consider using llm.structured output to generate tasks instead of classical regex parsing.
    """
    
    def __init__(self):
        self.section_names = {
            1: "overview",
            2: "strategic_landscape_electoral_goals", 
            3: "campaign_timeline",
            4: "recommended_total_budget",
            5: "know_your_community",
            6: "voter_contact_plan"
        }
        self.gemini_client = GeminiClient()
    
    def extract_json(self, campaign_plan_text: str, campaign_info: CampaignInfo, 
                     timeline_tasks: List[Dict[str, Any]] = None,
                     voter_contact_tasks: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Convert campaign plan text to structured JSON format.
        
        Args:
            campaign_plan_text: The full campaign plan text
            campaign_info: Campaign information object
            timeline_tasks: Pre-generated structured timeline tasks
            voter_contact_tasks: Pre-generated structured voter contact tasks
            
        Returns:
            Dict containing structured campaign plan data
        """
        try:
            logger.info("Starting JSON extraction from campaign plan text")
            
            # Split the plan into sections
            sections = self._parse_sections(campaign_plan_text)
            logger.info(f"Parsed {len(sections)} sections from campaign plan")
            
            # Use provided structured tasks or fallback to empty lists
            if timeline_tasks is None:
                timeline_tasks = []
            if voter_contact_tasks is None:
                voter_contact_tasks = []
            
            # Tasks now come with all required fields from the generators
            
            logger.info(f"Extracted {len(timeline_tasks)} timeline tasks and {len(voter_contact_tasks)} voter contact tasks")
            
            # Build comprehensive JSON response
            json_response = {
                "campaign_info": {
                    "candidate_name": campaign_info.candidate_name,
                    "office_and_jurisdiction": campaign_info.office_and_jurisdiction,
                    "election_date": campaign_info.election_date.isoformat(),
                    "primary_date": campaign_info.primary_date.isoformat() if campaign_info.primary_date else None,
                    "race_type": campaign_info.race_type.value,
                    "incumbent_status": campaign_info.incumbent_status.value if campaign_info.incumbent_status else None,
                    "seats_available": campaign_info.seats_available,
                    "number_of_opponents": campaign_info.number_of_opponents,
                    "win_number": campaign_info.win_number,
                    "total_likely_voters": campaign_info.total_likely_voters,
                    "available_cell_phones": campaign_info.available_cell_phones,
                    "available_landlines": campaign_info.available_landlines,
                    "additional_race_context": campaign_info.additional_race_context,
                    "generated_date": date.today().isoformat()
                },
                "sections": {},
                "tasks": {
                    "timeline": timeline_tasks,
                    "voter_contact": voter_contact_tasks,
                    "all_tasks": timeline_tasks + voter_contact_tasks,
                    "total_count": len(timeline_tasks) + len(voter_contact_tasks)
                },
                "metadata": {
                    "format_version": "1.0",
                    "extraction_date": datetime.now().isoformat(),
                    "sections_count": len(sections),
                    "total_tasks": len(timeline_tasks) + len(voter_contact_tasks)
                }
            }
            
            # Add all sections with their content in markdown format
            for section_num, content in sections.items():
                section_key = self.section_names.get(section_num, f"section_{section_num}")
                json_response["sections"][section_key] = content.strip()
            
            logger.info("Successfully completed JSON extraction")
            return json_response
            
        except Exception as e:
            logger.error(f"Error during JSON extraction: {str(e)}")
            raise RuntimeError(f"JSON extraction failed: {str(e)}")
    
    def _parse_sections(self, campaign_plan_text: str) -> Dict[int, str]:
        """Parse campaign plan text into numbered sections."""
        sections = {}
        current_section = None
        current_content = []
        
        lines = campaign_plan_text.split('\n')
        
        for line in lines:
            # Check if this is a section header (## N. SECTION NAME or # N. SECTION NAME)
            line_stripped = line.strip()
            if line_stripped.startswith('#') and '.' in line_stripped:
                # Extract section number from pattern like "## 1. SECTION NAME"
                try:
                    hash_end = 0
                    while hash_end < len(line_stripped) and line_stripped[hash_end] == '#':
                        hash_end += 1
                    
                    content = line_stripped[hash_end:].strip()
                    if content and content[0].isdigit():
                        dot_pos = content.find('.')
                        if dot_pos > 0:
                            section_num = int(content[:dot_pos])
                            section_match = True
                        else:
                            section_match = False
                    else:
                        section_match = False
                except (ValueError, IndexError):
                    section_match = False
            else:
                section_match = False
            
            if section_match:
                # Save previous section if exists
                if current_section is not None and current_content:
                    sections[current_section] = '\n'.join(current_content)
                
                # Start new section
                current_section = section_num
                current_content = [line]
            else:
                # Add line to current section
                if current_section is not None:
                    current_content.append(line)
        
        # Save last section
        if current_section is not None and current_content:
            sections[current_section] = '\n'.join(current_content)
        
        return sections
    
    def _parse_date_string(self, date_str: str) -> date:
        """
        Parse date string like 'July 15' into a date object.
        Uses campaign election year for context.
        """
        try:
            # For now, use current year (TODO: Fix to use campaign year)
            current_year = date.today().year
            date_with_year = f"{date_str}, {current_year}"
            return datetime.strptime(date_with_year, "%B %d, %Y").date()
        except:
            # Try alternative formats
            try:
                return datetime.strptime(date_str, "%m/%d").date().replace(year=current_year)
            except:
                logger.warning(f"Could not parse date: {date_str}")
                return None
    
    def save_json_to_file(self, json_data: Dict[str, Any], file_path: str) -> None:
        """Save JSON data to file with pretty formatting."""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            logger.info(f"JSON data saved to {file_path}")
        except Exception as e:
            logger.error(f"Failed to save JSON to {file_path}: {str(e)}")
            raise