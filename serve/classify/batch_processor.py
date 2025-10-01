#!/usr/bin/env python3

import asyncio
import time
from typing import List, Optional, Dict, Any, Callable
from dataclasses import dataclass, field
import json
from pathlib import Path

from shared.llm_gemini import GeminiModelType
from shared.logger import get_logger

from .models import EnrichedMessage, SmartCategorization
from .smart_classifier import WorldClassClassifier
from .validator import ClassificationValidator, ValidationResult

logger = get_logger(__name__)


@dataclass
class BatchProcessingConfig:
    """Configuration for ultra-high-throughput batch processing"""
    batch_size: int = 200  # Larger batches for efficiency
    max_parallel_batches: int = 50  # Massive parallelization
    max_retries: int = 2
    retry_delay: float = 0.1  # Faster retries
    enable_validation: bool = False  # Disable validation for speed
    checkpoint_interval: int = 500
    model_type: GeminiModelType = GeminiModelType.FLASH
    temperature: float = 0.0
    ultra_fast_mode: bool = True  # Enable all speed optimizations


@dataclass
class ProcessingStats:
    """Statistics for batch processing"""
    total_messages: int = 0
    processed_messages: int = 0
    successful_classifications: int = 0
    failed_classifications: int = 0
    validation_errors: int = 0
    validation_warnings: int = 0
    total_batches: int = 0
    completed_batches: int = 0
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    @property
    def duration(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time

    @property
    def messages_per_second(self) -> float:
        return self.processed_messages / self.duration if self.duration > 0 else 0

    @property
    def success_rate(self) -> float:
        return self.successful_classifications / self.processed_messages if self.processed_messages > 0 else 0


@dataclass
class BatchResult:
    """Result from processing a single batch"""
    batch_id: int
    messages: List[EnrichedMessage]
    classifications: List[SmartCategorization]
    validation_results: Optional[List[ValidationResult]]
    processing_time: float
    error_count: int = 0
    retry_count: int = 0


class BatchProcessor:
    """
    High-performance batch processor for civic message classification
    """

    def __init__(self, config: BatchProcessingConfig = None, checkpoint_dir: str = "./checkpoints"):
        self.config = config or BatchProcessingConfig()
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(exist_ok=True)

        self.classifier = WorldClassClassifier(
            model_type=self.config.model_type,
            temperature=self.config.temperature,
            target_concurrency=100  # High-throughput configuration from CLAUDE.md
        )

        if self.config.enable_validation:
            self.validator = ClassificationValidator()
        else:
            self.validator = None

        self.stats = ProcessingStats()
        self.progress_callback: Optional[Callable] = None

    def set_progress_callback(self, callback: Callable[[ProcessingStats], None]):
        """Set callback for progress updates"""
        self.progress_callback = callback

    async def process_batch(self, batch_id: int, messages: List[EnrichedMessage], retry_count: int = 0) -> BatchResult:
        """Process a single batch of messages"""
        logger.debug(f"Processing batch {batch_id} with {len(messages)} messages (retry {retry_count})")

        start_time = time.time()

        try:
            # Extract MessageData for classification
            message_data_list = [msg.original_data for msg in messages]

            # Classify messages
            classifications = await self.classifier.classify_batch(message_data_list)

            # Validate classifications if enabled
            validation_results = None
            if self.validator and not self.config.ultra_fast_mode:
                validation_data = self.validator.validate_batch(message_data_list, classifications)
                validation_results = validation_data["results"]

                # Apply confidence adjustments
                for i, (classification, validation_result) in enumerate(zip(classifications, validation_results)):
                    if validation_result.confidence_adjustment is not None:
                        classifications[i].confidence_score = validation_result.confidence_adjustment

            # Update enriched messages with classifications
            for i, (message, classification) in enumerate(zip(messages, classifications)):
                message.smart_classification = classification
                message.is_substantive = not classification.should_be_uncategorized

            processing_time = time.time() - start_time

            # Count errors
            error_count = sum(
                1 for result in validation_results or []
                if not result.is_valid
            )

            return BatchResult(
                batch_id=batch_id,
                messages=messages,
                classifications=classifications,
                validation_results=validation_results,
                processing_time=processing_time,
                error_count=error_count,
                retry_count=retry_count
            )

        except Exception as e:
            logger.error(f"Error processing batch {batch_id} (retry {retry_count}): {e}")

            # If we haven't exceeded max retries, try again
            if retry_count < self.config.max_retries:
                logger.info(f"Retrying batch {batch_id} in {self.config.retry_delay} seconds...")
                await asyncio.sleep(self.config.retry_delay)
                return await self.process_batch(batch_id, messages, retry_count + 1)

            # Max retries exceeded, return error result
            processing_time = time.time() - start_time
            return BatchResult(
                batch_id=batch_id,
                messages=messages,
                classifications=[],
                validation_results=None,
                processing_time=processing_time,
                error_count=len(messages),
                retry_count=retry_count
            )

    def create_batches(self, messages: List[EnrichedMessage]) -> List[List[EnrichedMessage]]:
        """Split messages into batches"""
        batches = []
        for i in range(0, len(messages), self.config.batch_size):
            batch = messages[i:i + self.config.batch_size]
            batches.append(batch)

        logger.info(f"Created {len(batches)} batches of size {self.config.batch_size}")
        return batches

    async def process_single_message(self, message: EnrichedMessage, message_index: int) -> EnrichedMessage:
        """Process a single message - individual task pattern from production matcher"""
        try:
            # Extract MessageData for classification
            message_data = message.original_data

            # Classify message using the classifier's async method
            classification = await self.classifier.classify_message(message_data)

            # Update enriched message with classification
            message.smart_classification = classification
            message.is_substantive = not classification.should_be_uncategorized

            return message

        except Exception as e:
            logger.error(f"❌ Error processing individual message {message_index}: {str(e)}")
            # Return message with error classification
            from .models import SmartCategorization, Sentiment, MessageQuality, ContentType
            message.smart_classification = SmartCategorization(
                issues=[],
                should_be_uncategorized=True,
                uncategorized_reason=f"Processing error: {str(e)}",
                overall_sentiment=Sentiment.OTHER,
                message_quality=MessageQuality.MINIMAL_RESPONSE,
                content_type=ContentType.GENERAL_COMPLAINT,
                confidence_score=0.1
            )
            message.is_substantive = False
            return message

    async def process_all_messages_production_pattern(self, messages: List[EnrichedMessage]) -> List[EnrichedMessage]:
        """PRODUCTION PATTERN: Process ALL messages as individual concurrent tasks (10k/min pattern)"""
        logger.info(f"🚀 PRODUCTION PATTERN: Processing {len(messages)} messages with INDIVIDUAL TASK CONCURRENCY")
        logger.info(f"   Target: 10,000 messages/minute = 166+ messages/second")
        logger.info(f"   Pattern: ThreadPoolExecutor + Individual Tasks (from production matcher)")

        # Initialize stats
        self.stats = ProcessingStats()
        self.stats.total_messages = len(messages)
        self.stats.start_time = time.time()

        # PRODUCTION PATTERN: Create individual task for EVERY message
        print(f"   🔥 Creating {len(messages):,} individual concurrent tasks...")
        all_tasks = []
        for idx, message in enumerate(messages):
            task = self.process_single_message(message, idx)
            all_tasks.append(task)

        print(f"   ⚡ Launching {len(all_tasks):,} concurrent classification calls...")

        # Execute ALL tasks in parallel with progress tracking
        start_time = time.time()

        # Process ALL messages simultaneously (production pattern)
        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        duration = time.time() - start_time

        # Process results and handle exceptions
        processed_messages = []
        error_count = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"❌ Message {i} failed: {result}")
                error_count += 1
                # Create error message from original
                error_message = messages[i]
                from .models import SmartCategorization, Sentiment, MessageQuality, ContentType
                error_message.smart_classification = SmartCategorization(
                    issues=[],
                    should_be_uncategorized=True,
                    uncategorized_reason=f"Exception during processing: {str(result)}",
                    overall_sentiment=Sentiment.OTHER,
                    message_quality=MessageQuality.MINIMAL_RESPONSE,
                    content_type=ContentType.GENERAL_COMPLAINT,
                    confidence_score=0.1
                )
                error_message.is_substantive = False
                processed_messages.append(error_message)
            else:
                processed_messages.append(result)

        # Update stats
        self.stats.processed_messages = len(processed_messages)
        self.stats.successful_classifications = len(processed_messages) - error_count
        self.stats.failed_classifications = error_count
        self.stats.end_time = time.time()

        # Calculate performance metrics
        total_messages = len(processed_messages)
        messages_per_second = total_messages / max(0.1, duration)
        messages_per_minute = messages_per_second * 60
        target_achievement = (messages_per_minute / 10000) * 100  # 10k/min target

        logger.info(f"✅ PRODUCTION PATTERN COMPLETE:")
        logger.info(f"   📊 {total_messages} messages in {duration:.1f}s ({messages_per_second:.1f} msg/sec)")
        logger.info(f"   🚀 Throughput: {messages_per_minute:.0f} msg/min")
        logger.info(f"   🎯 Target achievement: {target_achievement:.1f}% of 10k/min goal")
        logger.info(f"   ✅ Success rate: {((total_messages - error_count)/total_messages*100):.1f}%")
        logger.info(f"   ❌ Errors: {error_count}")

        return processed_messages

    async def process_all_messages(self, messages: List[EnrichedMessage]) -> List[EnrichedMessage]:
        """Process all messages with parallel batching"""
        logger.info(f"Starting batch processing of {len(messages)} messages")

        # Initialize stats
        self.stats = ProcessingStats()
        self.stats.total_messages = len(messages)
        self.stats.start_time = time.time()

        # Create batches
        batches = self.create_batches(messages)
        self.stats.total_batches = len(batches)

        # Load checkpoint if exists
        checkpoint_file = self.checkpoint_dir / "processing_checkpoint.json"
        start_batch = self.load_checkpoint(checkpoint_file) if checkpoint_file.exists() else 0

        if start_batch > 0:
            logger.info(f"Resuming from checkpoint: batch {start_batch}")

        # Process batches with controlled parallelism
        all_results = []

        for batch_start in range(start_batch, len(batches), self.config.max_parallel_batches):
            batch_end = min(batch_start + self.config.max_parallel_batches, len(batches))
            parallel_batches = batches[batch_start:batch_end]

            logger.info(f"Processing batches {batch_start} to {batch_end - 1}")

            # Create tasks for parallel execution
            tasks = [
                self.process_batch(batch_start + i, batch)
                for i, batch in enumerate(parallel_batches)
            ]

            # Execute batches in parallel
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Handle results and exceptions
            for i, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    logger.error(f"Batch {batch_start + i} failed with exception: {result}")
                    # Create empty result
                    result = BatchResult(
                        batch_id=batch_start + i,
                        messages=parallel_batches[i],
                        classifications=[],
                        validation_results=None,
                        processing_time=0,
                        error_count=len(parallel_batches[i])
                    )

                all_results.append(result)
                self.update_stats(result)

            # Save checkpoint
            if (batch_end % self.config.checkpoint_interval == 0 or
                batch_end == len(batches)):
                self.save_checkpoint(checkpoint_file, batch_end)

            # Progress callback
            if self.progress_callback:
                self.progress_callback(self.stats)

        # Combine all results
        processed_messages = []
        for result in all_results:
            if result.classifications:  # Only add successful results
                processed_messages.extend(result.messages)

        self.stats.end_time = time.time()

        # Clean up checkpoint
        if checkpoint_file.exists():
            checkpoint_file.unlink()

        logger.info(f"Batch processing complete: {len(processed_messages)} messages processed in {self.stats.duration:.1f}s")
        logger.info(f"Success rate: {self.stats.success_rate:.1%}, Speed: {self.stats.messages_per_second:.1f} msgs/sec")

        return processed_messages

    def update_stats(self, result: BatchResult):
        """Update processing statistics"""
        self.stats.completed_batches += 1
        self.stats.processed_messages += len(result.messages)

        if result.classifications:
            self.stats.successful_classifications += len([
                c for c in result.classifications
                if not getattr(c, 'error', False)
            ])

        self.stats.failed_classifications += result.error_count

        if result.validation_results:
            self.stats.validation_errors += sum(
                1 for vr in result.validation_results
                if not vr.is_valid
            )
            self.stats.validation_warnings += sum(
                len([i for i in vr.issues if i.severity == "warning"])
                for vr in result.validation_results
            )

    def save_checkpoint(self, checkpoint_file: Path, batch_number: int):
        """Save processing checkpoint"""
        checkpoint_data = {
            "batch_number": batch_number,
            "timestamp": time.time(),
            "stats": {
                "processed_messages": self.stats.processed_messages,
                "successful_classifications": self.stats.successful_classifications,
                "failed_classifications": self.stats.failed_classifications,
            }
        }

        with open(checkpoint_file, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)

        logger.debug(f"Checkpoint saved: batch {batch_number}")

    def load_checkpoint(self, checkpoint_file: Path) -> int:
        """Load processing checkpoint"""
        try:
            with open(checkpoint_file, 'r') as f:
                data = json.load(f)

            batch_number = data.get("batch_number", 0)
            logger.info(f"Loaded checkpoint: resuming from batch {batch_number}")
            return batch_number

        except Exception as e:
            logger.warning(f"Failed to load checkpoint: {e}")
            return 0

    def generate_processing_report(self) -> str:
        """Generate processing report"""
        report_lines = [
            "# Batch Processing Report",
            "",
            f"**Total Messages:** {self.stats.total_messages}",
            f"**Processed Messages:** {self.stats.processed_messages}",
            f"**Success Rate:** {self.stats.success_rate:.1%}",
            f"**Processing Speed:** {self.stats.messages_per_second:.1f} messages/second",
            f"**Total Time:** {self.stats.duration:.1f} seconds",
            "",
            "## Batch Statistics",
            f"**Total Batches:** {self.stats.total_batches}",
            f"**Completed Batches:** {self.stats.completed_batches}",
            f"**Average Batch Size:** {self.config.batch_size}",
            "",
            "## Quality Metrics",
            f"**Successful Classifications:** {self.stats.successful_classifications}",
            f"**Failed Classifications:** {self.stats.failed_classifications}",
        ]

        if self.config.enable_validation:
            report_lines.extend([
                f"**Validation Errors:** {self.stats.validation_errors}",
                f"**Validation Warnings:** {self.stats.validation_warnings}",
            ])

        return "\n".join(report_lines)


