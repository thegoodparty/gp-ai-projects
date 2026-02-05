#!/usr/bin/env python3

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
import uuid
import numpy as np

@dataclass
class RawMessage:
    """Original message from CSV with complete metadata"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    csv_file: str = ""
    csv_row_index: int = 0
    original_text: str = ""
    timestamp: Optional[datetime] = None
    campaign_source: str = ""  # "josh", "cara", "berkeley"
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

@dataclass
class FilterResult:
    """Result of content filtering with reasons"""
    passed: bool
    reasons: List[str] = field(default_factory=list)
    filter_stats: Dict[str, Any] = field(default_factory=dict)

@dataclass
class FilteredMessage:
    """Message after content filtering with tracking"""
    id: str
    original_message_id: str
    csv_file: str
    csv_row_index: int
    filtered_text: str
    filter_result: FilterResult
    original_text: str
    campaign_source: str
    metadata: Dict[str, Any] = field(default_factory=dict)  # CSV metadata including phone number
    created_at: datetime = field(default_factory=datetime.now)

@dataclass
class PreprocessingStep:
    """Individual preprocessing transformation"""
    step_name: str
    before_text: str
    after_text: str
    changes_made: List[str] = field(default_factory=list)

@dataclass
class ProcessedMessage:
    """Message after text preprocessing with transformation tracking"""
    id: str
    filtered_message_id: str
    csv_file: str
    csv_row_index: int
    processed_text: str
    original_text: str
    filtered_text: str
    campaign_source: str
    preprocessing_steps: List[PreprocessingStep] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)

@dataclass
class AtomicMessage:
    """Atomic message split from compound message with parent tracking"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_message_id: str = ""
    csv_file: str = ""
    csv_row_index: int = 0
    atomic_text: str = ""
    atomic_index: int = 0  # Index within parent message
    split_context: str = ""  # Context from splitting process
    original_text: str = ""  # Original CSV text
    processed_text: str = ""  # After preprocessing
    campaign_source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)  # CSV metadata including phone number
    created_at: datetime = field(default_factory=datetime.now)

    # Accountability tracking fields
    ai_summary: str = ""  # AI-generated summary (separate from atomic_text)
    anonymized_keywords: List[str] = field(default_factory=list)  # Keywords replaced with "local area"
    original_atomic_text: str = ""  # Text before AI summarization
    was_filtered: bool = False  # Whether this message was filtered out
    filter_reasons: List[str] = field(default_factory=list)  # Why it was filtered
    is_opt_out: bool = False  # True if message is STOP/unsubscribe (passes through but not clustered)

@dataclass
class EmbeddingData:
    """Embedding vectors at different dimensions"""
    embedding_3072d: Optional[np.ndarray] = None  # Original Gemini embeddings
    embedding_300d: Optional[np.ndarray] = None   # PCA reduced (3072d → 300d) - used for clustering
    embedding_3d: Optional[np.ndarray] = None     # UMAP 3D visualization only
    embedding_model: str = "gemini"
    generation_timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class EmbeddedMessage:
    """Message with embeddings at multiple dimensions"""
    id: str
    atomic_message_id: str
    csv_file: str
    csv_row_index: int
    text: str
    original_text: str
    campaign_source: str
    embeddings: EmbeddingData
    metadata: Dict[str, Any] = field(default_factory=dict)  # CSV metadata including phone number
    created_at: datetime = field(default_factory=datetime.now)
    is_opt_out: bool = False  # True if message is STOP/unsubscribe (passes through but not clustered)

@dataclass
class ClusterAssignment:
    """Cluster assignment with metadata"""
    cluster_id: int
    cluster_confidence: float = 0.0
    distance_to_centroid: float = 0.0
    is_noise: bool = False
    clustering_algorithm: str = "hdbscan"
    clustering_parameters: Dict[str, Any] = field(default_factory=dict)
    original_cluster_id: Optional[int] = None
    merged_cluster_id: Optional[int] = None
    merge_source_clusters: List[int] = field(default_factory=list)

