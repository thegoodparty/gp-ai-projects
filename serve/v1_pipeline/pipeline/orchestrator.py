#!/usr/bin/env python3

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serve.v1_pipeline.pipeline.sqs_publisher import SQSEventPublisher

import pandas as pd
import yaml

project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from serve.v1_pipeline.adapters.clustering_adapter import ClusteringAdapter

# Import models
from serve.v1_pipeline.models.unified_record import (
    ConsolidatedMessage,
    PipelineResult,
    UnifiedCampaignRecord,
)
from shared.logger import get_logger

logger = get_logger(__name__)


class V1PipelineOrchestrator:
    """
    Main orchestrator for the V1 Message Analysis Pipeline
    Coordinates consolidation, clustering (discovery), and event publishing stages
    """

    def __init__(self, config_path: str | None = None):
        """Initialize the pipeline orchestrator"""
        self.config_path = config_path or str(Path(__file__).parent.parent / "config/pipeline_config.yaml")

        # Track original config directory for path resolution (in case config_path is a temp file)
        self.original_config_dir = Path(__file__).parent.parent / 'config'

        # Load configuration
        self.config = self._load_config()

        # Initialize components
        self.input_dir: str | None = None
        self.output_dir: str | None = None
        self.clusterer: 'ClusteringAdapter' | None = None
        self.sqs_publisher: 'SQSEventPublisher' | None = None

        # Pipeline state
        self.errors: list[str] = []
        self.warnings: list[str] = []

        logger.info("V1 Pipeline Orchestrator initialized")

    def _load_config(self) -> dict[str, Any]:
        """Load pipeline configuration"""
        try:
            if Path(self.config_path).exists():
                with open(self.config_path) as f:
                    config = yaml.safe_load(f)
                logger.info(f"Loaded configuration from {self.config_path}")

                # Resolve relative paths relative to config file directory
                config = self._resolve_config_paths(config)

                # Substitute environment variables
                config = self._substitute_env_vars(config)
            else:
                # Default configuration
                config = self._get_default_config()
                logger.warning(f"Config file not found, using defaults: {self.config_path}")

            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}", exc_info=True)
            return self._get_default_config()

    def _resolve_config_paths(self, config: dict[str, Any]) -> dict[str, Any]:
        """Resolve relative paths in config to be relative to original config file directory"""
        # Use original config directory, not temp file directory
        config_dir = self.original_config_dir
        logger.info(f"🔍 Config directory: {config_dir}")

        # Resolve consolidation paths
        if 'consolidation' in config:
            if 'input_dir' in config['consolidation']:
                original_input = config['consolidation']['input_dir']
                input_path = Path(original_input)
                if not input_path.is_absolute():
                    resolved_path = config_dir / input_path
                    resolved_absolute = str(resolved_path.resolve())
                    config['consolidation']['input_dir'] = resolved_absolute
                    logger.info(f"🔍 Input dir: {original_input} -> {resolved_absolute}")

            if 'output_dir' in config['consolidation']:
                original_output = config['consolidation']['output_dir']
                output_path = Path(original_output)
                if not output_path.is_absolute():
                    resolved_path = config_dir / output_path
                    resolved_absolute = str(resolved_path.resolve())
                    config['consolidation']['output_dir'] = resolved_absolute
                    logger.info(f"🔍 Output dir: {original_output} -> {resolved_absolute}")

        return config

    def _substitute_env_vars(self, config: dict[str, Any]) -> dict[str, Any]:
        """Recursively substitute environment variables in config values"""
        import re

        def substitute_value(value: Any) -> Any:
            if isinstance(value, str):
                pattern = r'\$\{([^}]+)\}'
                matches = re.findall(pattern, value)
                for var_name in matches:
                    env_value = os.getenv(var_name, '')
                    value = value.replace(f'${{{var_name}}}', env_value)
                return value
            elif isinstance(value, dict):
                return {k: substitute_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [substitute_value(item) for item in value]
            else:
                return value

        result = substitute_value(config)
        return result if isinstance(result, dict) else config

    def _get_default_config(self) -> dict[str, Any]:
        """Get default configuration"""
        return {
            'pipeline': {
                'mode': 'integrated'
            },
            'consolidation': {
                'input_dir': '../input',
                'output_dir': './output'
            },
            'clustering': {
                'enabled': True,
                'min_messages_for_clustering': 10,
                'skip_on_error': True
            },
            'top_clusters': {
                'enabled': False,
                'count': 3,
                'min_respondents': 10,
                'llm_model': 'flash',
                'temperature': 0.0,
                'skip_on_error': True
            }
        }

    def _initialize_components(self) -> None:
        """Initialize pipeline components"""
        try:
            # Set input and output directories
            consolidation_config = self.config.get('consolidation', {})
            self.input_dir = consolidation_config.get('input_dir', '../input')
            self.output_dir = consolidation_config.get('output_dir', './output')

            logger.info(f"🔍 Input directory: {self.input_dir}")
            logger.info(f"🔍 Output directory: {self.output_dir}")

            # Create output directory if it doesn't exist
            if self.output_dir:
                Path(self.output_dir).mkdir(parents=True, exist_ok=True)

            # Initialize clusterer if enabled
            if self.config.get('clustering', {}).get('enabled', True):
                self.clusterer = ClusteringAdapter()


            # Initialize SQS publisher if enabled
            if self.config.get('sqs_events', {}).get('enabled', False):
                from serve.v1_pipeline.pipeline.sqs_publisher import SQSEventPublisher
                sqs_config = self.config.get('sqs_events', {}).copy()
                sqs_config['output_dir'] = self.output_dir
                self.sqs_publisher = SQSEventPublisher(sqs_config)

            logger.info("All pipeline components initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize components: {e}", exc_info=True)
            raise

    def _convert_to_consolidated_messages(self, df: pd.DataFrame, campaign_name: str, poll_id: str | None = None) -> list[ConsolidatedMessage]:
        """
        Convert consolidated DataFrame to ConsolidatedMessage objects
        Supports bare-bones CSVs with only required fields: phone_number, message_text
        All demographic fields are optional
        """
        messages = []

        for _, row in df.iterrows():
            try:
                sent_at_raw = row.get('sent_at', row.get('Sent At', None))
                sent_at = datetime.now(timezone.utc)

                if pd.notna(sent_at_raw) and sent_at_raw:
                    try:
                        timestamp_str = str(sent_at_raw)
                        timestamp_str = timestamp_str.replace('.000Z', 'Z').replace('..', '.')
                        sent_at = pd.to_datetime(timestamp_str)
                    except Exception:
                        pass

                # Handle both old format (Contact Phone Number) and new format (phone_number)
                phone_number = str(row.get('phone_number', row.get('Contact Phone Number', ''))).strip()

                # Required: message_text
                message_text = str(row.get('message_text', row.get('Message Text', '')))

                # Validate required fields
                if not phone_number or not message_text:
                    logger.warning(f"Skipping row: missing required fields (phone_number={phone_number}, message_text={message_text})")
                    continue

                # Determine poll_id: CSV poll_id column > filename > campaign_name
                row_poll_id = poll_id
                if pd.notna(row.get('poll_id')):
                    row_poll_id = str(row.get('poll_id'))
                elif poll_id is None:
                    row_poll_id = campaign_name

                # Optional: round (default to 'R1' if missing)
                round_val = str(row.get('round', 'R1'))

                message = ConsolidatedMessage(
                    phone_number=phone_number,
                    message_text=message_text,
                    sent_at=sent_at,
                    round=round_val,
                    campaign_id=campaign_name,
                    campaign_name=str(row.get('Campaign Name', campaign_name)),
                    carrier=str(row.get('Carrier')) if pd.notna(row.get('Carrier')) else None,
                    poll_id=row_poll_id
                )

                messages.append(message)

            except Exception as e:
                logger.warning(f"Failed to convert row to ConsolidatedMessage: {e}")
                continue

        logger.info(f"Converted {len(messages)} rows to ConsolidatedMessage objects")
        return messages

    async def run_pipeline(self, campaign_name: str) -> PipelineResult:
        """
        Run the complete pipeline for a campaign

        Args:
            campaign_name: Name of the campaign to process

        Returns:
            PipelineResult with processing details and statistics
        """
        start_time = time.time()
        logger.info(f"🚀 Starting V1 Pipeline for campaign: {campaign_name}")

        try:
            # Initialize components
            self._initialize_components()

            # Stage 1: Data Consolidation
            logger.info("📊 Stage 1: Data Consolidation")
            consolidation_start = time.time()

            consolidated_df, consolidation_analysis = await self._run_consolidation_stage(campaign_name)
            consolidation_time = time.time() - consolidation_start

            if consolidated_df.empty:
                logger.warning(f"⚠️ No messages found for campaign: {campaign_name}")
                logger.info("✅ Pipeline completed with 0 messages to process")

                # Publish completion events for all poll_ids with 0 responses
                sqs_result: dict[str, Any] = {}
                if self.sqs_publisher and self.config.get('sqs_events', {}).get('enabled', False):
                    logger.info("💾 Publishing empty poll completion events")

                    # Extract all poll_ids from consolidation analysis
                    poll_ids = [file_info['poll_id'] for file_info in consolidation_analysis.get('files', [])]

                    if poll_ids:
                        total_complete_events = 0
                        for poll_id in poll_ids:
                            try:
                                stats = await self.sqs_publisher.publish_empty_poll_event(poll_id)
                                total_complete_events += stats.get('complete_events_sent', 0)
                            except Exception as e:
                                logger.error(f"Failed to publish empty poll event for {poll_id}: {e}", exc_info=True)

                        sqs_result = {
                            'success': True,
                            'complete_events_sent': total_complete_events
                        }
                        logger.info(f"✅ Published {total_complete_events} empty poll completion events")
                    else:
                        logger.warning("⚠️ No poll_ids found in consolidation analysis")

                processing_time = time.time() - start_time
                return PipelineResult(
                    campaign_id=campaign_name,
                    input_messages=0,
                    atomic_messages=0,
                    output_records=0,
                    processing_time=processing_time,
                    consolidation_result=consolidation_analysis,
                    clustering_result={'success': True, 'processed_messages': 0},
                    sqs_result=sqs_result,
                    errors=[],
                    warnings=["No messages found in CSV files"]
                )

            logger.info(f"✅ Consolidation completed in {consolidation_time:.2f}s")

            # Extract poll_id from consolidation analysis (use first file's poll_id as fallback)
            # If CSV has poll_id or campaign_id column, that will override this in _convert_to_consolidated_messages
            poll_id_fallback: str | None = campaign_name
            if consolidation_analysis.get('files'):
                poll_id_fallback = consolidation_analysis['files'][0].get('poll_id', campaign_name)

            # Check if CSV has poll_id column (normalized column names)
            has_poll_id_column = 'poll_id' in consolidated_df.columns
            if has_poll_id_column:
                logger.info("Found poll_id column in CSV - will use values from column")
                poll_id_fallback = None  # Signal to use per-row values
            else:
                logger.info(f"No poll_id column found - using filename as poll_id: {poll_id_fallback}")

            # Convert to message objects
            messages = self._convert_to_consolidated_messages(consolidated_df, campaign_name, poll_id_fallback)
            total_messages = len(messages)

            # Stage 2: Clustering
            clustering_result: dict[str, Any] = {}
            clustering_results = {}

            if self.clusterer and self.config.get('clustering', {}).get('enabled', True):
                logger.info("🔍 Stage 2: Message Clustering")
                clustering_start = time.time()

                try:
                    clustering_results = await self._run_clustering_stage(messages, campaign_name)
                    clustering_time = time.time() - clustering_start

                    if not clustering_results:
                        logger.warning("⚠️ Clustering produced 0 results - all messages may have been filtered out")
                        logger.info(f"✅ Clustering completed in {clustering_time:.2f}s (0 clustered messages)")
                    else:
                        logger.info(f"✅ Clustering completed in {clustering_time:.2f}s")

                        # Debug: Check clustering results
                        sample_phone = list(clustering_results.keys())[0] if clustering_results else None
                        if sample_phone:
                            sample_result = clustering_results[sample_phone]
                            logger.info(f"🔍 DEBUG Sample clustering result for {sample_phone}: {type(sample_result)}")
                            if isinstance(sample_result, dict):
                                logger.info(f"🔍 DEBUG Sample keys: {list(sample_result.keys())}")
                                if 'cluster_data' in sample_result:
                                    logger.info(f"🔍 DEBUG cluster_data keys: {list(sample_result['cluster_data'].keys())}")
                            logger.info(f"🔍 DEBUG Total clustering results: {len(clustering_results)}")

                    clustering_result = {
                        'processed_messages': len(clustering_results),
                        'processing_time': clustering_time,
                        'success': True
                    }
                except (ValueError, OSError, RuntimeError, ImportError, KeyError) as e:
                    logger.error(f"Clustering stage failed: {e}", exc_info=True)
                    self.errors.append(f"Clustering stage failed: {str(e)}")
                    clustering_results = {}
                    clustering_result = {'success': False, 'error': str(e), 'processed_messages': 0}
                    if not self.config.get('clustering', {}).get('skip_on_error', True):
                        raise
                except Exception as e:
                    logger.error(f"Clustering stage failed with unexpected error: {e}", exc_info=True)
                    self.errors.append(f"Clustering stage failed: {str(e)}")
                    clustering_results = {}
                    clustering_result = {'success': False, 'error': str(e), 'processed_messages': 0}
                    raise
            else:
                clustering_results = {}

            # Stage 3: Data Merging
            logger.info("🔄 Stage 3: Data Merging")
            import asyncio
            unified_records = await asyncio.to_thread(self._merge_all_data, messages, clustering_results, campaign_name)

            # Stage 3.5: LLM-Based Cluster Recommendations
            top_clusters_recommendations = None
            top_clusters_assessment = None
            if self.config.get('top_clusters', {}).get('enabled', True) and unified_records:
                logger.info("🤖 Stage 3.5: Generating LLM-based cluster recommendations...")
                recommendation_start = time.time()

                try:
                    from serve.v1_pipeline.stages.llm_cluster_recommender import (
                        format_recommendations_for_logging,
                        recommend_top_clusters_via_llm,
                    )

                    top_clusters_recommendations, top_clusters_assessment = await recommend_top_clusters_via_llm(
                        unified_records,
                        self.config
                    )

                    recommendation_time = time.time() - recommendation_start

                    if top_clusters_recommendations:
                        formatted_output = format_recommendations_for_logging(
                            top_clusters_recommendations,
                            top_clusters_assessment
                        )
                        logger.info(f"\n{formatted_output}")
                        logger.info(f"✅ Recommendations generated in {recommendation_time:.2f}s")
                    else:
                        logger.warning("⚠️ No cluster recommendations generated - no substantive clusters found")
                        logger.info("   This may occur if: (1) all messages were filtered out during clustering, or")
                        logger.info(f"   (2) no clusters meet the minimum threshold ({self.config.get('top_clusters', {}).get('min_respondents', 10)} unique respondents)")

                except Exception as e:
                    logger.error(f"Cluster recommendation stage failed: {e}", exc_info=True)
                    self.errors.append(f"Cluster recommendation stage failed: {str(e)}")
                    if not self.config.get('top_clusters', {}).get('skip_on_error', True):
                        raise

            # At this point, all stages completed successfully
            # Stage 4: Event Saving to S3 (validation mode - only runs on successful pipeline completion)
            sqs_result = {}
            if self.sqs_publisher and self.config.get('sqs_events', {}).get('enabled', False):
                logger.info("💾 Stage 4: Event Saving to S3 (Validation Mode)")
                sqs_start = time.time()

                # Check if all messages were filtered out (had CSV data but 0 unified records)
                if not unified_records and total_messages > 0:
                    logger.warning(f"⚠️ All {total_messages} messages were filtered out - publishing empty poll completion events")

                    # Extract poll_ids from consolidation analysis
                    poll_ids = [file_info['poll_id'] for file_info in consolidation_analysis.get('files', [])]

                    total_complete_events = 0
                    for poll_id in poll_ids:
                        try:
                            stats = await self.sqs_publisher.publish_empty_poll_event(poll_id)
                            total_complete_events += stats.get('complete_events_sent', 0)
                        except Exception as e:
                            logger.error(f"Failed to publish empty poll event for {poll_id}: {e}", exc_info=True)

                    sqs_stats = {
                        'polls_processed': len(poll_ids),
                        'complete_events_sent': total_complete_events
                    }
                else:
                    # Normal flow - publish events from unified records
                    sqs_stats = await self.sqs_publisher.publish_events(unified_records)

                sqs_time = time.time() - sqs_start

                logger.info(f"✅ Event saving completed in {sqs_time:.2f}s")
                logger.info(f"   Saved {sqs_stats['complete_events_sent']} complete events")

                sqs_result = {
                    'success': True,
                    'processing_time': float(sqs_time),
                    **sqs_stats
                }
            else:
                logger.info("⏭️ Skipping event saving (disabled in config)")

            # Generate final results
            processing_time = time.time() - start_time
            input_messages = total_messages
            atomic_messages = len(clustering_results)
            output_records = len(unified_records)

            result = PipelineResult(
                campaign_id=campaign_name,
                input_messages=input_messages,
                atomic_messages=atomic_messages,
                output_records=output_records,
                processing_time=processing_time,
                consolidation_result=consolidation_analysis,
                clustering_result=clustering_result,
                sqs_result=sqs_result,
                errors=self.errors,
                warnings=self.warnings
            )

            logger.info("🎉 Pipeline completed successfully!")
            logger.info(f"📈 Processing Summary: {result.summary}")

            return result

        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"💥 Pipeline failed after {processing_time:.2f}s: {e}", exc_info=True)

            # Return failed result
            return PipelineResult(
                campaign_id=campaign_name,
                input_messages=0,
                atomic_messages=0,
                output_records=0,
                processing_time=processing_time,
                consolidation_result={'success': False, 'error': str(e)},
                clustering_result={},
                errors=[str(e)] + self.errors,
                warnings=self.warnings
            )

    async def _run_consolidation_stage(self, campaign_name: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Load CSV files directly - filename becomes poll_id"""
        try:
            if not self.input_dir:
                raise ValueError("Input directory not configured")
            input_dir = Path(self.input_dir)

            campaign_dir = input_dir / campaign_name.lower()
            if campaign_dir.exists() and campaign_dir.is_dir():
                logger.info(f"Using campaign-specific directory: {campaign_dir}")
                csv_files = list(campaign_dir.glob("*.csv"))
            else:
                all_csv_files = list(input_dir.glob("*.csv"))

                campaign_lower = campaign_name.lower()
                csv_files = [
                    f for f in all_csv_files
                    if campaign_lower in f.stem.lower()
                ]

                if not csv_files:
                    logger.warning(f"No CSV files found matching campaign '{campaign_name}'")
                    logger.info(f"Available files in {input_dir}:")
                    for f in all_csv_files[:10]:
                        logger.info(f"  - {f.name}")
                    if len(all_csv_files) > 10:
                        logger.info(f"  ... and {len(all_csv_files) - 10} more")
                    raise ValueError(
                        f"No CSV files found for campaign '{campaign_name}'. "
                        f"Expected files containing '{campaign_lower}' in filename, "
                        f"or a subdirectory at {campaign_dir}"
                    )

            if not csv_files:
                raise ValueError(f"No CSV files found in {input_dir}")

            logger.info(f"Found {len(csv_files)} CSV file(s) for campaign '{campaign_name}' in {input_dir}")

            dfs = []
            poll_ids = []
            for csv_file in csv_files:
                poll_id = csv_file.stem
                poll_ids.append(poll_id)

                logger.info(f"Loading: {csv_file.name} (poll_id: {poll_id})")
                df = pd.read_csv(csv_file)
                df = self._normalize_csv_columns(df)
                dfs.append(df)

            combined_df = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]

            # Validate required columns (case-insensitive)
            columns_lower = [col.lower() for col in combined_df.columns]

            # Check for phone_number column (various formats)
            has_phone = any(
                col in columns_lower
                for col in ['phone_number', 'contact phone number', 'phone', 'contact_phone_number']
            )
            if not has_phone:
                raise ValueError(
                    f"CSV must contain phone number column (phone_number, Contact Phone Number, or phone). "
                    f"Found columns: {list(combined_df.columns)}"
                )

            # Check for message_text column (various formats)
            has_message = any(
                col in columns_lower
                for col in ['message_text', 'message text', 'message', 'text', 'body']
            )
            if not has_message:
                raise ValueError(
                    f"CSV must contain message text column (message_text, Message Text, message, text, or body). "
                    f"Found columns: {list(combined_df.columns)}"
                )

            analysis = {
                "mode": "filename_based_loading",
                "file_count": len(csv_files),
                "total_rows": len(combined_df),
                "files": [{"filename": f.name, "poll_id": poll_id} for f, poll_id in zip(csv_files, poll_ids)]
            }

            logger.info(f"Loaded {len(combined_df)} rows from {len(csv_files)} file(s)")
            logger.info(f"Poll IDs: {poll_ids}")
            return combined_df, analysis

        except Exception as e:
            logger.error(f"Consolidation stage failed: {e}", exc_info=True)
            raise

    def _normalize_csv_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize CSV column names to standard format
        Handles Serve CSV format with columns like 'Contact Phone Number', 'Message Text', 'Sent At'
        """
        column_mappings = {
            'phone_number': ['phone_number', 'Contact Phone Number', 'phone', 'Phone', 'contact_phone'],
            'message_text': ['message_text', 'Message Text', 'message', 'text', 'body', 'Message'],
            'sent_at': ['sent_at', 'Sent At', 'timestamp', 'date', 'created_at'],
            'round': ['round', 'Round', 'contact_round', 'Round Number'],
            'poll_id': ['poll_id', 'Poll ID'],
            'campaign_name': ['campaign_name', 'Campaign Name'],
            'carrier': ['carrier', 'Carrier']
        }

        for standard_name, possible_names in column_mappings.items():
            for col in df.columns:
                if col in possible_names and standard_name not in df.columns:
                    df[standard_name] = df[col]
                    logger.debug(f"Mapped column: {col} → {standard_name}")
                    break

        return df

    async def _run_clustering_stage(self, messages: list[ConsolidatedMessage], campaign_name: str) -> dict[str, dict[str, Any]]:
        """Run the clustering stage"""
        if not self.output_dir:
            raise ValueError("Output directory not configured")
        if not self.clusterer:
            raise ValueError("Clusterer not initialized")

        # Log dataset size for context
        logger.info(f"Running clustering on {len(messages)} messages")

        # Let hierarchical_discovery handle all dataset sizes:
        # - 1 message: Direct LLM analysis
        # - 2-9 messages: Limited clustering or batch analysis
        # - 10+ messages: Full hierarchical clustering with optimal k

        output_dir = Path(self.output_dir) / "discovery_reports"
        output_dir.mkdir(parents=True, exist_ok=True)

        result = await self.clusterer.process_messages(messages, campaign_name, persistent_output_dir=str(output_dir))
        return result

    def _merge_all_data(self,
                       messages: list[ConsolidatedMessage],
                       clustering_results: dict[str, dict[str, Any]],
                       campaign_name: str) -> list[UnifiedCampaignRecord]:
        """
        Merge all data sources into unified records

        Note: clustering_results is now keyed by atomic_id, not phone_number.
        One ConsolidatedMessage may map to multiple atomic messages (if message was split).
        """
        unified_records = []

        # Build phone number lookup map for fast access
        phone_map = {msg.phone_number: msg for msg in messages}
        logger.info(f"Built phone lookup map with {len(phone_map)} unique phone numbers")

        # Iterate over clustering results (keyed by atomic_id)
        for atomic_id, clustering in clustering_results.items():
            # Extract phone number from clustering result
            phone_number = clustering.get('phone_number')
            if not phone_number:
                logger.warning(f"Clustering result {atomic_id} missing phone_number, skipping")
                continue

            # Find matching ConsolidatedMessage by phone number
            message = phone_map.get(phone_number)
            if not message:
                logger.warning(f"No ConsolidatedMessage found for phone {phone_number} (atomic_id: {atomic_id}), skipping")
                continue

            # Create unified record (one per atomic message)
            unified_record = UnifiedCampaignRecord.from_consolidated_message(
                consolidated=message,
                campaign_id=campaign_name,
                clustering_result=clustering
            )

            unified_records.append(unified_record)

        logger.info(f"Created {len(unified_records)} unified records from {len(messages)} original messages")
        if len(unified_records) > len(messages):
            logger.info(f"   → {len(unified_records) - len(messages)} additional records created from message splitting")

        output_dir = Path(self.config.get('consolidation', {}).get('output_dir', './output/consolidated'))
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            comprehensive_csv_file = output_dir / f"{campaign_name}_all_cluster_analysis.csv"
            self._export_comprehensive_csv(unified_records, comprehensive_csv_file)
            logger.info(f"📊 Comprehensive cluster analysis CSV exported to: {comprehensive_csv_file}")
        except Exception as e:
            logger.warning(f"Failed to export comprehensive CSV: {e}")

        return unified_records

    def _export_comprehensive_csv(self, unified_records: list[UnifiedCampaignRecord], csv_file_path: Path) -> None:
        """
        Export comprehensive CSV with all cluster configurations
        One row per atomic message with cluster data for all k values
        """
        import csv

        if not unified_records:
            logger.warning("No unified records to export")
            return

        all_cluster_counts: set[str] = set()
        for record in unified_records:
            if record.multi_cluster_data:
                all_cluster_counts.update(record.multi_cluster_data.keys())

        cluster_counts = sorted(all_cluster_counts, key=lambda x: int(x) if x.isdigit() else float('inf'))
        logger.info(f"Cluster configurations in export: {cluster_counts}")

        csv_rows = []
        for record in unified_records:
            row = {
                'atomic_id': record.atomic_id,
                'phone_number': record.phone_number,
                'original_message': record.original_message if record.original_message else record.message_text,
                'atomic_message': record.atomic_message if record.atomic_message else record.message_text,
                'poll_id': record.poll_id or record.campaign_id,
                'round': record.round,
            }

            for cluster_count in cluster_counts:
                cluster_data = record.multi_cluster_data.get(cluster_count, {}) if record.multi_cluster_data else {}

                cluster_id = cluster_data.get('cluster_id', -1)
                row[f'k{cluster_count}_cluster_id'] = cluster_id if cluster_id != -1 else ''
                row[f'k{cluster_count}_theme'] = cluster_data.get('cluster_theme', '')
                row[f'k{cluster_count}_category'] = cluster_data.get('cluster_category', '')
                row[f'k{cluster_count}_summary'] = cluster_data.get('issues_summary', '')
                row[f'k{cluster_count}_sentiment'] = cluster_data.get('cluster_sentiment', '')

            csv_rows.append(row)

        if csv_rows:
            fieldnames = list(csv_rows[0].keys())
            with open(csv_file_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(csv_rows)

            logger.info(f"Exported {len(csv_rows)} atomic messages with {len(cluster_counts)} cluster configurations")
        else:
            logger.warning("No CSV rows to write")


# Convenience function
async def run_campaign_pipeline(campaign_name: str, config_path: str | None = None) -> PipelineResult:
    """
    Convenience function to run the pipeline for a campaign

    Args:
        campaign_name: Name of the campaign to process
        config_path: Optional path to pipeline config file

    Returns:
        PipelineResult with processing statistics
    """
    orchestrator = V1PipelineOrchestrator(config_path)
    return await orchestrator.run_pipeline(campaign_name)
