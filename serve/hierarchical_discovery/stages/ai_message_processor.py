#!/usr/bin/env python3

import uuid
import asyncio
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel, Field

from shared.logger import get_logger
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from ..models import FilteredMessage, AtomicMessage, PipelineConfig

logger = get_logger(__name__)


class ProcessingResult(BaseModel):
    """Structured output for AI message processing"""
    status: Literal["filtered_out", "single", "multiple"] = Field(
        description="Whether message is filtered out, single topic, or multiple topics"
    )
    messages: List[str] = Field(
        default_factory=list,
        description="List of cleaned civic messages. Empty if filtered_out, 1 item if single, 2+ if multiple"
    )
    filter_reason: Optional[str] = Field(
        default=None,
        description="Reason for filtering if status is filtered_out"
    )

class AIMessageProcessor:
    """Unified AI-powered message processor for preprocessing, splitting, and anonymization"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.ai_config = config.ai_processing

        # High-throughput LLM client configuration
        self.llm_client = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=0.0,
            thinking_level=ThinkingLevel.MINIMAL,
            max_connections=self.ai_config.get("llm_batch_size", 50),
            max_keepalive_connections=25
        )

        # ThreadPoolExecutor for non-blocking LLM calls
        self.thread_pool = ThreadPoolExecutor(max_workers=self.ai_config.get("llm_batch_size", 50))

    def __enter__(self):
        return self

    def __exit__(self):
        self.cleanup()
        return False

    def cleanup(self):
        if hasattr(self, 'thread_pool'):
            try:
                self.thread_pool.shutdown(wait=True, cancel_futures=False)
                logger.debug("ThreadPoolExecutor shut down successfully")
            except Exception as e:
                logger.warning(f"Error shutting down ThreadPoolExecutor: {e}")

        if hasattr(self, 'llm_client') and hasattr(self.llm_client, 'close'):
            try:
                self.llm_client.close()
            except Exception as e:
                logger.warning(f"Error closing LLM client: {e}")

    def __del__(self):
        self.cleanup()

    def _create_processing_prompt(self, message_text: str, campaign_source: str) -> str:
        """Create comprehensive AI processing prompt for structured output"""

        return f"""You are processing a civic engagement message for topic discovery analysis. Your task is to clean, filter, split, and anonymize the message.

CAMPAIGN SOURCE: {campaign_source}

PROCESSING STEPS:
1. CONTENT FILTERING: Remove profanity, personal attacks, spam, and non-civic content
2. PREPROCESSING: Clean sentiment, normalize civic terms, remove personal references
3. AGGRESSIVE SPLITTING: Split message into SEPARATE atomic messages for EACH distinct topic mentioned
   - ALWAYS split when multiple topics are present, even if they're causally related
   - Example: "Need affordable housing OR schools will lose students" = TWO atomics (housing + schools)
   - Example: "Fix roads AND sidewalks" = TWO atomics (roads + sidewalks)
   - Example: "Traffic causes pollution" = TWO atomics (traffic + pollution)
4. LOCATION ANONYMIZATION (CRITICALLY IMPORTANT - ALWAYS DO THIS):
   - Identify and remove ALL city, town, municipality, and county names from the message
   - This includes: city names, town names, county names, region names, neighborhood names
   - Replace with generic terms: "the local area", "this community", "the area", or simply remove
   - Examples:
     * "Burnsville roads are terrible" → "Roads in the local area are terrible"
     * "Berkeley schools need funding" → "Local schools need funding"
     * "Traffic in downtown Minneapolis" → "Traffic in the downtown area"
   - KEEP these specific location references:
     * Street names (e.g., "Main Street", "Highway 101")
     * Building/facility names (e.g., "City Hall", "Community Center")
     * Generic descriptors (e.g., "downtown", "suburbs")
   - WHY: We cluster by TOPIC, not by location. Location names cause superficial clustering.
   - This is MANDATORY for every single message - do not skip this step

CIVIC RELEVANCE CRITERIA:
Include messages about: infrastructure, public services, governance, community safety, economic development, housing, transportation, environment, education, healthcare, local policies, municipal operations, civic engagement

Exclude messages that are: purely social/personal, spam, advertising, off-topic, overly emotional without substantive content

INPUT MESSAGE: "{message_text}"

SPLITTING RULES (CRITICAL):
- EACH distinct topic = separate atomic message, even if related/connected
- Look for AND/OR conjunctions, multiple sentences, or compound statements
- Split "cause → effect" statements into separate atomics for cause AND effect
- Only keep as single message if message discusses ONE specific thing in depth

INSTRUCTIONS:
- If message is NOT civic-relevant: set status="filtered_out", messages=[], provide filter_reason
- If message is civic-relevant with SINGLE topic: set status="single", messages=[cleaned message]
- If message contains MULTIPLE civic topics: set status="multiple", messages=[topic1, topic2, topic3, ...]
- Clean language while preserving meaning and civic context
- Remove personal identifiers but keep civic issue details
- Maximum 5 atomic messages per input
- Each atomic message should be 15-200 words and focus on ONE SPECIFIC civic concern"""

    async def process_message_async(self, filtered_message: FilteredMessage, index: int) -> List[AtomicMessage]:
        """Process a single message with AI using structured outputs"""
        try:
            # Skip if already filtered out
            if not filtered_message.filter_result.passed:
                return []

            message_text = filtered_message.filtered_text
            if not message_text or not message_text.strip():
                return []

            # Create processing prompt
            prompt = self._create_processing_prompt(message_text, filtered_message.campaign_source)

            # Non-blocking LLM call with structured output schema
            response = await asyncio.get_event_loop().run_in_executor(
                self.thread_pool,
                lambda: self.llm_client.generate_structured_content(
                    prompt=prompt,
                    response_schema=ProcessingResult
                )
            )

            if not response:
                logger.warning(f"Empty response for message {index}")
                return []

            # Parse structured response
            try:
                # GeminiClient should return a ProcessingResult object
                if isinstance(response, ProcessingResult):
                    result = response
                elif isinstance(response, dict):
                    result = ProcessingResult(**response)
                else:
                    # Fallback: try to parse from response content
                    import json
                    if hasattr(response, 'content'):
                        content = response.content.strip()
                    else:
                        content = str(response).strip()
                    result = ProcessingResult(**json.loads(content))
            except Exception as parse_error:
                logger.error(f"Failed to parse structured response for message {index}: {parse_error}")
                return []

            # Handle based on status
            if result.status == "filtered_out":
                if result.filter_reason:
                    logger.debug(f"Message {index} filtered: {result.filter_reason}")
                return []

            atomic_messages = []

            # Create atomic messages from the structured list
            for i, cleaned_text in enumerate(result.messages):
                if cleaned_text and cleaned_text.strip():
                    atomic_message = self._create_atomic_message(
                        filtered_message, cleaned_text.strip(), i
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
            "location_anonymization": "LLM-based (campaign-specific locations)",
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
            f"Location Anonymization: {stats['location_anonymization']}",
            f"Average Processed Length: {stats['average_processed_length']:.1f} chars",
            "",
            "Campaign Performance:"
        ]

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
                                   config: PipelineConfig) -> Dict[str, Any]:
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
        processor = AIMessageProcessor(config)

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