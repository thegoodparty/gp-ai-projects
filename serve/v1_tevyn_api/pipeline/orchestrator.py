#!/usr/bin/env python3

import sys
import os
import time
import asyncio
import pandas as pd
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime
import yaml

# Add project paths
sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

# Import existing consolidator
from serve.consolidate_replies_results import RepliesResultsConsolidator

# Import adapters
from serve.v1_tevyn_api.adapters.classification_adapter import ClassificationAdapter
from serve.v1_tevyn_api.adapters.clustering_adapter import ClusteringAdapter

# Import models
from serve.v1_tevyn_api.models.unified_record import (
    ConsolidatedMessage, UnifiedCampaignRecord, PipelineResult,
    ClassificationResult, ClusteringResult
)

logger = get_logger(__name__)


class TevynPipelineOrchestrator:
    """
    Main orchestrator for the V1 Tevyn API Pipeline
    Coordinates consolidation, classification, clustering, and upload stages
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize the pipeline orchestrator"""
        self.config_path = config_path or str(Path(__file__).parent.parent / "config/pipeline_config.yaml")

        # Track original config directory for path resolution (in case config_path is a temp file)
        self.original_config_dir = Path(__file__).parent.parent / 'config'

        # Load configuration
        self.config = self._load_config()

        # Initialize components
        self.consolidator = None
        self.classifier = None
        self.clusterer = None
        self.uploader = None

        # Pipeline state
        self.checkpoints = {}
        self.errors = []
        self.warnings = []

        logger.info("Tevyn Pipeline Orchestrator initialized")

    def _load_config(self) -> Dict[str, Any]:
        """Load pipeline configuration"""
        try:
            if Path(self.config_path).exists():
                with open(self.config_path, 'r') as f:
                    config = yaml.safe_load(f)
                logger.info(f"Loaded configuration from {self.config_path}")

                # Resolve relative paths relative to config file directory
                config = self._resolve_config_paths(config)
            else:
                # Default configuration
                config = self._get_default_config()
                logger.warning(f"Config file not found, using defaults: {self.config_path}")

            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            return self._get_default_config()

    def _resolve_config_paths(self, config: Dict[str, Any]) -> Dict[str, Any]:
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

    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration"""
        return {
            'pipeline': {
                'mode': 'integrated',
                'checkpoint_enabled': True,
                'skip_upload': False
            },
            'consolidation': {
                'input_dir': '../input',
                'output_dir': './output'
            },
            'classification': {
                'enabled': True,
                'batch_size': 100,
                'skip_on_error': True
            },
            'clustering': {
                'enabled': True,
                'batch_size': 500,
                'min_messages_for_clustering': 5,
                'skip_on_error': True
            },
            'upload': {
                'enabled': True,
                'api_url': 'https://ai-dev.goodparty.org/serve/messages',
                'api_key': os.getenv('SERVE_API_KEY'),
                'batch_size': 25,
                'retry_attempts': 3
            }
        }

    def _initialize_components(self):
        """Initialize pipeline components"""
        try:
            # Initialize consolidator
            consolidation_config = self.config.get('consolidation', {})
            input_dir = consolidation_config.get('input_dir', '../input')
            output_dir = consolidation_config.get('output_dir', './output')

            logger.info(f"🔍 Initializing consolidator with input_dir: {input_dir}")
            logger.info(f"🔍 Initializing consolidator with output_dir: {output_dir}")

            # Create output directory if it doesn't exist
            Path(output_dir).mkdir(parents=True, exist_ok=True)

            self.consolidator = RepliesResultsConsolidator(input_dir, output_dir)

            # Initialize classifier if enabled
            if self.config.get('classification', {}).get('enabled', True):
                self.classifier = ClassificationAdapter()

            # Initialize clusterer if enabled
            if self.config.get('clustering', {}).get('enabled', True):
                self.clusterer = ClusteringAdapter()

            # Initialize uploader if enabled
            if self.config.get('upload', {}).get('enabled', True):
                from serve.v1_tevyn_api.pipeline.uploader import DynamoDBUploader
                self.uploader = DynamoDBUploader(self.config.get('upload', {}))

            logger.info("All pipeline components initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize components: {e}")
            raise

    def _convert_to_consolidated_messages(self, df, campaign_name: str) -> List[ConsolidatedMessage]:
        """Convert consolidated DataFrame to ConsolidatedMessage objects"""
        messages = []

        for _, row in df.iterrows():
            try:
                # Parse sent_at datetime
                sent_at = pd.to_datetime(row.get('Sent At', row.get('sent_at', datetime.utcnow())))

                message = ConsolidatedMessage(
                    phone_number=str(row.get('Contact Phone Number', '')),
                    message_text=str(row.get('Message Text', '')),
                    sent_at=sent_at,
                    round=str(row.get('round', 'Unknown')),

                    # Demographics
                    age=int(row['voters_age']) if pd.notna(row.get('voters_age')) else None,
                    age_group=str(row.get('age_group', 'Unknown')),
                    location=str(row.get('location', 'Unknown')),
                    ward=str(row.get('ward')) if pd.notna(row.get('ward')) else None,
                    voters_gender=str(row.get('voters_gender')) if pd.notna(row.get('voters_gender')) else None,
                    voting_performance_category=str(row.get('voting_performance_category', 'Unknown')),
                    residence_city=str(row.get('residence_addresses_city', 'Unknown')),

                    # Placeholders
                    homeowner_status=str(row.get('homeowner_status', 'Unknown')),
                    business_owner=str(row.get('business_owner', 'Unknown')),
                    has_children_under_18=str(row.get('has_children_under_18', 'Unknown')),
                    education_level=str(row.get('education_level', 'Unknown')),
                    income_level=str(row.get('income_level', 'Unknown')),

                    # Message metadata
                    campaign_id=campaign_name,
                    campaign_name=str(row.get('Campaign Name', campaign_name)),
                    carrier=str(row.get('Carrier')) if pd.notna(row.get('Carrier')) else None
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
        logger.info(f"🚀 Starting V1 Tevyn Pipeline for campaign: {campaign_name}")

        try:
            # Initialize components
            self._initialize_components()

            # Stage 1: Data Consolidation
            logger.info("📊 Stage 1: Data Consolidation")
            consolidation_start = time.time()

            consolidated_df, consolidation_analysis = await self._run_consolidation_stage(campaign_name)
            if consolidated_df.empty:
                raise ValueError(f"No data found for campaign: {campaign_name}")

            consolidation_time = time.time() - consolidation_start
            logger.info(f"✅ Consolidation completed in {consolidation_time:.2f}s")

            # Convert to message objects
            messages = self._convert_to_consolidated_messages(consolidated_df, campaign_name)
            total_messages = len(messages)

            # Stage 2: Classification
            classification_result = {}
            if self.classifier and self.config.get('classification', {}).get('enabled', True):
                logger.info("🏷️ Stage 2: Message Classification")
                classification_start = time.time()

                try:
                    classification_results = await self._run_classification_stage(messages)
                    classification_time = time.time() - classification_start
                    logger.info(f"✅ Classification completed in {classification_time:.2f}s")

                    classification_result = {
                        'processed_messages': len(classification_results),
                        'processing_time': classification_time,
                        'success': True
                    }
                except Exception as e:
                    logger.error(f"Classification stage failed: {e}")
                    classification_results = {}
                    classification_result = {'success': False, 'error': str(e)}
                    if not self.config.get('classification', {}).get('skip_on_error', True):
                        raise
            else:
                classification_results = {}

            # Stage 3: Clustering
            clustering_result = {}
            if self.clusterer and self.config.get('clustering', {}).get('enabled', True):
                logger.info("🔍 Stage 3: Message Clustering")
                clustering_start = time.time()

                try:
                    clustering_results = await self._run_clustering_stage(messages, campaign_name)
                    clustering_time = time.time() - clustering_start
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
                except Exception as e:
                    logger.error(f"Clustering stage failed: {e}")
                    clustering_results = {}
                    clustering_result = {'success': False, 'error': str(e)}
                    if not self.config.get('clustering', {}).get('skip_on_error', True):
                        raise
            else:
                clustering_results = {}

            # Stage 4: Data Merging
            logger.info("🔄 Stage 4: Data Merging")
            unified_records = self._merge_all_data(messages, classification_results, clustering_results, campaign_name)

            # Stage 5: Upload to DynamoDB
            upload_result = {}
            if self.uploader and self.config.get('upload', {}).get('enabled', True):
                logger.info("☁️ Stage 5: DynamoDB Upload")
                upload_start = time.time()

                try:
                    upload_stats = await self._run_upload_stage(unified_records)
                    upload_time = time.time() - upload_start
                    logger.info(f"✅ Upload completed in {upload_time:.2f}s")

                    upload_result = {
                        'uploaded_records': upload_stats.get('successful_uploads', 0),
                        'failed_records': upload_stats.get('failed_uploads', 0),
                        'processing_time': upload_time,
                        'success': True
                    }
                except Exception as e:
                    logger.error(f"Upload stage failed: {e}")
                    upload_result = {'success': False, 'error': str(e)}
                    if not self.config.get('upload', {}).get('skip_on_error', True):
                        raise
            else:
                logger.info("⏭️ Skipping upload stage (disabled in config)")

            # Generate final results
            processing_time = time.time() - start_time
            successful_records = len(unified_records)
            failed_records = total_messages - successful_records

            result = PipelineResult(
                campaign_id=campaign_name,
                total_messages=total_messages,
                successful_records=successful_records,
                failed_records=failed_records,
                processing_time=processing_time,
                consolidation_result=consolidation_analysis,
                classification_result=classification_result,
                clustering_result=clustering_result,
                upload_result=upload_result,
                errors=self.errors,
                warnings=self.warnings
            )

            logger.info(f"🎉 Pipeline completed successfully!")
            logger.info(f"📈 Processing Summary: {result.summary}")

            return result

        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"💥 Pipeline failed after {processing_time:.2f}s: {e}")

            # Return failed result
            return PipelineResult(
                campaign_id=campaign_name,
                total_messages=0,
                successful_records=0,
                failed_records=0,
                processing_time=processing_time,
                consolidation_result={'success': False, 'error': str(e)},
                classification_result={},
                clustering_result={},
                upload_result={},
                errors=[str(e)] + self.errors,
                warnings=self.warnings
            )

    async def _run_consolidation_stage(self, campaign_name: str):
        """Run the consolidation stage"""
        try:
            # Use existing consolidator
            campaigns = self.consolidator.discover_files()

            if campaign_name not in campaigns:
                raise ValueError(f"Campaign '{campaign_name}' not found in input directory")

            files = campaigns[campaign_name]
            df, analysis = self.consolidator.process_campaign(campaign_name, files)

            return df, analysis

        except Exception as e:
            logger.error(f"Consolidation stage failed: {e}")
            raise

    async def _run_classification_stage(self, messages: List[ConsolidatedMessage]) -> Dict[str, ClassificationResult]:
        """Run the classification stage"""
        batch_size = self.config.get('classification', {}).get('batch_size', 100)
        return await self.classifier.process_messages_batch(messages, batch_size)

    async def _run_clustering_stage(self, messages: List[ConsolidatedMessage], campaign_name: str) -> Dict[str, ClusteringResult]:
        """Run the clustering stage"""
        # Check minimum message count for clustering
        min_messages = self.config.get('clustering', {}).get('min_messages_for_clustering', 5)
        if len(messages) < min_messages:
            logger.warning(f"Only {len(messages)} messages - skipping clustering (minimum: {min_messages})")
            return {}

        batch_size = self.config.get('clustering', {}).get('batch_size', 500)
        anonymize_keywords = self.config.get('clustering', {}).get('anonymize_keywords', [])

        return await self.clusterer.process_messages_batch(
            messages,
            batch_size,
            campaign_name,
            anonymize_keywords
        )

    def _merge_all_data(self,
                       messages: List[ConsolidatedMessage],
                       classification_results: Dict[str, ClassificationResult],
                       clustering_results: Dict[str, ClusteringResult],
                       campaign_name: str) -> List[UnifiedCampaignRecord]:
        """Merge all data sources into unified records"""
        unified_records = []

        for i, message in enumerate(messages):
            phone_number = message.phone_number

            # Get analysis results
            classification = classification_results.get(phone_number)
            clustering = clustering_results.get(phone_number)

            # Debug: Log first record's classification
            if i == 0 and classification:
                logger.info(f"First record classification - Phone: {phone_number}, Primary: {classification.primary_issue_category}, Secondary: {classification.secondary_issue}")

            # Create unified record
            unified_record = UnifiedCampaignRecord.from_consolidated_message(
                consolidated=message,
                campaign_id=campaign_name,
                classification_result=classification,
                clustering_result=clustering
            )

            # Debug: Verify it was set on the unified record
            if i == 0:
                logger.info(f"Unified record - Primary: {unified_record.primary_issue_category}, Secondary: {unified_record.secondary_issue}")

            unified_records.append(unified_record)

        logger.info(f"Created {len(unified_records)} unified records")

        # Save unified records for inspection (debug)
        import json
        output_dir = Path(self.config.get('consolidation', {}).get('output_dir', './output/consolidated'))
        output_dir.mkdir(parents=True, exist_ok=True)
        unified_records_file = output_dir / f"{campaign_name}_unified_records.json"

        # Convert to serializable format
        records_data = []
        for record in unified_records:
            record_dict = {
                'campaign_id': record.campaign_id,
                'phone_number': record.phone_number,
                'message_text': record.message_text,
                'classification': {
                    'primary_issue': record.primary_issue_category,
                    'secondary_issue': record.secondary_issue,
                    'stance': record.issue_stance,
                    'sentiment': record.overall_sentiment,
                    'confidence': record.classification_confidence
                } if record.primary_issue_category else None,
                'multi_cluster_data': record.multi_cluster_data,
                'demographics': {
                    'age': record.age,
                    'gender': record.voters_gender,
                    'ward': record.ward
                }
            }
            records_data.append(record_dict)

        with open(unified_records_file, 'w') as f:
            json.dump(records_data, f, indent=2, default=str)

        logger.info(f"💾 Saved unified records to: {unified_records_file}")

        # Also create comprehensive CSV output with all data combined
        try:
            comprehensive_csv_file = output_dir / f"{campaign_name}_comprehensive_analysis.csv"
            self._export_comprehensive_csv(unified_records, comprehensive_csv_file)
            logger.info(f"📊 Comprehensive CSV exported to: {comprehensive_csv_file}")
        except Exception as e:
            logger.warning(f"Failed to export comprehensive CSV: {e}")

        return unified_records

    def _export_comprehensive_csv(self, unified_records: List[UnifiedCampaignRecord], csv_file_path: Path):
        """Export comprehensive CSV with all consolidation, classification, and dynamic multi-cluster data"""
        import csv

        if not unified_records:
            logger.warning("No unified records to export")
            return

        # First pass: Discover all available cluster configurations
        all_cluster_counts = set()
        for record in unified_records:
            if record.multi_cluster_data:
                all_cluster_counts.update(record.multi_cluster_data.keys())

        # Sort cluster counts for consistent column ordering
        cluster_counts = sorted(all_cluster_counts, key=lambda x: int(x) if x.isdigit() else float('inf'))
        logger.info(f"Found cluster configurations: {cluster_counts}")

        # Prepare CSV data with all fields
        csv_rows = []
        for record in unified_records:
            row = {
                # Core message data
                'phone_number': record.phone_number,
                'message_text': record.message_text,
                'sent_at': record.sent_at.isoformat() if hasattr(record.sent_at, 'isoformat') else str(record.sent_at),
                'round': record.round,
                'campaign_id': record.campaign_id,
                'campaign_name': record.campaign_name,
                'carrier': record.carrier or '',

                # Demographics
                'age': record.age or '',
                'age_group': record.age_group or '',
                'location': record.location or '',
                'ward': record.ward or '',
                'voters_gender': record.voters_gender or '',
                'voting_performance_category': record.voting_performance_category or '',
                'residence_city': record.residence_city or '',
                'homeowner_status': record.homeowner_status or '',
                'business_owner': record.business_owner or '',
                'has_children_under_18': record.has_children_under_18 or '',
                'education_level': record.education_level or '',
                'income_level': record.income_level or '',

                # Classification results
                'primary_issue_category': record.primary_issue_category or '',
                'secondary_issue': record.secondary_issue or '',
                'issue_stance': record.issue_stance or '',
                'overall_sentiment': record.overall_sentiment or '',
                'classification_confidence': record.classification_confidence or 0.0,

                # Metadata
                'created_at': record.created_at.isoformat() if hasattr(record.created_at, 'isoformat') else str(record.created_at),
                'updated_at': record.updated_at.isoformat() if hasattr(record.updated_at, 'isoformat') else str(record.updated_at)
            }

            # Add dynamic multi-cluster columns
            for cluster_count in cluster_counts:
                cluster_data = {}
                if record.multi_cluster_data and cluster_count in record.multi_cluster_data:
                    cluster_data = record.multi_cluster_data[cluster_count]

                # Add columns for this cluster configuration
                row[f'cluster_{cluster_count}_id'] = cluster_data.get('cluster_id', '') if cluster_data.get('cluster_id', -1) != -1 else ''
                row[f'cluster_{cluster_count}_theme'] = cluster_data.get('cluster_theme', '')
                row[f'cluster_{cluster_count}_category'] = cluster_data.get('cluster_category', '')
                row[f'cluster_{cluster_count}_topics'] = ', '.join(cluster_data.get('key_topics', []))
                row[f'cluster_{cluster_count}_sentiment'] = cluster_data.get('cluster_sentiment', '')
                row[f'cluster_{cluster_count}_relevance'] = cluster_data.get('civic_relevance', '')
                row[f'cluster_{cluster_count}_confidence'] = cluster_data.get('theme_confidence', 0.0)
                row[f'cluster_{cluster_count}_analysis'] = cluster_data.get('detailed_analysis', '')
                row[f'cluster_{cluster_count}_quotes'] = cluster_data.get('verbatim_quotes', '')

            csv_rows.append(row)

        # Write CSV file
        if csv_rows:
            fieldnames = list(csv_rows[0].keys())
            with open(csv_file_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in csv_rows:
                    writer.writerow(row)

            logger.info(f"Exported {len(csv_rows)} records to comprehensive CSV: {csv_file_path}")
        else:
            logger.warning("No CSV rows to write")

    async def _run_upload_stage(self, records: List[UnifiedCampaignRecord]) -> Dict[str, int]:
        """Run the upload stage"""
        return await self.uploader.batch_upload_records(records)


# Convenience function
async def run_campaign_pipeline(campaign_name: str, config_path: Optional[str] = None) -> PipelineResult:
    """
    Convenience function to run the pipeline for a campaign

    Args:
        campaign_name: Name of the campaign to process
        config_path: Optional path to pipeline config file

    Returns:
        PipelineResult with processing statistics
    """
    orchestrator = TevynPipelineOrchestrator(config_path)
    return await orchestrator.run_pipeline(campaign_name)