@dataclass
class ClusteredMessage:
    """Message with cluster assignment"""
    id: str
    embedded_message_id: str
    csv_file: str
    csv_row_index: int
    text: str
    original_text: str
    campaign_source: str
    cluster_assignment: ClusterAssignment
    embeddings: EmbeddingData
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    is_opt_out: bool = False  # True if message is STOP/unsubscribe (passes through but not clustered)

@dataclass
class ClusterTheme:
    """Theme analysis for a cluster"""
    theme: str
    summary: str
    key_topics: List[str] = field(default_factory=list)
    sentiment: str = "neutral"
    civic_relevance: str = ""
    confidence_score: float = 0.0
    # Enhanced fields for better analysis
    category: str = ""
    issues_summary: str = ""
    detailed_analysis: str = ""
    verbatim_quotes: List[str] = field(default_factory=list)
    quotes: List[Dict[str, str]] = field(default_factory=list)
    action_items: List[str] = field(default_factory=list)
    # Person-level metrics for cluster merger
    cluster_id: int = 0
    unique_respondents: int = 0
    total_mentions: int = 0
    avg_mentions_per_respondent: float = 0.0
    respondent_coverage_pct: float = 0.0

@dataclass
class SubClusterAnalysis:
    """Analysis of a sub-cluster within a main cluster"""
    sub_cluster_id: int
    parent_cluster_id: int
    size: int
    theme_analysis: ClusterTheme
    example_messages: List[str] = field(default_factory=list)
    message_ids: List[str] = field(default_factory=list)
    analysis_model: str = "gemini"
    analysis_timestamp: datetime = field(default_factory=datetime.now)
    cost_estimate: float = 0.0
    dbcv_score: float = -1.0

@dataclass
class ClusterAnalysis:
    """Complete analysis of a cluster with optional sub-clusters"""
    cluster_id: int
    size: int
    theme_analysis: ClusterTheme
    example_messages: List[str] = field(default_factory=list)
    message_ids: List[str] = field(default_factory=list)
    analysis_model: str = "gemini"
    analysis_timestamp: datetime = field(default_factory=datetime.now)
    cost_estimate: float = 0.0
    sub_clusters: List[SubClusterAnalysis] = field(default_factory=list)
    has_sub_clusters: bool = False

