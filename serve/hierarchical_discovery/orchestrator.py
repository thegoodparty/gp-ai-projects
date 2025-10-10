#!/usr/bin/env python3

import asyncio
import yaml
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
import traceback

from shared.logger import get_logger
from .models import PipelineConfig, PipelineState, PipelineResult, MessageTracker
from .stages.data_loader import load_data_stage
from .stages.content_filter import content_filter_stage
from .stages.ai_message_processor import ai_message_processor_stage
from .stages.embedding_generator import embedding_generator_stage_sync
from .stages.hierarchical_cluster_engine import hierarchical_cluster_engine_stage
from .stages.cluster_analyzer import cluster_analyzer_stage
from .stages.multi_cluster_analyzer import multi_cluster_analyzer_stage
from .stages.visualization_generator import visualization_generator_stage
from .stages.dendrogram_generator import dendrogram_generator_stage
from .stages.accountability_exporter import export_accountability_stage

logger = get_logger(__name__)

def serialize_list_for_csv(items: List[str], delimiter: str = ' | ') -> str:
    if not items:
        return ''
    cleaned_items = []
    for item in items:
        item_str = str(item).strip()
        if item_str.startswith('[') and item_str.endswith(']'):
            item_str = item_str[1:-1]
        cleaned_items.append(item_str.strip())
    return delimiter.join(cleaned_items)

