"""
Main task orchestrator that coordinates the complete AI task generation pipeline.
This is the primary interface for generating campaign tasks from AI-generated campaign plans.
"""

import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Union

from .task_models import (
    CampaignTask, 
    TaskGenerationRequest, 
    TaskGenerationResponse, 
    TaskExtractionResult
)
from .structured_task_extractor import StructuredTaskExtractor
from ai_generated_campaign_plan.schema.models import CampaignInfo, CleanedCampaignInfo
from ai_generated_campaign_plan.utils.utils import CampaignUtils
from shared.llm_gemini import GeminiClient
from shared.logger import get_logger

logger = get_logger(__name__)


class AITaskOrchestrator:
    """
    Complete AI task generation system that extracts tasks from campaign plan sections,
    maps them to appropriate AI templates, and returns API-compatible task objects.
    
    This is the main entry point for integrating AI-generated tasks with the existing
    gp-api task system.
    """
    
    def __init__(
        self,
        llm_client: Optional[GeminiClient] = None,
        api_base_url: str = "https://gp-api.goodparty.org",
        enable_template_mapping: bool = True
    ):
        self.llm_client = llm_client or GeminiClient()
        self.extractor = StructuredTaskExtractor(self.llm_client)
        self.campaign_utils = CampaignUtils(self.llm_client)
        self.logger = logger
        
        self.logger.info(f"AITaskOrchestrator initialized (template_mapping={'enabled' if enable_template_mapping else 'disabled'})")
    
    async def generate_tasks_from_campaign_plan(
        self, 
        request: TaskGenerationRequest
    ) -> TaskGenerationResponse:
        """
        Main method to generate AI tasks from a complete campaign plan.
        
        Args:
            request: Task generation request with campaign info and sections
            
        Returns:
            TaskGenerationResponse with generated tasks and metadata
        """
        self.logger.info(f"Starting AI task generation for campaign: {request.campaign_info.candidate_name}")
        
        try:
            # Clean campaign info
            cleaned_campaign_info = self.campaign_utils.clean_campaign_info(request.campaign_info)
            
            # Extract sections 3 and 6
            timeline_section = request.sections.get("section_3", "")
            voter_contact_section = request.sections.get("section_6", "")
            
            if not timeline_section or not voter_contact_section:
                raise ValueError("Both section_3 (timeline) and section_6 (voter_contact) are required")
            
            # Extract tasks from sections
            extraction_result = await self.extractor.extract_tasks_from_sections(
                timeline_section, 
                voter_contact_section, 
                cleaned_campaign_info
            )
            
            if not extraction_result.success:
                self.logger.error(f"Task extraction failed: {extraction_result.errors}")
                return TaskGenerationResponse(
                    success=False,
                    generation_metadata={
                        "errors": extraction_result.errors,
                        "extraction_failed": True
                    }
                )
            
            # Tasks already have template IDs from LLM-based selection during extraction
            enhanced_tasks = extraction_result.tasks
            template_mappings = {}
            
            # Log template mapping statistics
            mapped_count = sum(1 for task in enhanced_tasks if task.defaultAiTemplateId)
            mapping_rate = mapped_count / len(enhanced_tasks) if enhanced_tasks else 0
            self.logger.info(f"LLM template selection: {mapped_count}/{len(enhanced_tasks)} tasks ({mapping_rate:.1%} success rate)")
            
            # Static task generation not implemented
            static_tasks = []
            
            # Combine and sort tasks
            combined_tasks = sorted(enhanced_tasks, key=lambda x: x.date or "9999-12-31")
            
            # Generate response metadata
            generation_metadata = {
                "generation_timestamp": datetime.now().isoformat(),
                "candidate_name": request.campaign_info.candidate_name,
                "election_date": str(cleaned_campaign_info.election_date),
                "extraction_success": extraction_result.success,
                "extracted_count": extraction_result.extracted_count,
                "skipped_count": extraction_result.skipped_count,
                "template_mapping_enabled": request.template_mapping_enabled,
                "sections_processed": list(request.sections.keys())
            }
            
            if extraction_result.errors:
                generation_metadata["extraction_errors"] = extraction_result.errors
            
            response = TaskGenerationResponse(
                success=True,
                ai_tasks=enhanced_tasks,
                static_tasks=static_tasks,
                combined_tasks=combined_tasks,
                generation_metadata=generation_metadata,
                template_mappings=template_mappings
            )
            
            self.logger.info(f"Successfully generated {len(enhanced_tasks)} AI tasks for {request.campaign_info.candidate_name}")
            return response
            
        except Exception as e:
            self.logger.error(f"Task generation failed: {str(e)}")
            return TaskGenerationResponse(
                success=False,
                generation_metadata={
                    "error": str(e),
                    "generation_failed": True,
                    "generation_timestamp": datetime.now().isoformat()
                }
            )
    
    async def generate_tasks_from_sections(
        self,
        timeline_section: str,
        voter_contact_section: str,
        campaign_info: Union[CampaignInfo, CleanedCampaignInfo],
        enable_template_mapping: bool = True
    ) -> List[CampaignTask]:
        """
        Simplified method to generate tasks directly from section strings.
        
        Args:
            timeline_section: Section 3 timeline content
            voter_contact_section: Section 6 voter contact content  
            campaign_info: Campaign information
            enable_template_mapping: Whether to map AI templates
            
        Returns:
            List of campaign tasks
        """
        # Clean campaign info if needed
        if isinstance(campaign_info, CampaignInfo):
            cleaned_campaign_info = self.campaign_utils.clean_campaign_info(campaign_info)
        else:
            cleaned_campaign_info = campaign_info
        
        # Extract tasks
        extraction_result = await self.extractor.extract_tasks_from_sections(
            timeline_section, voter_contact_section, cleaned_campaign_info
        )
        
        if not extraction_result.success:
            self.logger.error("Task extraction failed")
            return []
        
        # Template IDs are already assigned during LLM extraction
        # Log statistics if template mapping was requested
        if enable_template_mapping:
            mapped_count = sum(1 for task in extraction_result.tasks if task.defaultAiTemplateId)
            mapping_rate = mapped_count / len(extraction_result.tasks) if extraction_result.tasks else 0
            self.logger.info(f"LLM template selection: {mapped_count}/{len(extraction_result.tasks)} tasks ({mapping_rate:.1%} success rate)")
        
        return extraction_result.tasks
    
    
    def get_task_statistics(self, tasks: List[CampaignTask]) -> Dict[str, Any]:
        """
        Generate comprehensive statistics about generated tasks.
        
        Args:
            tasks: List of campaign tasks
            
        Returns:
            Statistics dictionary
        """
        if not tasks:
            return {"total_tasks": 0}
        
        stats = {
            "total_tasks": len(tasks),
            "ai_generated_tasks": len(tasks),
            "static_tasks": 0,
            "by_flow_type": {},
            "by_source": {},
            "by_week": {},
            "with_templates": sum(1 for t in tasks if t.defaultAiTemplateId),
            "pro_required": sum(1 for t in tasks if t.proRequired),
            "date_range": {}
        }
        
        # Stats by flow type
        for task in tasks:
            flow_type = task.flowType.value
            stats["by_flow_type"][flow_type] = stats["by_flow_type"].get(flow_type, 0) + 1
        
        # Remove source-based statistics since source field is removed
        
        # Stats by week
        for task in tasks:
            week = task.week
            stats["by_week"][str(week)] = stats["by_week"].get(str(week), 0) + 1
        
        # Date range
        scheduled_dates = [t.date for t in tasks if t.date]
        if scheduled_dates:
            stats["date_range"]["earliest"] = min(scheduled_dates)
            stats["date_range"]["latest"] = max(scheduled_dates)
        
        return stats


