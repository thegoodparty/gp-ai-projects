import re
import sys
from pathlib import Path
from typing import List

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from shared.logger import get_logger
from serve.analyze_texts.models import MessageRecord

logger = get_logger(__name__)


class DataCleaner:
    def __init__(self, normalize_whitespace: bool = True, fix_encoding: bool = True):
        self.normalize_whitespace = normalize_whitespace
        self.fix_encoding = fix_encoding
        self.cleaned_count = 0

    def clean_text(self, text: str) -> str:
        if not text:
            return text

        original_text = text
        text = text.strip()

        if self.normalize_whitespace:
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n\s*\n', '\n', text)

        if self.fix_encoding:
            text = re.sub(r'[\u200b-\u200d\ufeff]', '', text)
            text = re.sub(r'["\u201c\u201d]', '"', text)
            text = re.sub(r"['\u2018\u2019]", "'", text)

        text = re.sub(r'^["\s]*to\s*".*?"["\s]*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^\s*👍\s*to\s*".*?".*$', '', text, flags=re.IGNORECASE)

        text = text.strip()

        if text != original_text:
            self.cleaned_count += 1

        return text

    def clean_messages(self, messages: List[MessageRecord]) -> List[MessageRecord]:
        logger.info(f"Cleaning {len(messages)} messages")

        cleaned_messages = []

        for message in messages:
            cleaned_text = self.clean_text(message.message_text)

            if cleaned_text:
                message.message_text = cleaned_text
                cleaned_messages.append(message)

        logger.info(f"Cleaned {self.cleaned_count} messages ({len(messages) - len(cleaned_messages)} became empty)")

        return cleaned_messages


def clean_data_stage(messages: List[MessageRecord], config: dict) -> List[MessageRecord]:
    logger.info("=== STAGE 1: DATA CLEANING ===")

    cleaner_config = config.get("cleaner", {})
    cleaner = DataCleaner(
        normalize_whitespace=cleaner_config.get("normalize_whitespace", True),
        fix_encoding=cleaner_config.get("fix_encoding", True)
    )

    cleaned_messages = cleaner.clean_messages(messages)

    logger.info(f"Cleaned {len(cleaned_messages)} messages (removed {len(messages) - len(cleaned_messages)} empty)")

    return cleaned_messages