class HierarchicalDiscoveryOrchestrator:
    """Main orchestrator for the hierarchical clustering civic message discovery pipeline"""

    def __init__(self, config_path: str = "serve/hierarchical_discovery/config.yaml", data_source_override: Optional[str] = None):
        self.config_path = Path(config_path)
        self.config = self._load_config()

        # Override data source if provided
        if data_source_override:
            logger.info(f"Overriding config data source '{self.config.data_source}' with '{data_source_override}'")
            self.config.data_source = data_source_override

        self.pipeline_state = PipelineState(config=self.config)
        self.optimization_results = None  # Store Optuna optimization results

        # Setup output directories
        self._setup_output_directories()

        logger.info(f"HierarchicalDiscoveryOrchestrator initialized")
        logger.info(f"Data source: {self.config.data_source}")
        logger.info(f"Pipeline ID: {self.pipeline_state.pipeline_id}")

    def _load_config(self) -> PipelineConfig:
        """Load configuration from YAML file"""
        try:
            with open(self.config_path, 'r') as f:
                config_data = yaml.safe_load(f)

            # Convert nested dicts to PipelineConfig
            config = PipelineConfig()

            # Map YAML structure to PipelineConfig and store all config data
            for key, value in config_data.items():
                setattr(config, key, value)

            logger.info(f"Configuration loaded from {self.config_path}")
            return config

        except Exception as e:
            logger.error(f"Failed to load config from {self.config_path}: {e}")
            logger.info("Using default configuration")
            return PipelineConfig()

    def _setup_output_directories(self):
        """Setup output directory structure"""
        # Get the directory where this file is located (serve/hierarchical_discovery/)
        discovery_dir = Path(__file__).parent
        # Make output directory relative to hierarchical_discovery folder
        base_dir = discovery_dir / self.config.output.get('base_dir', 'output')
        subdirs = self.config.output.get('subdirs', {})

        self.output_paths = {
            'base': base_dir,
            'reports': base_dir / subdirs.get('reports', 'reports'),
            'visualizations': base_dir / subdirs.get('visualizations', 'visualizations'),
            'dendrograms': base_dir / subdirs.get('dendrograms', 'dendrograms'),
            'checkpoints': base_dir / subdirs.get('checkpoints', 'checkpoints'),
            'exports': base_dir / subdirs.get('exports', 'exports')
        }

        # Create directories
        for path in self.output_paths.values():
            path.mkdir(parents=True, exist_ok=True)

        self.pipeline_state.output_dir = str(self.output_paths['base'])
        self.pipeline_state.checkpoint_dir = str(self.output_paths['checkpoints'])

        logger.info(f"Output directories created under: {self.output_paths['base']}")

    def _determine_cluster_ranges(self, cluster_ranges_config, dataset_size: int) -> List[int]:
        """Determine cluster ranges based on config and dataset size"""

        if isinstance(cluster_ranges_config, list):
            # User specified explicit ranges
            logger.info(f"Using user-specified cluster ranges: {cluster_ranges_config}")
            return cluster_ranges_config

        elif cluster_ranges_config == "auto":
            # Adaptive cluster ranges based on dataset size
            if dataset_size < 50:
                # Very small dataset - minimal clustering
                ranges = [3, 5]
            elif dataset_size < 100:
                # Small dataset (like Berkeley)
                ranges = [5, 10, 15]
            elif dataset_size < 300:
                # Small-medium dataset
                ranges = [10, 15, 20, 25]
            elif dataset_size < 500:
                # Medium dataset
                ranges = [15, 20, 30, 40]
            elif dataset_size < 1000:
                # Large dataset (like Cara)
                ranges = [20, 30, 40, 50]
            elif dataset_size < 2000:
                # Very large dataset
                ranges = [25, 40, 60, 80]
            else:
                # Huge dataset
                ranges = [30, 50, 75, 100]

            logger.info(f"Dataset size: {dataset_size} messages -> Auto-selected cluster ranges: {ranges}")
            return ranges

        else:
            # Invalid config, use defaults
            logger.warning(f"Invalid cluster_ranges config: {cluster_ranges_config}. Using default ranges.")
            if dataset_size < 500:
                return [10, 15, 20, 25]
            else:
                return [20, 30, 40, 50]

    def _update_pipeline_state(self, stage_name: str, duration: float, **kwargs):
        """Update pipeline state after stage completion"""
        self.pipeline_state.stages_completed.append(stage_name)
        self.pipeline_state.stage_durations[stage_name] = duration
        self.pipeline_state.current_stage = ""

        # Update counts if provided
        for key, value in kwargs.items():
            if hasattr(self.pipeline_state, key):
                setattr(self.pipeline_state, key, value)

        logger.info(f"Stage '{stage_name}' completed in {duration:.2f}s")

    def _handle_stage_error(self, stage_name: str, error: Exception):
        """Handle stage execution errors"""
        error_msg = f"Stage '{stage_name}' failed: {str(error)}"
        self.pipeline_state.errors.append(error_msg)
        self.pipeline_state.current_stage = f"error_{stage_name}"

        logger.error(error_msg)
        logger.error(f"Traceback: {traceback.format_exc()}")

        # Save error checkpoint
        self._save_checkpoint(f"error_{stage_name}")

    def _save_checkpoint(self, stage_name: str):
        """Save pipeline checkpoint"""
        checkpoints_config = getattr(self.config, 'checkpoints', {'enabled': True})
        if not checkpoints_config.get('enabled', True):
            return

        try:
            checkpoint_file = self.output_paths['checkpoints'] / f"checkpoint_{stage_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            checkpoint_data = {
                'pipeline_id': self.pipeline_state.pipeline_id,
                'stage': stage_name,
                'config': self.config.__dict__,
                'state': self.pipeline_state.__dict__,
                'timestamp': datetime.now().isoformat()
            }

            with open(checkpoint_file, 'w') as f:
                json.dump(checkpoint_data, f, indent=2, default=str)

            logger.info(f"Checkpoint saved: {checkpoint_file}")

        except Exception as e:
            logger.warning(f"Failed to save checkpoint: {e}")

    async def run_multi_cluster_pipeline(self, anonymize_keywords: Optional[List[str]] = None, disable_optimization: bool = False, return_data: bool = False, in_memory_messages: Optional[List] = None) -> Dict[str, Any]:
        """Run pipeline with multiple cluster counts

        Args:
            anonymize_keywords: Keywords to anonymize during AI processing
            disable_optimization: Whether to disable optimizations
            return_data: Whether to return data objects instead of writing files
            in_memory_messages: Optional list of RawMessage objects to process (skips CSV loading)
        """
        logger.info("🔥 ENTERING run_multi_cluster_pipeline method")

        self.anonymize_keywords = anonymize_keywords or []

        # Get cluster ranges from config
        hierarchical_config = getattr(self.config, 'hierarchical', {})
        cluster_ranges_config = hierarchical_config.get('cluster_ranges', 'auto')
        multi_cluster_enabled = hierarchical_config.get('multi_cluster_analysis', False)

        logger.info(f"🔥 Multi-cluster enabled: {multi_cluster_enabled}")

        if not multi_cluster_enabled:
            # Fall back to single cluster run
            logger.info("🔥 Falling back to single cluster run")
            return await self.run_pipeline(anonymize_keywords, disable_optimization, return_data, in_memory_messages)

        logger.info(f"🚀 STARTING MULTI-CLUSTER HIERARCHICAL DISCOVERY PIPELINE")
        logger.info("=" * 60)

        # Run common pipeline stages once (data loading through embedding)
        embedded_messages = await self._run_common_stages(anonymize_keywords, in_memory_messages)

        # Determine cluster ranges based on dataset size
        cluster_ranges = self._determine_cluster_ranges(cluster_ranges_config, len(embedded_messages))
        logger.info(f"Cluster ranges: {cluster_ranges}")
        logger.info("=" * 60)

        # Run clustering for each cluster count (clustering only, no analysis yet)
        multi_cluster_results = {}
        for n_clusters in cluster_ranges:
            logger.info(f"🔄 Running hierarchical clustering with {n_clusters} clusters...")

            # Create a copy of config for this cluster count to avoid conflicts
            import copy
            temp_config = copy.deepcopy(self.config)
            temp_config.hierarchical['n_clusters'] = n_clusters
            temp_config.hierarchical['distance_threshold'] = None

            # Run clustering only (no analysis)
            clustered_messages = hierarchical_cluster_engine_stage(embedded_messages, temp_config)

            multi_cluster_results[str(n_clusters)] = {
                'clustered_messages': clustered_messages,
                'n_clusters': n_clusters,
                'cluster_count': len(set(msg.cluster_assignment.cluster_id for msg in clustered_messages))
            }

        # Run multi-cluster analysis on all cluster configurations
        logger.info(f"🎯 Running multi-cluster theme analysis for {len(cluster_ranges)} configurations...")
        multi_analysis_result = await multi_cluster_analyzer_stage(multi_cluster_results, self.config)

        # Extract cost data from multi-cluster analysis
        if isinstance(multi_analysis_result, dict) and 'cost' in multi_analysis_result:
            stage_cost = multi_analysis_result.get("cost", 0)
            usage_stats = multi_analysis_result.get("usage_stats", {})
            self.pipeline_state.total_cost += stage_cost
            self.pipeline_state.stage_costs["multi_cluster_analysis"] = stage_cost
            self.pipeline_state.gemini_usage["multi_cluster_analysis"] = usage_stats

            logger.info(f"💰 Multi-Cluster Analysis Cost: ${stage_cost:.4f} (Total: ${self.pipeline_state.total_cost:.4f})")
            analyzed_multi_results = multi_analysis_result["analyses"]
        else:
            analyzed_multi_results = multi_analysis_result

        # Combine clustering and analysis results
        multi_results = {}
        for cluster_count_str in multi_cluster_results.keys():
            multi_results[cluster_count_str] = {
                'clustered_messages': multi_cluster_results[cluster_count_str]['clustered_messages'],
                'analyzed_clusters': analyzed_multi_results.get(cluster_count_str, []),
                'n_clusters': multi_cluster_results[cluster_count_str]['n_clusters'],
                'cluster_count': multi_cluster_results[cluster_count_str]['cluster_count']
            }

        # Create consolidated output
        consolidated_result = self._create_multi_cluster_output(multi_results, embedded_messages)

        # Only generate exports/visualizations if not in data-only mode
        if not return_data:
            logger.info("🚀 About to call _export_multi_cluster_results...")

            # Generate multi-cluster visualizations and exports
            await self._export_multi_cluster_results(consolidated_result)

            logger.info("✅ Completed _export_multi_cluster_results")
        else:
            logger.info("🔄 Skipping CSV export and visualizations (return_data=True)")

        logger.info("🎯 Multi-cluster pipeline completed successfully")

        return consolidated_result

    async def _run_common_stages(self, anonymize_keywords: Optional[List[str]] = None, in_memory_messages: Optional[List] = None):
        """Run data loading, filtering, AI processing, and embedding stages

        Args:
            anonymize_keywords: Keywords to anonymize during AI processing
            in_memory_messages: Optional list of RawMessage objects to process (skips CSV loading)
        """

        # Stage 1: Data Loading
        start_time = datetime.now()
        self.pipeline_state.current_stage = "data_loading"

        if in_memory_messages:
            logger.info("Using in-memory messages (skipping CSV data loading)")
            raw_messages = in_memory_messages
        else:
            raw_messages = load_data_stage(self.config)

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("data_loading", duration, raw_messages_count=len(raw_messages))

        if not raw_messages:
            raise ValueError("No messages loaded from data sources")

        # Stage 2: Content Filtering
        start_time = datetime.now()
        self.pipeline_state.current_stage = "filtering"

        filtered_messages = content_filter_stage(raw_messages, self.config)
        passed_messages = [msg for msg in filtered_messages if msg.filter_result.passed]

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("filtering", duration, filtered_messages_count=len(passed_messages))

        # Stage 3: AI Message Processing
        start_time = datetime.now()
        self.pipeline_state.current_stage = "ai_processing"

        atomic_result = await ai_message_processor_stage(filtered_messages, self.config, anonymize_keywords=anonymize_keywords)

        if isinstance(atomic_result, dict):
            atomic_messages = atomic_result["messages"]
            stage_cost = atomic_result.get("cost", 0)
            usage_stats = atomic_result.get("usage_stats", {})
            self.pipeline_state.total_cost += stage_cost
            self.pipeline_state.stage_costs["ai_processing"] = stage_cost
            self.pipeline_state.gemini_usage["ai_processing"] = usage_stats
        else:
            atomic_messages = atomic_result

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("ai_processing", duration, atomic_messages_count=len(atomic_messages))

        # Stage 4: Embedding Generation
        start_time = datetime.now()
        self.pipeline_state.current_stage = "embedding"

        embedding_result = embedding_generator_stage_sync(atomic_messages, self.config)

        if isinstance(embedding_result, dict):
            embedded_messages = embedding_result["messages"]
            stage_cost = embedding_result.get("cost", 0)
            usage_stats = embedding_result.get("usage_stats", {})
            self.pipeline_state.total_cost += stage_cost
            self.pipeline_state.stage_costs["embedding"] = stage_cost
            self.pipeline_state.gemini_usage["embedding"] = usage_stats
        else:
            embedded_messages = embedding_result

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("embedding", duration, embedded_messages_count=len(embedded_messages))

        return embedded_messages

    async def _run_clustering_analysis(self, embedded_messages, temp_config, n_clusters):
        """Run clustering and analysis for a specific cluster count"""

        # Hierarchical Clustering
        clustered_messages = hierarchical_cluster_engine_stage(embedded_messages, temp_config)

        # Cluster Analysis
        try:
            logger.info(f"Starting cluster analysis for {n_clusters} clusters with {len(clustered_messages)} messages...")
            analysis_result = cluster_analyzer_stage(clustered_messages, temp_config)
            logger.info(f"Cluster analysis result type: {type(analysis_result)}")
            logger.info(f"Cluster analysis result length: {len(analysis_result) if analysis_result else 0}")

            # The cluster_analyzer_stage returns List[ClusterAnalysis] directly
            analyzed_clusters = analysis_result if analysis_result else []

            if analyzed_clusters and len(analyzed_clusters) > 0:
                logger.info(f"✅ Successfully analyzed {len(analyzed_clusters)} clusters for {n_clusters}-cluster configuration")
                # Log first theme as example
                first_cluster = analyzed_clusters[0]
                if hasattr(first_cluster, 'theme_analysis') and hasattr(first_cluster.theme_analysis, 'theme'):
                    logger.info(f"Example theme: Cluster {first_cluster.cluster_id} -> '{first_cluster.theme_analysis.theme}'")
            else:
                logger.warning(f"❌ Cluster analysis returned empty results for {n_clusters} clusters")

        except Exception as e:
            logger.error(f"❌ Cluster analysis failed for {n_clusters} clusters: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            analyzed_clusters = []

        return {
            'clustered_messages': clustered_messages,
            'analyzed_clusters': analyzed_clusters,
            'n_clusters': n_clusters,
            'cluster_count': len(set(msg.cluster_assignment.cluster_id for msg in clustered_messages))
        }

    def _create_multi_cluster_output(self, multi_results, embedded_messages):
        """Create consolidated multi-cluster output with JSON structure"""

        consolidated_messages = []

        for embedded_msg in embedded_messages:
            # Extract phone number from metadata (with fallback for older cached data)
            metadata = getattr(embedded_msg, 'metadata', {})
            phone_number = metadata.get("Contact Phone Number", "")
            sent_at = metadata.get("Sent At", "")

            # Debug logging for first few messages with comprehensive metadata tracing
            if len(consolidated_messages) < 5:
                logger.info(f"ORCHESTRATOR CSV_EXPORT MESSAGE {len(consolidated_messages)} (csv_row={embedded_msg.csv_row_index}):")
                logger.info(f"  phone='{phone_number}', sent_at='{sent_at}'")
                logger.info(f"  text_snippet='{embedded_msg.text[:50]}...'")
                logger.info(f"  embedded_msg has metadata attr: {hasattr(embedded_msg, 'metadata')}")
                logger.info(f"  metadata_keys={list(metadata.keys())}")
                logger.info(f"  full_metadata={metadata}")

            msg_data = {
                'message_id': embedded_msg.id,
                'csv_row_index': embedded_msg.csv_row_index,
                'phone_number': phone_number,
                'sent_at': sent_at,
                'original_text': embedded_msg.original_text,
                'processed_text': embedded_msg.text,
                'campaign_source': embedded_msg.campaign_source,
                'cluster_assignments': {},
                'cluster_themes': {},
                'cluster_categories': {},
                'cluster_issues_summaries': {},
                'cluster_detailed_analyses': {},
                'cluster_verbatim_quotes': {},
                'cluster_action_items': {},
                'cluster_key_topics': {},
                'cluster_sentiments': {},
                'cluster_civic_relevance': {},
                'cluster_confidence_scores': {},
                'cluster_metadata': {},
                # Person-level metrics
                'cluster_unique_respondents': {},
                'cluster_total_mentions': {},
                'cluster_avg_mentions_per_respondent': {},
                'cluster_respondent_coverage_pct': {}
            }

            # Collect cluster assignments and themes for each cluster count
            for n_clusters_str, result in multi_results.items():
                n_clusters = int(n_clusters_str)

                # Find this message in the clustered results
                clustered_msg = next(
                    (cm for cm in result['clustered_messages'] if cm.id == embedded_msg.id),
                    None
                )

                if clustered_msg:
                    cluster_id = clustered_msg.cluster_assignment.cluster_id

                    # Find theme name from analyzed clusters
                    theme = f"Cluster {cluster_id}"  # Fallback theme using cluster ID

                    # Check if we have analyzed clusters for this result
                    if len(result['analyzed_clusters']) > 0:
                        # Debug logging only for 15 clusters to avoid spam
                        if n_clusters == 15:
                            logger.info(f"Looking for cluster_id {cluster_id} in {len(result['analyzed_clusters'])} analyzed clusters")

                        for cluster in result['analyzed_clusters']:
                            # Handle ClusterAnalysis objects
                            if hasattr(cluster, 'cluster_id'):
                                if cluster.cluster_id == cluster_id:
                                    # Extract all enhanced fields from theme_analysis
                                    if hasattr(cluster, 'theme_analysis'):
                                        ta = cluster.theme_analysis
                                        theme = getattr(ta, 'theme', f"Cluster {cluster_id}")

                                        # Store all enhanced fields
                                        msg_data['cluster_categories'][n_clusters_str] = getattr(ta, 'category', '')
                                        msg_data['cluster_issues_summaries'][n_clusters_str] = getattr(ta, 'issues_summary', '')
                                        msg_data['cluster_detailed_analyses'][n_clusters_str] = getattr(ta, 'detailed_analysis', '')
                                        msg_data['cluster_verbatim_quotes'][n_clusters_str] = getattr(ta, 'verbatim_quotes', [])
                                        msg_data['cluster_action_items'][n_clusters_str] = getattr(ta, 'action_items', [])
                                        msg_data['cluster_key_topics'][n_clusters_str] = getattr(ta, 'key_topics', [])
                                        msg_data['cluster_sentiments'][n_clusters_str] = getattr(ta, 'sentiment', '')
                                        msg_data['cluster_civic_relevance'][n_clusters_str] = getattr(ta, 'civic_relevance', '')
                                        msg_data['cluster_confidence_scores'][n_clusters_str] = getattr(ta, 'confidence_score', 0.0)

                                        # Extract person-level metrics from cluster analysis
                                        msg_data['cluster_unique_respondents'][n_clusters_str] = getattr(cluster, 'unique_respondents', 0)
                                        msg_data['cluster_total_mentions'][n_clusters_str] = getattr(cluster, 'total_mentions', 0)
                                        msg_data['cluster_avg_mentions_per_respondent'][n_clusters_str] = getattr(cluster, 'avg_mentions_per_respondent', 0.0)
                                        msg_data['cluster_respondent_coverage_pct'][n_clusters_str] = getattr(cluster, 'respondent_coverage_pct', 0.0)
                                    else:
                                        theme = f"Cluster {cluster_id}"
                                        # Set empty values for enhanced fields if no theme_analysis
                                        msg_data['cluster_categories'][n_clusters_str] = ''
                                        msg_data['cluster_issues_summaries'][n_clusters_str] = ''
                                        msg_data['cluster_detailed_analyses'][n_clusters_str] = ''
                                        msg_data['cluster_verbatim_quotes'][n_clusters_str] = []
                                        msg_data['cluster_action_items'][n_clusters_str] = []
                                        msg_data['cluster_key_topics'][n_clusters_str] = []
                                        msg_data['cluster_sentiments'][n_clusters_str] = ''
                                        msg_data['cluster_civic_relevance'][n_clusters_str] = ''
                                        msg_data['cluster_confidence_scores'][n_clusters_str] = 0.0

                                        # Set empty values for person-level metrics if no theme_analysis
                                        msg_data['cluster_unique_respondents'][n_clusters_str] = 0
                                        msg_data['cluster_total_mentions'][n_clusters_str] = 0
                                        msg_data['cluster_avg_mentions_per_respondent'][n_clusters_str] = 0.0
                                        msg_data['cluster_respondent_coverage_pct'][n_clusters_str] = 0.0
                                    break
                            # Handle dict format
                            elif isinstance(cluster, dict) and cluster.get('cluster_id') == cluster_id:
                                theme = cluster.get('theme', f"Cluster {cluster_id}")
                                # Extract enhanced fields from dict format
                                msg_data['cluster_categories'][n_clusters_str] = cluster.get('category', '')
                                msg_data['cluster_issues_summaries'][n_clusters_str] = cluster.get('issues_summary', '')
                                msg_data['cluster_detailed_analyses'][n_clusters_str] = cluster.get('detailed_analysis', '')
                                msg_data['cluster_verbatim_quotes'][n_clusters_str] = cluster.get('verbatim_quotes', [])
                                msg_data['cluster_action_items'][n_clusters_str] = cluster.get('action_items', [])
                                msg_data['cluster_key_topics'][n_clusters_str] = cluster.get('key_topics', [])
                                msg_data['cluster_sentiments'][n_clusters_str] = cluster.get('sentiment', '')
                                msg_data['cluster_civic_relevance'][n_clusters_str] = cluster.get('civic_relevance', '')
                                msg_data['cluster_confidence_scores'][n_clusters_str] = cluster.get('confidence_score', 0.0)

                                # Extract person-level metrics from dict format
                                msg_data['cluster_unique_respondents'][n_clusters_str] = cluster.get('unique_respondents', 0)
                                msg_data['cluster_total_mentions'][n_clusters_str] = cluster.get('total_mentions', 0)
                                msg_data['cluster_avg_mentions_per_respondent'][n_clusters_str] = cluster.get('avg_mentions_per_respondent', 0.0)
                                msg_data['cluster_respondent_coverage_pct'][n_clusters_str] = cluster.get('respondent_coverage_pct', 0.0)
                                break

                        if n_clusters == 15 and cluster_id == 0:  # Log first successful mapping
                            logger.info(f"Mapped cluster_id {cluster_id} to theme: {theme}")
                    else:
                        # No analyzed clusters available - cluster analysis failed
                        logger.warning(f"No cluster analysis available for {n_clusters} clusters - using fallback theme 'Cluster {cluster_id}'")

                    msg_data['cluster_assignments'][n_clusters_str] = cluster_id
                    msg_data['cluster_themes'][n_clusters_str] = theme

            consolidated_messages.append(msg_data)

        return {
            'messages': consolidated_messages,
            'cluster_results': multi_results,
            'pipeline_state': self.pipeline_state,
            'dataset_name': self.config.data_source,
            'cluster_ranges': list(multi_results.keys()),
            'total_messages': len(consolidated_messages)
        }

    async def _export_multi_cluster_results(self, consolidated_result):
        """Export multi-cluster results to CSV and generate visualizations"""

        logger.info("🔄 Starting multi-cluster CSV export...")

        # Generate timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_name = consolidated_result['dataset_name']

        # Create multi-cluster CSV
        csv_filename = self.output_paths['exports'] / f"multi_cluster_results_{dataset_name}_{timestamp}.csv"

        logger.info(f"📄 CSV file will be: {csv_filename}")
        logger.info(f"📊 Consolidated result contains {len(consolidated_result.get('messages', []))} messages")

        # Prepare CSV data
        csv_data = []
        for msg in consolidated_result['messages']:
            row = {
                'message_id': msg['message_id'],
                'csv_row_index': msg['csv_row_index'],
                'phone_number': msg['phone_number'],
                'sent_at': msg['sent_at'],
                'original_text': msg['original_text'],
                'processed_text': msg['processed_text'],
                'campaign_source': msg['campaign_source']
            }

            # Add cluster assignments and all enhanced analysis fields for each cluster count
            for cluster_count in consolidated_result['cluster_ranges']:
                row[f'cluster_{cluster_count}'] = msg['cluster_assignments'].get(cluster_count, '')
                row[f'theme_{cluster_count}'] = msg['cluster_themes'].get(cluster_count, '')
                row[f'category_{cluster_count}'] = msg['cluster_categories'].get(cluster_count, '')
                row[f'issues_summary_{cluster_count}'] = msg['cluster_issues_summaries'].get(cluster_count, '')
                row[f'detailed_analysis_{cluster_count}'] = msg['cluster_detailed_analyses'].get(cluster_count, '')

                quotes = msg['cluster_verbatim_quotes'].get(cluster_count, [])
                row[f'verbatim_quotes_{cluster_count}'] = ' | '.join(quotes) if isinstance(quotes, list) else quotes

                action_items = msg['cluster_action_items'].get(cluster_count, [])
                row[f'action_items_{cluster_count}'] = ', '.join(action_items) if isinstance(action_items, list) else action_items

                key_topics = msg['cluster_key_topics'].get(cluster_count, [])
                row[f'key_topics_{cluster_count}'] = ', '.join(key_topics) if isinstance(key_topics, list) else key_topics

                row[f'sentiment_{cluster_count}'] = msg['cluster_sentiments'].get(cluster_count, '')
                row[f'civic_relevance_{cluster_count}'] = msg['cluster_civic_relevance'].get(cluster_count, '')
                row[f'confidence_score_{cluster_count}'] = msg['cluster_confidence_scores'].get(cluster_count, '')

                # Add person-level metrics columns
                row[f'unique_respondents_{cluster_count}'] = msg['cluster_unique_respondents'].get(cluster_count, 0)
                row[f'total_mentions_{cluster_count}'] = msg['cluster_total_mentions'].get(cluster_count, 0)
                row[f'avg_mentions_per_respondent_{cluster_count}'] = msg['cluster_avg_mentions_per_respondent'].get(cluster_count, 0.0)
                row[f'respondent_coverage_pct_{cluster_count}'] = msg['cluster_respondent_coverage_pct'].get(cluster_count, 0.0)

            csv_data.append(row)

        logger.info(f"📋 Generated {len(csv_data)} rows for CSV export")

        # Write CSV
        if csv_data:
            import pandas as pd
            df = pd.DataFrame(csv_data)
            logger.info(f"📊 DataFrame columns: {list(df.columns)}")
            df.to_csv(csv_filename, index=False)
            logger.info(f"✅ Multi-cluster results exported to: {csv_filename}")
        else:
            logger.warning("❌ No CSV data to export - csv_data is empty")

        # Generate dendrograms and visualizations for each cluster count
        await self._generate_multi_cluster_dendrograms_and_visualizations(consolidated_result, timestamp)

        # Generate summary report
        await self._generate_multi_cluster_report(consolidated_result, timestamp)

    async def _generate_multi_cluster_dendrograms_and_visualizations(self, consolidated_result, timestamp):
        """Generate dendrograms and visualizations for each cluster count"""
        logger.info("🎨 Generating multi-cluster dendrograms and visualizations...")

        cluster_results = consolidated_result.get('cluster_results', {})
        dataset_name = consolidated_result['dataset_name']

        # Generate dendrograms and visualizations for each cluster configuration
        for cluster_count_str in cluster_results.keys():
            cluster_count = int(cluster_count_str)
            result = cluster_results[cluster_count_str]
            clustered_messages = result.get('clustered_messages', [])

            if not clustered_messages:
                logger.warning(f"No clustered messages found for {cluster_count} clusters")
                continue

            logger.info(f"📊 Generating dendrogram and visualization for {cluster_count} clusters...")

            # Extract embeddings for this cluster configuration
            import numpy as np
            embeddings = []
            for msg in clustered_messages:
                if hasattr(msg, 'embeddings') and hasattr(msg.embeddings, 'embedding_3072d'):
                    embeddings.append(msg.embeddings.embedding_3072d)

            if not embeddings:
                logger.warning(f"No embeddings found for {cluster_count} clusters")
                continue

            embeddings_array = np.array(embeddings)

            # Generate dendrogram for this cluster count
            dendrogram_config = getattr(self.config, 'dendrogram', {})
            if isinstance(dendrogram_config, dict):
                dendrogram_enabled = dendrogram_config.get('enabled', True)
            else:
                dendrogram_enabled = getattr(dendrogram_config, 'enabled', True)

            if dendrogram_enabled:
                try:
                    # Temporarily modify the config for this cluster count
                    hierarchical_config = getattr(self.config, 'hierarchical', {})
                    if isinstance(hierarchical_config, dict):
                        original_n_clusters = hierarchical_config.get('n_clusters', 15)
                        hierarchical_config['n_clusters'] = cluster_count
                    else:
                        original_n_clusters = getattr(hierarchical_config, 'n_clusters', 15)
                        hierarchical_config.n_clusters = cluster_count

                    dendrogram_result = dendrogram_generator_stage(
                        clustered_messages=clustered_messages,
                        embeddings=embeddings_array,
                        config=self.config,
                        output_dir=str(self.output_paths['dendrograms'])
                    )

                    # Restore original config
                    if isinstance(hierarchical_config, dict):
                        hierarchical_config['n_clusters'] = original_n_clusters
                    else:
                        hierarchical_config.n_clusters = original_n_clusters

                    logger.info(f"✅ Dendrogram generated for {cluster_count} clusters")
                except Exception as e:
                    logger.error(f"❌ Failed to generate dendrogram for {cluster_count} clusters: {e}")
                    # Ensure config is restored even on error
                    if isinstance(hierarchical_config, dict):
                        hierarchical_config['n_clusters'] = original_n_clusters
                    else:
                        hierarchical_config.n_clusters = original_n_clusters

            # Generate visualization for this cluster count
            try:
                analyzed_clusters = result.get('analyzed_clusters', [])

                # Create serializable result structure matching the working single-cluster format
                result_dict = {
                    'pipeline_id': f"multi_cluster_{cluster_count}_{timestamp}",
                    'data_source': dataset_name,
                    'completion_time': datetime.now().isoformat(),
                    'stages_completed': ['data_loading', 'filtering', 'processing', 'embedding', 'clustering', 'analysis'],
                    'stage_durations': {},
                    'total_duration': 0,
                    'clustering_algorithm': 'hierarchical',
                    'message_counts': {
                        'raw_messages': len(clustered_messages),
                        'filtered_messages': len(clustered_messages),
                        'processed_messages': len(clustered_messages),
                        'atomic_messages': len(clustered_messages),
                        'embedded_messages': len(clustered_messages),
                        'clustered_messages': len(clustered_messages)
                    },
                    'cluster_statistics': {
                        'total_clusters': cluster_count,
                        'noise_points': 0,
                        'analyzed_clusters': len(analyzed_clusters)
                    },
                    'cluster_analyses': [
                        {
                            'cluster_id': analysis.cluster_id,
                            'size': analysis.size,
                            'theme': analysis.theme_analysis.theme,
                            'summary': analysis.theme_analysis.summary,
                            'key_topics': analysis.theme_analysis.key_topics,
                            'sentiment': analysis.theme_analysis.sentiment,
                            'civic_relevance': analysis.theme_analysis.civic_relevance,
                            'confidence_score': analysis.theme_analysis.confidence_score,
                            'example_messages': analysis.example_messages[:3] if hasattr(analysis, 'example_messages') else []
                        }
                        for analysis in analyzed_clusters
                    ],
                    'clustered_messages': [
                        {
                            'id': msg.id,
                            'text': msg.text,
                            'cluster_id': msg.cluster_assignment.cluster_id,
                            'cluster_confidence': msg.cluster_assignment.cluster_confidence,
                            'is_noise': msg.cluster_assignment.is_noise,
                            'coordinates': {
                                'x': float(msg.embeddings.embedding_15d[0]) if (hasattr(msg.embeddings, 'embedding_15d') and msg.embeddings.embedding_15d is not None and len(msg.embeddings.embedding_15d) > 0) else 0.0,
                                'y': float(msg.embeddings.embedding_15d[1]) if (hasattr(msg.embeddings, 'embedding_15d') and msg.embeddings.embedding_15d is not None and len(msg.embeddings.embedding_15d) > 1) else 0.0,
                                'z': float(msg.embeddings.embedding_15d[2]) if (hasattr(msg.embeddings, 'embedding_15d') and msg.embeddings.embedding_15d is not None and len(msg.embeddings.embedding_15d) > 2) else 0.0
                            }
                        }
                        for msg in clustered_messages
                    ]
                }

                # Save temporary result file for visualization
                import json
                result_file = self.output_paths['checkpoints'] / f"temp_result_{cluster_count}clusters_{timestamp}.json"
                with open(result_file, 'w') as f:
                    json.dump(result_dict, f, indent=2)

                # Generate visualization
                viz_result = visualization_generator_stage(
                    result_file=result_file,
                    config=self.config,
                    viz_dir=self.output_paths['visualizations']
                )

                # Clean up temporary result file
                if result_file.exists():
                    result_file.unlink()

                logger.info(f"✅ Visualization generated for {cluster_count} clusters: {viz_result}")
            except Exception as e:
                logger.error(f"❌ Failed to generate visualization for {cluster_count} clusters: {e}")
                import traceback
                logger.error(f"❌ Error details: {traceback.format_exc()}")

        logger.info("🎨 Multi-cluster dendrograms and visualizations generation complete!")

    async def _generate_multi_cluster_report(self, consolidated_result, timestamp):
        """Generate detailed report for multi-cluster analysis"""

        dataset_name = consolidated_result['dataset_name']
        report_filename = self.output_paths['reports'] / f"multi_cluster_report_{dataset_name}_{timestamp}.md"

        report_content = f"""# Multi-Cluster Hierarchical Analysis Report - Detailed

**Dataset:** {dataset_name}
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Total Messages:** {consolidated_result['total_messages']:,}
**Cluster Ranges Tested:** {', '.join(consolidated_result['cluster_ranges'])}

## Executive Summary

This report provides a comprehensive analysis of civic messages from {dataset_name}'s campaign across multiple clustering configurations. The analysis reveals key themes and concerns across different levels of granularity.

---

"""

        # Add detailed sections for each cluster count
        for cluster_count in consolidated_result['cluster_ranges']:
            n_clusters = int(cluster_count)
            result = consolidated_result['cluster_results'][cluster_count]
            analyzed_clusters = result['analyzed_clusters']

            if not analyzed_clusters:
                continue

            report_content += f"\n## {n_clusters}-Cluster Analysis\n\n"

            # Sort by unique respondents
            sorted_clusters = sorted(
                analyzed_clusters,
                key=lambda x: getattr(x, 'unique_respondents', 0) if hasattr(x, 'unique_respondents') else (x.get('unique_respondents', 0) if isinstance(x, dict) else 0),
                reverse=True
            )

            # Show top 10 clusters for each configuration
            for idx, cluster in enumerate(sorted_clusters[:10], 1):
                # Extract cluster info (handle both object and dict formats)
                if hasattr(cluster, 'theme_analysis'):
                    theme = cluster.theme_analysis.theme
                    sentiment = getattr(cluster.theme_analysis, 'sentiment', '')
                    category = getattr(cluster.theme_analysis, 'category', '')
                    summary = getattr(cluster.theme_analysis, 'issues_summary', '')
                    analysis = getattr(cluster.theme_analysis, 'detailed_analysis', '')
                    key_topics = getattr(cluster.theme_analysis, 'key_topics', [])
                    quotes = getattr(cluster.theme_analysis, 'verbatim_quotes', [])
                    unique_respondents = getattr(cluster, 'unique_respondents', 0)
                    total_mentions = getattr(cluster, 'total_mentions', 0)
                elif isinstance(cluster, dict):
                    theme = cluster.get('theme', f"Cluster {cluster.get('cluster_id', '?')}")
                    theme_data = cluster.get('theme_analysis', {})
                    sentiment = theme_data.get('sentiment', '')
                    category = theme_data.get('category', '')
                    summary = theme_data.get('issues_summary', '')
                    analysis = theme_data.get('detailed_analysis', '')
                    key_topics = theme_data.get('key_topics', [])
                    quotes = theme_data.get('verbatim_quotes', [])
                    unique_respondents = cluster.get('unique_respondents', 0)
                    total_mentions = cluster.get('total_mentions', 0)
                else:
                    continue

                if not theme:
                    continue

                report_content += f"### {idx}. {theme}\n\n"

                if sentiment:
                    report_content += f"**Sentiment:** {sentiment}  \n"

                report_content += f"**People:** {unique_respondents} unique respondents  \n"
                report_content += f"**Total Mentions:** {total_mentions}  \n"

                if category:
                    report_content += f"**Category:** {category}  \n"

                report_content += "\n"

                if summary:
                    report_content += f"**Summary:**  \n{summary}\n\n"

                if analysis:
                    report_content += f"**Analysis:**  \n{analysis}\n\n"

                if key_topics:
                    topics_str = ', '.join(key_topics[:10]) if isinstance(key_topics, list) else str(key_topics)
                    report_content += f"**Key Topics:** {topics_str}\n\n"

                if quotes:
                    report_content += "**Representative Quotes:**\n"
                    quote_list = quotes[:5] if isinstance(quotes, list) else [quotes]
                    for quote in quote_list:
                        if quote and str(quote).strip():
                            report_content += f"- {quote}\n"
                    report_content += "\n"

                report_content += "---\n\n"

        report_content += f"""
## Cost Summary

**Total Cost:** ${consolidated_result['pipeline_state'].total_cost:.4f}
**API Calls:** {consolidated_result['pipeline_state'].api_calls:,}

Generated with Multi-Cluster Hierarchical Discovery Pipeline
"""

        with open(report_filename, 'w') as f:
            f.write(report_content)

        logger.info(f"Detailed multi-cluster report generated: {report_filename}")

    async def run_pipeline(self, anonymize_keywords: Optional[List[str]] = None, disable_optimization: bool = False, return_data: bool = False, in_memory_messages: Optional[List] = None) -> PipelineResult:
        """Run the complete hierarchical discovery pipeline

        Args:
            anonymize_keywords: Keywords to anonymize during AI processing
            disable_optimization: Whether to disable optimizations
            return_data: Whether to return data objects instead of writing files
            in_memory_messages: Optional list of RawMessage objects to process (skips CSV loading)
        """
        self.anonymize_keywords = anonymize_keywords or []
        logger.info("🚀 STARTING HIERARCHICAL CLUSTERING CIVIC MESSAGE DISCOVERY PIPELINE")
        logger.info("=" * 60)

        pipeline_result = PipelineResult(pipeline_state=self.pipeline_state)

        try:
            # Stage 1: Data Loading
            start_time = datetime.now()
            self.pipeline_state.current_stage = "data_loading"

            if in_memory_messages:
                logger.info("Using in-memory messages (skipping CSV data loading)")
                raw_messages = in_memory_messages
            else:
                raw_messages = load_data_stage(self.config)

            duration = (datetime.now() - start_time).total_seconds()
            self._update_pipeline_state("data_loading", duration, raw_messages_count=len(raw_messages))

            if not raw_messages:
                raise ValueError("No messages loaded from data sources")

            pipeline_result.raw_messages = raw_messages
            self._save_checkpoint("data_loading")

            # Stage 2: Content Filtering
            start_time = datetime.now()
            self.pipeline_state.current_stage = "filtering"

            filtered_messages = content_filter_stage(raw_messages, self.config)
            pipeline_result.filtered_messages = filtered_messages

            passed_messages = [msg for msg in filtered_messages if msg.filter_result.passed]

            duration = (datetime.now() - start_time).total_seconds()
            self._update_pipeline_state("filtering", duration, filtered_messages_count=len(passed_messages))
            self._save_checkpoint("filtering")

            # Stage 3: AI Message Processing (unified preprocessing/splitting/anonymization)
            start_time = datetime.now()
            self.pipeline_state.current_stage = "ai_processing"

            atomic_result = await ai_message_processor_stage(filtered_messages, self.config, anonymize_keywords=anonymize_keywords)

            # Handle new return format with cost tracking
            if isinstance(atomic_result, dict):
                atomic_messages = atomic_result["messages"]
                stage_cost = atomic_result.get("cost", 0)
                usage_stats = atomic_result.get("usage_stats", {})

                # Update cost tracking
                self.pipeline_state.total_cost += stage_cost
                self.pipeline_state.stage_costs["ai_processing"] = stage_cost
                self.pipeline_state.gemini_usage["ai_processing"] = usage_stats
                self.pipeline_state.api_calls += usage_stats.get("api_call_count", 0)

                logger.info(f"💰 AI Processing Cost: ${stage_cost:.4f} (Total: ${self.pipeline_state.total_cost:.4f})")
            else:
                # Fallback for old format
                atomic_messages = atomic_result

            pipeline_result.atomic_messages = atomic_messages

            duration = (datetime.now() - start_time).total_seconds()
            self._update_pipeline_state("ai_processing", duration, atomic_messages_count=len(atomic_messages))
            self._save_checkpoint("ai_processing")

            # Stage 4: Embedding Generation
            start_time = datetime.now()
            self.pipeline_state.current_stage = "embedding"

            embedding_result = embedding_generator_stage_sync(atomic_messages, self.config)

            # Handle new return format with cost tracking
            if isinstance(embedding_result, dict):
                embedded_messages = embedding_result["messages"]
                stage_cost = embedding_result.get("cost", 0)
                usage_stats = embedding_result.get("usage_stats", {})

                # Update cost tracking
                self.pipeline_state.total_cost += stage_cost
                self.pipeline_state.stage_costs["embedding"] = stage_cost
                self.pipeline_state.gemini_usage["embedding"] = usage_stats
                # Handle both embedding and regular usage stats
                api_calls = usage_stats.get("total_embeddings_created", usage_stats.get("api_call_count", 0))
                self.pipeline_state.api_calls += api_calls

                logger.info(f"💰 Embedding Generation Cost: ${stage_cost:.4f} (Total: ${self.pipeline_state.total_cost:.4f})")
            else:
                # Fallback for old format
                embedded_messages = embedding_result

            pipeline_result.embedded_messages = embedded_messages

            duration = (datetime.now() - start_time).total_seconds()
            self._update_pipeline_state("embedding", duration, embedded_messages_count=len(embedded_messages))
            self._save_checkpoint("embedding")

            # Stage 5: Hierarchical Clustering
            start_time = datetime.now()
            self.pipeline_state.current_stage = "hierarchical_clustering"

            hierarchical_result = hierarchical_cluster_engine_stage(embedded_messages, self.config)

            # Handle both old and new return formats
            if isinstance(hierarchical_result, dict):
                clustered_messages = hierarchical_result["clustered_messages"]
                linkage_matrix = hierarchical_result.get("linkage_matrix")
            else:
                clustered_messages = hierarchical_result
                linkage_matrix = None

            pipeline_result.clustered_messages = clustered_messages

            # Calculate cluster statistics
            total_clusters = len(set(msg.cluster_assignment.cluster_id for msg in clustered_messages if not msg.cluster_assignment.is_noise))
            noise_points = sum(1 for msg in clustered_messages if msg.cluster_assignment.is_noise)

            duration = (datetime.now() - start_time).total_seconds()
            self._update_pipeline_state("hierarchical_clustering", duration,
                                      clustered_messages_count=len(clustered_messages),
                                      total_clusters=total_clusters,
                                      noise_points=noise_points)
            self._save_checkpoint("hierarchical_clustering")

            # Stage 6.5: Dendrogram Generation (Hierarchical-specific)
            if self.config.dendrogram.get('enabled', True):
                start_time = datetime.now()
                self.pipeline_state.current_stage = "dendrogram_generation"

                logger.info("=== DENDROGRAM GENERATION STAGE ===")
                # Extract embeddings for dendrogram
                embeddings = [msg.embeddings.embedding_3072d for msg in embedded_messages]
                import numpy as np
                embeddings_array = np.array(embeddings)

                dendrogram_result = dendrogram_generator_stage(
                    clustered_messages=clustered_messages,
                    embeddings=embeddings_array,
                    config=self.config,
                    output_dir=str(self.output_paths['dendrograms'])
                )

                duration = (datetime.now() - start_time).total_seconds()
                self._update_pipeline_state("dendrogram_generation", duration)
                self._save_checkpoint("dendrogram_generation")

                logger.info(f"Dendrogram generation complete: {dendrogram_result}")
            else:
                logger.info("Dendrogram generation is disabled")

            # Stage 7: Cluster Analysis
            start_time = datetime.now()
            self.pipeline_state.current_stage = "analysis"

            # Hierarchical clustering doesn't use sub-clustering, so pass None
            analysis_result = cluster_analyzer_stage(clustered_messages, self.config, None)

            # Handle new return format with cost tracking
            if isinstance(analysis_result, dict):
                cluster_analyses = analysis_result["analyses"]
                stage_cost = analysis_result.get("cost", 0)
                usage_stats = analysis_result.get("usage_stats", {})

                # Update cost tracking
                self.pipeline_state.total_cost += stage_cost
                self.pipeline_state.stage_costs["analysis"] = stage_cost
                self.pipeline_state.gemini_usage["analysis"] = usage_stats
                self.pipeline_state.api_calls += usage_stats.get("api_call_count", 0)

                logger.info(f"💰 Cluster Analysis Cost: ${stage_cost:.4f} (Total: ${self.pipeline_state.total_cost:.4f})")
            else:
                # Fallback for old format
                cluster_analyses = analysis_result

            pipeline_result.cluster_analyses = cluster_analyses

            duration = (datetime.now() - start_time).total_seconds()
            self._update_pipeline_state("analysis", duration, analyzed_clusters=len(cluster_analyses))
            self._save_checkpoint("analysis")

            # Pipeline completion
            self.pipeline_state.current_stage = "completed"
            total_duration = (datetime.now() - self.pipeline_state.start_time).total_seconds()

            logger.info("✅ HIERARCHICAL CLUSTERING PIPELINE COMPLETED SUCCESSFULLY")
            logger.info("=" * 60)
            logger.info(f"Total duration: {total_duration:.2f} seconds")
            logger.info(f"Pipeline ID: {self.pipeline_state.pipeline_id}")

            # Generate cost summary
            pipeline_result.cost_summary = self._generate_cost_summary()

            # Log cost summary
            logger.info("💰 COST SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total Gemini API Cost: ${self.pipeline_state.total_cost:.4f}")
            logger.info(f"Total API Calls: {self.pipeline_state.api_calls:,}")
            for stage, cost in self.pipeline_state.stage_costs.items():
                logger.info(f"  {stage}: ${cost:.4f}")

            # Generate final reports and exports (only if not in data-only mode)
            if not return_data:
                await self._generate_final_outputs(pipeline_result)
            else:
                logger.info("🔄 Skipping final outputs (return_data=True)")

            return pipeline_result

        except Exception as e:
            self._handle_stage_error(self.pipeline_state.current_stage, e)
            raise

    async def _generate_final_outputs(self, pipeline_result: PipelineResult):
        """Generate final reports and exports"""
        logger.info("Generating final outputs...")

        try:
            # Save pipeline result
            result_file = self.output_paths['exports'] / f"pipeline_result_{self.config.data_source}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            # Create serializable version of result
            result_dict = {
                'pipeline_id': pipeline_result.pipeline_state.pipeline_id,
                'data_source': self.config.data_source,
                'completion_time': datetime.now().isoformat(),
                'stages_completed': pipeline_result.pipeline_state.stages_completed,
                'stage_durations': pipeline_result.pipeline_state.stage_durations,
                'total_duration': sum(pipeline_result.pipeline_state.stage_durations.values()),
                'clustering_algorithm': 'hierarchical',
                'message_counts': {
                    'raw_messages': len(pipeline_result.raw_messages),
                    'filtered_messages': len([m for m in pipeline_result.filtered_messages if m.filter_result.passed]),
                    'processed_messages': len(pipeline_result.processed_messages),
                    'atomic_messages': len(pipeline_result.atomic_messages),
                    'embedded_messages': len(pipeline_result.embedded_messages),
                    'clustered_messages': len(pipeline_result.clustered_messages)
                },
                'cluster_statistics': {
                    'total_clusters': pipeline_result.pipeline_state.total_clusters,
                    'noise_points': pipeline_result.pipeline_state.noise_points,
                    'analyzed_clusters': len(pipeline_result.cluster_analyses)
                },
                'cluster_analyses': [
                    {
                        'cluster_id': analysis.cluster_id,
                        'size': analysis.size,
                        'theme': analysis.theme_analysis.theme,
                        'summary': analysis.theme_analysis.summary,
                        'key_topics': analysis.theme_analysis.key_topics,
                        'sentiment': analysis.theme_analysis.sentiment,
                        'civic_relevance': analysis.theme_analysis.civic_relevance,
                        'confidence_score': analysis.theme_analysis.confidence_score,
                        'example_messages': analysis.example_messages[:3]  # First 3 examples
                    }
                    for analysis in pipeline_result.cluster_analyses
                ],
                'clustered_messages': [
                    {
                        'id': msg.id,
                        'text': msg.text,
                        'cluster_id': msg.cluster_assignment.cluster_id,
                        'cluster_confidence': msg.cluster_assignment.cluster_confidence,
                        'is_noise': msg.cluster_assignment.is_noise,
                        'coordinates': {
                            'x': float(msg.embeddings.embedding_15d[0]) if (hasattr(msg.embeddings, 'embedding_15d') and msg.embeddings.embedding_15d is not None and len(msg.embeddings.embedding_15d) > 0) else 0.0,
                            'y': float(msg.embeddings.embedding_15d[1]) if (hasattr(msg.embeddings, 'embedding_15d') and msg.embeddings.embedding_15d is not None and len(msg.embeddings.embedding_15d) > 1) else 0.0,
                            'z': float(msg.embeddings.embedding_15d[2]) if (hasattr(msg.embeddings, 'embedding_15d') and msg.embeddings.embedding_15d is not None and len(msg.embeddings.embedding_15d) > 2) else 0.0
                        }
                    }
                    for msg in pipeline_result.clustered_messages
                ]
            }

            with open(result_file, 'w') as f:
                json.dump(result_dict, f, indent=2, default=str)

            logger.info(f"Pipeline result saved: {result_file}")

            # Export accountability CSV BEFORE visualization so viz can use it
            try:
                accountability_csv_path = export_accountability_stage(
                    pipeline_result,
                    self.config,
                    Path(self.pipeline_state.output_dir),
                    self.pipeline_state.pipeline_id,
                    self.anonymize_keywords
                )
                logger.info(f"Accountability CSV exported: {accountability_csv_path}")
            except Exception as e:
                logger.warning(f"Accountability export failed (non-critical): {e}")

            # Generate visualization (after accountability CSV is created)
            try:
                start_time = datetime.now()
                self.pipeline_state.current_stage = "visualization"
                html_file = visualization_generator_stage(result_file, self.config, self.output_paths['visualizations'])
                duration = (datetime.now() - start_time).total_seconds()
                self._update_pipeline_state("visualization", duration)
                logger.info(f"Interactive visualization generated: {html_file}")
            except Exception as e:
                logger.warning(f"Visualization generation failed (non-critical): {e}")

            # Generate summary report
            await self._generate_summary_report(pipeline_result)

        except Exception as e:
            logger.error(f"Failed to generate final outputs: {e}")

    def _generate_cost_summary(self) -> Dict[str, Any]:
        """Generate comprehensive cost summary"""
        total_tokens = 0
        total_api_calls = 0

        # Aggregate tokens from all stages
        for stage, usage_stats in self.pipeline_state.gemini_usage.items():
            if usage_stats:
                total_tokens += usage_stats.get('total_tokens', usage_stats.get('total_input_tokens', 0))

        cost_summary = {
            "total_cost": self.pipeline_state.total_cost,
            "total_api_calls": self.pipeline_state.api_calls,
            "total_tokens": total_tokens,
            "stage_breakdown": dict(self.pipeline_state.stage_costs),
            "stage_usage": dict(self.pipeline_state.gemini_usage),
            "cost_per_message": 0,
            "cost_per_token": 0
        }

        # Calculate per-message and per-token costs
        if self.pipeline_state.clustered_messages_count > 0:
            cost_summary["cost_per_message"] = self.pipeline_state.total_cost / self.pipeline_state.clustered_messages_count

        if total_tokens > 0:
            cost_summary["cost_per_token"] = self.pipeline_state.total_cost / total_tokens

        return cost_summary

    async def _generate_summary_report(self, pipeline_result: PipelineResult):
        """Generate human-readable summary report"""
        try:
            report_file = self.output_paths['reports'] / f"hierarchical_discovery_report_{self.config.data_source}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

            # Calculate statistics
            total_duration = sum(pipeline_result.pipeline_state.stage_durations.values())

            report_content = f"""# Hierarchical Clustering Civic Message Discovery Report

**Pipeline ID:** {pipeline_result.pipeline_state.pipeline_id}
**Data Source:** {self.config.data_source}
**Clustering Algorithm:** Hierarchical (Agglomerative)
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Total Duration:** {total_duration:.1f} seconds

## Pipeline Overview

This report summarizes the results of analyzing civic engagement messages through our hierarchical clustering discovery pipeline, which processes raw messages through filtering, AI processing, embedding generation, hierarchical clustering optimization, and thematic analysis.

## Processing Statistics

| Stage | Messages | Duration (s) | Notes |
|-------|----------|--------------|-------|
| Raw Messages | {len(pipeline_result.raw_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('data_loading', 0):.1f} | Initial dataset loaded |
| After Filtering | {len([m for m in pipeline_result.filtered_messages if m.filter_result.passed]):,} | {pipeline_result.pipeline_state.stage_durations.get('filtering', 0):.1f} | Non-substantive content removed |
| After Preprocessing | {len(pipeline_result.processed_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('preprocessing', 0):.1f} | Text normalized and standardized |
| AI Processed Messages | {len(pipeline_result.atomic_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('ai_processing', 0):.1f} | Messages processed and anonymized by AI |
| Embedded Messages | {len(pipeline_result.embedded_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('embedding', 0):.1f} | Vector embeddings generated |
| Clustered Messages | {len(pipeline_result.clustered_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('hierarchical_clustering', 0):.1f} | Messages grouped by hierarchical similarity |
| Analyzed Clusters | {len(pipeline_result.cluster_analyses)} | {pipeline_result.pipeline_state.stage_durations.get('analysis', 0):.1f} | Cluster themes identified |

## Hierarchical Clustering Results

- **Total Clusters:** {pipeline_result.pipeline_state.total_clusters}
- **Noise Points:** {pipeline_result.pipeline_state.noise_points:,} ({pipeline_result.pipeline_state.noise_points/len(pipeline_result.clustered_messages)*100:.1f}%)
- **Successfully Clustered:** {len(pipeline_result.clustered_messages) - pipeline_result.pipeline_state.noise_points:,} messages

## Top Civic Themes Discovered

"""

            # Add top 15 clusters
            for i, analysis in enumerate(pipeline_result.cluster_analyses[:15], 1):
                report_content += f"""### {i}. {analysis.theme_analysis.theme}

**Cluster ID:** {analysis.cluster_id} | **Size:** {analysis.size} messages | **Sentiment:** {analysis.theme_analysis.sentiment} | **Confidence:** {analysis.theme_analysis.confidence_score:.2f}

{analysis.theme_analysis.summary}

**Key Topics:** {', '.join(analysis.theme_analysis.key_topics)}

**Civic Relevance:** {analysis.theme_analysis.civic_relevance}

**Example Messages:**
"""
                for j, example in enumerate(analysis.example_messages[:3], 1):
                    report_content += f"  {j}. \"{example}\"\n"

                report_content += "\n"

            # Add technical details
            report_content += f"""## Technical Details

### Configuration Used
- **Hierarchical Clustering Parameters:** Optimized using Optuna {'(enabled)' if self.config.hierarchical.get('optimization', {}).get('enabled', True) else '(disabled)'}
- **Linkage Method:** {self.config.hierarchical.get('linkage', 'ward')}
- **Distance Metric:** {self.config.hierarchical.get('affinity', 'euclidean')}
- **Embedding Model:** {self.config.embeddings.get('model', 'gemini')}
- **AI Processing:** {'Enabled' if self.config.ai_processing.get('enabled', True) else 'Disabled'}

### Performance
- **Total Processing Time:** {total_duration:.1f} seconds
- **Messages per Second:** {len(pipeline_result.raw_messages)/total_duration:.1f}
- **Slowest Stage:** {max(pipeline_result.pipeline_state.stage_durations.items(), key=lambda x: x[1])[0]} ({max(pipeline_result.pipeline_state.stage_durations.values()):.1f}s)

### Cost Analysis
- **Total Gemini API Cost:** ${pipeline_result.pipeline_state.total_cost:.4f}
- **Total API Calls:** {pipeline_result.pipeline_state.api_calls:,}
- **Cost per Message:** ${pipeline_result.pipeline_state.total_cost/len(pipeline_result.raw_messages):.6f}
- **Stage Breakdown:**"""

            # Add stage-by-stage cost breakdown
            for stage, cost in pipeline_result.pipeline_state.stage_costs.items():
                if cost > 0:
                    report_content += f"\n  - {stage.title()}: ${cost:.4f}"

            report_content += f"""

### Data Quality
- **Filtering Pass Rate:** {len([m for m in pipeline_result.filtered_messages if m.filter_result.passed])/len(pipeline_result.raw_messages)*100:.1f}%
- **Average Cluster Confidence:** {sum(a.theme_analysis.confidence_score for a in pipeline_result.cluster_analyses)/len(pipeline_result.cluster_analyses):.3f}
- **Noise Ratio:** {pipeline_result.pipeline_state.noise_points/len(pipeline_result.clustered_messages)*100:.1f}%

### Hierarchical Clustering Benefits
- **Deterministic Results:** Unlike HDBSCAN, hierarchical clustering produces consistent results across runs
- **Interpretable Structure:** Dendrograms provide visual insight into cluster relationships and merge decisions
- **Flexible Cluster Count:** Can be configured to find optimal number of clusters using silhouette analysis
- **Full Connectivity:** Every point is assigned to a cluster (no inherent noise concept)

---

*Generated by Hierarchical Clustering Civic Message Discovery Pipeline v1.0*
"""

            with open(report_file, 'w') as f:
                f.write(report_content)

            logger.info(f"Summary report generated: {report_file}")

        except Exception as e:
            logger.error(f"Failed to generate summary report: {e}")

def run_hierarchical_discovery_pipeline(config_path: str = "serve/hierarchical_discovery/config.yaml", data_source: Optional[str] = None, anonymize_keywords: Optional[List[str]] = None, disable_optimization: bool = False, return_data: bool = False) -> PipelineResult:
    """Main entry point for running the hierarchical discovery pipeline"""
    print(f"🔥🔥🔥 ENTERING run_hierarchical_discovery_pipeline with config_path: {config_path}")

    orchestrator = HierarchicalDiscoveryOrchestrator(config_path, data_source_override=data_source)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


    # Check if multi-cluster analysis is enabled
    hierarchical_config = getattr(orchestrator.config, 'hierarchical', {})
    multi_cluster_enabled = hierarchical_config.get('multi_cluster_analysis', False)

    print(f"🔥🔥🔥 multi_cluster_enabled: {multi_cluster_enabled}")

    if multi_cluster_enabled:
        print(f"🔥🔥🔥 CALLING run_multi_cluster_pipeline")
        return loop.run_until_complete(orchestrator.run_multi_cluster_pipeline(anonymize_keywords=anonymize_keywords, disable_optimization=disable_optimization, return_data=return_data))
    else:
        print(f"🔥🔥🔥 CALLING run_pipeline (single cluster)")
        return loop.run_until_complete(orchestrator.run_pipeline(anonymize_keywords=anonymize_keywords, disable_optimization=disable_optimization, return_data=return_data))

if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "serve/hierarchical_discovery/config.yaml"

    logger.info(f"Starting Hierarchical Clustering Civic Message Discovery Pipeline with config: {config_path}")

    try:
        result = run_hierarchical_discovery_pipeline(config_path)
        logger.info(f"Pipeline completed successfully! Pipeline ID: {result.pipeline_state.pipeline_id}")

        # Print key statistics
        print(f"\n{'='*60}")
        print(f"HIERARCHICAL CLUSTERING CIVIC MESSAGE DISCOVERY PIPELINE - RESULTS SUMMARY")
        print(f"{'='*60}")
        print(f"Pipeline ID: {result.pipeline_state.pipeline_id}")
        print(f"Data Source: {result.pipeline_state.config.data_source}")
        print(f"Total Messages Processed: {len(result.raw_messages):,}")
        print(f"Clusters Discovered: {result.pipeline_state.total_clusters}")
        print(f"Total Duration: {sum(result.pipeline_state.stage_durations.values()):.1f} seconds")
        print(f"Output Directory: {result.pipeline_state.output_dir}")
        print(f"{'='*60}")

    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        sys.exit(1)