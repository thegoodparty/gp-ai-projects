#!/usr/bin/env python3

import sys
from pathlib import Path
from typing import List, Dict, Optional
import asyncio

# Add project paths
sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

# Import the existing classification pipeline
from serve.classify.run_pipeline import ClassificationPipeline
from serve.classify.batch_processor import BatchProcessor, BatchProcessingConfig
from serve.classify.models import MessageData, EnrichedMessage

# Import our models
from serve.v1_tevyn_api.models.unified_record import ConsolidatedMessage, ClassificationResult

logger = get_logger(__name__)


class ClassificationAdapter:
    """
    Adapter for the existing classification pipeline to work with unified records
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize classification adapter"""
        self.config_path = config_path or str(Path(__file__).parent.parent.parent / "classify/config.yaml")

        # Initialize classification pipeline
        try:
            self.pipeline = ClassificationPipeline(self.config_path)
            logger.info("Classification pipeline initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize classification pipeline: {e}")
            raise

        # Initialize batch processor for direct classification
        from serve.classify.batch_processor import GeminiModelType
        batch_config = BatchProcessingConfig(
            model_type=GeminiModelType.FLASH,
            temperature=0.0,
            enable_validation=False,
            batch_size=200,
            max_parallel_batches=50
        )
        self.batch_processor = BatchProcessor(config=batch_config)
        logger.info("Batch processor initialized for direct classification")

    def _convert_to_enriched_messages(self, messages: List[ConsolidatedMessage]) -> List[EnrichedMessage]:
        """Convert ConsolidatedMessage objects to EnrichedMessage objects for classification"""
        enriched_messages = []

        for msg in messages:
            message_data = MessageData(
                campaign_id=msg.campaign_id or 'unknown',
                campaign_name=msg.campaign_name or 'Unknown Campaign',
                contact_phone_number=msg.phone_number,
                carrier=msg.carrier or 'UNKNOWN',
                campaign_number='+13132038028',
                is_automatic_reply=False,
                send_direction='INBOUND',
                send_status='',
                error_code='',
                sent_at=msg.sent_at.isoformat() if hasattr(msg.sent_at, 'isoformat') else str(msg.sent_at),
                message_text=msg.message_text,
                texter_name='',
                message_type='SMS',
                mms_attachments=''
            )

            enriched_message = EnrichedMessage(
                original_data=message_data,
                is_substantive=False
            )

            enriched_messages.append(enriched_message)

        logger.debug(f"Converted {len(enriched_messages)} ConsolidatedMessage → EnrichedMessage objects")
        return enriched_messages

    async def process_messages(self, messages: List[ConsolidatedMessage]) -> Dict[str, ClassificationResult]:
        """
        Process messages through classification pipeline (configuration-driven)

        Args:
            messages: List of consolidated messages to classify

        Returns:
            Dict mapping phone numbers to classification results
        """
        if not messages:
            logger.warning("No messages provided for classification")
            return {}

        logger.info(f"Starting classification of {len(messages)} messages")

        # Check if we should use the existing pipeline or direct BatchProcessor
        use_existing_pipeline = self.pipeline.config.get('use_existing_pipeline', False)
        return_raw = self.pipeline.config.get('return_raw', False)

        if use_existing_pipeline and return_raw:
            # Use existing classify pipeline with return_data=True
            logger.info("Using existing classify pipeline with return_data mode")
            return await self._process_via_pipeline(messages)
        else:
            # Use direct BatchProcessor for in-memory processing
            logger.info("Using direct BatchProcessor for classification")
            return await self._process_via_batch_processor(messages)

    async def _process_via_batch_processor(self, messages: List[ConsolidatedMessage]) -> Dict[str, ClassificationResult]:
        """Process messages directly through BatchProcessor (in-memory, no CSV)"""
        try:
            # Convert ConsolidatedMessage → EnrichedMessage
            enriched_messages = self._convert_to_enriched_messages(messages)
            logger.debug(f"Converted {len(enriched_messages)} messages to EnrichedMessage format")

            # Run direct classification through BatchProcessor
            classified_messages = await self.batch_processor.process_all_messages_production_pattern(enriched_messages)
            logger.info(f"BatchProcessor completed: {len(classified_messages)} messages classified")

            # Extract classification results from EnrichedMessage objects
            classification_map = {}
            for msg in classified_messages:
                phone_number = msg.original_data.contact_phone_number

                # Extract from smart_classification (primary data structure)
                if msg.smart_classification and msg.smart_classification.issues:
                    first_issue = msg.smart_classification.issues[0]
                    primary_category = first_issue.primary_category
                    secondary_category = first_issue.secondary_category
                    stance = first_issue.stance.value

                    classification_result = ClassificationResult(
                        phone_number=phone_number,
                        primary_issue_category=primary_category,
                        secondary_issue=secondary_category,
                        issue_stance=stance,
                        overall_sentiment=msg.smart_classification.overall_sentiment.value,
                        message_quality=msg.smart_classification.message_quality.value,
                        content_type=msg.smart_classification.content_type.value,
                        confidence_score=1.0,
                        is_substantive=msg.is_substantive,
                        hierarchical_issues=[
                            {
                                'primary_category': issue.primary_category,
                                'secondary_category': issue.secondary_category,
                                'stance': issue.stance.value,
                                'specific_concern': issue.specific_concern
                            }
                            for issue in msg.smart_classification.issues
                        ]
                    )
                else:
                    # Handle uncategorized or non-substantive messages
                    classification_result = ClassificationResult(
                        phone_number=phone_number,
                        primary_issue_category="Uncategorized",
                        secondary_issue="general_feedback",
                        issue_stance="neutral",
                        overall_sentiment="other",
                        message_quality="substantive",
                        content_type="policy_feedback",
                        confidence_score=0.0,
                        is_substantive=msg.is_substantive
                    )

                classification_map[phone_number] = classification_result

            logger.info(f"Classification completed successfully for {len(classification_map)} messages")

            # Debug: Log first few classification results
            for phone, result in list(classification_map.items())[:3]:
                logger.info(f"Sample classification - Phone: {phone}, Primary: {result.primary_issue_category}, Secondary: {result.secondary_issue}")

            return classification_map

        except Exception as e:
            logger.error(f"Classification processing failed: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Return empty results with default values for all messages
            return {msg.phone_number: ClassificationResult(phone_number=msg.phone_number) for msg in messages}

    async def _process_via_pipeline(self, messages: List[ConsolidatedMessage]) -> Dict[str, ClassificationResult]:
        """Process messages through optimized in-memory path"""
        try:
            logger.info("Using optimized BatchProcessor (in-memory processing, no CSV intermediary)")
            return await self._process_via_batch_processor(messages)

        except Exception as e:
            logger.error(f"Pipeline processing failed: {e}")
            return {msg.phone_number: ClassificationResult(phone_number=msg.phone_number) for msg in messages}

    async def process_messages_batch(self, messages: List[ConsolidatedMessage], batch_size: int = 100) -> Dict[str, ClassificationResult]:
        """
        Process messages in batches for better performance

        Args:
            messages: List of messages to classify
            batch_size: Number of messages per batch

        Returns:
            Combined classification results
        """
        all_results = {}

        # Process messages in batches
        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]
            logger.info(f"Processing classification batch {i//batch_size + 1} ({len(batch)} messages)")

            batch_results = await self.process_messages(batch)
            all_results.update(batch_results)

            # Small delay between batches to prevent overwhelming the system
            await asyncio.sleep(0.1)

        return all_results


# Convenience function for direct usage
async def classify_messages(messages: List[ConsolidatedMessage],
                            config_path: Optional[str] = None) -> Dict[str, ClassificationResult]:
    """
    Convenience function to classify messages

    Args:
        messages: List of consolidated messages
        config_path: Path to classification config file

    Returns:
        Dict mapping phone numbers to classification results
    """
    adapter = ClassificationAdapter(config_path)
    return await adapter.process_messages_batch(messages)