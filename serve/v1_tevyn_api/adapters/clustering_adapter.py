#!/usr/bin/env python3

import sys
import tempfile
from pathlib import Path
from typing import List, Dict, Optional
import pandas as pd
import asyncio
import yaml

# Add project paths
sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

# Import the existing hierarchical discovery pipeline
from serve.hierarchical_discovery.orchestrator import run_hierarchical_discovery_pipeline

# Import our models
from serve.v1_tevyn_api.models.unified_record import ConsolidatedMessage, ClusteringResult

logger = get_logger(__name__)


class ClusteringAdapter:
    """
    Adapter for the existing hierarchical discovery pipeline to work with unified records
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize clustering adapter"""
        self.config_path = config_path or str(Path(__file__).parent.parent.parent / "hierarchical_discovery/config.yaml")

        # Load the config to understand the system
        try:
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)
            logger.info("Hierarchical discovery config loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load hierarchical discovery config: {e}")
            raise

    def _convert_to_raw_messages(self, messages: List[ConsolidatedMessage], campaign_name: str) -> List:
        """Convert ConsolidatedMessage objects to RawMessage objects for hierarchical discovery"""
        from serve.hierarchical_discovery.models import RawMessage
        from datetime import datetime

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

    def _create_temp_csv(self, messages: List[ConsolidatedMessage], campaign_name: str = "temp") -> str:
        """Create temporary CSV file in format expected by hierarchical discovery pipeline"""
        temp_dir = Path(tempfile.mkdtemp())
        temp_file = temp_dir / f"{campaign_name}_all_rounds_consolidated.csv"

        # Create DataFrame in expected format for hierarchical discovery
        data = []
        for i, msg in enumerate(messages):
            data.append({
                'Campaign ID': msg.campaign_id or 'temp_campaign',
                'Campaign Name': msg.campaign_name or 'Temp Campaign',
                'Contact Phone Number': msg.phone_number,
                'Carrier': msg.carrier or 'UNKNOWN',
                'Campaign Number': '+13132038028',
                'Is Automatic Reply?': 'FALSE',
                'Send Direction': 'INBOUND',
                'Send Status': '',
                'Error Code': '',
                'Sent At': msg.sent_at.isoformat() if hasattr(msg.sent_at, 'isoformat') else str(msg.sent_at),
                'Message Text': msg.message_text,
                'Texter Name': '',
                'Message Type': 'SMS',
                'MMS Attachments': '',
                'round': msg.round,
                'source_file': f'{campaign_name}_all_rounds_consolidated.csv'
            })

        df = pd.DataFrame(data)
        df.to_csv(temp_file, index=False)
        logger.info(f"📝 Created temporary CSV with {len(data)} messages: {temp_file}")

        # Verify phone numbers in temp CSV
        unique_phones = df['Contact Phone Number'].nunique()
        logger.info(f"   - {unique_phones} unique phone numbers")
        logger.info(f"   - Sample phones: {df['Contact Phone Number'].head(5).tolist()}")

        return str(temp_file)

    def _create_temp_config(self, temp_data_dir: str, campaign_name: str, temp_csv_path: str) -> str:
        """Create temporary config file for hierarchical discovery"""
        temp_config = self.config.copy()

        # Modify config for our temporary data
        # Normalize campaign name for hierarchical discovery (berkley -> berkeley)
        normalized_campaign = "berkeley" if campaign_name.lower() == "berkley" else campaign_name.lower()
        temp_config['data_source'] = normalized_campaign

        # CRITICAL: Override data_files to point to our temp CSV
        if 'data_files' not in temp_config:
            temp_config['data_files'] = {}
        temp_config['data_files'][normalized_campaign] = temp_csv_path
        logger.info(f"📂 Configured data_files['{normalized_campaign}'] = {temp_csv_path}")

        # Set output directory to temp location
        temp_output_dir = str(Path(temp_data_dir) / "output")
        temp_config['output']['base_dir'] = temp_output_dir

        # Enable content filtering to remove STOP messages and emoji reactions
        temp_config['filtering']['enabled'] = True
        logger.info("✅ Content filtering ENABLED to remove STOP messages and emoji reactions")

        # Enable AI processing for message cleaning and splitting
        temp_config['ai_processing']['enabled'] = True
        logger.info("✅ AI processing ENABLED for message cleaning")

        # Optimize for speed and simplicity
        temp_config['hierarchical']['n_clusters'] = 15  # Focus on 15 clusters only
        temp_config['dendrogram']['enabled'] = False  # Skip dendrograms for speed
        temp_config['output']['save_intermediates'] = False

        # Create temp config file
        temp_config_file = str(Path(temp_data_dir) / "temp_config.yaml")
        with open(temp_config_file, 'w') as f:
            yaml.dump(temp_config, f)

        return temp_config_file

    def _parse_clustering_results(self, result_file: Path) -> Dict[str, ClusteringResult]:
        """Parse hierarchical clustering results from output CSV"""
        clustering_map = {}

        try:
            if not result_file.exists():
                logger.warning(f"Clustering result file not found: {result_file}")
                return {}

            # Read the multi-cluster results CSV
            df = pd.read_csv(result_file)
            logger.debug(f"Processing {len(df)} clustering results from {result_file}")

            for _, row in df.iterrows():
                phone_number = str(row.get('phone_number', ''))
                if not phone_number:
                    continue

                # Parse key topics from string
                key_topics_str = row.get('key_topics_15', '')
                key_topics = []
                if key_topics_str and pd.notna(key_topics_str):
                    # Split by comma and clean up
                    key_topics = [topic.strip() for topic in str(key_topics_str).split(',') if topic.strip()]

                clustering_result = ClusteringResult(
                    phone_number=phone_number,
                    cluster_id=int(row.get('cluster_15', -1)) if pd.notna(row.get('cluster_15')) else -1,
                    cluster_theme=str(row.get('theme_15', 'Uncategorized')),
                    cluster_category=str(row.get('category_15', 'Other')),
                    key_topics=key_topics,
                    cluster_sentiment=str(row.get('sentiment_15', 'neutral')),
                    civic_relevance=str(row.get('civic_relevance_15', 'General civic engagement')),
                    theme_confidence=float(row.get('confidence_score_15', 0.0)) if pd.notna(row.get('confidence_score_15')) else 0.0,
                    detailed_analysis=str(row.get('detailed_analysis_15', '')) if pd.notna(row.get('detailed_analysis_15')) else None,
                    verbatim_quotes=str(row.get('verbatim_quotes_15', '')) if pd.notna(row.get('verbatim_quotes_15')) else None
                )

                clustering_map[phone_number] = clustering_result

            logger.info(f"Successfully parsed {len(clustering_map)} clustering results")

        except Exception as e:
            logger.error(f"Failed to parse clustering results from {result_file}: {e}")

        return clustering_map

    def _parse_clustering_results_from_objects(self, pipeline_result) -> Dict[str, ClusteringResult]:
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

                # Group messages by phone number first (since one original message can split into multiple atomic messages)
                phone_to_messages = {}
                for msg_data in messages:
                    phone_number = str(msg_data.get('phone_number', ''))
                    if not phone_number:
                        logger.warning(f"🔍 DEBUG Skipping message with no phone number: {list(msg_data.keys())[:5]}")
                        continue

                    if phone_number not in phone_to_messages:
                        phone_to_messages[phone_number] = []
                    phone_to_messages[phone_number].append(msg_data)

                logger.info(f"🔍 Grouped {len(messages)} atomic messages into {len(phone_to_messages)} unique phone numbers")

                # Process each phone number (aggregate data if multiple atomic messages exist)
                for phone_number, msg_list in phone_to_messages.items():
                    # Use the first message's cluster assignment (all atomic messages from same phone should have similar clusters)
                    msg_data = msg_list[0]

                    # If multiple atomic messages exist for this phone, log it
                    if len(msg_list) > 1:
                        logger.debug(f"Phone {phone_number} has {len(msg_list)} atomic messages, using first for clustering")

                    # Extract ALL cluster configurations (not just one)
                    cluster_assignments = msg_data.get('cluster_assignments', {})
                    cluster_themes = msg_data.get('cluster_themes', {})
                    cluster_key_topics = msg_data.get('cluster_key_topics', {})
                    cluster_sentiments = msg_data.get('cluster_sentiments', {})
                    cluster_categories = msg_data.get('cluster_categories', {})
                    cluster_civic_relevance = msg_data.get('cluster_civic_relevance', {})
                    cluster_confidence_scores = msg_data.get('cluster_confidence_scores', {})
                    cluster_detailed_analyses = msg_data.get('cluster_detailed_analyses', {})
                    cluster_verbatim_quotes = msg_data.get('cluster_verbatim_quotes', {})

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

                            multi_cluster_data[cluster_count] = {
                                'cluster_id': int(cluster_assignments[cluster_count]) if cluster_assignments[cluster_count] != '' else -1,
                                'cluster_theme': str(cluster_themes.get(cluster_count, 'Uncategorized')),
                                'cluster_category': str(cluster_categories.get(cluster_count, 'Other')),
                                'key_topics': key_topics,
                                'cluster_sentiment': str(cluster_sentiments.get(cluster_count, 'neutral')),
                                'civic_relevance': str(cluster_civic_relevance.get(cluster_count, 'General civic engagement')),
                                'theme_confidence': float(cluster_confidence_scores.get(cluster_count, 0.0)) if cluster_confidence_scores.get(cluster_count) else 0.0,
                                'detailed_analysis': str(cluster_detailed_analyses.get(cluster_count, '')) if cluster_detailed_analyses.get(cluster_count) else None,
                                'verbatim_quotes': verbatim_quotes
                            }

                    # Store multi-cluster data for this phone number
                    if multi_cluster_data:
                        clustering_map[phone_number] = {'cluster_data': multi_cluster_data}

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
            logger.error(f"Failed to parse clustering results from objects: {e}")
            logger.debug(f"Pipeline result type: {type(pipeline_result)}")
            if hasattr(pipeline_result, '__dict__'):
                logger.debug(f"Pipeline result attributes: {list(pipeline_result.__dict__.keys())}")
            elif isinstance(pipeline_result, dict):
                logger.debug(f"Pipeline result keys: {list(pipeline_result.keys())}")

        return clustering_map

    async def process_messages(self, messages: List[ConsolidatedMessage], campaign_name: str = "temp", anonymize_keywords: List[str] = None) -> Dict[str, ClusteringResult]:
        """
        Process messages through hierarchical discovery pipeline

        Args:
            messages: List of consolidated messages to cluster
            campaign_name: Name for the temporary campaign
            anonymize_keywords: List of keywords to anonymize during AI summarization

        Returns:
            Dict mapping phone numbers to clustering results
        """
        if not messages:
            logger.warning("No messages provided for clustering")
            return {}

        if len(messages) < 3:
            logger.warning(f"Only {len(messages)} messages provided - clustering may not be effective")

        logger.info(f"Starting clustering of {len(messages)} messages")

        # Check if we should use in-memory processing (no CSV)
        use_in_memory = self.config.get('clustering', {}).get('return_raw', True)

        temp_data_dir = None
        try:
            # Import orchestrator
            from serve.hierarchical_discovery.orchestrator import HierarchicalDiscoveryOrchestrator

            # Normalize campaign name for hierarchical discovery
            normalized_campaign = "berkeley" if campaign_name.lower() == "berkley" else campaign_name.lower()

            if use_in_memory:
                logger.info("Using in-memory processing (no CSV intermediary)")

                # Convert ConsolidatedMessage → RawMessage objects
                raw_messages = self._convert_to_raw_messages(messages, normalized_campaign)

                # Create minimal temp config (no CSV paths needed)
                temp_data_dir = Path(tempfile.mkdtemp())
                temp_config_path = self._create_temp_config(str(temp_data_dir), campaign_name, temp_csv_path="")

                orchestrator = HierarchicalDiscoveryOrchestrator(temp_config_path, data_source_override=normalized_campaign)

                # Check if multi-cluster analysis is enabled
                hierarchical_config = getattr(orchestrator.config, 'hierarchical', {})
                multi_cluster_enabled = hierarchical_config.get('multi_cluster_analysis', False)

                if multi_cluster_enabled:
                    pipeline_result = await orchestrator.run_multi_cluster_pipeline(
                        anonymize_keywords=anonymize_keywords,
                        disable_optimization=True,
                        return_data=True,
                        in_memory_messages=raw_messages
                    )
                else:
                    pipeline_result = await orchestrator.run_pipeline(
                        anonymize_keywords=anonymize_keywords,
                        disable_optimization=True,
                        return_data=True,
                        in_memory_messages=raw_messages
                    )

            else:
                logger.info("Using CSV intermediary (legacy mode)")

                # Create temporary data structure
                temp_data_dir = Path(tempfile.mkdtemp())
                logger.debug(f"Created temporary directory: {temp_data_dir}")

                # Create temporary CSV file
                temp_csv_path = self._create_temp_csv(messages, campaign_name)

                # Create temporary config
                temp_config_path = self._create_temp_config(str(temp_data_dir), campaign_name, temp_csv_path)

                orchestrator = HierarchicalDiscoveryOrchestrator(temp_config_path, data_source_override=normalized_campaign)

                # Check if multi-cluster analysis is enabled
                hierarchical_config = getattr(orchestrator.config, 'hierarchical', {})
                multi_cluster_enabled = hierarchical_config.get('multi_cluster_analysis', False)

                if multi_cluster_enabled:
                    pipeline_result = await orchestrator.run_multi_cluster_pipeline(
                        anonymize_keywords=anonymize_keywords,
                        disable_optimization=True,
                        return_data=True
                    )
                else:
                    pipeline_result = await orchestrator.run_pipeline(
                        anonymize_keywords=anonymize_keywords,
                        disable_optimization=True,
                        return_data=True
                    )

            # Parse results from returned data objects
            clustering_map = self._parse_clustering_results_from_objects(pipeline_result)

            logger.info(f"Clustering completed successfully for {len(clustering_map)} messages")
            return clustering_map

        except Exception as e:
            logger.error(f"Clustering processing failed: {e}")
            # Return empty results with default values for all messages
            return {msg.phone_number: ClusteringResult(phone_number=msg.phone_number) for msg in messages}

        finally:
            # Cleanup temp files
            if temp_data_dir and temp_data_dir.exists():
                try:
                    import shutil
                    shutil.rmtree(str(temp_data_dir))
                    logger.debug(f"Cleaned up temporary directory: {temp_data_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp directory {temp_data_dir}: {e}")

    async def process_messages_batch(self, messages: List[ConsolidatedMessage], batch_size: int = 500, campaign_name: str = "temp", anonymize_keywords: List[str] = None) -> Dict[str, ClusteringResult]:
        """
        Process messages in batches for better performance

        Args:
            messages: List of messages to cluster
            batch_size: Number of messages per batch
            campaign_name: Name for the temporary campaign
            anonymize_keywords: List of keywords to anonymize during AI summarization

        Returns:
            Combined clustering results
        """
        # For clustering, we typically want to process all messages together
        # to get meaningful clusters. So we'll only batch if we have a very large dataset
        if len(messages) <= batch_size:
            return await self.process_messages(messages, campaign_name, anonymize_keywords)

        logger.info(f"Processing {len(messages)} messages in batches for clustering")
        all_results = {}

        # Process messages in batches
        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]
            batch_name = f"{campaign_name}_batch_{i//batch_size + 1}"
            logger.info(f"Processing clustering batch {i//batch_size + 1} ({len(batch)} messages)")

            batch_results = await self.process_messages(batch, batch_name, anonymize_keywords)
            all_results.update(batch_results)

            # Small delay between batches
            await asyncio.sleep(0.5)

        return all_results

    def get_optimal_cluster_count(self) -> int:
        """Get the optimal cluster count from config (defaults to 15)"""
        clustering_config = self.config.get('clustering', {})
        n_clusters = clustering_config.get('n_clusters', [15])

        if isinstance(n_clusters, list):
            return 15 if 15 in n_clusters else n_clusters[0] if n_clusters else 15
        return int(n_clusters)


# Convenience function for direct usage
async def cluster_messages(messages: List[ConsolidatedMessage],
                          config_path: Optional[str] = None,
                          campaign_name: str = "temp") -> Dict[str, ClusteringResult]:
    """
    Convenience function to cluster messages

    Args:
        messages: List of consolidated messages
        config_path: Path to hierarchical discovery config file
        campaign_name: Name for the campaign

    Returns:
        Dict mapping phone numbers to clustering results
    """
    adapter = ClusteringAdapter(config_path)
    return await adapter.process_messages(messages, campaign_name)