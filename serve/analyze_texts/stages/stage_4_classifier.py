import asyncio
import sys
from pathlib import Path
from typing import List
from concurrent.futures import ThreadPoolExecutor

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from shared.logger import get_logger
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from serve.analyze_texts.models import MessageRecord, ClassifiedMessage, IssueClassification, HIERARCHICAL_TAXONOMY

logger = get_logger(__name__)


class MessageClassifier:
    def __init__(self, llm_config: dict = None):
        llm_config = llm_config or {}
        max_workers = llm_config.get("max_workers", 400)
        max_connections = llm_config.get("max_connections", 1200)

        self.llm_client = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=llm_config.get("temperature", 0.0),
            thinking_level=ThinkingLevel.MINIMAL,
            max_connections=max_connections,
            max_keepalive_connections=max_connections // 4
        )

        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self.max_workers = max_workers

        logger.info(f"MessageClassifier initialized with {max_connections} connections, {max_workers} workers")

    def create_taxonomy_description(self) -> str:
        lines = []
        for primary_cat, subcategories in HIERARCHICAL_TAXONOMY.items():
            lines.append(f"\n**{primary_cat}**:")
            for secondary_cat, description in subcategories.items():
                lines.append(f"  - {secondary_cat}: {description}")

        return "\n".join(lines)

    def classify_message_sync(self, message: MessageRecord) -> IssueClassification:
        taxonomy_desc = self.create_taxonomy_description()

        prompt = f"""Classify this civic message into our hierarchical taxonomy.

MESSAGE: "{message.message_text}"

TAXONOMY:
{taxonomy_desc}

Determine:
1. **primary_category**: The top-level category
2. **secondary_category**: The subcategory within that primary category
3. **stance**: Is the person positive, negative, neutral, or requesting about this issue?
4. **specific_concern**: A brief, specific description of what they're saying

Return in this exact JSON format:
{{
    "primary_category": "category_name",
    "secondary_category": "subcategory_name",
    "stance": "positive|negative|neutral|requesting",
    "specific_concern": "brief description"
}}"""

        try:
            result = self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=IssueClassification
            )

            return result

        except Exception as e:
            logger.error(f"Classification failed: {str(e)}")
            return IssueClassification(
                primary_category="other",
                secondary_category="uncategorized",
                stance="neutral",
                specific_concern=f"Classification error: {str(e)}"
            )

    async def classify_message_async(self, message: MessageRecord, index: int) -> tuple:
        loop = asyncio.get_event_loop()
        classification = await loop.run_in_executor(
            self.thread_pool,
            self.classify_message_sync,
            message
        )
        return (index, message, classification)

    async def classify_messages_parallel(self, messages: List[MessageRecord]) -> List[ClassifiedMessage]:
        logger.info(f"Classifying {len(messages)} messages in parallel...")

        tasks = [self.classify_message_async(message, idx) for idx, message in enumerate(messages)]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        classified_messages = []

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Classification task failed: {result}")
                continue

            idx, message, classification = result

            classified_msg = ClassifiedMessage(
                message=message,
                classification=classification
            )

            classified_messages.append(classified_msg)

        logger.info(f"Classification complete: {len(classified_messages)} messages classified")

        return classified_messages

    def classify_messages_sync(self, messages: List[MessageRecord]) -> List[ClassifiedMessage]:
        try:
            loop = asyncio.get_running_loop()
            import concurrent.futures

            def run_in_new_loop():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    return new_loop.run_until_complete(self.classify_messages_parallel(messages))
                finally:
                    new_loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_in_new_loop)
                return future.result()

        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self.classify_messages_parallel(messages))
            finally:
                loop.close()

    def get_usage_stats(self):
        if hasattr(self.llm_client, 'get_usage_stats'):
            return self.llm_client.get_usage_stats()
        return {}


def classify_data_stage(messages: List[MessageRecord], config: dict) -> List[ClassifiedMessage]:
    logger.info("=== STAGE 4: MESSAGE CLASSIFICATION ===")

    classifier_config = config.get("classifier", {})
    classifier = MessageClassifier(llm_config=classifier_config.get("llm_config", {}))

    classified_messages = classifier.classify_messages_sync(messages)

    usage_stats = classifier.get_usage_stats()
    if usage_stats:
        logger.info(f"Classification LLM Usage - Calls: {usage_stats.get('api_call_count', 0)}, "
                   f"Tokens: {usage_stats.get('total_tokens', 0):,}, "
                   f"Cost: ${usage_stats.get('total_cost', 0):.4f}")

    return classified_messages
