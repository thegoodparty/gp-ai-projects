#!/usr/bin/env python3

import json
import os
import sys
import uuid
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
)
from serve.v1_pipeline.models.unified_record import UnifiedCampaignRecord
from shared.logger import get_logger

logger = get_logger(__name__)


class SQSEventPublisher:
    def __init__(self, config: dict[str, Any], s3_client: Any = None, sqs_client: Any = None):
        self.publish_to_sqs = config.get('publish_to_sqs', True)
        self.output_dir = config.get('output_dir', '/app/serve/v1_pipeline/output')
        self.s3_bucket = config.get('s3_bucket', os.getenv('S3_BUCKET', ''))
        self._s3_client = s3_client

        if self.publish_to_sqs:
            self.queue_url = config.get('queue_url', os.getenv('SQS_QUEUE_URL'))
            if not self.queue_url:
                raise ValueError("publish_to_sqs=True but no queue_url configured (config or SQS_QUEUE_URL env)")
            self.sqs_client = sqs_client or boto3.client('sqs', region_name='us-west-2')
            logger.info("SQS publishing: ENABLED")
            logger.info(f"  Queue URL: {self.queue_url}")
        else:
            self.queue_url = None
            self.sqs_client = sqs_client
            logger.info("SQS publishing: DISABLED")

        self.top_n = config.get('publish_top_n', 3)
        self.min_respondents = config.get('min_unique_respondents', 1)
        self.s3_output_path = config.get('s3_output_path', os.getenv('S3_OUTPUT_PATH', ''))

        logger.info("Event Publisher initialized")
        logger.info(f"  Output directory: {self.output_dir}")
        logger.info(f"  S3 output path: {self.s3_output_path}")
        logger.info(f"  Top N clusters: {self.top_n}")
        logger.info(f"  Min unique respondents: {self.min_respondents}")

    @property
    def s3_client(self):
        if self._s3_client is None:
            self._s3_client = boto3.client('s3', region_name='us-west-2')
        return self._s3_client

    def _compute_responses_location(self, campaign_name: str) -> str:
        if not campaign_name or not self.s3_output_path:
            return ""
        if self.s3_output_path.startswith("s3://"):
            s3_prefix = "/".join(self.s3_output_path.split("/")[3:]).rstrip("/")
        else:
            s3_prefix = self.s3_output_path.rstrip("/")
        key = f"{s3_prefix}/consolidated/{campaign_name}_all_cluster_analysis.json"
        return key.lstrip("/")

    def _upload_responses_to_s3(self, campaign_name: str, responses_location: str) -> None:
        filename = f"{campaign_name}_all_cluster_analysis.json"
        json_file = Path(self.output_dir) / filename
        if not json_file.exists():
            json_file = Path(self.output_dir) / "consolidated" / filename
        if json_file.exists():
            file_bytes = json_file.read_bytes()
            try:
                json.loads(file_bytes)
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"Corrupted JSON at {json_file}, uploading empty array instead")
                file_bytes = b"[]"
        else:
            file_bytes = b"[]"
            logger.info(f"No local JSON file at {json_file}, uploading empty array")

        try:
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=responses_location,
                Body=file_bytes,
            )
        except Exception as e:
            raise RuntimeError(f"S3 upload failed for s3://{self.s3_bucket}/{responses_location}: {e}") from e
        logger.info(f"Uploaded responses to s3://{self.s3_bucket}/{responses_location} ({len(file_bytes)} bytes)")

    async def publish_poll_completion(
        self,
        poll_ids: list[str],
        unified_records: list[UnifiedCampaignRecord],
        campaign_name: str = "",
    ) -> dict[str, Any]:
        responses_location = self._compute_responses_location(campaign_name)

        if responses_location:
            self._upload_responses_to_s3(campaign_name, responses_location)

        polls: dict[str, list[UnifiedCampaignRecord]] = defaultdict(list)
        for record in unified_records:
            if record.poll_id:
                polls[record.poll_id].append(record)

        all_events = []
        for poll_id in poll_ids:
            records = polls.get(poll_id, [])
            if records:
                cluster_stats = self._aggregate_cluster_stats(records)
                top_clusters = self._rank_clusters(cluster_stats)
                poll_issues = [
                    PollIssueAnalysisData(
                        pollId=poll_id,
                        rank=rank,
                        clusterId=c['cluster_id'],
                        theme=c['theme'],
                        summary=c['summary'],
                        analysis=c['analysis'],
                        quotes=c['quotes'],
                        responseCount=c['responseCount'],
                    )
                    for rank, c in enumerate(top_clusters, 1)
                ]
                unique_respondents = len(set(r.phone_number for r in records if not r.is_opt_out))
                logger.info(f"Poll {poll_id}: {unique_respondents} respondents, {len(poll_issues)} issues")
            else:
                poll_issues = []
                unique_respondents = 0
                logger.info(f"Poll {poll_id}: 0 records, sending empty completion")

            event = self._build_complete_event(poll_id, unique_respondents, poll_issues, responses_location)
            all_events.append(event.to_json())

            if self.publish_to_sqs:
                self._send_to_sqs(event)
                logger.info(f"  Sent completion event for {poll_id} to SQS")

        self._save_events_locally(all_events)

        return {'polls_processed': len(poll_ids), 'complete_events_sent': len(poll_ids)}

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

    def _build_complete_event(self, poll_id: str, total_responses: int, issues: list[PollIssueAnalysisData], responses_location: str = "") -> PollAnalysisCompleteEvent:
        return PollAnalysisCompleteEvent(
            type='pollAnalysisComplete',
            data=PollAnalysisCompleteData(
                pollId=poll_id,
                totalResponses=total_responses,
                responsesLocation=responses_location,
                issues=issues
            )
        )

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

    def _send_to_sqs(self, event: PollAnalysisCompleteEvent) -> None:
        """Send event to SQS FIFO queue with proper MessageGroupId"""
        if not self.publish_to_sqs or not self.sqs_client:
            logger.warning("SQS publishing disabled but _send_to_sqs called")
            return

        message_body = json.dumps(event.to_json())

        try:
            response = self.sqs_client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message_body,
                MessageGroupId=f"gp-queue-polls-{event.data.pollId}",
                MessageDeduplicationId=str(uuid.uuid4())  # Unique per message
            )
            logger.debug(f"Sent {event.type} to SQS: MessageId={response['MessageId']}")
        except Exception as e:
            logger.error(f"Failed to send {event.type} to SQS: {e}", exc_info=True)
            raise
