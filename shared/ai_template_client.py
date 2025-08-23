"""
AI Template Client for fetching and managing campaign task templates from GP API.
"""

import requests
from typing import Dict, List, Optional
from shared.logger import get_logger

logger = get_logger(__name__)

class AITemplateClient:
    """Client for fetching AI content templates from GP API."""
    
    def __init__(self):
        self.base_url = "https://gp-api.goodparty.org/v1/content/type/aiContentTemplate"
        self.templates_cache = None
        self.template_name_to_id_map = None
    
    def fetch_templates(self) -> List[Dict]:
        """Fetch all AI templates from GP API."""
        try:
            logger.info("Fetching AI templates from GP API")
            response = requests.get(self.base_url, timeout=30)
            response.raise_for_status()
            
            templates = response.json()
            logger.info(f"Successfully fetched {len(templates)} AI templates")
            
            # Cache the templates
            self.templates_cache = templates
            self._build_name_to_id_map()
            
            return templates
            
        except requests.RequestException as e:
            logger.error(f"Failed to fetch AI templates: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Error processing AI templates: {str(e)}")
            return []
    
    def _build_name_to_id_map(self):
        """Build a mapping from template names to IDs for easy lookup."""
        if not self.templates_cache:
            return
        
        self.template_name_to_id_map = {}
        for template in self.templates_cache:
            template_data = template.get('data', {})
            template_name = template_data.get('name', '')
            template_id = template.get('id', '')
            
            if template_name and template_id:
                self.template_name_to_id_map[template_name.lower()] = template_id
        
        logger.info(f"Built template name-to-ID map with {len(self.template_name_to_id_map)} entries")
    
    def get_template_id_by_name(self, template_name: str) -> Optional[str]:
        """Get template ID by name (case-insensitive)."""
        if not self.template_name_to_id_map:
            self.fetch_templates()
        
        return self.template_name_to_id_map.get(template_name.lower())
    
    def get_campaign_task_template_mappings(self) -> Dict[str, str]:
        """
        Get template ID mappings for campaign tasks based on known template names.
        Returns a mapping of task descriptions to template IDs.
        """
        if not self.templates_cache:
            self.fetch_templates()
        
        # Known template name mappings for campaign tasks
        template_mappings = {
            # Week 1 - Election Day GOTV
            "election_day_text": "SMS Election Day",
            "election_day_robocall": "Robocall Election Day",
            "gotv_door_knocking": "Door Knocking Get Out The Vote",
            "gotv_phone_banking": "Phone Banking Get Out The Vote",
            "gotv_social_media": "Social Media Copy",  # Use general social media template
            
            # Week 2 - Final GOTV + Persuasion
            "persuasive_text": "SMS Persuasive",
            "persuasive_robocall": "Robocall Persuasive",
            "social_media_qa": "Social Media Copy",  # Use general social media template
            
            # Week 4 - 1 Month Out
            "one_month_text": "SMS 1 Month Until Election",
            "one_month_robocall": "Robocall 1 Month Until Election",
            
            # Persuasion Phase
            "persuasive_door_knocking": "Door Knocking Persuasive",
            "persuasive_phone_banking": "Phone Banking Persuasive",
            "issue_social_media": "Social Post Top Issues",  # Use actual template name
            "event_calendar_social": "Social Media Copy",  # Use general social media template
            
            # Voter ID Phase
            "voter_id_door_knocking": "Door Knocking Voter ID",
            "voter_id_phone_banking": "Phone Banking Voter ID",
            "community_social_media": "Social Media Copy",  # Use general social media template
            
            # General templates
            "launch_social_media": "Launch Social Media Copy",
            "launch_speech": "Launch Speech",
            "candidate_website": "Candidate Website",
        }
        
        # Map template names to IDs
        task_template_ids = {}
        for task_key, template_name in template_mappings.items():
            template_id = self.get_template_id_by_name(template_name)
            if template_id:
                task_template_ids[task_key] = template_id
                logger.debug(f"Mapped {task_key} -> {template_name} -> {template_id}")
            else:
                logger.warning(f"Template not found: {template_name}")
        
        return task_template_ids
    
    def get_template_id_for_task(self, task_category: str, task_week: int, task_description: str = "") -> Optional[str]:
        """
        Get the appropriate template ID for a specific campaign task.
        
        Args:
            task_category: The task category (text, robocall, doorKnocking, etc.)
            task_week: Campaign week number (1-9)
            task_description: Optional task description for more specific matching
            
        Returns:
            Template ID if found, None otherwise
        """
        template_mappings = self.get_campaign_task_template_mappings()
        
        # Week-specific mappings
        if task_week == 1:  # Election Day
            if task_category == "text":
                return template_mappings.get("election_day_text")
            elif task_category == "robocall":
                return template_mappings.get("election_day_robocall")
            elif task_category == "doorKnocking":
                return template_mappings.get("gotv_door_knocking")
            elif task_category == "phoneBanking":
                return template_mappings.get("gotv_phone_banking")
            elif task_category == "socialMedia":
                return template_mappings.get("gotv_social_media")
        
        elif task_week == 2:  # Final GOTV + Persuasion
            if task_category == "text":
                return template_mappings.get("persuasive_text")
            elif task_category == "robocall":
                return template_mappings.get("persuasive_robocall")
            elif task_category == "doorKnocking":
                return template_mappings.get("gotv_door_knocking")
            elif task_category == "phoneBanking":
                return template_mappings.get("gotv_phone_banking")
            elif task_category == "socialMedia":
                return template_mappings.get("social_media_qa")
        
        elif task_week == 4:  # 1 Month Out
            if task_category == "text":
                return template_mappings.get("one_month_text")
            elif task_category == "robocall":
                return template_mappings.get("one_month_robocall")
            elif task_category == "doorKnocking":
                return template_mappings.get("persuasive_door_knocking")
            elif task_category == "phoneBanking":
                return template_mappings.get("persuasive_phone_banking")
            elif task_category == "socialMedia":
                return template_mappings.get("issue_social_media")
        
        elif task_week in [3, 5, 6]:  # Persuasion Phase
            if task_category == "doorKnocking":
                return template_mappings.get("persuasive_door_knocking")
            elif task_category == "phoneBanking":
                return template_mappings.get("persuasive_phone_banking")
            elif task_category == "socialMedia":
                if task_week == 6:
                    return template_mappings.get("event_calendar_social")
                else:
                    return template_mappings.get("issue_social_media")
        
        elif task_week in [7, 8]:  # Voter ID Phase
            if task_category == "doorKnocking":
                return template_mappings.get("voter_id_door_knocking")
            elif task_category == "phoneBanking":
                return template_mappings.get("voter_id_phone_banking")
            elif task_category == "socialMedia":
                return template_mappings.get("community_social_media")
        
        # Default fallback for categories that require templates
        if task_category in ["text", "robocall", "doorKnocking", "phoneBanking", "socialMedia"]:
            logger.warning(f"No specific template found for {task_category} in week {task_week}")
            # Return a general template based on category
            if task_category == "text":
                return template_mappings.get("persuasive_text")
            elif task_category == "robocall":
                return template_mappings.get("persuasive_robocall")
            elif task_category == "doorKnocking":
                return template_mappings.get("persuasive_door_knocking")
            elif task_category == "phoneBanking":
                return template_mappings.get("persuasive_phone_banking")
            elif task_category == "socialMedia":
                return template_mappings.get("issue_social_media")
        
        return None
    
    def list_available_templates(self) -> List[Dict[str, str]]:
        """List all available templates with their names and IDs."""
        if not self.templates_cache:
            self.fetch_templates()
        
        template_list = []
        for template in self.templates_cache or []:
            template_data = template.get('data', {})
            template_list.append({
                'id': template.get('id', ''),
                'name': template_data.get('name', ''),
                'category': template_data.get('category', {}).get('fields', {}).get('title', 'Unknown')
            })
        
        return template_list


# Global instance for easy access
ai_template_client = AITemplateClient()
