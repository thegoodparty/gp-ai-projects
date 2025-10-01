#!/usr/bin/env python3

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid


@dataclass
class ConsolidatedMessage:
    """Raw consolidated message from replies + results join"""
    phone_number: str
    message_text: str
    sent_at: datetime
    round: str  # R1, R2, R3

    # Demographics (from results file)
    age: Optional[int] = None
    age_group: str = "Unknown"
    location: str = "Unknown"
    ward: Optional[str] = None
    voters_gender: Optional[str] = None
    voting_performance_category: str = "Unknown"
    residence_city: str = "Unknown"

    # Placeholders for future demographics
    homeowner_status: str = "Unknown"
    business_owner: str = "Unknown"
    has_children_under_18: str = "Unknown"
    education_level: str = "Unknown"
    income_level: str = "Unknown"

    # Message metadata
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    carrier: Optional[str] = None


@dataclass
class ClassificationResult:
    """Results from classification pipeline"""
    phone_number: str
    primary_issue_category: str = "Uncategorized"
    secondary_issue: str = "general_feedback"
    issue_stance: str = "neutral"
    overall_sentiment: str = "other"
    message_quality: str = "substantive"
    content_type: str = "policy_feedback"
    confidence_score: float = 0.0
    is_substantive: bool = True

    # Raw classification data for debugging
    raw_issues_with_stance: Optional[str] = None
    hierarchical_issues: Optional[List[Dict]] = None


@dataclass
class ClusteringResult:
    """Results from hierarchical clustering pipeline"""
    phone_number: str
    cluster_id: int = -1
    cluster_theme: str = "Uncategorized"
    cluster_category: str = "Other"
    key_topics: List[str] = None
    cluster_sentiment: str = "neutral"
    civic_relevance: str = "General civic engagement"
    theme_confidence: float = 0.0

    # Additional clustering metadata
    detailed_analysis: Optional[str] = None
    verbatim_quotes: Optional[str] = None

    def __post_init__(self):
        if self.key_topics is None:
            self.key_topics = []


