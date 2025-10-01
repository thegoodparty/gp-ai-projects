#!/usr/bin/env python3

import uuid
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from shared.logger import get_logger
from shared.llm_gemini import GeminiClient, GeminiModelType
from ..models import FilteredMessage, AtomicMessage, PipelineConfig

logger = get_logger(__name__)

class AIMessageProcessor:
    """Unified AI-powered message processor for preprocessing, splitting, and anonymization"""

    def __init__(self, config: PipelineConfig, anonymize_keywords: Optional[List[str]] = None):
        self.config = config
        self.ai_config = config.ai_processing
        self.anonymize_keywords = anonymize_keywords or []

        # High-throughput LLM client configuration
        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0,
            thinking_budget=0,
            max_connections=self.ai_config.get("llm_batch_size", 50),
            max_keepalive_connections=25
        )

        # ThreadPoolExecutor for non-blocking LLM calls
        self.thread_pool = ThreadPoolExecutor(max_workers=self.ai_config.get("llm_batch_size", 50))

    def _create_processing_prompt(self, message_text: str, campaign_source: str) -> str:
        """Create comprehensive AI processing prompt"""

        anonymize_section = ""
        if self.anonymize_keywords:
            keywords_list = "', '".join(self.anonymize_keywords)
            anonymize_section = f"""
4. ANONYMIZATION: Replace ONLY these exact terms with generic equivalents:
   - Replace '{keywords_list}' with 'the local area' or 'this community'
   - Do NOT anonymize any other location names, streets, buildings, or infrastructure
   - ONLY replace the exact keywords provided, nothing else
"""

        return f"""You are processing a civic engagement message for topic discovery analysis. Your task is to clean, filter, split, and optionally anonymize the message.

CAMPAIGN SOURCE: {campaign_source}

PROCESSING STEPS:
1. CONTENT FILTERING: Remove profanity, personal attacks, spam, and non-civic content
2. PREPROCESSING: Clean sentiment, normalize civic terms, remove personal references
3. SPLITTING: If message contains multiple distinct civic concerns, split into separate atomic messages
{anonymize_section}

CIVIC RELEVANCE CRITERIA:
Include messages about: infrastructure, public services, governance, community safety, economic development, housing, transportation, environment, education, healthcare, local policies, municipal operations, civic engagement

Exclude messages that are: purely social/personal, spam, advertising, off-topic, overly emotional without substantive content

INPUT MESSAGE: "{message_text}"

INSTRUCTIONS:
- If message is not civic-relevant, return: FILTERED_OUT
- If message is civic-relevant but single topic, return: SINGLE:[cleaned message]
- If message contains multiple civic topics, return: MULTIPLE:[topic1]|||[topic2]|||[topic3]
- Clean language while preserving meaning and civic context
- Remove personal identifiers but keep civic issue details
- Maximum 5 atomic messages per input
- Each atomic message should be 15-200 words and focus on one specific civic concern

RESPONSE FORMAT:
FILTERED_OUT
OR
SINGLE:[cleaned civic message]
OR
MULTIPLE:[atomic message 1]|||[atomic message 2]|||[atomic message 3]"""

    async def process_message_async(self, filtered_message: FilteredMessage, index: int) -> List[AtomicMessage]:
        """Process a single message with AI"""
        try:
            # Skip if already filtered out
            if not filtered_message.filter_result.passed:
                return []

            message_text = filtered_message.filtered_text
            if not message_text or not message_text.strip():
                return []

            # Create processing prompt
            prompt = self._create_processing_prompt(message_text, filtered_message.campaign_source)

            # Non-blocking LLM call via ThreadPoolExecutor
            response = await asyncio.get_event_loop().run_in_executor(
                self.thread_pool,
                lambda: self.llm_client.generate_content(
                    prompt=prompt
                )
            )

            if not response:
                logger.warning(f"Empty response for message {index}")
                return []

            # Handle both string and object responses
            if hasattr(response, 'content'):
                content = response.content.strip()
            else:
                content = str(response).strip()

            # Parse AI response
            if content.startswith("FILTERED_OUT"):
                return []

            atomic_messages = []

            if content.startswith("SINGLE:"):
                cleaned_text = content[7:].strip()
                if cleaned_text:
                    atomic_message = self._create_atomic_message(
                        filtered_message, cleaned_text, 0
                    )
                    atomic_messages.append(atomic_message)

            elif content.startswith("MULTIPLE:"):
                parts = content[9:].split("|||")
                for i, part in enumerate(parts):  # Process ALL atomic messages, not just first 5
                    cleaned_part = part.strip()
                    if cleaned_part:
                        atomic_message = self._create_atomic_message(
                            filtered_message, cleaned_part, i
                        )
                        atomic_messages.append(atomic_message)

            return atomic_messages

        except Exception as e:
            logger.error(f"Error processing message {index}: {e}")
            return []

    def _create_atomic_message(self, filtered_message: FilteredMessage, processed_text: str, atomic_index: int) -> AtomicMessage:
        """Create an AtomicMessage from processed text"""
        atomic_message = AtomicMessage(
            id=str(uuid.uuid4()),
            parent_message_id=filtered_message.original_message_id,
            csv_file=filtered_message.csv_file,
            csv_row_index=filtered_message.csv_row_index,
            atomic_index=atomic_index,
            atomic_text=processed_text,
            processed_text=processed_text,
            original_text=filtered_message.original_text,
            campaign_source=filtered_message.campaign_source,
            metadata=filtered_message.metadata,  # Pass through metadata including phone number
            created_at=datetime.now()
        )

        # Debug first few atomic messages with comprehensive metadata logging
        if hasattr(self, '_debug_count'):
            self._debug_count += 1
        else:
            self._debug_count = 1

        if self._debug_count <= 5:
            phone = filtered_message.metadata.get("Contact Phone Number", "NOT_FOUND")
            sent_at = filtered_message.metadata.get("Sent At", "NOT_FOUND")
            logger.info(f"AI_PROCESSOR MESSAGE {self._debug_count} (csv_row={filtered_message.csv_row_index}):")
            logger.info(f"  phone='{phone}', sent_at='{sent_at}'")
            logger.info(f"  atomic_index={atomic_index}")
            logger.info(f"  filtered_metadata_keys={list(filtered_message.metadata.keys())}")
            logger.info(f"  atomic_metadata_keys={list(atomic_message.metadata.keys())}")

        return atomic_message

    async def process_messages_batch(self, filtered_messages: List[FilteredMessage]) -> List[AtomicMessage]:
        """Process all messages with high-throughput AI processing"""
        if not filtered_messages:
            return []

        logger.info(f"Starting AI processing of {len(filtered_messages)} messages")

        # Filter to only passed messages
        passed_messages = [msg for msg in filtered_messages if msg.filter_result.passed]
        logger.info(f"Processing {len(passed_messages)} messages that passed content filtering")

        if not passed_messages:
            return []

        # Create individual task for every message
        all_tasks = []
        for idx, message in enumerate(passed_messages):
            task = self.process_message_async(message, idx)
            all_tasks.append(task)

        # Execute all tasks in parallel
        logger.info(f"Executing {len(all_tasks)} AI processing tasks in parallel")
        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # Flatten results and handle exceptions
        atomic_messages = []
        error_count = 0

        for result in results:
            if isinstance(result, Exception):
                error_count += 1
                logger.error(f"Task failed: {result}")
            elif isinstance(result, list):
                atomic_messages.extend(result)

        logger.info(f"AI processing completed: {len(atomic_messages)} atomic messages created")
        if error_count > 0:
            logger.warning(f"{error_count} messages failed processing")

        return atomic_messages

    def analyze_processing_impact(self, filtered_messages: List[FilteredMessage], atomic_messages: List[AtomicMessage]) -> Dict[str, Any]:
        """Analyze the impact of AI processing"""
        passed_filtered = [msg for msg in filtered_messages if msg.filter_result.passed]

        # Calculate statistics
        total_input = len(passed_filtered)
        total_output = len(atomic_messages)

        # Count split messages (messages that produced multiple atomic messages)
        message_splits = {}
        for atomic in atomic_messages:
            parent_id = atomic.parent_message_id
            if parent_id not in message_splits:
                message_splits[parent_id] = 0
            message_splits[parent_id] += 1

        split_count = len([count for count in message_splits.values() if count > 1])
        single_count = len([count for count in message_splits.values() if count == 1])
        filtered_out_count = total_input - len(message_splits)

        # Campaign-specific stats
        campaign_stats = {}
        for atomic in atomic_messages:
            campaign = atomic.campaign_source
            if campaign not in campaign_stats:
                campaign_stats[campaign] = {"atomic_messages": 0, "avg_length": 0}
            campaign_stats[campaign]["atomic_messages"] += 1

        # Calculate average lengths per campaign
        for campaign in campaign_stats:
            campaign_atomics = [a for a in atomic_messages if a.campaign_source == campaign]
            if campaign_atomics:
                avg_length = sum(len(a.processed_text) for a in campaign_atomics) / len(campaign_atomics)
                campaign_stats[campaign]["avg_length"] = avg_length

        return {
            "input_messages": total_input,
            "output_atomic_messages": total_output,
            "expansion_ratio": total_output / total_input if total_input > 0 else 0,
            "split_messages": split_count,
            "single_messages": single_count,
            "filtered_out_by_ai": filtered_out_count,
            "ai_filtering_rate": filtered_out_count / total_input if total_input > 0 else 0,
            "campaign_stats": campaign_stats,
            "anonymize_keywords": self.anonymize_keywords,
            "average_processed_length": sum(len(a.processed_text) for a in atomic_messages) / len(atomic_messages) if atomic_messages else 0
        }

    def generate_processing_report(self, filtered_messages: List[FilteredMessage], atomic_messages: List[AtomicMessage]) -> str:
        """Generate human-readable processing report"""
        stats = self.analyze_processing_impact(filtered_messages, atomic_messages)

        report_lines = [
            "AI Message Processing Report:",
            f"  Input Messages: {stats['input_messages']:,}",
            f"  Output Atomic Messages: {stats['output_atomic_messages']:,}",
            f"  Expansion Ratio: {stats['expansion_ratio']:.2f}x",
            "",
            "Processing Results:",
            f"  Single Topic Messages: {stats['single_messages']:,}",
            f"  Split Messages: {stats['split_messages']:,}",
            f"  Filtered Out by AI: {stats['filtered_out_by_ai']:,} ({stats['ai_filtering_rate']:.1%})",
            "",
            f"Average Processed Length: {stats['average_processed_length']:.1f} chars"
        ]

        if self.anonymize_keywords:
            report_lines.extend([
                "",
                f"Anonymized Keywords: {', '.join(self.anonymize_keywords)}"
            ])

        report_lines.extend([
            "",
            "Campaign Performance:"
        ])

        for campaign, campaign_stats in stats["campaign_stats"].items():
            report_lines.append(f"  {campaign}: {campaign_stats['atomic_messages']:,} messages "
                              f"(avg {campaign_stats['avg_length']:.1f} chars)")

        return "\n".join(report_lines)

    def get_usage_stats(self) -> Dict[str, Any]:
        """Get LLM usage statistics"""
        if hasattr(self.llm_client, 'get_usage_stats'):
            return self.llm_client.get_usage_stats()
        return {}


