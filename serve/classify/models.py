from enum import Enum
from typing import List, Optional, Dict
from pydantic import BaseModel, Field, field_validator
from dataclasses import dataclass, field


class Sentiment(str, Enum):
    FRUSTRATED_URGENT = "frustrated_urgent"
    CONSTRUCTIVE_DETAILED = "constructive_detailed"
    APPRECIATIVE_POSITIVE = "appreciative_positive"
    ANGRY_CONFRONTATIONAL = "angry_confrontational"
    OTHER = "other"


class MessageQuality(str, Enum):
    SUBSTANTIVE = "substantive"              # Meaningful policy/issue content worth analyzing
    MINIMAL_RESPONSE = "minimal_response"    # Short acknowledgments, thumbs up, thanks
    HATE_SPEECH = "hate_speech"              # Offensive, discriminatory, or hateful content
    SPAM_NONSENSE = "spam_nonsense"          # Gibberish, spam, phone numbers, completely off-topic
    PERSONAL_ATTACK = "personal_attack"      # Attacks on individuals rather than issues


class ContentType(str, Enum):
    POLICY_FEEDBACK = "policy_feedback"       # Specific policy opinions or suggestions
    GENERAL_COMPLAINT = "general_complaint"   # Non-specific complaints about services
    SUPPORT_APPRECIATION = "support_appreciation"  # Thank you messages, expressions of support
    QUESTION_REQUEST = "question_request"     # Asking for information or help
    INAPPROPRIATE = "inappropriate"           # Hate speech, personal attacks, harassment


class HierarchicalIssue(BaseModel):
    """Hierarchical issue classification for better organization"""
    primary_category: str = Field(description="Top-level category (economic, safety, infrastructure, etc.)")
    secondary_category: str = Field(description="Broad subcategory within the primary category")


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


class IssueStance(str, Enum):
    POSITIVE = "positive"         # Supporting, praising
    NEGATIVE = "negative"         # Complaining, frustrated
    NEUTRAL = "neutral"          # Asking questions, neutral observation
    REQUESTING = "requesting"     # Asking for specific action/improvement


class HierarchicalIssueWithContext(BaseModel):
    """Enhanced hierarchical issue with stance and context awareness"""
    primary_category: str = Field(description="Top-level category (economic, safety, infrastructure, etc.)")
    secondary_category: str = Field(description="Broad subcategory within the primary category")
    stance: IssueStance = Field(description="User's sentiment toward this specific issue")
    specific_concern: str = Field(description="Brief, specific description of the concern")
    is_root_cause: bool = Field(
        description="Is this issue the underlying cause of other issues mentioned?",
        default=False
    )

    @field_validator('primary_category', 'secondary_category', mode='before')
    @classmethod
    def normalize_case(cls, v: str) -> str:
        """Normalize all categories to lowercase for consistency"""
        if isinstance(v, str):
            return v.lower()
        return v


class IssueListResponse(BaseModel):
    """Structured response schema for LLM issue identification"""
    issues: List[HierarchicalIssueWithContext] = Field(
        description="List of all civic issues identified in the message",
        default_factory=list
    )


class SmartCategorization(BaseModel):
    """World-class categorization with context awareness based on manual review insights"""
    issues: List[HierarchicalIssueWithContext] = Field(
        description="All identified issues with their specific stances",
        default_factory=list
    )
    should_be_uncategorized: bool = Field(
        description="True if message is not relevant to local government (federal issues, wrong numbers, etc.)"
    )
    uncategorized_reason: Optional[str] = Field(
        description="Why uncategorized (e.g., 'federal issue', 'personal attack', 'wrong number')",
        default=None
    )
    overall_sentiment: Sentiment = Field(
        description="Overall emotional tone of the message"
    )
    message_quality: MessageQuality = Field(
        description="Quality and appropriateness of the message content"
    )
    content_type: ContentType = Field(
        description="Type of content and communication intent"
    )
    confidence_score: Optional[float] = Field(
        description="Confidence score of the classification (0.0-1.0)",
        ge=0.0,
        le=1.0,
        default=None
    )
    taxonomy_suggestions: Optional[str] = Field(
        description="Suggestions for improving our classification categories - missing categories, better labels, or category splits",
        default=None
    )


class MessageClassification(BaseModel):
    """Legacy model - kept for backward compatibility"""
    hierarchical_issues: List[HierarchicalIssue] = Field(
        description="Hierarchical issue classification for better organization",
        default_factory=list
    )
    sentiment: Sentiment = Field(
        description="Overall emotional tone of the message"
    )
    message_quality: MessageQuality = Field(
        description="Quality and appropriateness of the message content"
    )
    content_type: ContentType = Field(
        description="Type of content and communication intent"
    )
    confidence_score: Optional[float] = Field(
        description="Confidence score of the classification (0.0-1.0)",
        ge=0.0,
        le=1.0,
        default=None
    )
    taxonomy_suggestions: Optional[str] = Field(
        description="Suggestions for improving our classification categories - missing categories, better labels, or category splits",
        default=None
    )


class MessageData(BaseModel):
    campaign_id: str
    campaign_name: str
    contact_phone_number: str
    carrier: Optional[str]
    campaign_number: str
    is_automatic_reply: bool
    send_direction: str
    send_status: Optional[str]
    error_code: Optional[str]
    sent_at: str
    message_text: str
    texter_name: Optional[str]
    message_type: str
    mms_attachments: Optional[str]