@dataclass
class PipelineConfig:
    """Configuration for the entire pipeline"""

    # Data source
    data_source: str = "josh"  # "josh", "cara", "berkeley", "all"

    # Filtering configuration
    filtering: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "remove_profanity": True,
        "remove_non_substantive": True,
        "min_length": 15,
        "require_civic_relevance": False
    })

    # Preprocessing configuration
    preprocessing: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "normalize_locations": True,
        "neutralize_sentiment": True,
        "standardize_civic_terms": True,
        "remove_personal_references": True,
        "focus_on_issues": True
    })

    # Embedding configuration
    embeddings: Dict[str, Any] = field(default_factory=lambda: {
        "model": "gemini",
        "dimensions_3072": True,
        "pca_dimensions": 100,
        "umap_dimensions": 15,
        "cache_embeddings": True,
        "batch_size": 100
    })

    # Clustering configuration
    clustering: Dict[str, Any] = field(default_factory=lambda: {
        "algorithm": "hdbscan",
        "min_cluster_size": 5,
        "min_samples": 3,
        "cluster_selection_epsilon": 0.06071795842258864,
        "alpha": 0.9812379567864946,
        "cluster_selection_method": "eom"
    })

    # Hierarchical clustering configuration
    hierarchical: Dict[str, Any] = field(default_factory=lambda: {
        "linkage": "ward",
        "affinity": "euclidean",
        "n_clusters": None,
        "distance_threshold": None,
        "min_cluster_size": 3,
        "max_clusters": 50,
        "compute_distances": True,
        "optimization": {
            "enabled": True,
            "budget": 200,
            "timeout": 300,
            "dynamic_scaling": {
                "enabled": True,
                "min_cluster_size_factor": [0.01, 0.05],
                "max_clusters_factor": [0.1, 0.3]
            },
            "constraints": {
                "min_clusters": 3,
                "max_clusters": 50,
                "min_largest_cluster": 5,
                "max_largest_cluster": 500
            }
        }
    })

    # Dendrogram visualization configuration
    dendrogram: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "max_display_clusters": 30,
        "figsize": [15, 10],
        "orientation": "top",
        "leaf_rotation": 90,
        "leaf_font_size": 10,
        "truncate_mode": "level",
        "p": 30,
        "show_leaf_counts": True,
        "color_threshold": None,
        "distance_sort": "descending"
    })

    # Analysis configuration
    analysis: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "parallel_analysis": True,
        "max_workers": 50,
        "max_example_messages": 10,
        "save_example_messages": 5
    })

    # Sub-clustering configuration
    sub_clustering: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "min_parent_cluster_size": 10,
        "optimization_budget": 50,
        "min_sub_cluster_size": 3
    })

    # Output configuration
    output: Dict[str, Any] = field(default_factory=lambda: {
        "save_intermediates": True,
        "export_formats": ["json", "csv"],
        "visualization_port": 3030,
        "reports_enabled": True
    })

    # Performance configuration
    performance: Dict[str, Any] = field(default_factory=lambda: {
        "max_connections": 100,
        "max_keepalive_connections": 25,
        "thinking_budget": 0,
        "temperature": 0.0
    })

@dataclass
class PipelineState:
    """Current state of the pipeline execution"""
    pipeline_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    config: PipelineConfig = field(default_factory=PipelineConfig)

    # Stage completion tracking
    stages_completed: List[str] = field(default_factory=list)
    current_stage: str = ""

    # Message counts at each stage
    raw_messages_count: int = 0
    filtered_messages_count: int = 0
    processed_messages_count: int = 0
    atomic_messages_count: int = 0
    embedded_messages_count: int = 0
    clustered_messages_count: int = 0

    # Cluster statistics
    total_clusters: int = 0
    noise_points: int = 0
    analyzed_clusters: int = 0

    # Performance metrics
    start_time: datetime = field(default_factory=datetime.now)
    stage_durations: Dict[str, float] = field(default_factory=dict)
    total_cost: float = 0.0
    api_calls: int = 0

    # Detailed cost tracking
    stage_costs: Dict[str, float] = field(default_factory=dict)
    gemini_usage: Dict[str, Any] = field(default_factory=dict)

    # Error tracking
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Output paths
    output_dir: str = ""
    checkpoint_dir: str = ""

    @property
    def is_complete(self) -> bool:
        required_stages = ["data_loading", "filtering", "preprocessing",
                          "atomic_splitting", "embedding", "clustering", "analysis"]
        return all(stage in self.stages_completed for stage in required_stages)

    @property
    def current_status(self) -> str:
        if self.is_complete:
            return "completed"
        elif self.errors:
            return "error"
        elif self.current_stage:
            return f"running_{self.current_stage}"
        else:
            return "initialized"

