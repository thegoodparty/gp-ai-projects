#!/usr/bin/env python3

import asyncio
import aiohttp
import json
import time
import os
from typing import List, Dict, Any, Optional
from pathlib import Path

# Add project paths
import sys
sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

# Import models
from serve.v1_tevyn_api.models.unified_record import UnifiedCampaignRecord

logger = get_logger(__name__)


class DynamoDBUploader:
    """
    Uploader for sending unified campaign records to DynamoDB via Lambda API
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the uploader with configuration"""
        self.config = config
        self.api_url = config.get('api_url', 'https://ai-dev.goodparty.org/serve/messages')
        self.api_key = config.get('api_key', os.getenv('SERVE_API_KEY'))
        self.batch_size = config.get('batch_size', 25)
        self.retry_attempts = config.get('retry_attempts', 3)
        self.retry_delay = config.get('retry_delay', 1.0)
        self.timeout = config.get('timeout', 30)

        # Statistics
        self.total_uploaded = 0
        self.total_failed = 0
        self.upload_errors = []

        logger.info(f"DynamoDB Uploader initialized - Table: {config.get('table_name', 'campaign_data_dev')}")

    async def _make_request(self, session: aiohttp.ClientSession, campaign_id: str, record_data: Dict[str, Any]) -> bool:
        """Make a single upload request to the Lambda API"""
        url = f"{self.api_url.rstrip('/')}/{campaign_id}"

        headers = {
            'Content-Type': 'application/json',
            'x-api-key': self.api_key
        }

        for attempt in range(self.retry_attempts):
            try:
                async with session.post(url, json=record_data, headers=headers, timeout=self.timeout) as response:
                    if response.status == 200:
                        response_data = await response.json()
                        if response_data.get('success'):
                            return True
                        else:
                            logger.warning(f"API returned success=false: {response_data}")
                            return False
                    elif response.status == 400:
                        # Client error - don't retry
                        error_text = await response.text()
                        logger.error(f"Client error (400): {error_text}")
                        self.upload_errors.append(f"400 error: {error_text}")
                        return False
                    else:
                        # Server error - retry
                        error_text = await response.text()
                        logger.warning(f"Server error {response.status} (attempt {attempt + 1}): {error_text}")

                        if attempt < self.retry_attempts - 1:
                            await asyncio.sleep(self.retry_delay * (2 ** attempt))  # Exponential backoff
                        else:
                            self.upload_errors.append(f"{response.status} error: {error_text}")
                            return False

            except asyncio.TimeoutError:
                logger.warning(f"Request timeout (attempt {attempt + 1})")
                if attempt < self.retry_attempts - 1:
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
                else:
                    self.upload_errors.append("Timeout after all retry attempts")
                    return False

            except Exception as e:
                logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
                if attempt < self.retry_attempts - 1:
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
                else:
                    self.upload_errors.append(f"Request error: {str(e)}")
                    return False

        return False

    async def _upload_batch(self, session: aiohttp.ClientSession, records: List[UnifiedCampaignRecord]) -> Dict[str, int]:
        """Upload a batch of records concurrently"""
        if not records:
            return {'successful': 0, 'failed': 0}

        campaign_id = records[0].campaign_id
        logger.debug(f"Uploading batch of {len(records)} records to campaign {campaign_id}")

        # Create upload tasks
        tasks = []
        for record in records:
            record_data = record.to_dynamodb_item()
            task = self._make_request(session, campaign_id, record_data)
            tasks.append(task)

        # Execute all requests concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successes and failures
        successful = sum(1 for result in results if result is True)
        failed = len(results) - successful

        # Log any exceptions
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Record {i} upload failed with exception: {result}")
                self.upload_errors.append(f"Record {i}: {str(result)}")

        return {'successful': successful, 'failed': failed}

    async def batch_upload_records(self, records: List[UnifiedCampaignRecord]) -> Dict[str, int]:
        """
        Upload all records in three-record subdivisions via Lambda API

        Args:
            records: List of unified campaign records to upload

        Returns:
            Dictionary with upload statistics
        """
        if not records:
            logger.warning("No records provided for upload")
            return {'successful_uploads': 0, 'failed_uploads': 0}

        start_time = time.time()
        campaign_id = records[0].campaign_id if records else "unknown"

        logger.info(f"Starting upload of {len(records)} unified records (will create 3 DynamoDB records each) for campaign {campaign_id}")

        total_successful = 0
        total_failed = 0

        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=30,
            ttl_dns_cache=300,
            use_dns_cache=True
        )

        timeout = aiohttp.ClientTimeout(total=self.timeout)

        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # Upload all three record types
                for record in records:
                    # 1. Upload message record
                    message_data = self._create_message_record(record)
                    success = await self._make_request(session, campaign_id, message_data)
                    if success:
                        total_successful += 1
                    else:
                        total_failed += 1

                    # 2. Upload classification record
                    classify_data = self._create_classification_record(record)
                    success = await self._make_request(session, campaign_id, classify_data)
                    if success:
                        total_successful += 1
                    else:
                        total_failed += 1

                    # 3. Upload discovery record (if clustering data exists)
                    if record.multi_cluster_data:
                        discover_data = self._create_discovery_record(record)
                        success = await self._make_request(session, campaign_id, discover_data)
                        if success:
                            total_successful += 1
                        else:
                            total_failed += 1

                    # Small delay to avoid overwhelming API
                    await asyncio.sleep(0.01)

        except Exception as e:
            logger.error(f"Upload session failed: {e}")
            self.upload_errors.append(f"Session error: {str(e)}")

        processing_time = time.time() - start_time
        self.total_uploaded = total_successful
        self.total_failed = total_failed

        success_rate = (total_successful / (total_successful + total_failed)) * 100 if (total_successful + total_failed) > 0 else 0
        logger.info(f"Upload completed in {processing_time:.2f}s")
        logger.info(f"Results: {total_successful} successful, {total_failed} failed ({success_rate:.1f}%)")

        return {
            'successful_uploads': total_successful,
            'failed_uploads': total_failed,
            'total_records': len(records),
            'success_rate': success_rate,
            'processing_time': processing_time,
            'errors': self.upload_errors
        }

    def _create_message_record(self, record: UnifiedCampaignRecord) -> Dict[str, Any]:
        """Create message record data"""
        return {
            'campaign_id': record.campaign_id,
            'record_id': f"message#{record.phone_number}",
            'record_type': 'message',
            'phone_number': record.phone_number,
            'message_text': record.message_text,
            'sent_at': record.sent_at.isoformat() if hasattr(record.sent_at, 'isoformat') else str(record.sent_at),
            'round': record.round or 'Unknown',
            'carrier': record.carrier or 'Unknown',
            'campaign_name': record.campaign_name or record.campaign_id,
            'age': record.age,
            'location': record.location or 'Unknown',
            'income': record.income_level or 'Unknown',
            'homeowner': record.homeowner_status or 'Unknown',
            'business_owner': record.business_owner or 'Unknown',
            'families_with_children': record.has_children_under_18 or 'Unknown',
            'education_level': record.education_level or 'Unknown',
        }

    def _create_classification_record(self, record: UnifiedCampaignRecord) -> Dict[str, Any]:
        """Create classification record data with embedded demographics"""
        data = {
            'campaign_id': record.campaign_id,
            'record_id': f"classify#{record.phone_number}",
            'record_type': 'classify',
            'phone_number': record.phone_number,
            'message_text': record.message_text,
            'overall_sentiment': record.overall_sentiment or 'other',
            'message_quality': record.message_quality or 'substantive',
            'content_type': record.content_type or 'policy_feedback',
            'classification_confidence': float(record.classification_confidence or 0.0),
            'is_substantive': record.is_substantive if record.is_substantive is not None else True,
            'hierarchical_issues': record.hierarchical_issues or [],
            'age': record.age,
            'location': record.location or 'Unknown',
            'income': record.income_level or 'Unknown',
            'homeowner': record.homeowner_status or 'Unknown',
            'business_owner': record.business_owner or 'Unknown',
            'families_with_children': record.has_children_under_18 or 'Unknown',
            'education_level': record.education_level or 'Unknown',
        }

        return data

    def _create_discovery_record(self, record: UnifiedCampaignRecord) -> Dict[str, Any]:
        """Create discovery/clustering record data with embedded demographics"""
        data = {
            'campaign_id': record.campaign_id,
            'record_id': f"discover#{record.phone_number}",
            'record_type': 'discover',
            'phone_number': record.phone_number,
            'age': record.age,
            'location': record.location or 'Unknown',
            'income': record.income_level or 'Unknown',
            'homeowner': record.homeowner_status or 'Unknown',
            'business_owner': record.business_owner or 'Unknown',
            'families_with_children': record.has_children_under_18 or 'Unknown',
            'education_level': record.education_level or 'Unknown',
        }

        if record.multi_cluster_data:
            for cluster_count, cluster_info in record.multi_cluster_data.items():
                prefix = f"cluster_{cluster_count}"
                data[f"{prefix}_id"] = cluster_info.get('cluster_id', -1)
                data[f"{prefix}_theme"] = cluster_info.get('cluster_theme', '')
                data[f"{prefix}_category"] = cluster_info.get('cluster_category', '')
                data[f"{prefix}_topics"] = cluster_info.get('key_topics', [])
                data[f"{prefix}_sentiment"] = cluster_info.get('cluster_sentiment', '')
                data[f"{prefix}_relevance"] = cluster_info.get('civic_relevance', '')
                data[f"{prefix}_confidence"] = float(cluster_info.get('theme_confidence', 0.0))
                data[f"{prefix}_analysis"] = cluster_info.get('detailed_analysis', '')
                data[f"{prefix}_quotes"] = cluster_info.get('verbatim_quotes', '')

        return data

    async def upload_single_record(self, record: UnifiedCampaignRecord) -> bool:
        """
        Upload a single record (convenience method)

        Args:
            record: Unified campaign record to upload

        Returns:
            True if successful, False otherwise
        """
        results = await self.batch_upload_records([record])
        return results['successful_uploads'] > 0

    def get_upload_statistics(self) -> Dict[str, Any]:
        """Get current upload statistics"""
        return {
            'total_uploaded': self.total_uploaded,
            'total_failed': self.total_failed,
            'error_count': len(self.upload_errors),
            'recent_errors': self.upload_errors[-5:] if self.upload_errors else []
        }

    def clear_statistics(self):
        """Clear upload statistics and error log"""
        self.total_uploaded = 0
        self.total_failed = 0
        self.upload_errors.clear()


