"""
Utility functions for extracting tasks from campaign plan section content.
"""

import json
import re
from typing import List, Dict, Any, Optional
from shared.logger import get_logger

from datetime import datetime, date

logger = get_logger(__name__)

def convert_date_to_weeks_from_election(date_str: str, election_date: date = None) -> int:
    """
    Convert a date string to weeks from election date.
    
    Args:
        date_str: Date string in format like "Aug 25, 2025" or "2025-11-05"
        election_date: Election date (defaults to Nov 5, 2025 if not provided)
        
    Returns:
        Integer representing weeks from election date
    """
    if not election_date:
        # Default election date for testing
        election_date = date(2025, 11, 5)
    
    try:
        # Try parsing different date formats
        task_date = None
        
        # Format: "Aug 25, 2025"
        try:
            task_date = datetime.strptime(date_str, "%b %d, %Y").date()
        except ValueError:
            pass
        
        # Format: "2025-11-05"
        if not task_date:
            try:
                task_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                pass
        
        # Format: "August 25, 2025"
        if not task_date:
            try:
                task_date = datetime.strptime(date_str, "%B %d, %Y").date()
            except ValueError:
                pass
        
        if task_date:
            # Calculate weeks difference
            days_diff = (election_date - task_date).days
            weeks_diff = max(1, round(days_diff / 7))  # Minimum 1 week
            return weeks_diff
        else:
            logger.warning(f"Could not parse date: {date_str}")
            return 1  # Default to 1 week
            
    except Exception as e:
        logger.warning(f"Error converting date {date_str} to weeks: {str(e)}")
        return 1  # Default to 1 week