class TaskSystemIntegration:
    """
    Helper class for integrating AI tasks with the existing gp-api task system.
    Provides utilities for backend integration.
    """
    
    @staticmethod
    def tasks_to_api_format(tasks: List[CampaignTask]) -> List[Dict[str, Any]]:
        """
        Convert CampaignTask objects to the exact API JSON format expected by gp-api.
        
        Args:
            tasks: List of campaign task objects
            
        Returns:
            List of task dictionaries matching API format
        """
        return [task.model_dump(exclude_none=True) for task in tasks]
    
    @staticmethod
    def merge_with_static_tasks(
        ai_tasks: List[CampaignTask], 
        static_tasks: List[Dict[str, Any]],
        prefer_ai: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Merge AI-generated tasks with existing static tasks.
        
        Args:
            ai_tasks: AI-generated campaign tasks
            static_tasks: Static tasks from existing system
            prefer_ai: Whether to prefer AI tasks over static ones for same week/type
            
        Returns:
            Merged task list in API format
        """
        # Convert AI tasks to API format
        ai_tasks_dict = TaskSystemIntegration.tasks_to_api_format(ai_tasks)
        
        if not prefer_ai:
            # Simple concatenation
            return static_tasks + ai_tasks_dict
        
        # Smart merging - replace static tasks with AI equivalents where applicable
        ai_by_week_type = {}
        for ai_task in ai_tasks_dict:
            key = (ai_task.get("week"), ai_task.get("flowType"))
            if key not in ai_by_week_type:
                ai_by_week_type[key] = []
            ai_by_week_type[key].append(ai_task)
        
        merged_tasks = []
        
        # Add static tasks, skipping those replaced by AI
        for static_task in static_tasks:
            key = (static_task.get("week"), static_task.get("flowType"))
            if key not in ai_by_week_type:
                merged_tasks.append(static_task)
        
        # Add all AI tasks
        merged_tasks.extend(ai_tasks_dict)
        
        # Sort by week and scheduled date
        def sort_key(task):
            week = task.get("week", 999)
            scheduled_date = task.get("date", "9999-12-31")
            return (week, scheduled_date)
        
        return sorted(merged_tasks, key=sort_key)
    
    @staticmethod
    def filter_tasks_by_week(
        tasks: List[Dict[str, Any]], 
        current_week: int
    ) -> List[Dict[str, Any]]:
        """
        Filter tasks by week number (matching existing API behavior).
        
        Args:
            tasks: List of task dictionaries
            current_week: Current week number before election
            
        Returns:
            Filtered tasks for the specified week
        """
        return [task for task in tasks if task.get("week") == current_week]
    
    @staticmethod
    def add_completion_status(
        tasks: List[Dict[str, Any]], 
        completed_task_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Add completion status to tasks (matching existing API behavior).
        
        Args:
            tasks: List of task dictionaries
            completed_task_ids: List of completed task IDs from campaign
            
        Returns:
            Tasks with completion status added
        """
        # Completed field removed from task model - no processing needed
        return tasks