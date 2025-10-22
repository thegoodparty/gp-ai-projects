from enum import Enum
from typing import List, Optional, Dict
from pydantic import BaseModel, Field


class IssueStance(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    REQUESTING = "requesting"


HIERARCHICAL_TAXONOMY = {
    "infrastructure_and_transportation": {
        "roads_and_bridges": "Roads, bridges, pavement, traffic management",
        "transit_and_walkways": "Public transit, sidewalks, bike lanes",
        "lighting_and_signage": "Street lighting, traffic signs, speed limit signs"
    },
    "public_safety": {
        "police": "Police services, crime prevention",
        "fire_and_ems": "Fire/EMS response",
        "emergency_management": "Emergency management, disaster prep"
    },
    "education": {
        "schools": "K-12 schools, funding, performance",
        "school_safety": "School safety, transportation",
        "educational_programs": "After-school programs, special education, extracurriculars"
    },
    "housing_and_development": {
        "affordable_housing": "Affordable housing, homelessness",
        "zoning_and_permits": "Zoning, permits, code enforcement",
        "taxes_and_assessments": "Property taxes, assessments"
    },
    "health_and_human_services": {
        "public_health": "Public health programs",
        "mental_health": "Mental health, substance abuse",
        "senior_and_disability": "Senior services, disability support"
    },
    "economic_development": {
        "jobs_and_business": "Job creation, business support, workforce development",
        "downtown_revitalization": "Downtown revitalization, main street programs",
        "industrial_development": "Warehouses, logistics, manufacturing, industrial zoning",
        "tourism": "Tourism, economic attractions"
    },
    "quality_of_life": {
        "recreation_and_libraries": "Parks, recreation, libraries, community centers",
        "utilities_and_waste": "Trash/recycling, utilities, water/sewer",
        "environmental": "Noise, air quality, environmental concerns"
    },
    "government_operations": {
        "budget_and_taxes": "Budget, taxes, fees",
        "service_and_transparency": "Customer service, transparency",
        "civic_engagement": "Elections, civic engagement, public meetings"
    },
    "other": {
        "uncategorized": "Issues that don't fit standard municipal categories",
        "general_feedback": "General feedback or comments about city services"
    }
}


class MessageRecord(BaseModel):
    original_row_idx: int
    atomic_idx: int
    phone_number: str
    message_text: str
    original_message_text: str
    poll_id: str
    record_id: str
    campaign_source: str
    round: str
    voters_age: Optional[float] = None
    voters_gender: Optional[str] = None
    age_group: str = "Unknown"
    voting_performance_category: str = "Unknown"
    location: str = "Unknown"
    ward: str = "Unknown"
    income_level: str = "Unknown"
    education_level: str = "Unknown"
    homeowner_status: str = "Unknown"
    business_owner: str = "Unknown"
    has_children_under_18: str = "Unknown"


class AtomizationResult(BaseModel):
    is_compound: bool = Field(description="Whether the message contains multiple distinct concerns")
    atomic_messages: List[str] = Field(
        description="List of atomic messages. If not compound, contains original message. If compound, list of separate concerns."
    )
    reasoning: Optional[str] = Field(
        description="Brief explanation of why the message is or isn't compound",
        default=None
    )


class AtomicMessage(BaseModel):
    original_row_idx: int
    atomic_idx: int
    atomic_text: str
    anonymized_text: str
    is_compound: bool


class IssueClassification(BaseModel):
    primary_category: str = Field(description="Primary category from taxonomy")
    secondary_category: str = Field(description="Secondary category from taxonomy")
    stance: IssueStance = Field(description="User's sentiment toward this issue")
    specific_concern: str = Field(description="Brief description of the specific concern")


class ClassifiedMessage(BaseModel):
    message: MessageRecord
    classification: IssueClassification


class ClusterInfo(BaseModel):
    cluster_id: int
    size: int
    theme: str
    summary: str
    example_messages: List[str]


class ClusterAnalysis(BaseModel):
    cluster_id: int
    theme: str
    summary: str
    analysis: str
    quotes: List[Dict[str, str]]
    sentiment: str
    message_count: int
    example_messages: List[str]


class RefinedCategorySummary(BaseModel):
    primary_category: str
    secondary_category: str
    refined_theme: str
    refined_summary: str
    refined_analysis: str
    refined_quotes: List[Dict[str, str]]
    cluster_analyses: List[ClusterAnalysis]
    message_count: int
    unique_respondents: int
    sentiment_distribution: Dict[str, int]


class CategorySummary(BaseModel):
    primary_category: str
    secondary_category: str
    message_count: int
    unique_respondents: int
    method: str
    summary: str
    key_themes: List[str]
    verbatim_quotes: List[str]
    verbatim_quotes_with_attribution: Optional[List[Dict[str, str]]] = None
    action_items: List[str]
    sentiment_distribution: Dict[str, int]
    demographic_breakdown: Optional[Dict[str, Dict[str, int]]] = None
    clusters: Optional[List[ClusterInfo]] = None


class FilterStats(BaseModel):
    total_messages: int
    removed_stop: int
    removed_emoji_starter: int
    removed_non_substantive: int
    remaining: int


class EnrichedMessageExport(BaseModel):
    poll_id: str
    message: str
    atomic_message: str
    record_id: str
    phone_number: str
    theme: str
    summary: str
    analysis: str
    quotes: str
    category: str
    sentiment: str
    cluster_analysis: str
    age: Optional[float] = None
    business_owner: str = "Unknown"
    education_level: str = "Unknown"
    families_with_children: str = "Unknown"
    homeowner: str = "Unknown"
    income: str = "Unknown"
    location: str = "Unknown"


class PipelineStats(BaseModel):
    campaign: str
    stage_timings: Dict[str, float]
    total_messages_loaded: int
    messages_after_cleaning: int
    messages_after_filtering: int
    messages_after_atomization: int
    messages_classified: int
    categories_synthesized: int
    llm_cost: float
    embedding_cost: float
    total_cost: float
    llm_calls: int
    embeddings_generated: int