async def main():
    """Test the batch processor"""
    from .models import MessageData, EnrichedMessage

    # Create test messages
    test_messages = []
    for i in range(10):
        message_data = MessageData(
            campaign_id=f"test_{i}",
            campaign_name="Test Campaign",
            contact_phone_number=f"123456789{i}",
            carrier="TEST",
            campaign_number="123",
            is_automatic_reply=False,
            send_direction="INBOUND",
            send_status="",
            error_code="",
            sent_at="2025-01-01T00:00:00.000Z",
            message_text=f"Test message {i} about property taxes and truck traffic!",
            texter_name="",
            message_type="SMS",
            mms_attachments=""
        )

        enriched_message = EnrichedMessage(
            original_data=message_data,
            is_substantive=True,
            original_csv_row=i + 1,
            original_csv_file="test.csv"
        )
        test_messages.append(enriched_message)

    # Configure processor
    config = BatchProcessingConfig(
        batch_size=3,
        max_parallel_batches=2,
        enable_validation=True
    )

    processor = BatchProcessor(config)

    # Set up progress callback
    def progress_callback(stats):
        print(f"Progress: {stats.processed_messages}/{stats.total_messages} "
              f"({stats.processed_messages/stats.total_messages:.1%})")

    processor.set_progress_callback(progress_callback)

    # Process messages
    processed_messages = await processor.process_all_messages(test_messages)

    print(f"\nProcessed {len(processed_messages)} messages")
    print(processor.generate_processing_report())

    # Show sample results
    for i, msg in enumerate(processed_messages[:3]):
        print(f"\nMessage {i+1}: {msg.original_data.message_text}")
        if msg.smart_classification:
            print(f"  Issues: {len(msg.smart_classification.issues)}")
            print(f"  Uncategorized: {msg.smart_classification.should_be_uncategorized}")


if __name__ == "__main__":
    asyncio.run(main())