class AtomicMessage(BaseModel):
    """A single atomic message extracted from a compound message"""
    original_message_data: MessageData
    atomic_text: str = Field(description="The individual atomic message text")
    atomic_index: int = Field(description="Index of this atomic message within the original message")
    is_stop_message: bool = Field(description="Whether this is a STOP/opt-out message")
    
    def to_message_data(self) -> MessageData:
        """Convert atomic message back to MessageData format for processing"""
        message_data = self.original_message_data.model_copy()
        message_data.message_text = self.atomic_text
        return message_data


class EnrichedMessage(BaseModel):
    original_data: MessageData
    smart_classification: Optional[SmartCategorization] = None
    classification: Optional[MessageClassification] = None  # Legacy support
    is_substantive: bool = Field(
        description="Whether the message contains substantive policy feedback"
    )
    original_csv_row: Optional[int] = Field(
        description="Original row number from CSV for lineage tracking",
        default=None
    )
    original_csv_file: Optional[str] = Field(
        description="Original CSV filename for lineage tracking",
        default=None
    )

    def to_csv_row(self) -> dict:
        """Convert to flat dictionary for CSV export with enhanced fields"""
        row = self.original_data.model_dump()

        # Use smart classification if available, fallback to legacy
        classification = self.smart_classification or self.classification

        if classification:
            if isinstance(classification, SmartCategorization):
                # Enhanced format with issue-specific stances
                issues_with_stance = []
                root_causes = []

                for issue in classification.issues:
                    issue_str = f"{issue.primary_category}/{issue.secondary_category}:{issue.stance.value}"
                    if issue.specific_concern:
                        issue_str += f"({issue.specific_concern})"
                    issues_with_stance.append(issue_str)

                    if issue.is_root_cause:
                        root_causes.append(f"{issue.primary_category}/{issue.secondary_category}")

                row.update({
                    'hierarchical_issues_with_stance': '|'.join(issues_with_stance),
                    'root_causes': '|'.join(root_causes),
                    'should_be_uncategorized': classification.should_be_uncategorized,
                    'uncategorized_reason': classification.uncategorized_reason,
                    'overall_sentiment': classification.overall_sentiment.value,
                    'message_quality': classification.message_quality.value,
                    'content_type': classification.content_type.value,
                    'confidence_score': classification.confidence_score,
                    'taxonomy_suggestions': classification.taxonomy_suggestions
                })
            else:
                # Legacy format
                hierarchical_issues_str = '|'.join([
                    f"{issue.primary_category}/{issue.secondary_category}"
                    for issue in classification.hierarchical_issues
                ]) if classification.hierarchical_issues else ""

                row.update({
                    'hierarchical_issues': hierarchical_issues_str,
                    'sentiment': classification.sentiment.value,
                    'message_quality': classification.message_quality.value,
                    'content_type': classification.content_type.value,
                    'confidence_score': classification.confidence_score,
                    'taxonomy_suggestions': classification.taxonomy_suggestions
                })

        row.update({
            'is_substantive': self.is_substantive,
            'original_csv_row': self.original_csv_row,
            'original_csv_file': self.original_csv_file
        })
        return row


class ClassificationBatch(BaseModel):
    """For batch processing multiple messages"""
    messages: List[EnrichedMessage]

    def get_substantive_messages(self) -> List[EnrichedMessage]:
        """Return only messages with substantive content"""
        return [msg for msg in self.messages if msg.is_substantive]

    def get_by_sentiment(self, sentiment: Sentiment) -> List[EnrichedMessage]:
        """Get messages by overall sentiment type"""
        return [
            msg for msg in self.messages
            if (msg.smart_classification and msg.smart_classification.overall_sentiment == sentiment) or
               (msg.classification and msg.classification.sentiment == sentiment)
        ]

    def get_by_issue_stance(self, primary_category: str, stance: IssueStance) -> List[EnrichedMessage]:
        """Get messages by specific issue stance (e.g., negative about property taxes)"""
        filtered_messages = []
        for msg in self.messages:
            if msg.smart_classification:
                for issue in msg.smart_classification.issues:
                    if issue.primary_category.lower() == primary_category.lower() and issue.stance == stance:
                        filtered_messages.append(msg)
                        break
        return filtered_messages

    def get_uncategorized_messages(self) -> List[EnrichedMessage]:
        """Get messages that should be uncategorized"""
        return [
            msg for msg in self.messages
            if msg.smart_classification and msg.smart_classification.should_be_uncategorized
        ]

    def get_root_cause_issues(self) -> Dict[str, int]:
        """Get frequency count of root cause issues"""
        root_causes = {}
        for msg in self.messages:
            if msg.smart_classification:
                for issue in msg.smart_classification.issues:
                    if issue.is_root_cause:
                        key = f"{issue.primary_category}/{issue.secondary_category}"
                        root_causes[key] = root_causes.get(key, 0) + 1
        return root_causes


@dataclass
class HierarchicalCategoryCounts:
    """Hierarchical category distribution with primary and secondary level counts"""
    primary_counts: Dict[str, int] = field(default_factory=dict)
    secondary_counts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    total_categorized: int = 0

