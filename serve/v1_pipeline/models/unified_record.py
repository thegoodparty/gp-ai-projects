#!/usr/bin/env python3

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass
class ConsolidatedMessage:
    """Raw consolidated message from replies + results join"""
    phone_number: str
    message_text: str
    sent_at: datetime
    round: str  # R1, R2, R3

    # Demographics (from results file)
    age: int | None = None
    age_group: str = "Unknown"
    location: str = "Unknown"
    ward: str | None = None
    voters_gender: str | None = None
    voting_performance_category: str = "Unknown"
    residence_city: str = "Unknown"

    # Placeholders for future demographics
    homeowner_status: str = "Unknown"
    business_owner: str = "Unknown"
    has_children_under_18: str = "Unknown"
    education_level: str = "Unknown"
    income_level: str = "Unknown"

    # Message metadata
    campaign_id: str | None = None
    campaign_name: str | None = None
    carrier: str | None = None
    poll_id: str | None = None


@dataclass
class ClusteringResult:
    """Results from hierarchical clustering pipeline"""
    phone_number: str
    cluster_id: int = -1
    cluster_theme: str = "Uncategorized"
    cluster_category: str = "Other"
    key_topics: list[str] | None = None
    cluster_sentiment: str = "neutral"
    civic_relevance: str = "General civic engagement"
    theme_confidence: float = 0.0

    # Additional clustering metadata
    detailed_analysis: str | None = None
    verbatim_quotes: str | None = None

    def __post_init__(self) -> None:
        if self.key_topics is None:
            self.key_topics = []


@dataclass
class UnifiedCampaignRecord:
    """Unified record combining all data sources for DynamoDB upload"""

    # Identity
    campaign_id: str
    record_id: str
    atomic_id: str
    phone_number: str

    # Message Data
    message_text: str
    sent_at: datetime
    round: str  # R1, R2, R3

    # Demographics (from consolidation)
    age: int | None = None
    age_group: str = "Unknown"
    location: str = "Unknown"
    ward: str | None = None
    voters_gender: str | None = None
    voting_performance_category: str = "Unknown"
    residence_city: str = "Unknown"
    homeowner_status: str = "Unknown"
    business_owner: str = "Unknown"
    has_children_under_18: str = "Unknown"
    education_level: str = "Unknown"
    income_level: str = "Unknown"

    # Multi-cluster data (from hierarchical pipeline)
    multi_cluster_data: dict[str, dict[str, Any]] | None = None

    # Processed message variants (from hierarchical pipeline)
    original_message: str | None = None
    atomic_message: str | None = None

    # Message metadata
    campaign_name: str | None = None
    carrier: str | None = None
    poll_id: str | None = None

    # Processing metadata
    created_at: datetime | None = None
    updated_at: datetime | None = None
    processing_version: str = "v1.0"

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.utcnow()
        if self.updated_at is None:
            self.updated_at = datetime.utcnow()
        if not self.record_id:
            self.record_id = str(uuid.uuid4())
        if not self.atomic_id:
            self.atomic_id = str(uuid.uuid4())

    def to_dynamodb_item(self) -> dict[str, Any]:
        """Convert to DynamoDB format for Lambda upload"""
        # Convert to dict and handle datetime serialization
        item_dict = asdict(self)

        # Convert datetime objects to ISO strings
        if isinstance(item_dict.get('sent_at'), datetime):
            item_dict['sent_at'] = self.sent_at.isoformat()
        if isinstance(item_dict.get('created_at'), datetime) and self.created_at:
            item_dict['created_at'] = self.created_at.isoformat()
        if isinstance(item_dict.get('updated_at'), datetime) and self.updated_at:
            item_dict['updated_at'] = self.updated_at.isoformat()

        # Ensure all required fields are present
        item_dict.setdefault('campaign_id', self.campaign_id)
        item_dict.setdefault('record_id', self.record_id)

        return item_dict

    @classmethod
    def from_consolidated_message(cls,
                                  consolidated: ConsolidatedMessage,
                                  campaign_id: str,
                                  clustering_result: dict[str, Any] | None = None) -> 'UnifiedCampaignRecord':
        """Create unified record from consolidated message and analysis results"""

        # Handle multi-cluster data and message variants
        multi_cluster_data = None
        original_message = None
        atomic_message = None
        atomic_id = None
        if clustering_result and isinstance(clustering_result, dict):
            if 'cluster_data' in clustering_result:
                multi_cluster_data = clustering_result['cluster_data']
            original_message = clustering_result.get('message')
            atomic_message = clustering_result.get('atomic_message')
            atomic_id = clustering_result.get('atomic_id')

        # Generate atomic_id if not provided (for backward compatibility)
        if not atomic_id:
            atomic_id = str(uuid.uuid4())

        return cls(
            # Identity
            campaign_id=campaign_id,
            record_id=str(uuid.uuid4()),
            atomic_id=atomic_id,
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
            poll_id=consolidated.poll_id,

            # Multi-cluster data
            multi_cluster_data=multi_cluster_data,
            original_message=original_message,
            atomic_message=atomic_message
        )


@dataclass
class PipelineResult:
    """Results from running the complete pipeline"""
    campaign_id: str
    input_messages: int
    atomic_messages: int
    output_records: int
    processing_time: float

    # Stage-specific results
    consolidation_result: dict[str, Any]
    clustering_result: dict[str, Any]

    # Error tracking
    errors: list[str]
    warnings: list[str]

    # Optional stage results (with defaults)
    sqs_result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.errors:
            self.errors = []
        if not self.warnings:
            self.warnings = []

    @property
    def messages_expanded(self) -> int:
        """Calculate how many messages were expanded during clustering"""
        return self.atomic_messages - self.input_messages

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage (output vs atomic messages)"""
        if self.atomic_messages == 0:
            return 0.0
        return (self.output_records / self.atomic_messages) * 100

    @property
    def summary(self) -> dict[str, Any]:
        """Generate summary dictionary"""
        return {
            'campaign_id': self.campaign_id,
            'input_messages': self.input_messages,
            'atomic_messages': self.atomic_messages,
            'messages_expanded': f"+{self.messages_expanded}" if self.messages_expanded > 0 else str(self.messages_expanded),
            'output_records': self.output_records,
            'success_rate': f"{self.success_rate:.1f}%",
            'processing_time': f"{self.processing_time:.2f}s",
            'errors_count': len(self.errors),
            'warnings_count': len(self.warnings)
        }
