#!/usr/bin/env python3

import sys
import os
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

from serve.v1_tevyn_api.models.unified_record import (
    ConsolidatedMessage,
    ClassificationResult,
    ClusteringResult
)

logger = get_logger(__name__)


class DynamoDBUploadAdapter:
    """
    Adapter for uploading campaign data to DynamoDB with type subdivisions
    """

    def __init__(self, table_name: str = None, region: str = 'us-west-2'):
        """Initialize DynamoDB upload adapter"""
        self.table_name = table_name or os.getenv('DYNAMODB_TABLE_NAME', 'campaign_data_dev')
        self.region = region

        self.dynamodb = boto3.resource('dynamodb', region_name=self.region)
        self.table = self.dynamodb.Table(self.table_name)

        logger.info(f"Initialized DynamoDB adapter for table: {self.table_name}")

    async def upload_messages(self, messages: List[ConsolidatedMessage], campaign_id: str) -> int:
        """
        Upload message records with demographics

        Returns:
            Number of records uploaded
        """
        if not messages:
            logger.warning("No messages to upload")
            return 0

        logger.info(f"Uploading {len(messages)} message records for campaign: {campaign_id}")

        uploaded_count = 0
        batch_size = 25

        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]

            with self.table.batch_writer() as writer:
                for msg in batch:
                    item = self._create_message_item(msg, campaign_id)
                    try:
                        writer.put_item(Item=item)
                        uploaded_count += 1
                    except ClientError as e:
                        logger.error(f"Failed to upload message for {msg.phone_number}: {e}")

        logger.info(f"✅ Successfully uploaded {uploaded_count} message records")
        return uploaded_count

    async def upload_classifications(self,
                                     classification_results: Dict[str, ClassificationResult],
                                     messages: List[ConsolidatedMessage],
                                     campaign_id: str) -> int:
        """
        Upload classification records with embedded demographics

        Args:
            classification_results: Dict mapping phone_number to ClassificationResult
            messages: List of ConsolidatedMessage objects containing demographics
            campaign_id: Campaign identifier

        Returns:
            Number of records uploaded
        """
        if not classification_results:
            logger.warning("No classifications to upload")
            return 0

        logger.info(f"Uploading {len(classification_results)} classification records for campaign: {campaign_id}")

        messages_by_phone = {msg.phone_number: msg for msg in messages}

        uploaded_count = 0
        batch_size = 25

        items = list(classification_results.items())
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]

            with self.table.batch_writer() as writer:
                for phone_number, classification in batch:
                    message = messages_by_phone.get(phone_number)
                    item = self._create_classification_item(phone_number, classification, message, campaign_id)
                    try:
                        writer.put_item(Item=item)
                        uploaded_count += 1
                    except ClientError as e:
                        logger.error(f"Failed to upload classification for {phone_number}: {e}")

        logger.info(f"✅ Successfully uploaded {uploaded_count} classification records")
        return uploaded_count

    async def upload_discoveries(self,
                                clustering_results: Dict[str, ClusteringResult],
                                messages: List[ConsolidatedMessage],
                                campaign_id: str) -> int:
        """
        Upload discovery/clustering records with embedded demographics

        Args:
            clustering_results: Dict mapping phone_number to ClusteringResult
            messages: List of ConsolidatedMessage objects containing demographics
            campaign_id: Campaign identifier

        Returns:
            Number of records uploaded
        """
        if not clustering_results:
            logger.warning("No discovery results to upload")
            return 0

        logger.info(f"Uploading {len(clustering_results)} discovery records for campaign: {campaign_id}")

        messages_by_phone = {msg.phone_number: msg for msg in messages}

        uploaded_count = 0
        batch_size = 25

        items = list(clustering_results.items())
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]

            with self.table.batch_writer() as writer:
                for phone_number, clustering_data in batch:
                    message = messages_by_phone.get(phone_number)
                    item = self._create_discovery_item(phone_number, clustering_data, message, campaign_id)
                    try:
                        writer.put_item(Item=item)
                        uploaded_count += 1
                    except ClientError as e:
                        logger.error(f"Failed to upload discovery for {phone_number}: {e}")

        logger.info(f"✅ Successfully uploaded {uploaded_count} discovery records")
        return uploaded_count

    def _create_message_item(self, msg: ConsolidatedMessage, campaign_id: str) -> dict:
        """Create DynamoDB item for message record"""
        timestamp = datetime.utcnow().isoformat()

        return {
            'campaign_id': campaign_id,
            'record_id': f"message#{msg.phone_number}",
            'record_type': 'message',
            'phone_number': msg.phone_number,
            'message_text': msg.message_text,
            'sent_at': msg.sent_at.isoformat() if hasattr(msg.sent_at, 'isoformat') else str(msg.sent_at),
            'round': msg.round or 'Unknown',
            'carrier': msg.carrier or 'Unknown',
            'campaign_name': msg.campaign_name or campaign_id,
            'age': msg.age,
            'age_group': msg.age_group or 'Unknown',
            'location': msg.location or 'Unknown',
            'ward': msg.ward or 'Unknown',
            'voters_gender': msg.voters_gender or 'Unknown',
            'voting_performance_category': msg.voting_performance_category or 'Unknown',
            'residence_city': msg.residence_city or 'Unknown',
            'homeowner_status': msg.homeowner_status or 'Unknown',
            'business_owner': msg.business_owner or 'Unknown',
            'has_children_under_18': msg.has_children_under_18 or 'Unknown',
            'education_level': msg.education_level or 'Unknown',
            'income_level': msg.income_level or 'Unknown',
            'created_at': timestamp,
            'updated_at': timestamp,
        }

    def _create_classification_item(self,
                                   phone_number: str,
                                   classification: ClassificationResult,
                                   message: Optional[ConsolidatedMessage],
                                   campaign_id: str) -> dict:
        """Create DynamoDB item for classification record with embedded demographics"""
        timestamp = datetime.utcnow().isoformat()

        item = {
            'campaign_id': campaign_id,
            'record_id': f"classify#{phone_number}",
            'record_type': 'classify',
            'phone_number': phone_number,
            'primary_issue_category': classification.primary_issue_category or 'Uncategorized',
            'secondary_issue': classification.secondary_issue or 'general_feedback',
            'issue_stance': classification.issue_stance or 'neutral',
            'overall_sentiment': classification.overall_sentiment or 'other',
            'message_quality': classification.message_quality or 'substantive',
            'content_type': classification.content_type or 'policy_feedback',
            'classification_confidence': float(classification.classification_confidence or 0.0),
            'is_substantive': classification.is_substantive if classification.is_substantive is not None else True,
            'created_at': timestamp,
            'updated_at': timestamp,
        }

        if classification.hierarchical_issues:
            item['hierarchical_issues'] = classification.hierarchical_issues

        if message:
            item.update({
                'age': message.age,
                'age_group': message.age_group or 'Unknown',
                'location': message.location or 'Unknown',
                'ward': message.ward or 'Unknown',
                'voters_gender': message.voters_gender or 'Unknown',
                'voting_performance_category': message.voting_performance_category or 'Unknown',
                'residence_city': message.residence_city or 'Unknown',
                'homeowner_status': message.homeowner_status or 'Unknown',
                'business_owner': message.business_owner or 'Unknown',
                'has_children_under_18': message.has_children_under_18 or 'Unknown',
                'education_level': message.education_level or 'Unknown',
                'income_level': message.income_level or 'Unknown',
            })

        return item

    def _create_discovery_item(self,
                              phone_number: str,
                              clustering_data: Dict,
                              message: Optional[ConsolidatedMessage],
                              campaign_id: str) -> dict:
        """Create DynamoDB item for discovery/clustering record with embedded demographics"""
        timestamp = datetime.utcnow().isoformat()

        item = {
            'campaign_id': campaign_id,
            'record_id': f"discover#{phone_number}",
            'record_type': 'discover',
            'phone_number': phone_number,
            'created_at': timestamp,
            'updated_at': timestamp,
        }

        if isinstance(clustering_data, dict) and 'cluster_data' in clustering_data:
            multi_cluster_data = clustering_data['cluster_data']

            for cluster_count, cluster_info in multi_cluster_data.items():
                prefix = f"cluster_{cluster_count}"
                item[f"{prefix}_id"] = cluster_info.get('cluster_id', -1)
                item[f"{prefix}_theme"] = cluster_info.get('cluster_theme', '')
                item[f"{prefix}_category"] = cluster_info.get('cluster_category', '')
                item[f"{prefix}_topics"] = cluster_info.get('key_topics', [])
                item[f"{prefix}_sentiment"] = cluster_info.get('cluster_sentiment', '')
                item[f"{prefix}_relevance"] = cluster_info.get('civic_relevance', '')
                item[f"{prefix}_confidence"] = float(cluster_info.get('theme_confidence', 0.0))
                item[f"{prefix}_analysis"] = cluster_info.get('detailed_analysis', '')
                item[f"{prefix}_quotes"] = cluster_info.get('verbatim_quotes', [])

        if message:
            item.update({
                'age': message.age,
                'age_group': message.age_group or 'Unknown',
                'location': message.location or 'Unknown',
                'ward': message.ward or 'Unknown',
                'voters_gender': message.voters_gender or 'Unknown',
                'voting_performance_category': message.voting_performance_category or 'Unknown',
                'residence_city': message.residence_city or 'Unknown',
                'homeowner_status': message.homeowner_status or 'Unknown',
                'business_owner': message.business_owner or 'Unknown',
                'has_children_under_18': message.has_children_under_18 or 'Unknown',
                'education_level': message.education_level or 'Unknown',
                'income_level': message.income_level or 'Unknown',
            })

        return item