def extract_tasks_from_section_content(section_content: str, section_name: str) -> List[Dict[str, Any]]:
    """
    Extract tasks from section content that contains embedded JSON.
    Enhanced to handle truncated JSON from AI generation failures.
    
    Args:
        section_content: The section content that may contain JSON with tasks
        section_name: Name of the section for logging purposes
        
    Returns:
        List of task dictionaries
    """
    tasks = []
    
    try:
        # Handle Joan Borker (7) style severely truncated content - extract from markdown bullet points
        if ('Sep 24,' in section_content or 'Social Media Post:' in section_content) and '|' in section_content:
            logger.info(f"Detected severely truncated content in {section_name}, attempting markdown extraction")
            
            # Extract bullet points in format: - Aug 23, 2025 | Title | Description
            bullet_pattern = r'- ([A-Z][a-z]{2} \d{2}, \d{4}) \| ([^|]+) \| ([^\\n]+)'
            bullet_matches = re.findall(bullet_pattern, section_content)
            
            for match in bullet_matches:
                date_str, title, description = match
                try:
                    # Convert to task format
                    task = {
                        "date": date_str.strip(),
                        "title": title.strip(),
                        "description": description.strip(),
                        "cta": "Schedule",
                        "type": "general",
                        "category": "general",
                        "deadline": 1,  # Default to 1 week from election
                        "link": "",
                        "week": 1,  # Default week

                        "proRequired": False
                    }
                    tasks.append(task)
                except Exception as e:
                    logger.warning(f"Failed to parse bullet point task: {str(e)}")
                    continue
            
            if tasks:
                logger.info(f"Extracted {len(tasks)} tasks from markdown bullets in {section_name}")
                return tasks
        
        # First, try the enhanced extraction for truncated content (like Joan Borker 5)
        tasks_pattern = r'```json\s*\{\s*"tasks":\s*\[(.*?)(?:\]\s*\}|$)'
        tasks_match = re.search(tasks_pattern, section_content, re.DOTALL)
        
        if tasks_match:
            tasks_content = tasks_match.group(1)
            logger.info(f"Found tasks content in {section_name}, length: {len(tasks_content)}")
            
            # Try to reconstruct the JSON by adding proper closing
            full_json = '{"tasks": [' + tasks_content + ']}'
            
            # If it ends abruptly, try to close the last task object
            if not tasks_content.strip().endswith('}'):
                # Find the last incomplete task and try to close it
                lines = tasks_content.split('\n')
                for i in range(len(lines) - 1, -1, -1):
                    line = lines[i].strip()
                    if line and not line.endswith(',') and not line.endswith('}'):
                        # This looks like an incomplete line, remove it
                        lines = lines[:i]
                        break
                
                # Reconstruct without the incomplete part
                cleaned_content = '\n'.join(lines)
                if cleaned_content.strip().endswith(','):
                    cleaned_content = cleaned_content.strip()[:-1]  # Remove trailing comma
                
                full_json = '{"tasks": [' + cleaned_content + ']}'
            
            try:
                parsed_json = json.loads(full_json)
                if 'tasks' in parsed_json:
                    logger.info(f"Successfully parsed {len(parsed_json['tasks'])} tasks from {section_name}")
                    for task_data in parsed_json['tasks']:
                        if isinstance(task_data, dict) and 'date' in task_data and 'title' in task_data:
                            tasks.append(task_data)
                    return tasks
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse reconstructed JSON: {str(e)}")
                
                # Try extracting individual task objects with more flexible patterns
                # Pattern for complete task objects
                task_pattern = r'\{\s*"date":\s*"[^"]+",\s*"title":\s*"[^"]+",.*?\}'
                individual_tasks = re.findall(task_pattern, tasks_content, re.DOTALL)
                
                logger.info(f"Found {len(individual_tasks)} complete task objects in {section_name}")
                
                for task_str in individual_tasks:
                    try:
                        task = json.loads(task_str)
                        if 'date' in task and 'title' in task:
                            tasks.append(task)
                    except json.JSONDecodeError:
                        # Try to fix common issues in this specific task
                        fixed_task = task_str
                        
                        # Fix unterminated strings
                        if fixed_task.count('"') % 2 == 1:
                            fixed_task = fixed_task + '"'
                        
                        # Ensure proper closing
                        if not fixed_task.endswith('}'):
                            fixed_task = fixed_task + '}'
                        
                        try:
                            task = json.loads(fixed_task)
                            if 'date' in task and 'title' in task:
                                tasks.append(task)
                        except json.JSONDecodeError:
                            continue
                        
                if tasks:
                    return tasks
        
        # Fallback to original method for complete JSON blocks
        json_pattern = r'```json\s*(\{.*?\})\s*```'
        json_matches = re.findall(json_pattern, section_content, re.DOTALL)
        
        for json_content in json_matches:
            try:
                parsed_json = json.loads(json_content)
                
                # Look for tasks in the parsed JSON
                if 'tasks' in parsed_json and isinstance(parsed_json['tasks'], list):
                    logger.info(f"Found {len(parsed_json['tasks'])} tasks in {section_name} JSON content")
                    
                    for task_data in parsed_json['tasks']:
                        if isinstance(task_data, dict):
                            # Ensure required fields exist
                            if 'date' in task_data and 'title' in task_data:
                                tasks.append(task_data)
                            else:
                                logger.warning(f"Skipping invalid task in {section_name}: missing required fields")
                        else:
                            logger.warning(f"Skipping non-dict task in {section_name}")
                
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON in {section_name}: {str(e)}")
                continue
            except Exception as e:
                logger.warning(f"Error processing JSON in {section_name}: {str(e)}")
                continue
    
    except Exception as e:
        logger.error(f"Error extracting tasks from {section_name}: {str(e)}")
    
    return tasks

