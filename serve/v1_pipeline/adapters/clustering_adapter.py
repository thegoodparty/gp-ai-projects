#!/usr/bin/env python3

import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from serve.v1_pipeline.models.unified_record import ClusteringResult, ConsolidatedMessage
from shared.logger import get_logger

if TYPE_CHECKING:
    from serve.hierarchical_discovery.models import RawMessage

logger = get_logger(__name__)


class ClusteringAdapter:
    """
    Adapter for the existing hierarchical discovery pipeline to work with unified records
    """

    def __init__(self, config_path: str | None = None):
        """Initialize clustering adapter"""
        self.config_path = config_path or str(Path(__file__).parent.parent.parent / "hierarchical_discovery/config.yaml")

        # Load the config to understand the system
        try:
            with open(self.config_path) as f:
                self.config = yaml.safe_load(f)
            logger.info("Hierarchical discovery config loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load hierarchical discovery config: {e}", exc_info=True)
            raise

    def _convert_to_raw_messages(self, messages: list[ConsolidatedMessage], campaign_name: str) -> list['RawMessage']:
        """Convert ConsolidatedMessage objects to RawMessage objects for hierarchical discovery"""
        from datetime import datetime

        from serve.hierarchical_discovery.models import RawMessage

        raw_messages = []
        for i, msg in enumerate(messages):
            raw_message = RawMessage(
                csv_file=f"{campaign_name}_in_memory",
                csv_row_index=i,
                original_text=msg.message_text,
                timestamp=msg.sent_at if hasattr(msg, 'sent_at') else datetime.now(),
                campaign_source=campaign_name.lower(),
                metadata={
                    'Campaign ID': msg.campaign_id or campaign_name,
                    'Campaign Name': msg.campaign_name or campaign_name,
                    'Contact Phone Number': msg.phone_number,
                    'Carrier': msg.carrier or 'UNKNOWN',
                    'Send Direction': 'INBOUND',
                    'Message Text': msg.message_text,
                    'round': msg.round or 'Unknown'
                }
            )
            raw_messages.append(raw_message)

        logger.info(f"Converted {len(raw_messages)} ConsolidatedMessage → RawMessage objects")
        return raw_messages

    def _create_temp_config(self, temp_data_dir: str, campaign_name: str, temp_csv_path: str) -> str:
        """Create temporary config file for hierarchical discovery"""
        temp_config = self.config.copy()

        normalized_campaign = "berkeley" if campaign_name.lower() == "berkley" else campaign_name.lower()
        temp_config['data_source'] = normalized_campaign

        if 'data_files' not in temp_config:
            temp_config['data_files'] = {}
        temp_config['data_files'][normalized_campaign] = temp_csv_path
        logger.info(f"📂 Configured data_files['{normalized_campaign}'] = {temp_csv_path}")

        temp_output_dir = str(Path(temp_data_dir) / "output")
        temp_config['output']['base_dir'] = temp_output_dir

        temp_config['filtering']['enabled'] = True
        logger.info("✅ Content filtering ENABLED to remove STOP messages and emoji reactions")

        temp_config['ai_processing']['enabled'] = True
        logger.info("✅ AI processing ENABLED for message cleaning")

        temp_config['dendrogram']['enabled'] = False

        if 'hierarchical' not in temp_config:
            temp_config['hierarchical'] = {}

        temp_config['hierarchical']['cluster_ranges'] = 'optimal_k'
        temp_config['hierarchical']['optimal_k_config'] = {
            'min_k': 5,
            'max_k': 50,
            'step': 1,
            'max_cv': 1.0
        }
        logger.info("✅ Optimal k analysis ENABLED (k=5 to k=50)")

        temp_config['output']['save_intermediates'] = False

        temp_config_file = str(Path(temp_data_dir) / "temp_config.yaml")
        with open(temp_config_file, 'w') as f:
            yaml.dump(temp_config, f)

        return temp_config_file

    def _parse_clustering_results_from_objects(self, pipeline_result) -> dict[str, ClusteringResult]:
        """Parse hierarchical clustering results from returned data objects"""
        clustering_map = {}

        try:
            # Handle multi-cluster pipeline results (returns dict with consolidated messages)
            if isinstance(pipeline_result, dict) and 'messages' in pipeline_result:
                messages = pipeline_result['messages']
                logger.info(f"🔍 Processing {len(messages)} clustering result objects from multi-cluster pipeline")

                # Debug: Show first message structure
                if messages:
                    first_msg = messages[0]
                    logger.info(f"🔍 DEBUG First message keys: {list(first_msg.keys())}")
                    logger.info(f"🔍 DEBUG phone_number value: '{first_msg.get('phone_number')}'")
                    logger.info(f"🔍 DEBUG cluster_assignments: {first_msg.get('cluster_assignments')}")

                # Process EACH atomic message as a separate record (no grouping by phone number)
                logger.info(f"🔍 Processing {len(messages)} atomic messages as individual records")

                # Process each atomic message individually
                for msg_idx, msg_data in enumerate(messages):
                    phone_number = str(msg_data.get('phone_number', ''))
                    if not phone_number:
                        logger.warning(f"🔍 DEBUG Skipping message with no phone number: {list(msg_data.keys())[:5]}")
                        continue

                    # Generate unique atomic_id for this atomic message
                    import uuid
                    atomic_id = str(uuid.uuid4())

                    # Extract ALL cluster configurations (not just one)
                    cluster_assignments = msg_data.get('cluster_assignments', {})
                    cluster_themes = msg_data.get('cluster_themes', {})
                    cluster_issues_summaries = msg_data.get('cluster_issues_summaries', {})
                    cluster_key_topics = msg_data.get('cluster_key_topics', {})
                    cluster_sentiments = msg_data.get('cluster_sentiments', {})
                    cluster_categories = msg_data.get('cluster_categories', {})
                    cluster_civic_relevance = msg_data.get('cluster_civic_relevance', {})
                    cluster_confidence_scores = msg_data.get('cluster_confidence_scores', {})
                    cluster_detailed_analyses = msg_data.get('cluster_detailed_analyses', {})
                    cluster_verbatim_quotes = msg_data.get('cluster_verbatim_quotes', {})
                    cluster_quotes = msg_data.get('cluster_quotes', {})

                    message = msg_data.get('message', '')
                    atomic_message = msg_data.get('atomic_message', '')

                    # Build multi-cluster data for ALL cluster configurations
                    multi_cluster_data = {}

                    for cluster_count in cluster_assignments.keys():
                        if cluster_count in cluster_assignments:
                            key_topics_data = cluster_key_topics.get(cluster_count, [])
                            if isinstance(key_topics_data, str):
                                key_topics = [topic.strip() for topic in key_topics_data.split(',') if topic.strip()]
                            elif isinstance(key_topics_data, list):
                                key_topics = key_topics_data
                            else:
                                key_topics = []

                            verbatim_quotes_data = cluster_verbatim_quotes.get(cluster_count, [])
                            if isinstance(verbatim_quotes_data, list):
                                verbatim_quotes = verbatim_quotes_data
                            elif isinstance(verbatim_quotes_data, str) and verbatim_quotes_data:
                                verbatim_quotes = [verbatim_quotes_data]
                            else:
                                verbatim_quotes = []

                            quotes_data = cluster_quotes.get(cluster_count, [])
                            if isinstance(quotes_data, list):
                                quotes = quotes_data
                            else:
                                quotes = []

                            multi_cluster_data[cluster_count] = {
                                'cluster_id': int(cluster_assignments[cluster_count]) if cluster_assignments[cluster_count] != '' else -1,
                                'cluster_theme': str(cluster_themes.get(cluster_count, 'Uncategorized')),
                                'cluster_category': str(cluster_categories.get(cluster_count, 'Other')),
                                'issues_summary': cluster_issues_summaries.get(cluster_count, ''),
                                'key_topics': key_topics,
                                'cluster_sentiment': str(cluster_sentiments.get(cluster_count, 'neutral')),
                                'civic_relevance': str(cluster_civic_relevance.get(cluster_count, 'General civic engagement')),
                                'theme_confidence': float(cluster_confidence_scores.get(cluster_count, 0.0)) if cluster_confidence_scores.get(cluster_count) else 0.0,
                                'detailed_analysis': cluster_detailed_analyses.get(cluster_count, ''),
                                'verbatim_quotes': verbatim_quotes,
                                'quotes': quotes
                            }

                    # Store multi-cluster data for this atomic message (use atomic_id as key)
                    if multi_cluster_data:
                        clustering_map[atomic_id] = {
                            'atomic_id': atomic_id,
                            'phone_number': phone_number,
                            'cluster_data': multi_cluster_data,
                            'message': message,
                            'atomic_message': atomic_message
                        }

                logger.info(f"Successfully parsed {len(clustering_map)} clustering results from data objects")
                return clustering_map

            # Handle single-cluster pipeline results (PipelineResult object)
            elif hasattr(pipeline_result, 'clustered_messages') and hasattr(pipeline_result, 'cluster_analyses'):
                clustered_messages = pipeline_result.clustered_messages
                cluster_analyses = pipeline_result.cluster_analyses
                logger.debug(f"Processing {len(clustered_messages)} clustered messages from single-cluster pipeline")

                # Create a mapping of cluster_id to analysis
                analysis_map = {}
                for analysis in cluster_analyses:
                    if hasattr(analysis, 'cluster_id') and hasattr(analysis, 'theme_analysis'):
                        analysis_map[analysis.cluster_id] = analysis

                # Process each clustered message
                for msg in clustered_messages:
                    # Extract phone number from metadata
                    metadata = getattr(msg, 'metadata', {})
                    phone_number = metadata.get("Contact Phone Number", "")
                    if not phone_number:
                        continue

                    cluster_id = msg.cluster_assignment.cluster_id
                    analysis = analysis_map.get(cluster_id)

                    if analysis:
                        ta = analysis.theme_analysis
                        clustering_result = ClusteringResult(
                            phone_number=phone_number,
                            cluster_id=cluster_id,
                            cluster_theme=str(getattr(ta, 'theme', 'Uncategorized')),
                            cluster_category=str(getattr(ta, 'category', 'Other')),
                            key_topics=list(getattr(ta, 'key_topics', [])),
                            cluster_sentiment=str(getattr(ta, 'sentiment', 'neutral')),
                            civic_relevance=str(getattr(ta, 'civic_relevance', 'General civic engagement')),
                            theme_confidence=float(getattr(ta, 'confidence_score', 0.0)),
                            detailed_analysis=str(getattr(ta, 'detailed_analysis', '')) if getattr(ta, 'detailed_analysis', '') else None,
                            verbatim_quotes=' | '.join(getattr(ta, 'verbatim_quotes', [])) if getattr(ta, 'verbatim_quotes', []) else None
                        )
                    else:
                        # Fallback for missing analysis
                        clustering_result = ClusteringResult(
                            phone_number=phone_number,
                            cluster_id=cluster_id,
                            cluster_theme=f'Cluster {cluster_id}',
                            cluster_category='Other',
                            key_topics=[],
                            cluster_sentiment='neutral',
                            civic_relevance='General civic engagement',
                            theme_confidence=0.0
                        )

                    clustering_map[phone_number] = clustering_result

                logger.info(f"Successfully parsed {len(clustering_map)} clustering results from single-cluster pipeline")
                return clustering_map

            else:
                logger.warning(f"Unknown pipeline result format: {type(pipeline_result)}")
                return {}

        except Exception as e:
            logger.error(f"Failed to parse clustering results from objects: {e}", exc_info=True)
            logger.debug(f"Pipeline result type: {type(pipeline_result)}")
            if hasattr(pipeline_result, '__dict__'):
                logger.debug(f"Pipeline result attributes: {list(pipeline_result.__dict__.keys())}")
            elif isinstance(pipeline_result, dict):
                logger.debug(f"Pipeline result keys: {list(pipeline_result.keys())}")

        return clustering_map

    async def process_messages(self, messages: list[ConsolidatedMessage], campaign_name: str = "temp", persistent_output_dir: str | None = None) -> dict[str, dict[str, Any]]:
        """
        Process messages through hierarchical discovery pipeline

        Args:
            messages: List of consolidated messages to cluster
            campaign_name: Name for the temporary campaign
            persistent_output_dir: Directory to save outputs before temp cleanup

        Returns:
            Dict mapping atomic_id to clustering result dictionaries
        """
        if not messages:
            logger.warning("No messages provided for clustering")
            return {}

        if len(messages) < 3:
            logger.warning(f"Only {len(messages)} messages provided - clustering may not be effective")

        logger.info(f"Starting clustering of {len(messages)} messages")

        temp_data_dir = None
        try:
            from serve.hierarchical_discovery.orchestrator import HierarchicalDiscoveryOrchestrator

            normalized_campaign = "berkeley" if campaign_name.lower() == "berkley" else campaign_name.lower()

            logger.info("Using in-memory processing")

            raw_messages = self._convert_to_raw_messages(messages, normalized_campaign)

            temp_data_dir = Path(tempfile.mkdtemp())
            temp_config_path = self._create_temp_config(str(temp_data_dir), campaign_name, temp_csv_path="")

            orchestrator = HierarchicalDiscoveryOrchestrator(temp_config_path, data_source_override=normalized_campaign)

            hierarchical_config = getattr(orchestrator.config, 'hierarchical', {})
            multi_cluster_enabled = hierarchical_config.get('multi_cluster_analysis', False)

            if multi_cluster_enabled:
                pipeline_result = await orchestrator.run_multi_cluster_pipeline(
                    disable_optimization=True,
                    return_data=True,
                    in_memory_messages=raw_messages
                )
            else:
                pipeline_result = await orchestrator.run_pipeline(
                    disable_optimization=True,
                    return_data=True,
                    in_memory_messages=raw_messages
                )

            clustering_map = self._parse_clustering_results_from_objects(pipeline_result)

            if persistent_output_dir and temp_data_dir:
                self._preserve_discovery_outputs(temp_data_dir, persistent_output_dir, campaign_name)

            logger.info(f"Clustering completed successfully for {len(clustering_map)} messages")
            return clustering_map

        except Exception as e:
            logger.error(f"Clustering processing failed: {e}", exc_info=True)
            return {}

        finally:
            if temp_data_dir and temp_data_dir.exists():
                try:
                    import shutil
                    shutil.rmtree(str(temp_data_dir))
                    logger.debug(f"Cleaned up temporary directory: {temp_data_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp directory {temp_data_dir}: {e}")

    def _preserve_discovery_outputs(self, temp_dir: Path, persistent_dir: str, campaign_name: str):
        """
        Preserve hierarchical discovery outputs before temp cleanup
        Copies optimal k reports, analysis plots, and other artifacts to persistent storage
        """
        import shutil

        persistent_path = Path(persistent_dir)
        persistent_path.mkdir(parents=True, exist_ok=True)

        temp_output = temp_dir / "output"
        if not temp_output.exists():
            logger.debug(f"No temp output directory found at {temp_output}")
            return

        reports_dir = temp_output / "reports"
        plots_dir = temp_output / "plots"

        copied_files = []

        if reports_dir.exists():
            for report_file in reports_dir.glob("*.md"):
                dest = persistent_path / report_file.name
                shutil.copy2(report_file, dest)
                copied_files.append(report_file.name)
                logger.info(f"📄 Preserved report: {report_file.name}")

            for csv_file in reports_dir.glob("*.csv"):
                dest = persistent_path / csv_file.name
                shutil.copy2(csv_file, dest)
                copied_files.append(csv_file.name)
                logger.info(f"📊 Preserved CSV: {csv_file.name}")

        if plots_dir.exists():
            for plot_file in plots_dir.glob("*.png"):
                dest = persistent_path / plot_file.name
                shutil.copy2(plot_file, dest)
                copied_files.append(plot_file.name)
                logger.info(f"📈 Preserved plot: {plot_file.name}")

        if copied_files:
            logger.info(f"✅ Preserved {len(copied_files)} hierarchical discovery output files to {persistent_path}")
        else:
            logger.warning(f"⚠️ No hierarchical discovery outputs found to preserve in {temp_output}")

    def get_optimal_cluster_count(self) -> int:
        """Get the optimal cluster count from config (defaults to 15)"""
        clustering_config = self.config.get('clustering', {})
        n_clusters = clustering_config.get('n_clusters', [15])

        if isinstance(n_clusters, list):
            return 15 if 15 in n_clusters else n_clusters[0] if n_clusters else 15
        return int(n_clusters)


# Convenience function for direct usage
async def cluster_messages(messages: list[ConsolidatedMessage],
                          config_path: str | None = None,
                          campaign_name: str = "temp") -> dict[str, dict[str, Any]]:
    """
    Convenience function to cluster messages

    Args:
        messages: List of consolidated messages
        config_path: Path to hierarchical discovery config file
        campaign_name: Name for the campaign

    Returns:
        Dict mapping atomic_id to clustering result dictionaries
    """
    adapter = ClusteringAdapter(config_path)
    return await adapter.process_messages(messages, campaign_name)