class MockDynamoDBUploader:
    """
    Mock uploader for testing purposes (doesn't actually upload)
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        logger.info("Mock DynamoDB Uploader initialized (no actual uploads will be performed)")

    async def batch_upload_records(self, records: List[UnifiedCampaignRecord]) -> Dict[str, int]:
        """Mock upload that just logs what would be uploaded"""
        if not records:
            return {'successful_uploads': 0, 'failed_uploads': 0}

        logger.info(f"[MOCK] Would upload {len(records)} records")

        # Simulate some processing time
        await asyncio.sleep(0.5)

        # Log sample record
        if records:
            sample = records[0]
            logger.debug(f"[MOCK] Sample record: campaign_id={sample.campaign_id}, phone={sample.phone_number}")

        return {
            'successful_uploads': len(records),
            'failed_uploads': 0,
            'total_records': len(records),
            'success_rate': 100.0,
            'processing_time': 0.5,
            'errors': []
        }

    async def upload_single_record(self, record: UnifiedCampaignRecord) -> bool:
        results = await self.batch_upload_records([record])
        return results['successful_uploads'] > 0

    def get_upload_statistics(self) -> Dict[str, Any]:
        return {'total_uploaded': 0, 'total_failed': 0, 'error_count': 0, 'recent_errors': []}

    def clear_statistics(self):
        pass


def create_uploader(config: Dict[str, Any], mock: bool = False) -> 'DynamoDBUploader':
    """
    Factory function to create uploader (real or mock)

    Args:
        config: Uploader configuration
        mock: If True, create mock uploader

    Returns:
        DynamoDBUploader instance
    """
    if mock:
        return MockDynamoDBUploader(config)
    else:
        return DynamoDBUploader(config)