def extract_tasks_from_campaign_sections(sections: Dict[str, str]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Extract timeline and voter contact tasks from campaign plan sections.
    
    Args:
        sections: Dictionary of section content
        
    Returns:
        tuple: (timeline_tasks, voter_contact_tasks)
    """
    timeline_tasks = []
    voter_contact_tasks = []
    
    # Extract timeline tasks from section 3
    if 'campaign_timeline' in sections or 3 in sections:
        timeline_content = sections.get('campaign_timeline') or sections.get(3, '')
        timeline_tasks = extract_tasks_from_section_content(timeline_content, 'campaign_timeline')
        logger.info(f"Extracted {len(timeline_tasks)} timeline tasks from section content")
    
    # Extract voter contact tasks from section 6
    if 'voter_contact_plan' in sections or 6 in sections:
        voter_contact_content = sections.get('voter_contact_plan') or sections.get(6, '')
        voter_contact_tasks = extract_tasks_from_section_content(voter_contact_content, 'voter_contact_plan')
        logger.info(f"Extracted {len(voter_contact_tasks)} voter contact tasks from section content")
    
    return timeline_tasks, voter_contact_tasks

def clean_and_validate_task(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Clean and validate a task dictionary to ensure it has all required fields.
    Also applies AI template IDs and pro requirements based on task category and week.
    
    Args:
        task: Raw task dictionary
        
    Returns:
        Cleaned task dictionary or None if invalid
    """
    try:
        # Required fields
        required_fields = ['date', 'title', 'description', 'cta', 'type', 'category', 'deadline']
        
        # Check if all required fields exist
        for field in required_fields:
            if field not in task:
                logger.warning(f"Task missing required field '{field}': {task.get('title', 'Unknown')}")
                return None
        
        # Clean the task
        cleaned_task = {
            'date': str(task['date']).strip(),
            'title': str(task['title']).strip(),
            'description': str(task['description']).strip(),
            'cta': str(task['cta']).strip(),
            'type': str(task['type']).strip(),
            'category': str(task['category']).strip(),
            'deadline': task['deadline'] if isinstance(task['deadline'], int) else convert_date_to_weeks_from_election(str(task['deadline']).strip()),
        }
        
        # Optional fields
        if 'link' in task and task['link']:
            cleaned_task['link'] = str(task['link']).strip()
        
        # Week field
        week = None
        if 'week' in task:
            try:
                week = int(task['week'])
                cleaned_task['week'] = week
            except (ValueError, TypeError):
                logger.warning(f"Invalid week value for task '{task['title']}': {task['week']}")
        
        category = cleaned_task['category']
        
        # ENFORCE CORRECT CATEGORIZATION RULES
        # If the task appears to be a voter contact activity but is misclassified as "general", fix it
        title_lower = cleaned_task['title'].lower()
        desc_lower = cleaned_task['description'].lower()
        
        # Detect and fix misclassified voter contact tasks
        if category == 'general':
            if any(keyword in title_lower for keyword in ['text', 'sms', 'message']):
                cleaned_task['category'] = 'text'
                category = 'text'
                logger.info(f"Fixed misclassified task: {cleaned_task['title']} -> text")
            elif any(keyword in title_lower for keyword in ['robocall', 'robo call', 'call']):
                cleaned_task['category'] = 'robocall'
                category = 'robocall'
                logger.info(f"Fixed misclassified task: {cleaned_task['title']} -> robocall")
            elif any(keyword in title_lower for keyword in ['door', 'knocking', 'canvas']):
                cleaned_task['category'] = 'doorKnocking'
                category = 'doorKnocking'
                logger.info(f"Fixed misclassified task: {cleaned_task['title']} -> doorKnocking")
            elif any(keyword in title_lower for keyword in ['phone', 'banking', 'phone bank']):
                cleaned_task['category'] = 'phoneBanking'
                category = 'phoneBanking'
                logger.info(f"Fixed misclassified task: {cleaned_task['title']} -> phoneBanking")
            elif any(keyword in title_lower for keyword in ['social media', 'facebook', 'instagram', 'twitter']):
                cleaned_task['category'] = 'socialMedia'
                category = 'socialMedia'
                logger.info(f"Fixed misclassified task: {cleaned_task['title']} -> socialMedia")
        
        # Auto-assign event links for known events (after category fixing)
        if category == 'events' or 'event' in title_lower:
            if 'link' not in cleaned_task or not cleaned_task['link']:
                event_link = get_event_link(cleaned_task['title'])
                if event_link:
                    cleaned_task['link'] = event_link
                    logger.info(f"Auto-assigned event link for {cleaned_task['title']}: {event_link}")
        

        
        # Set pro requirements based on category (ALWAYS apply the correct rule)
        if category in ['text', 'robocall', 'doorKnocking', 'phoneBanking']:
            cleaned_task['proRequired'] = True
        else:
            cleaned_task['proRequired'] = False
        
        return cleaned_task
        
    except Exception as e:
        logger.error(f"Error cleaning task: {str(e)}")
        return None





def get_event_link(title: str) -> Optional[str]:
    """
    Get the appropriate link for generic event types based on the title.
    Returns None for most events since specific URLs depend on location and year.
    Only provides links for generic voting/civic resources.
    
    Args:
        title: Event title
        
    Returns:
        Event URL if it's a generic civic resource, None otherwise
    """
    title_lower = title.lower()
    
    # Only include generic civic/voting resources that apply broadly
    generic_civic_links = {
        'voter registration': 'https://www.vote.gov/register/',
        'register to vote': 'https://www.vote.gov/register/',
        'voting information': 'https://www.vote.gov/',
        'election information': 'https://www.vote.gov/',
        'absentee ballot': 'https://www.vote.gov/absentee-voting/',
        'early voting': 'https://www.vote.gov/early-voting/'
    }
    
    for event_key, link in generic_civic_links.items():
        if event_key in title_lower:
            return link
    
    # For all other events (festivals, fairs, community events), return None
    # These should be researched and added by the AI generation process
    # based on the specific campaign location
    return None
