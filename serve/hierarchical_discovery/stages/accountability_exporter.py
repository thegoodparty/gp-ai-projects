#!/usr/bin/env python3

"""
Accountability Exporter Stage - Creates comprehensive CSV for message tracking and audit trail
"""

import csv
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from ..models import (
    PipelineResult,
    RawMessage,
    FilteredMessage,
    AtomicMessage,
    ClusteredMessage,
    ClusterAnalysis,
    PipelineConfig
)
from shared.logger import get_logger

logger = get_logger(__name__)

class AccountabilityExporter:
    """Export comprehensive accountability CSV with full message tracking"""

    def __init__(self, config: PipelineConfig, output_dir: Path):
        self.config = config
        self.output_dir = output_dir
        self.accountability_config = config.output.get("accountability", {})

    def export_accountability_csv(
        self,
        pipeline_result: PipelineResult,
        pipeline_id: str,
        anonymize_keywords: Optional[List[str]] = None
    ) -> str:
        """Create comprehensive accountability CSV"""

        logger.info("Generating accountability CSV...")

        # Create comprehensive data structure
        accountability_data = self._build_accountability_data(
            pipeline_result,
            pipeline_id,
            anonymize_keywords or []
        )

        # Generate CSV filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_filename = f"accountability_{pipeline_result.pipeline_state.config.data_source}_{timestamp}.csv"
        csv_path = self.output_dir / "exports" / csv_filename

        # Ensure exports directory exists
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        # Write CSV
        self._write_accountability_csv(accountability_data, csv_path)

        logger.info(f"Accountability CSV exported: {csv_path}")
        return str(csv_path)

    def _build_accountability_data(
        self,
        pipeline_result: PipelineResult,
        pipeline_id: str,
        anonymize_keywords: List[str]
    ) -> List[Dict[str, Any]]:
        """Build comprehensive data structure for accountability"""

        # Create lookup dictionaries for efficient joining
        raw_messages_by_row = {
            msg.csv_row_index: msg for msg in pipeline_result.raw_messages
        }

        filtered_messages_by_row = {
            msg.csv_row_index: msg for msg in pipeline_result.filtered_messages
        }

        atomic_messages_by_row = {}
        for msg in pipeline_result.atomic_messages:
            row_idx = msg.csv_row_index
            if row_idx not in atomic_messages_by_row:
                atomic_messages_by_row[row_idx] = []
            atomic_messages_by_row[row_idx].append(msg)

        clustered_messages_by_atomic_id = {
            msg.embedded_message_id: msg for msg in pipeline_result.clustered_messages
        }

        cluster_analyses_by_id = {
            analysis.cluster_id: analysis for analysis in pipeline_result.cluster_analyses
        }

        accountability_rows = []

        # Get all unique row indices
        all_row_indices = set()
        for msg in pipeline_result.raw_messages:
            all_row_indices.add(msg.csv_row_index)

        # Process each original message row
        for row_idx in sorted(all_row_indices):
            raw_msg = raw_messages_by_row.get(row_idx)
            if not raw_msg:
                continue

            filtered_msg = filtered_messages_by_row.get(row_idx)
            atomic_msgs = atomic_messages_by_row.get(row_idx, [])

            # Base row data from original message
            base_data = {
                'csv_row_index': row_idx,
                'csv_file': raw_msg.csv_file,
                'original_text': raw_msg.original_text,
                'campaign_source': raw_msg.campaign_source,
                'pipeline_id': pipeline_id,
                'processing_timestamp': datetime.now().isoformat(),
                'anonymize_keywords_used': ', '.join(anonymize_keywords) if anonymize_keywords else '',
            }

            # Add filter information
            if filtered_msg:
                base_data.update({
                    'filter_passed': filtered_msg.filter_result.passed,
                    'filter_reasons': ', '.join(filtered_msg.filter_result.reasons),
                    'filtered_text': filtered_msg.filtered_text if filtered_msg.filter_result.passed else '',
                })
            else:
                base_data.update({
                    'filter_passed': False,
                    'filter_reasons': 'Not processed through filter',
                    'filtered_text': '',
                })

            # If no atomic messages, create one row for the original
            if not atomic_msgs:
                base_data.update({
                    'atomic_index': 0,
                    'atomic_text_before_ai': '',
                    'ai_summary': '',
                    'anonymized_keywords_in_message': '',
                    'split_context': 'no_split',
                    'cluster_id': -1,
                    'cluster_category': 'Other',
                    'cluster_theme': 'Not clustered',
                    'cluster_issues_summary': '',
                    'cluster_detailed_analysis': '',
                    'cluster_verbatim_quote_1': '',
                    'cluster_verbatim_quote_2': '',
                    'cluster_verbatim_quote_3': '',
                    'cluster_action_items': '',
                    'cluster_confidence': 0.0,
                    'cluster_size': 0,
                    'civic_relevance': '',
                    'cluster_sentiment': '',
                    'cluster_key_topics': '',
                    'cluster_confidence_score': 0.0,
                    'is_noise': True,
                })
                accountability_rows.append(base_data.copy())
            else:
                # Create row for each atomic message
                for atomic_msg in atomic_msgs:
                    row_data = base_data.copy()

                    # Find ALL corresponding clustered messages (main + sub-clusters)
                    clustered_msgs = []

                    # Search through clustered messages to find ALL matches
                    for clustered in pipeline_result.clustered_messages:
                        if clustered.csv_row_index == row_idx and clustered.text == atomic_msg.atomic_text:
                            cluster_analysis = cluster_analyses_by_id.get(clustered.cluster_assignment.cluster_id)
                            clustered_msgs.append((clustered, cluster_analysis))

                    # Add atomic message data
                    row_data.update({
                        'atomic_index': atomic_msg.atomic_index,
                        'atomic_text_before_ai': atomic_msg.original_atomic_text or atomic_msg.atomic_text,
                        'ai_summary': atomic_msg.ai_summary or atomic_msg.atomic_text,
                        'anonymized_keywords_in_message': ', '.join(atomic_msg.anonymized_keywords),
                        'split_context': atomic_msg.split_context,
                    })

                    # Create separate rows for each cluster assignment (main + sub-clusters)
                    if clustered_msgs:
                        for clustered_msg, cluster_analysis in clustered_msgs:
                            cluster_row_data = row_data.copy()
                            cluster_assignment = clustered_msg.cluster_assignment

                            # Determine cluster type for better identification
                            cluster_type = "sub-cluster" if cluster_assignment.cluster_id >= 1000 else "main-cluster"

                            cluster_row_data.update({
                                'cluster_id': cluster_assignment.cluster_id,
                                'cluster_confidence': cluster_assignment.cluster_confidence,
                                'is_noise': cluster_assignment.is_noise,
                                'distance_to_centroid': cluster_assignment.distance_to_centroid,
                                'cluster_type': cluster_type,
                            })

                            if cluster_analysis:
                                theme = cluster_analysis.theme_analysis
                                cluster_row_data.update({
                                    'cluster_category': theme.category,
                                    'cluster_theme': theme.theme,
                                    'cluster_issues_summary': theme.issues_summary,
                                    'cluster_detailed_analysis': theme.detailed_analysis[:1000] if theme.detailed_analysis else '',  # Truncate for CSV
                                    'cluster_verbatim_quote_1': theme.verbatim_quotes[0] if len(theme.verbatim_quotes) > 0 else '',
                                    'cluster_verbatim_quote_2': theme.verbatim_quotes[1] if len(theme.verbatim_quotes) > 1 else '',
                                    'cluster_verbatim_quote_3': theme.verbatim_quotes[2] if len(theme.verbatim_quotes) > 2 else '',
                                    'cluster_action_items': ', '.join(theme.action_items),
                                    'cluster_size': cluster_analysis.size,
                                    'civic_relevance': theme.civic_relevance,
                                    'cluster_sentiment': theme.sentiment,
                                    'cluster_key_topics': ', '.join(theme.key_topics),
                                    'cluster_confidence_score': theme.confidence_score,
                                })
                            else:
                                cluster_row_data.update({
                                    'cluster_category': 'Other',
                                    'cluster_theme': f'Cluster {cluster_assignment.cluster_id}',
                                    'cluster_issues_summary': '',
                                    'cluster_detailed_analysis': '',
                                    'cluster_verbatim_quote_1': '',
                                    'cluster_verbatim_quote_2': '',
                                    'cluster_verbatim_quote_3': '',
                                    'cluster_action_items': '',
                                    'cluster_size': 0,
                                    'civic_relevance': '',
                                    'cluster_sentiment': '',
                                    'cluster_key_topics': '',
                                    'cluster_confidence_score': 0.0,
                                })

                            accountability_rows.append(cluster_row_data)
                    else:
                        # No clustering data found
                        row_data.update({
                            'cluster_id': -1,
                            'cluster_category': 'Other',
                            'cluster_theme': 'Not clustered',
                            'cluster_issues_summary': '',
                            'cluster_detailed_analysis': '',
                            'cluster_verbatim_quote_1': '',
                            'cluster_verbatim_quote_2': '',
                            'cluster_verbatim_quote_3': '',
                            'cluster_action_items': '',
                            'cluster_confidence': 0.0,
                            'cluster_size': 0,
                            'civic_relevance': '',
                            'cluster_sentiment': '',
                            'cluster_key_topics': '',
                            'cluster_confidence_score': 0.0,
                            'is_noise': True,
                            'distance_to_centroid': 0.0,
                            'cluster_type': 'none',
                        })
                        accountability_rows.append(row_data)

        logger.info(f"Generated {len(accountability_rows)} accountability rows from {len(all_row_indices)} original messages")
        return accountability_rows

    def _write_accountability_csv(self, data: List[Dict[str, Any]], csv_path: Path):
        """Write accountability data to CSV file"""

        if not data:
            logger.warning("No data to write to accountability CSV")
            return

        # Define column order for readability
        columns = [
            'csv_row_index',
            'csv_file',
            'original_text',
            'filter_passed',
            'filter_reasons',
            'filtered_text',
            'atomic_index',
            'atomic_text_before_ai',
            'ai_summary',
            'anonymized_keywords_in_message',
            'split_context',
            'cluster_id',
            'cluster_category',
            'cluster_theme',
            'cluster_issues_summary',
            'cluster_detailed_analysis',
            'cluster_verbatim_quote_1',
            'cluster_verbatim_quote_2',
            'cluster_verbatim_quote_3',
            'cluster_action_items',
            'cluster_confidence',
            'cluster_size',
            'cluster_confidence_score',
            'is_noise',
            'distance_to_centroid',
            'cluster_type',
            'civic_relevance',
            'cluster_sentiment',
            'cluster_key_topics',
            'campaign_source',
            'pipeline_id',
            'processing_timestamp',
            'anonymize_keywords_used'
        ]

        # Write CSV
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(data)

        logger.info(f"Wrote {len(data)} rows to {csv_path}")


def export_accountability_stage(
    pipeline_result: PipelineResult,
    config: PipelineConfig,
    output_dir: Path,
    pipeline_id: str,
    anonymize_keywords: Optional[List[str]] = None
) -> str:
    """Main function to export accountability CSV"""

    # Check if accountability is enabled
    accountability_config = getattr(config, 'accountability', {})
    if not accountability_config.get('enabled', True):
        logger.info("Accountability export disabled in configuration")
        return ""

    exporter = AccountabilityExporter(config, output_dir)
    return exporter.export_accountability_csv(
        pipeline_result,
        pipeline_id,
        anonymize_keywords
    )