@dataclass
class PipelineResult:
    """Final result of pipeline execution"""
    pipeline_state: PipelineState
    raw_messages: List[RawMessage] = field(default_factory=list)
    filtered_messages: List[FilteredMessage] = field(default_factory=list)
    processed_messages: List[ProcessedMessage] = field(default_factory=list)
    atomic_messages: List[AtomicMessage] = field(default_factory=list)
    embedded_messages: List[EmbeddedMessage] = field(default_factory=list)
    clustered_messages: List[ClusteredMessage] = field(default_factory=list)
    cluster_analyses: List[ClusterAnalysis] = field(default_factory=list)

    # Summary statistics
    filtering_stats: Dict[str, Any] = field(default_factory=dict)
    preprocessing_stats: Dict[str, Any] = field(default_factory=dict)
    clustering_stats: Dict[str, Any] = field(default_factory=dict)
    cost_summary: Dict[str, Any] = field(default_factory=dict)

    # Export paths
    export_files: Dict[str, str] = field(default_factory=dict)

    def get_message_lineage(self, message_id: str) -> Dict[str, Any]:
        """Get complete lineage for a message"""
        lineage = {}

        # Find the message in clustered messages
        clustered = next((m for m in self.clustered_messages if m.id == message_id), None)
        if not clustered:
            return lineage

        lineage["clustered"] = clustered

        # Find embedded message
        embedded = next((m for m in self.embedded_messages if m.id == clustered.embedded_message_id), None)
        if embedded:
            lineage["embedded"] = embedded

            # Find atomic message
            atomic = next((m for m in self.atomic_messages if m.id == embedded.atomic_message_id), None)
            if atomic:
                lineage["atomic"] = atomic

                # Find processed message
                processed = next((m for m in self.processed_messages if m.id == atomic.parent_message_id), None)
                if processed:
                    lineage["processed"] = processed

                    # Find filtered message
                    filtered = next((m for m in self.filtered_messages if m.id == processed.filtered_message_id), None)
                    if filtered:
                        lineage["filtered"] = filtered

                        # Find raw message
                        raw = next((m for m in self.raw_messages if m.id == filtered.original_message_id), None)
                        if raw:
                            lineage["raw"] = raw

        return lineage

class MessageTracker:
    """Utility class for tracking message transformations"""

    @staticmethod
    def create_transformation_chain(raw_message: RawMessage) -> Dict[str, str]:
        """Create a transformation chain for tracking purposes"""
        return {
            "raw_id": raw_message.id,
            "csv_file": raw_message.csv_file,
            "csv_row": raw_message.csv_row_index,
            "original_text": raw_message.original_text,
            "campaign_source": raw_message.campaign_source
        }

    @staticmethod
    def trace_message_origin(clustered_message: ClusteredMessage,
                           pipeline_result: PipelineResult) -> Dict[str, Any]:
        """Trace a clustered message back to its original CSV row"""
        return pipeline_result.get_message_lineage(clustered_message.id)

# TypedDict definitions for better type safety on Dict return types
from typing import TypedDict, NotRequired


class UsageStats(TypedDict, total=False):
    """LLM usage statistics returned by get_usage_stats() methods"""
    api_call_count: int
    total_tokens: int
    total_cost: float
    prompt_tokens: NotRequired[int]
    completion_tokens: NotRequired[int]


class ValidationResult(TypedDict):
    """Data validation result from validate_data() methods"""
    valid: bool
    total_messages: int
    issues: List[str]
    warnings: NotRequired[List[str]]


class FilteringImpact(TypedDict):
    """Content filtering impact analysis"""
    total_input: int
    total_passed: int
    total_filtered: int
    filter_rate: float
    filter_reasons: Dict[str, int]
    campaign_stats: NotRequired[Dict[str, Any]]


class ProcessingImpact(TypedDict):
    """AI message processing impact analysis"""
    input_messages: int
    output_atomic_messages: int
    expansion_ratio: float
    split_messages: int
    single_messages: int
    filtered_out_by_ai: int
    ai_filtering_rate: float
    campaign_stats: NotRequired[Dict[str, Any]]


class DendrogramResult(TypedDict):
    """Dendrogram generation result"""
    success: bool
    dendrogram_files: Dict[str, str]
    stats: Dict[str, Any]
    error: NotRequired[str]


class ClusterPreparation(TypedDict):
    """Prepared cluster data for LLM analysis"""
    cluster_id: int
    message_count: int
    sample_messages: List[str]
    example_count: int