async def ai_message_processor_stage(filtered_messages: List[FilteredMessage],
                                   config: PipelineConfig,
                                   anonymize_keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    """Main entry point for AI message processing stage"""
    logger.info("=== AI MESSAGE PROCESSING STAGE ===")

    if not config.ai_processing.get("enabled", True):
        logger.info("AI processing is disabled, creating pass-through atomic messages")
        # Create simple atomic messages without AI processing
        atomic_messages = []
        for filtered_message in filtered_messages:
            if filtered_message.filter_result.passed:
                atomic_message = AtomicMessage(
                    id=str(uuid.uuid4()),
                    parent_message_id=filtered_message.original_message_id,
                    csv_file=filtered_message.csv_file,
                    csv_row_index=filtered_message.csv_row_index,
                    atomic_index=0,
                    atomic_text=filtered_message.filtered_text,
                    processed_text=filtered_message.filtered_text,
                    original_text=filtered_message.original_text,
                    campaign_source=filtered_message.campaign_source,
                    metadata=filtered_message.metadata,
                    created_at=datetime.now()
                )
                atomic_messages.append(atomic_message)

        return {
            "messages": atomic_messages,
            "cost": 0,
            "usage_stats": {}
        }

    try:
        processor = AIMessageProcessor(config, anonymize_keywords)

        # Process messages with AI
        atomic_messages = await processor.process_messages_batch(filtered_messages)

        # Generate and log report
        report = processor.generate_processing_report(filtered_messages, atomic_messages)
        logger.info(f"\n{report}")

        # Get usage statistics
        usage_stats = processor.get_usage_stats()
        if usage_stats:
            logger.info(f"LLM Usage - Calls: {usage_stats.get('api_call_count', 0)}, "
                       f"Tokens: {usage_stats.get('total_tokens', 0):,}, "
                       f"Cost: ${usage_stats.get('total_cost', 0):.4f}")

        # Return format compatible with orchestrator's cost tracking
        return {
            "messages": atomic_messages,
            "cost": usage_stats.get('total_cost', 0) if usage_stats else 0,
            "usage_stats": usage_stats
        }

    except Exception as e:
        logger.error(f"AI message processing failed: {e}")
        raise