"""
Task system models that match the exact gp-api backend expectations.
These classes ensure type safety and compatibility with the existing task system.
"""

from datetime import date, datetime
from typing import List, Optional, Dict, Any, Union
from enum import Enum
from pydantic import BaseModel, Field
import uuid


class CampaignTaskType(str, Enum):
    """Task flow types - must match exactly with gp-api CampaignTaskType enum"""
    TEXT = "text"
    ROBOCALL = "robocall" 
    DOOR_KNOCKING = "doorKnocking"
    PHONE_BANKING = "phoneBanking"
    SOCIAL_MEDIA = "socialMedia"
    EVENTS = "events"
    EDUCATION = "education"


class CampaignTask(BaseModel):
    """
    Campaign task model matching the exact gp-api TypeScript interface.
    
    This is the exact structure returned by the API and consumed by the webapp.
    """
    title: str = Field(..., description="Task display title shown to user")
    description: str = Field(..., description="Task description explaining purpose")
    cta: str = Field(..., description="Call-to-action button text (Schedule, Write post, etc.)")
    flowType: CampaignTaskType = Field(..., description="Task category determining UI flow")
    week: int = Field(..., ge=1, description="Week number before election")
    link: Optional[str] = Field(None, description="External URL for education/guidance tasks")
    proRequired: Optional[bool] = Field(None, description="Whether task requires premium account")
    deadline: Optional[int] = Field(None, description="Days before election when task expires")
    defaultAiTemplateId: Optional[str] = Field(None, description="AI template ID for content generation")
    
    # New fields for AI-generated tasks
    date: Optional[str] = Field(None, description="ISO date string for specific scheduling")


class AIContentTemplate(BaseModel):
    """AI Content Template model matching the actual CMS API structure"""
    id: str = Field(..., description="Template identifier")
    type: str = Field(default="aiContentTemplate", description="Template type")
    createdAt: str = Field(..., description="Template creation timestamp")
    updatedAt: str = Field(..., description="Template last update timestamp")
    data: Dict[str, Any] = Field(..., description="Template data containing name, content, category")
    
    @property
    def name(self) -> str:
        """Get template name from nested data structure"""
        return self.data.get("name", "Unknown Template")
    
    @property
    def content(self) -> Optional[str]:
        """Get template content"""
        return self.data.get("content")
    
    @property
    def category_title(self) -> Optional[str]:
        """Get category title if available"""
        category = self.data.get("category", {})
        fields = category.get("fields", {})
        return fields.get("title")


class TemplateTheme(str, Enum):
    """Template themes for intelligent mapping"""
    INTRO = "intro"
    PERSUASIVE = "persuasive" 
    GOTV = "gotv"
    EARLY_VOTING = "early_voting"
    LAUNCH = "launch"
    COMMUNITY_ENGAGEMENT = "community_engagement"
    EVENT_PROMOTION = "event_promotion"
    DEADLINE_REMINDER = "deadline_reminder"




class TaskExtractionResult(BaseModel):
    """Result of task extraction with success/failure info"""
    success: bool = Field(..., description="Whether extraction was successful")
    tasks: List[CampaignTask] = Field(default=[], description="Successfully extracted tasks")
    errors: List[str] = Field(default=[], description="Any extraction errors")
    extracted_count: int = Field(default=0, description="Number of tasks extracted")
    skipped_count: int = Field(default=0, description="Number of lines skipped")


# Re-export from main schema for convenience
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo, CampaignInfo


class TaskGenerationRequest(BaseModel):
    """Request model for generating AI tasks from campaign plan"""
    campaign_info: CampaignInfo = Field(..., description="Campaign information")
    sections: Dict[str, str] = Field(..., description="Generated campaign plan sections")
    include_static_tasks: bool = Field(default=True, description="Whether to include static tasks")
    template_mapping_enabled: bool = Field(default=True, description="Whether to map AI templates")


class TaskGenerationResponse(BaseModel):
    """Response model for AI task generation"""
    success: bool = Field(..., description="Whether generation was successful")
    ai_tasks: List[CampaignTask] = Field(default=[], description="AI-generated tasks")
    static_tasks: List[CampaignTask] = Field(default=[], description="Static tasks for comparison")
    combined_tasks: List[CampaignTask] = Field(default=[], description="Combined and sorted tasks")
    generation_metadata: Dict[str, Any] = Field(default={}, description="Generation metadata")
    template_mappings: Dict[str, str] = Field(default={}, description="Applied template mappings")