import re
import sys
from pathlib import Path
from typing import List, Tuple
from pydantic import BaseModel, Field

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from shared.logger import get_logger
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from serve.analyze_texts.models import MessageRecord, FilterStats

logger = get_logger(__name__)


class MessageSubstantivenessCheck(BaseModel):
    is_substantive: bool = Field(description="True if the message contains substantive civic feedback or concerns, False if it's a wrong number, opt-out request, or non-responsive message")
    reason: str = Field(description="Brief explanation of why the message is or isn't substantive")


class MessageFilter:
    STOP_PATTERNS = [
        r'^stop$',
        r'^unsubscribe$',
        r'^remove\s+me$',
        r'^opt\s+out$',
        r'^cancel$',
        r'^end$',
        r'^quit$',
        r'^don\'?t\s+text\s+me',
        r'^stop\s+texting',
        r'^remove\s+from\s+list',
        r'^take\s+me\s+off',
        r'^no\s+more\s+texts?',
        r'^delete\s+my\s+number'
    ]

    def __init__(self, remove_stop_messages: bool = True, remove_emoji_starters: bool = True, remove_non_substantive: bool = True, llm_config: dict = None):
        self.remove_stop_messages = remove_stop_messages
        self.remove_emoji_starters = remove_emoji_starters
        self.remove_non_substantive = remove_non_substantive

        llm_config = llm_config or {}
        max_connections = llm_config.get("max_connections", 400)

        self.llm_client = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=llm_config.get("temperature", 0.0),
            thinking_level=ThinkingLevel.MINIMAL,
            max_connections=max_connections,
            max_keepalive_connections=max_connections // 4
        )

    def is_stop_message(self, text: str) -> bool:
        text_clean = text.lower().strip()

        if len(text_clean.split()) > 2:
            words = text_clean.split()
            if words[0] in ['stop', 'unsubscribe'] or words[-1] in ['stop', 'unsubscribe']:
                return True

        for pattern in self.STOP_PATTERNS:
            if re.match(pattern, text_clean, re.IGNORECASE):
                return True

        return False

    def starts_with_emoji(self, text: str) -> bool:
        if not text:
            return False

        emoji_pattern = r'^[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\s]+'
        return bool(re.match(emoji_pattern, text.strip()))

    def is_non_substantive(self, text: str) -> bool:
        text_clean = text.strip()

        if not text_clean or text_clean.lower() in ['nan', 'n/a', 'na']:
            return True

        prompt = f"""Determine if this message contains substantive civic feedback or concerns.

A message is SUBSTANTIVE if it:
- Expresses concerns about community issues
- Shares opinions on local policies
- Requests action on civic matters
- Discusses political or social issues

A message is NOT SUBSTANTIVE if it:
- Is a wrong number ("This is Johnny with Green Valley plumbing")
- Indicates the recipient is not the intended person ("This is not my number")
- Is an opt-out request (already handled by stop_message filter)
- Contains only emojis or non-verbal responses
- Is empty or meaningless content

Message: "{text_clean}"

Analyze this message and determine if it contains substantive civic content."""

        try:
            response: MessageSubstantivenessCheck = self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=MessageSubstantivenessCheck
            )

            if not response.is_substantive:
                logger.debug(f"Non-substantive message detected: '{text_clean[:50]}...' - Reason: {response.reason}")

            return not response.is_substantive

        except Exception as e:
            logger.warning(f"Error checking message substantiveness, defaulting to substantive: {e}")
            return False

    def should_filter(self, message: MessageRecord) -> Tuple[bool, str]:
        text = message.message_text

        if self.remove_stop_messages and self.is_stop_message(text):
            return True, "stop_message"

        if self.remove_emoji_starters and self.starts_with_emoji(text):
            return True, "emoji_starter"

        if self.remove_non_substantive and self.is_non_substantive(text):
            return True, "non_substantive"

        return False, "kept"

    def filter_messages(self, messages: List[MessageRecord]) -> Tuple[List[MessageRecord], List[MessageRecord], FilterStats]:
        logger.info(f"Filtering {len(messages)} messages")

        substantive_messages = []
        filtered_messages = []
        removed_stop = 0
        removed_emoji = 0
        removed_non_substantive = 0

        for message in messages:
            should_remove, reason = self.should_filter(message)

            if should_remove:
                filtered_messages.append(message)
                if reason == "stop_message":
                    removed_stop += 1
                elif reason == "emoji_starter":
                    removed_emoji += 1
                elif reason == "non_substantive":
                    removed_non_substantive += 1
            else:
                substantive_messages.append(message)

        stats = FilterStats(
            total_messages=len(messages),
            removed_stop=removed_stop,
            removed_emoji_starter=removed_emoji,
            removed_non_substantive=removed_non_substantive,
            remaining=len(substantive_messages)
        )

        logger.info(f"Filtered out {removed_stop} STOP messages, {removed_emoji} emoji starters, {removed_non_substantive} non-substantive messages")
        logger.info(f"Remaining: {len(substantive_messages)} substantive messages")

        return substantive_messages, filtered_messages, stats


def filter_data_stage(messages: List[MessageRecord], config: dict) -> Tuple[List[MessageRecord], FilterStats]:
    logger.info("=== STAGE 2: DATA FILTERING ===")

    filter_config = config.get("filter", {})
    message_filter = MessageFilter(
        remove_stop_messages=filter_config.get("remove_stop_messages", True),
        remove_emoji_starters=filter_config.get("remove_emoji_starters", True),
        remove_non_substantive=filter_config.get("remove_non_substantive", True),
        llm_config=filter_config.get("llm_config", {})
    )

    substantive, filtered, stats = message_filter.filter_messages(messages)

    return substantive, stats
