import re
import json
from datetime import date, datetime
from typing import Dict, Any, List
from ai_generated_campaign_plan.schema.models import CampaignInfo
from shared.logger import get_logger

logger = get_logger(__name__)

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
    
    def extract_json(self, campaign_plan_text: str, campaign_info: CampaignInfo) -> Dict[str, Any]:
        """
        Convert campaign plan text to structured JSON format.
        
        Args:
            campaign_plan_text: The full campaign plan text
            campaign_info: Campaign information object
            
        Returns:
            Dict containing structured campaign plan data
        """
        try:
            logger.info("Starting JSON extraction from campaign plan text")
            
            # Split the plan into sections
            sections = self._parse_sections(campaign_plan_text)
            logger.info(f"Parsed {len(sections)} sections from campaign plan")
            
            # Extract tasks from relevant sections
            timeline_tasks = self._parse_timeline_tasks(sections.get(3, ""))
            voter_contact_tasks = self._parse_voter_contact_tasks(sections.get(6, ""))
            
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
            section_match = re.match(r'^#+\s*(\d+)\.\s*(.+)', line.strip())
            
            if section_match:
                # Save previous section if exists
                if current_section is not None and current_content:
                    sections[current_section] = '\n'.join(current_content)
                
                # Start new section
                section_num = int(section_match.group(1))
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
    
    def _parse_timeline_tasks(self, timeline_content: str) -> List[Dict[str, Any]]:
        """Parse timeline tasks from section 3 content into structured format."""
        tasks = []
        lines = timeline_content.split('\n')
        
        for line in lines:
            line = line.strip()
            # Look for lines matching: - Month DD | Event | Purpose
            if line.startswith('- ') and ' | ' in line:
                try:
                    parts = line[2:].split(' | ')  # Remove '- ' prefix
                    if len(parts) >= 3:
                        date_str = parts[0].strip()
                        event = parts[1].strip()
                        purpose = parts[2].strip()
                        
                        # Try to parse the date
                        parsed_date = self._parse_date_string(date_str)
                        
                        tasks.append({
                            "date": date_str,
                            "parsed_date": parsed_date.isoformat() if parsed_date else None,
                            "title": event,
                            "description": purpose,
                            "type": "timeline",
                            "category": "campaign_timeline"
                        })
                except Exception as e:
                    logger.warning(f"Failed to parse timeline task: {line}. Error: {str(e)}")
                    continue
        
        return tasks
    
    def _parse_voter_contact_tasks(self, contact_content: str) -> List[Dict[str, Any]]:
        """Parse voter contact tasks from section 6 content into structured format."""
        tasks = []
        lines = contact_content.split('\n')
        
        for line in lines:
            line = line.strip()
            # Look for lines matching: - [MONTH DD] – Contact Type: Message
            if line.startswith('- [') and '] –' in line:
                try:
                    # Extract date from brackets
                    date_start = line.find('[') + 1
                    date_end = line.find(']')
                    
                    if date_end > date_start:
                        date_str = line[date_start:date_end].strip()
                        
                        # Extract the rest after ] –
                        rest_start = line.find('] –') + 3
                        rest_content = line[rest_start:].strip()
                        
                        # Split on first colon to separate contact type and message
                        if ':' in rest_content:
                            contact_type, message = rest_content.split(':', 1)
                            contact_type = contact_type.strip()
                            message = message.strip()
                        else:
                            contact_type = rest_content
                            message = ""
                        
                        # Try to parse the date
                        parsed_date = self._parse_date_string(date_str)
                        
                        tasks.append({
                            "date": date_str,
                            "parsed_date": parsed_date.isoformat() if parsed_date else None,
                            "title": contact_type,
                            "description": message,
                            "type": "voter_contact",
                            "category": "voter_outreach",
                            "contact_method": self._categorize_contact_method(contact_type)
                        })
                except Exception as e:
                    logger.warning(f"Failed to parse voter contact task: {line}. Error: {str(e)}")
                    continue
        
        return tasks
    
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
    
    def _categorize_contact_method(self, contact_type: str) -> str:
        """Categorize contact method based on contact type string."""
        contact_type_lower = contact_type.lower()
        
        if any(word in contact_type_lower for word in ['text', 'sms', 'p2p']):
            return "text_message"
        elif any(word in contact_type_lower for word in ['call', 'phone', 'robocall']):
            return "phone_call"
        elif any(word in contact_type_lower for word in ['mail', 'postcard', 'letter']):
            return "direct_mail"
        elif any(word in contact_type_lower for word in ['door', 'canvass', 'knock']):
            return "door_to_door"
        elif any(word in contact_type_lower for word in ['digital', 'online', 'social', 'facebook', 'instagram']):
            return "digital_outreach"
        elif any(word in contact_type_lower for word in ['event', 'rally', 'meet']):
            return "event"
        else:
            return "other"
    
    def save_json_to_file(self, json_data: Dict[str, Any], file_path: str) -> None:
        """Save JSON data to file with pretty formatting."""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            logger.info(f"JSON data saved to {file_path}")
        except Exception as e:
            logger.error(f"Failed to save JSON to {file_path}: {str(e)}")
            raise