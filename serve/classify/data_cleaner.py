#!/usr/bin/env python3

import re
from typing import List, Tuple, Optional, Set, Dict, Any
from dataclasses import dataclass
from shared.logger import get_logger

from .models import EnrichedMessage, MessageQuality

logger = get_logger(__name__)


@dataclass
class CleaningStats:
    """Statistics about the cleaning process"""
    total_messages: int
    removed_stop_messages: int
    removed_duplicates: int
    removed_non_substantive: int
    remaining_substantive: int
    text_normalizations: int


class SmartDataCleaner:
    """
    Sophisticated data cleaner that preserves meaningful content while removing noise
    Based on insights from manual review of civic messages
    """

    def __init__(self, min_length: int = 10, remove_duplicates: bool = True):
        self.min_length = min_length
        self.remove_duplicates = remove_duplicates
        self.seen_messages: Set[str] = set()
        self.phone_message_map: Dict[str, str] = {}

    # STOP message patterns (comprehensive list)
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

    # Non-substantive patterns that should be removed
    NON_SUBSTANTIVE_PATTERNS = [
        # Pure greetings without content
        r'^(hi|hello|hey)[\s\w]*$',
        r'^(thanks?|thank\s+you)[\s\w]*$',
        r'^(good\s+morning|good\s+afternoon|good\s+evening)[\s\w]*$',

        # Simple acknowledgments
        r'^(ok|okay|yes|no|sure|absolutely)[\s\w]*$',
        r'^(got\s+it|understood|makes\s+sense)[\s\w]*$',
        r'^(will\s+do|sounds\s+good|perfect)[\s\w]*$',

        # Generic responses without substance
        r'^(great|good|nice)[\s!]*$',
        r'^(awesome|cool|sweet)[\s!]*$',

        # Single word/emoji responses
        r'^(lol|haha|wow|omg)[\s!]*$',
        r'^[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\s]*$',  # Only emojis

        # Wrong number responses
        r'^wrong\s+number',
        r'^not\s+interested',
        r'^don\'?t\s+live\s+(here|there|in)',
        r'^moved\s+(away|out)',
    ]

    # Meaningful short messages that should be kept
    MEANINGFUL_SHORT_PATTERNS = [
        r'\btax(es)?\b',
        r'\btruck\b',
        r'\btraffic\b',
        r'\bwater\b',
        r'\bread\b',
        r'\bparks?\b',
        r'\bschool\b',
        r'\bpolice\b',
        r'\bcrime\b',
        r'\bbusiness\b',
        r'\bdevelop(ment)?\b',
        r'\bzoning\b',
        r'\bhousing\b',
        r'\butilities?\b'
    ]

    def clean_text(self, text: str) -> str:
        """Clean and normalize text while preserving meaning"""
        if not text:
            return text

        original_text = text

        # Basic cleanup
        text = text.strip()

        # Remove excessive whitespace but preserve line breaks for compound messages
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n\s*\n', '\n', text)

        # Fix common text artifacts
        text = re.sub(r'[\u200b-\u200d\ufeff]', '', text)  # Remove zero-width chars
        text = re.sub(r'["\u201c\u201d]', '"', text)  # Normalize quotes
        text = re.sub(r"['\u2018\u2019]", "'", text)  # Normalize apostrophes

        # Remove phone artifacts but preserve meaningful quotes
        text = re.sub(r'^["\s]*to\s*".*?"["\s]*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^\s*👍\s*to\s*".*?".*$', '', text, flags=re.IGNORECASE)

        # Clean up but don't remove substance
        text = text.strip()

        if text != original_text:
            logger.debug(f"Text normalized: '{original_text[:50]}...' -> '{text[:50]}...'")

        return text

    def is_stop_message(self, text: str) -> bool:
        """Check if message is a STOP/opt-out message"""
        text_clean = text.lower().strip()

        # Handle messages with content plus STOP
        if len(text_clean.split()) > 2:
            # Check if STOP is at beginning or end with other content
            words = text_clean.split()
            if words[0] in ['stop', 'unsubscribe'] or words[-1] in ['stop', 'unsubscribe']:
                return True

        # Check exact patterns
        for pattern in self.STOP_PATTERNS:
            if re.match(pattern, text_clean, re.IGNORECASE):
                return True

        return False

    def is_non_substantive(self, text: str) -> bool:
        """Check if message lacks substantive content"""
        text_clean = text.lower().strip()

        # Check if it matches non-substantive patterns
        for pattern in self.NON_SUBSTANTIVE_PATTERNS:
            if re.match(pattern, text_clean, re.IGNORECASE | re.DOTALL):
                return True

        # Check length but allow meaningful short messages
        if len(text) < self.min_length:
            # Check if it contains meaningful civic terms
            for pattern in self.MEANINGFUL_SHORT_PATTERNS:
                if re.search(pattern, text_clean, re.IGNORECASE):
                    logger.debug(f"Keeping short but meaningful message: '{text[:30]}...'")
                    return False

            # Too short and no meaningful content
            return True

        return False

    def extract_feedback_from_stop(self, text: str) -> Optional[str]:
        """Extract any feedback from messages that contain STOP"""
        text_clean = text.strip()

        # Look for patterns like "STOP texting about warehouses"
        stop_with_content = re.search(
            r'^(stop|unsubscribe)\s+(.+)$',
            text_clean,
            re.IGNORECASE
        )

        if stop_with_content:
            content = stop_with_content.group(2).strip()
            if len(content) > 5:  # Has some substance
                return content

        # Look for content before STOP
        content_before_stop = re.search(
            r'^(.+?)\s+(stop|unsubscribe)$',
            text_clean,
            re.IGNORECASE
        )

        if content_before_stop:
            content = content_before_stop.group(1).strip()
            if len(content) > 5:
                return content

        return None

    def should_remove_duplicate(self, message: EnrichedMessage) -> bool:
        """Check if message should be removed as duplicate"""
        if not self.remove_duplicates:
            return False

        phone = message.original_data.contact_phone_number
        text = message.original_data.message_text

        if not phone or not text:
            return False

        text_normalized = self.clean_text(text).lower()

        # Check for exact duplicate from same phone
        phone_text_key = f"{phone}:{text_normalized}"
        if phone_text_key in self.seen_messages:
            logger.debug(f"Removing duplicate message from {phone}: '{text[:30]}...'")
            return True

        self.seen_messages.add(phone_text_key)

        # Check for very similar messages from same phone (potential resends)
        if phone in self.phone_message_map:
            previous_text = self.phone_message_map[phone]
            similarity = self._calculate_similarity(text_normalized, previous_text)
            if similarity > 0.9:  # Very similar
                logger.debug(f"Removing very similar message from {phone}: '{text[:30]}...'")
                return True

        self.phone_message_map[phone] = text_normalized
        return False

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Simple similarity calculation"""
        if not text1 or not text2:
            return 0.0

        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 and not words2:
            return 1.0

        intersection = words1.intersection(words2)
        union = words1.union(words2)

        return len(intersection) / len(union) if union else 0.0

    def clean_message(self, message: EnrichedMessage) -> Tuple[Optional[EnrichedMessage], str]:
        """
        Clean a single message and return cleaned message or None if should be removed
        Returns: (cleaned_message_or_none, removal_reason)
        """
        original_text = message.original_data.message_text

        if not original_text or not original_text.strip():
            return None, "empty_message"

        # Check for duplicates first
        if self.should_remove_duplicate(message):
            return None, "duplicate"

        # Clean the text
        cleaned_text = self.clean_text(original_text)

        if not cleaned_text:
            return None, "empty_after_cleaning"

        # Check for STOP messages but extract any feedback
        if self.is_stop_message(cleaned_text):
            feedback = self.extract_feedback_from_stop(cleaned_text)
            if feedback:
                logger.debug(f"Extracting feedback from STOP message: '{feedback}'")
                cleaned_text = feedback
            else:
                return None, "stop_message"

        # Check for non-substantive content
        if self.is_non_substantive(cleaned_text):
            return None, "non_substantive"

        # Create cleaned message
        cleaned_message = EnrichedMessage(
            original_data=message.original_data.model_copy(),
            is_substantive=True,
            original_csv_row=message.original_csv_row,
            original_csv_file=message.original_csv_file
        )

        # Update the text if it was modified
        if cleaned_text != original_text:
            cleaned_message.original_data.message_text = cleaned_text

        return cleaned_message, "kept"

    def clean_messages(self, messages: List[EnrichedMessage]) -> Tuple[List[EnrichedMessage], CleaningStats]:
        """
        Clean a batch of messages and return cleaned messages with statistics
        """
        logger.info(f"Cleaning {len(messages)} messages")

        # Reset state
        self.seen_messages.clear()
        self.phone_message_map.clear()

        cleaned_messages = []
        removal_counts = {
            "duplicate": 0,
            "stop_message": 0,
            "non_substantive": 0,
            "empty_message": 0,
            "empty_after_cleaning": 0
        }
        text_normalizations = 0

        for message in messages:
            original_text = message.original_data.message_text
            cleaned_message, removal_reason = self.clean_message(message)

            if cleaned_message:
                cleaned_messages.append(cleaned_message)

                # Count text normalizations
                if cleaned_message.original_data.message_text != original_text:
                    text_normalizations += 1
            else:
                removal_counts[removal_reason] = removal_counts.get(removal_reason, 0) + 1

        stats = CleaningStats(
            total_messages=len(messages),
            removed_stop_messages=removal_counts["stop_message"],
            removed_duplicates=removal_counts["duplicate"],
            removed_non_substantive=removal_counts["non_substantive"] + removal_counts["empty_message"] + removal_counts["empty_after_cleaning"],
            remaining_substantive=len(cleaned_messages),
            text_normalizations=text_normalizations
        )

        logger.info(f"Cleaning complete: {len(cleaned_messages)} substantive messages remaining from {len(messages)} total")
        logger.info(f"Removed: {stats.removed_stop_messages} STOP, {stats.removed_duplicates} duplicates, {stats.removed_non_substantive} non-substantive")

        return cleaned_messages, stats

    def generate_cleaning_report(self, stats: CleaningStats) -> str:
        """Generate a cleaning report"""
        report_lines = [
            "# Data Cleaning Report",
            "",
            f"**Total Messages Processed:** {stats.total_messages}",
            f"**Remaining Substantive Messages:** {stats.remaining_substantive}",
            f"**Retention Rate:** {stats.remaining_substantive / stats.total_messages:.1%}",
            "",
            "## Messages Removed:",
            f"- **STOP/Opt-out Messages:** {stats.removed_stop_messages}",
            f"- **Duplicate Messages:** {stats.removed_duplicates}",
            f"- **Non-substantive Messages:** {stats.removed_non_substantive}",
            "",
            f"**Text Normalizations Applied:** {stats.text_normalizations}",
        ]

        return "\n".join(report_lines)


def main():
    """Test the data cleaner"""
    from .models import MessageData, EnrichedMessage

    cleaner = SmartDataCleaner()

    # Test messages
    test_messages = [
        EnrichedMessage(
            original_data=MessageData(
                campaign_id="test1", campaign_name="Test", contact_phone_number="1234567890",
                carrier="TEST", campaign_number="123", is_automatic_reply=False,
                send_direction="INBOUND", send_status="", error_code="",
                sent_at="2025-01-01", message_text="STOP", texter_name="",
                message_type="SMS", mms_attachments=""
            ),
            is_substantive=False, original_csv_row=1, original_csv_file="test.csv"
        ),
        EnrichedMessage(
            original_data=MessageData(
                campaign_id="test2", campaign_name="Test", contact_phone_number="1234567891",
                carrier="TEST", campaign_number="123", is_automatic_reply=False,
                send_direction="INBOUND", send_status="", error_code="",
                sent_at="2025-01-01", message_text="Property taxes are too high!", texter_name="",
                message_type="SMS", mms_attachments=""
            ),
            is_substantive=False, original_csv_row=2, original_csv_file="test.csv"
        ),
        EnrichedMessage(
            original_data=MessageData(
                campaign_id="test3", campaign_name="Test", contact_phone_number="1234567892",
                carrier="TEST", campaign_number="123", is_automatic_reply=False,
                send_direction="INBOUND", send_status="", error_code="",
                sent_at="2025-01-01", message_text="Thanks", texter_name="",
                message_type="SMS", mms_attachments=""
            ),
            is_substantive=False, original_csv_row=3, original_csv_file="test.csv"
        ),
    ]

    cleaned_messages, stats = cleaner.clean_messages(test_messages)

    print(f"Cleaned {len(cleaned_messages)} messages from {len(test_messages)}")
    print(f"Stats: {stats}")

    for msg in cleaned_messages:
        print(f"Kept: '{msg.original_data.message_text}'")


if __name__ == "__main__":
    main()