@dataclass
class UnifiedCampaignRecord:
    """Unified record combining all data sources for DynamoDB upload"""

    # Identity
    campaign_id: str
    record_id: str
    phone_number: str

    # Message Data
    message_text: str
    sent_at: datetime
    round: str  # R1, R2, R3

    # Demographics (from consolidation)
    age: Optional[int] = None
    age_group: str = "Unknown"
    location: str = "Unknown"
    ward: Optional[str] = None
    voters_gender: Optional[str] = None
    voting_performance_category: str = "Unknown"
    residence_city: str = "Unknown"
    homeowner_status: str = "Unknown"
    business_owner: str = "Unknown"
    has_children_under_18: str = "Unknown"
    education_level: str = "Unknown"
    income_level: str = "Unknown"

    # Classification (from classify pipeline)
    primary_issue_category: str = "Uncategorized"
    secondary_issue: str = "general_feedback"
    issue_stance: str = "neutral"
    overall_sentiment: str = "other"
    message_quality: str = "substantive"
    content_type: str = "policy_feedback"
    classification_confidence: float = 0.0
    is_substantive: bool = True
    hierarchical_issues: Optional[List[Dict]] = None

    # Multi-cluster data (from hierarchical pipeline)
    multi_cluster_data: Optional[Dict[str, Dict[str, Any]]] = None

    # Message metadata
    campaign_name: Optional[str] = None
    carrier: Optional[str] = None

    # Processing metadata
    created_at: datetime = None
    updated_at: datetime = None
    processing_version: str = "v1.0"

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()
        if self.updated_at is None:
            self.updated_at = datetime.utcnow()
        if not self.record_id:
            self.record_id = str(uuid.uuid4())

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB format for Lambda upload"""
        # Convert to dict and handle datetime serialization
        item_dict = asdict(self)

        # Convert datetime objects to ISO strings
        if isinstance(item_dict.get('sent_at'), datetime):
            item_dict['sent_at'] = self.sent_at.isoformat()
        if isinstance(item_dict.get('created_at'), datetime):
            item_dict['created_at'] = self.created_at.isoformat()
        if isinstance(item_dict.get('updated_at'), datetime):
            item_dict['updated_at'] = self.updated_at.isoformat()

        # Convert list to comma-separated string for DynamoDB
        if self.key_topics:
            item_dict['key_topics'] = ','.join(self.key_topics)

        # Ensure all required fields are present
        item_dict.setdefault('campaign_id', self.campaign_id)
        item_dict.setdefault('record_id', self.record_id)

        return item_dict

    @classmethod
    def from_consolidated_message(cls,
                                  consolidated: ConsolidatedMessage,
                                  campaign_id: str,
                                  classification_result: Optional[ClassificationResult] = None,
                                  clustering_result: Optional[Dict[str, Any]] = None) -> 'UnifiedCampaignRecord':
        """Create unified record from consolidated message and analysis results"""

        # Use classification results if available
        if classification_result:
            classification_data = {
                'primary_issue_category': classification_result.primary_issue_category,
                'secondary_issue': classification_result.secondary_issue,
                'issue_stance': classification_result.issue_stance,
                'overall_sentiment': classification_result.overall_sentiment,
                'message_quality': classification_result.message_quality,
                'content_type': classification_result.content_type,
                'classification_confidence': classification_result.confidence_score,
                'is_substantive': classification_result.is_substantive,
                'hierarchical_issues': classification_result.hierarchical_issues
            }
        else:
            classification_data = {}

        # Handle multi-cluster data
        multi_cluster_data = None
        if clustering_result and isinstance(clustering_result, dict) and 'cluster_data' in clustering_result:
            multi_cluster_data = clustering_result['cluster_data']

        return cls(
            # Identity
            campaign_id=campaign_id,
            record_id=str(uuid.uuid4()),
            phone_number=consolidated.phone_number,

            # Message data
            message_text=consolidated.message_text,
            sent_at=consolidated.sent_at,
            round=consolidated.round,

            # Demographics
            age=consolidated.age,
            age_group=consolidated.age_group,
            location=consolidated.location,
            ward=consolidated.ward,
            voters_gender=consolidated.voters_gender,
            voting_performance_category=consolidated.voting_performance_category,
            residence_city=consolidated.residence_city,
            homeowner_status=consolidated.homeowner_status,
            business_owner=consolidated.business_owner,
            has_children_under_18=consolidated.has_children_under_18,
            education_level=consolidated.education_level,
            income_level=consolidated.income_level,

            # Message metadata
            campaign_name=consolidated.campaign_name,
            carrier=consolidated.carrier,

            # Analysis results
            **classification_data,

            # Multi-cluster data
            multi_cluster_data=multi_cluster_data
        )


@dataclass
class PipelineResult:
    """Results from running the complete pipeline"""
    campaign_id: str
    total_messages: int
    successful_records: int
    failed_records: int
    processing_time: float

    # Stage-specific results
    consolidation_result: Dict[str, Any]
    classification_result: Dict[str, Any]
    clustering_result: Dict[str, Any]
    upload_result: Dict[str, Any]

    # Error tracking
    errors: List[str]
    warnings: List[str]

    def __post_init__(self):
        if not self.errors:
            self.errors = []
        if not self.warnings:
            self.warnings = []

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage"""
        if self.total_messages == 0:
            return 0.0
        return (self.successful_records / self.total_messages) * 100

    @property
    def summary(self) -> Dict[str, Any]:
        """Generate summary dictionary"""
        return {
            'campaign_id': self.campaign_id,
            'total_messages': self.total_messages,
            'successful_records': self.successful_records,
            'failed_records': self.failed_records,
            'success_rate': f"{self.success_rate:.1f}%",
            'processing_time': f"{self.processing_time:.2f}s",
            'errors_count': len(self.errors),
            'warnings_count': len(self.warnings)
        }