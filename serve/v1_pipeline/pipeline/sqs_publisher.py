#!/usr/bin/env python3

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3

project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from serve.v1_pipeline.models.events import (
    PollAnalysisCompleteData,
    PollAnalysisCompleteEvent,
    PollIssueAnalysisData,
    PollIssueAnalysisEvent,
)
from serve.v1_pipeline.models.unified_record import UnifiedCampaignRecord
from shared.logger import get_logger

logger = get_logger(__name__)


class SQSEventPublisher:
    def __init__(self, config: dict[str, Any]):
        # Enable/disable SQS publishing
        self.publish_to_sqs = config.get('publish_to_sqs', True)

        # Output directory configuration (configurable, defaults to /app for Docker)
        self.output_dir = config.get('output_dir', '/app/serve/v1_pipeline/output')

        # SQS configuration (optional publishing)
        if self.publish_to_sqs:
            self.queue_url = config.get('queue_url', os.getenv('SQS_QUEUE_URL'))
            self.sqs_client = boto3.client('sqs', region_name='us-west-2')
            logger.info("SQS publishing: ENABLED")
            logger.info(f"  Queue URL: {self.queue_url}")
            logger.info(f"  MessageGroupId: gp-queue-polls")
        else:
            self.queue_url = None
            self.sqs_client = None
            logger.info("SQS publishing: DISABLED")

        # Common configuration
        self.top_n = config.get('publish_top_n', 3)
        self.min_respondents = config.get('min_unique_respondents', 1)
        self.s3_output_path = config.get('s3_output_path', os.getenv('S3_OUTPUT_PATH', ''))

        logger.info("Event Publisher initialized")
        logger.info(f"  Output directory: {self.output_dir}")
        logger.info(f"  S3 output path: {self.s3_output_path}")
        logger.info(f"  Top N clusters: {self.top_n}")
        logger.info(f"  Min unique respondents: {self.min_respondents}")

    async def publish_events(self, unified_records: list[UnifiedCampaignRecord], campaign_name: str = "") -> dict[str, Any]:
        polls = defaultdict(list)
        for record in unified_records:
            if record.poll_id:
                polls[record.poll_id].append(record)

        total_complete_events = 0
        all_events = []

        responses_location = ""
        if campaign_name and self.s3_output_path:
            if self.s3_output_path.startswith("s3://"):
                s3_prefix = "/".join(self.s3_output_path.split("/")[3:]).rstrip("/")
            else:
                s3_prefix = self.s3_output_path.rstrip("/")
            responses_location = f"{s3_prefix}/consolidated/{campaign_name}_all_cluster_analysis.json"

        for poll_id, records in polls.items():
            poll_issues = []
            logger.info(f"Processing poll_id: {poll_id} ({len(records)} records)")

            cluster_stats = self._aggregate_cluster_stats(records)
            logger.info(f"  Found {len(cluster_stats)} clusters")

            top_clusters = self._rank_clusters(cluster_stats)
            logger.info(f"  Processing top {len(top_clusters)} clusters")

            for rank, cluster_data in enumerate(top_clusters, start=1):
                poll_issues.append(
                    PollIssueAnalysisData(
                        pollId=poll_id,
                        rank=rank,
                        clusterId=cluster_data['cluster_id'],
                        theme=cluster_data['theme'],
                        summary=cluster_data['summary'],
                        analysis=cluster_data['analysis'],
                        quotes=cluster_data['quotes'],
                        responseCount=cluster_data['responseCount']
                    )
                )

                logger.info(f"  ✅ Rank {rank}: {cluster_data['theme']} ({cluster_data['responseCount']} respondents) - saved locally")

            unique_respondents = len(set(record.phone_number for record in records))
            logger.info(f"  Total unique respondents: {unique_respondents} (from {len(records)} atomic messages)")

            # Add individual issue events first
            for issue_data in poll_issues:
                issue_event = PollIssueAnalysisEvent(data=issue_data)
                all_events.append(issue_event.to_json())

            # Then add the completion event
            complete_event = self._build_complete_event(poll_id, unique_respondents, responses_location)
            all_events.append(complete_event.to_json())

            if self.publish_to_sqs:
                try:
                    self._send_to_sqs(all_events, poll_id)
                    logger.info("  ✅ Events sent to SQS + saved locally")
                except Exception as e:
                    logger.warning(f"  ⚠️ SQS send failed (continuing): {e}")
                    logger.info("  ✅ Events saved locally only")
            else:
                logger.info("  ✅ Events saved locally")

            total_complete_events += 1

        self._save_events_locally(all_events)

        return {
            'polls_processed': len(polls),
            'complete_events_sent': total_complete_events
        }

    def _aggregate_cluster_stats(self, records: list[UnifiedCampaignRecord]) -> dict[int, dict]:
        cluster_key = self._get_optimal_cluster_key(records)
        logger.debug(f"Using cluster configuration: {cluster_key}")

        clusters = {}
        phone_counts = defaultdict(set)

        for record in records:
            if not record.multi_cluster_data or cluster_key not in record.multi_cluster_data:
                continue

            cluster_data = record.multi_cluster_data[cluster_key]
            cluster_id = cluster_data.get('cluster_id', -1)

            if cluster_id == -1:
                continue

            # Track unique phones for this cluster
            phone_counts[cluster_id].add(record.phone_number)

            # Store cluster data (only once per cluster - all records have same cluster data)
            if cluster_id not in clusters:
                # The 'quotes' field already has the structure: [{quote: str, phone_number: str}, ...]
                # This is generated by the hierarchical discovery pipeline with proper phone attribution
                quotes = cluster_data.get('quotes', [])

                clusters[cluster_id] = {
                    'cluster_id': cluster_id,
                    'theme': cluster_data.get('cluster_theme', ''),
                    'summary': cluster_data.get('issues_summary', ''),
                    'analysis': cluster_data.get('detailed_analysis', ''),
                    'quotes': quotes  # Already has {quote, phone_number} structure
                }

        # Add response counts
        result = {}
        for cluster_id, data in clusters.items():
            result[cluster_id] = {
                **data,
                'responseCount': len(phone_counts[cluster_id])
            }

        return result

    def _get_optimal_cluster_key(self, records: list[UnifiedCampaignRecord]) -> str:
        for record in records:
            if record.multi_cluster_data:
                return list(record.multi_cluster_data.keys())[0]
        return '15'

    def _rank_clusters(self, cluster_stats: dict[int, dict]) -> list[dict]:
        filtered = [
            c for c in cluster_stats.values()
            if c['responseCount'] >= self.min_respondents
        ]

        ranked = sorted(filtered, key=lambda x: x['responseCount'], reverse=True)

        return ranked[:self.top_n]

    def _build_complete_event(self, poll_id: str, total_responses: int, responses_location: str = "") -> PollAnalysisCompleteEvent:
        return PollAnalysisCompleteEvent(
            type='pollAnalysisComplete',
            data=PollAnalysisCompleteData(
                pollId=poll_id,
                totalResponses=total_responses,
                responsesLocation=responses_location,
            )
        )

    async def publish_empty_poll_event(self, poll_id: str) -> dict[str, Any]:
        """
        Publish completion event for a poll with 0 responses
        Used when CSV has no valid messages after filtering
        """
        logger.info(f"Publishing empty poll completion event for poll_id: {poll_id}")

        complete_event = self._build_complete_event(poll_id, total_responses=0)
        events = [complete_event.to_json()]

        if self.publish_to_sqs:
            try:
                self._send_to_sqs(events, poll_id)
                logger.info("  ✅ Empty poll completion event - sent to SQS + saved locally")
            except Exception as e:
                logger.warning(f"  ⚠️ SQS send failed (continuing): {e}")
                logger.info("  ✅ Empty poll completion event - saved locally only")
        else:
            logger.info("  ✅ Empty poll completion event - saved locally")

        self._save_events_locally(events)

        return {
            'polls_processed': 1,
            'complete_events_sent': 1
        }

    def _save_events_locally(self, events: list[dict]) -> None:
        """
        Save events to local filesystem
        Note: entrypoint.sh will sync this directory to S3 after pipeline completes
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        events_dir = f"{self.output_dir}/events"
        os.makedirs(events_dir, exist_ok=True)

        events_file = f"{events_dir}/events_{timestamp}.json"
        events_json = json.dumps(events, indent=2)

        try:
            with open(events_file, 'w') as f:
                f.write(events_json)
            logger.info(f"✅ Saved {len(events)} events to {events_file}")
            logger.info("   (will be synced to S3 by entrypoint.sh after pipeline completes)")
        except Exception as e:
            logger.error(f"Failed to save events locally: {e}", exc_info=True)
            raise

    def _send_to_sqs(self, events: list[dict], poll_id: str) -> None:
        """Send events array to SQS FIFO queue with proper MessageGroupId"""
        if not self.publish_to_sqs or not self.sqs_client:
            logger.warning("SQS publishing disabled but _send_to_sqs called")
            return
        import uuid

        message_body = json.dumps(events)

        try:
            response = self.sqs_client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message_body,
                MessageGroupId=f"gp-queue-polls-{poll_id}",
                MessageDeduplicationId=str(uuid.uuid4())  # Unique per message
            )
            logger.debug(f"Sent {len(events)} events to SQS: MessageId={response['MessageId']}")
        except Exception as e:
            logger.error(f"Failed to send events to SQS: {e}", exc_info=True)
            raise
