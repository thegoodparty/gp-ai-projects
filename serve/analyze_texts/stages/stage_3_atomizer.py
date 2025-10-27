import re
import asyncio
import sys
import uuid
from pathlib import Path
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from shared.logger import get_logger
from shared.llm_gemini import GeminiClient, GeminiModelType
from serve.analyze_texts.models import MessageRecord, AtomizationResult

logger = get_logger(__name__)


class MessageAtomizer:
    def __init__(
        self,
        anonymize: bool = True,
        anonymization_rules: Dict[str, str] = None,
        llm_config: dict = None
    ):
        self.anonymize = anonymize
        self.anonymization_rules = anonymization_rules or {}

        llm_config = llm_config or {}
        max_workers = llm_config.get("max_workers", 400)
        max_connections = llm_config.get("max_connections", 1200)

        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=llm_config.get("temperature", 0.0),
            thinking_budget=llm_config.get("thinking_budget", 0),
            max_connections=max_connections,
            max_keepalive_connections=max_connections // 4
        )

        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self.max_workers = max_workers

        logger.info(f"MessageAtomizer initialized with {max_connections} connections, {max_workers} workers")

    def anonymize_text(self, text: str) -> str:
        if not self.anonymize:
            return text

        anonymized = text
        for old_term, new_term in self.anonymization_rules.items():
            pattern = r'\b' + re.escape(old_term) + r'\b'
            anonymized = re.sub(pattern, new_term, anonymized, flags=re.IGNORECASE)

        return anonymized

    def atomize_message_sync(self, message: MessageRecord) -> AtomizationResult:
        prompt = f"""Analyze this civic message to determine if it contains multiple distinct concerns or just one.

MESSAGE: "{message.message_text}"

If the message contains multiple SEPARATE concerns (e.g., "taxes too high, roads have potholes"), split it into atomic messages.
If it's a single concern (even if detailed), keep it as one message.

Guidelines:
- Compound: Multiple unrelated topics (taxes AND roads)
- Single: One topic with details (just about taxes with explanation)
- Preserve original wording - don't paraphrase
- Keep each atomic message complete and understandable on its own

Return in this exact JSON format:
{{
    "is_compound": true/false,
    "atomic_messages": ["message1", "message2", ...],
    "reasoning": "brief explanation"
}}"""

        try:
            result = self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=AtomizationResult
            )

            return result

        except Exception as e:
            logger.error(f"Atomization failed for message: {str(e)}")
            return AtomizationResult(
                is_compound=False,
                atomic_messages=[message.message_text],
                reasoning=f"Error: {str(e)}"
            )

    async def atomize_message_async(self, message: MessageRecord, index: int) -> tuple:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            self.thread_pool,
            self.atomize_message_sync,
            message
        )
        return (index, message, result)

    async def atomize_messages_parallel(self, messages: List[MessageRecord]) -> List[MessageRecord]:
        logger.info(f"Atomizing {len(messages)} messages in parallel...")

        tasks = [self.atomize_message_async(message, idx) for idx, message in enumerate(messages)]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        atomized_messages = []

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Atomization task failed: {result}")
                continue

            idx, original_message, atomization_result = result

            if atomization_result.is_compound and len(atomization_result.atomic_messages) > 1:
                for atomic_idx, atomic_text in enumerate(atomization_result.atomic_messages):
                    new_message = original_message.model_copy(deep=True)
                    new_message.record_id = str(uuid.uuid4())
                    new_message.atomic_idx = atomic_idx
                    new_message.message_text = atomic_text

                    if self.anonymize:
                        new_message.message_text = self.anonymize_text(atomic_text)

                    atomized_messages.append(new_message)

                logger.debug(f"Split message {idx} into {len(atomization_result.atomic_messages)} atomic parts")

            else:
                new_message = original_message.model_copy(deep=True)
                new_message.record_id = str(uuid.uuid4())
                new_message.atomic_idx = 0

                if self.anonymize:
                    new_message.message_text = self.anonymize_text(original_message.message_text)

                atomized_messages.append(new_message)

        logger.info(f"Atomization complete: {len(messages)} → {len(atomized_messages)} messages")

        return atomized_messages

    def atomize_messages_sync(self, messages: List[MessageRecord]) -> List[MessageRecord]:
        try:
            loop = asyncio.get_running_loop()
            import concurrent.futures

            def run_in_new_loop():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    return new_loop.run_until_complete(self.atomize_messages_parallel(messages))
                finally:
                    new_loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_in_new_loop)
                return future.result()

        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self.atomize_messages_parallel(messages))
            finally:
                loop.close()

    def get_usage_stats(self):
        if hasattr(self.llm_client, 'get_usage_stats'):
            return self.llm_client.get_usage_stats()
        return {}


def atomize_data_stage(messages: List[MessageRecord], config: dict) -> List[MessageRecord]:
    logger.info("=== STAGE 3: ATOMIZATION & ANONYMIZATION ===")

    atomizer_config = config.get("atomizer", {})

    if not atomizer_config.get("enabled", True):
        logger.info("Atomization disabled, skipping...")
        return messages

    atomizer = MessageAtomizer(
        anonymize=atomizer_config.get("anonymize", True),
        anonymization_rules=atomizer_config.get("anonymization_rules", {}),
        llm_config=atomizer_config.get("llm_config", {})
    )

    atomized_messages = atomizer.atomize_messages_sync(messages)

    usage_stats = atomizer.get_usage_stats()
    if usage_stats:
        logger.info(f"Atomization LLM Usage - Calls: {usage_stats.get('api_call_count', 0)}, "
                   f"Tokens: {usage_stats.get('total_tokens', 0):,}, "
                   f"Cost: ${usage_stats.get('total_cost', 0):.4f}")

    return atomized_messages
