#!/usr/bin/env python3

import re
import uuid
from typing import List, Set, Dict, Any
from datetime import datetime

from shared.logger import get_logger
from ..models import RawMessage, FilteredMessage, FilterResult, PipelineConfig

logger = get_logger(__name__)

class EnhancedContentFilter:
    """Enhanced content filter with complete tracking and configurable rules"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.filter_config = config.filtering

        # Simple STOP pattern (case-insensitive) - only filter opt-out messages and emoji reactions
        self.stop_patterns = [
            r'^stop\s*$',
            r'^unsubscribe\s*$',
            r'^remove\s+me\s*$',
            r'^opt\s*out\s*$',
            r'^👍\s+to\s+.*$',    # thumbs up to [anything] - specific pattern you mentioned
            r'^👍\s*$',           # standalone thumbs up emoji
            r'^👎\s*$',           # thumbs down emoji
            r'^❤️\s*$',           # heart emoji
            r'^😀\s*$',           # smile emoji
            r'^😊\s*$',           # happy emoji
            r'^[👍👎❤️😀😊🙂🙁😢😡🤔💯🔥✨🎉👏🤝💪🚀⭐]+\s*$',  # any combination of common emojis
            r'^.{1,2}\s*$'        # any 1-2 character message (catches most emoji reactions)
        ]

        # Civic relevance indicators (these suggest substantive content)
        self.civic_keywords = {
            # Infrastructure
            'roads', 'traffic', 'construction', 'sidewalks', 'bridges', 'water', 'sewer',
            'streetlights', 'potholes', 'snow removal', 'garbage', 'recycling', 'utilities',

            # Community services
            'police', 'fire', 'emergency', 'schools', 'education', 'library', 'parks',
            'recreation', 'senior center', 'health', 'hospital', 'clinic',

            # Economic development
            'business', 'development', 'zoning', 'permits', 'taxes', 'budget', 'revenue',
            'jobs', 'employment', 'economic', 'retail', 'commercial', 'industrial',

            # Governance
            'mayor', 'council', 'trustee', 'alderman', 'meeting', 'vote', 'election',
            'ordinance', 'policy', 'regulation', 'transparency', 'accountability',

            # Housing and planning
            'housing', 'affordable', 'development', 'subdivision', 'planning', 'growth',
            'density', 'rezoning', 'variances', 'building',

            # Transportation
            'public transit', 'bus', 'train', 'parking', 'bike lanes', 'pedestrian',
            'traffic lights', 'stop signs', 'speed limit',

            # Environmental
            'environment', 'pollution', 'noise', 'air quality', 'green space',
            'sustainability', 'energy', 'solar', 'climate',

            # Community issues
            'safety', 'crime', 'drugs', 'homelessness', 'youth', 'seniors',
            'accessibility', 'diversity', 'inclusion', 'neighborhood'
        }

        # Compile regex patterns for efficiency
        self.compiled_stop_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.stop_patterns]

    def clean_text(self, text: str) -> str:
        """Clean and normalize text"""
        if not text or not isinstance(text, str):
            return ""

        # Remove extra whitespace and normalize
        text = re.sub(r'\s+', ' ', text.strip())

        # Remove URLs
        text = re.sub(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', '', text)

        # Remove email addresses
        text = re.sub(r'\S+@\S+', '', text)

        # Remove phone numbers
        text = re.sub(r'\b\d{3}-\d{3}-\d{4}\b|\b\(\d{3}\)\s*\d{3}-\d{4}\b', '', text)

        # Remove excessive punctuation
        text = re.sub(r'[!]{2,}', '!', text)
        text = re.sub(r'[?]{2,}', '?', text)
        text = re.sub(r'[.]{3,}', '...', text)

        # Remove special characters and emojis
        text = re.sub(r'[^\w\s\.,!?;:\-\'"]', ' ', text)

        # Clean up whitespace again
        text = re.sub(r'\s+', ' ', text.strip())

        return text

    def is_stop_message(self, text: str) -> bool:
        """Check if text is a STOP/opt-out message"""
        if not text or not isinstance(text, str):
            return False

        text_clean = self.clean_text(text).lower().strip()
        return any(pattern.match(text_clean) for pattern in self.compiled_stop_patterns)

    def has_civic_relevance(self, text: str) -> bool:
        """Check if text contains civic-relevant keywords"""
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in self.civic_keywords)

    def check_length_requirements(self, text: str) -> Dict[str, Any]:
        """Check if text meets length requirements"""
        cleaned_text = self.clean_text(text)
        min_length = self.filter_config.get("min_length", 15)
        max_length = self.filter_config.get("max_message_length", 5000)

        return {
            "original_length": len(text),
            "cleaned_length": len(cleaned_text),
            "meets_min": len(cleaned_text) >= min_length,
            "meets_max": len(cleaned_text) <= max_length,
            "min_required": min_length,
            "max_allowed": max_length
        }

    def filter_message(self, raw_message: RawMessage) -> FilterResult:
        """
        Apply filtering rules to a message - only filter STOP/opt-out messages
        """
        text = raw_message.original_text
        reasons = []
        filter_stats = {}

        # Length check (basic sanity check)
        length_check = self.check_length_requirements(text)
        filter_stats["length"] = length_check

        if not length_check["meets_max"]:
            reasons.append(f"Too long: {length_check['cleaned_length']} > {length_check['max_allowed']} chars")

        # STOP message check - only filter opt-out messages
        is_stop = self.is_stop_message(text)
        filter_stats["stop_message"] = is_stop
        if is_stop:
            reasons.append("STOP/opt-out message")

        # Overall pass/fail
        passed = len(reasons) == 0

        return FilterResult(
            passed=passed,
            reasons=reasons,
            filter_stats=filter_stats
        )

    def create_filtered_message(self, raw_message: RawMessage, filter_result: FilterResult) -> FilteredMessage:
        """Create a FilteredMessage with tracking"""
        if filter_result.passed:
            filtered_text = self.clean_text(raw_message.original_text)
        else:
            filtered_text = ""  # Empty if filtered out

        filtered_message = FilteredMessage(
            id=str(uuid.uuid4()),
            original_message_id=raw_message.id,
            csv_file=raw_message.csv_file,
            csv_row_index=raw_message.csv_row_index,
            filtered_text=filtered_text,
            filter_result=filter_result,
            original_text=raw_message.original_text,
            campaign_source=raw_message.campaign_source,
            metadata=raw_message.metadata,  # Pass through metadata including phone number
            created_at=datetime.now()
        )

        # Debug first few filtered messages with comprehensive metadata logging
        if hasattr(self, '_debug_count'):
            self._debug_count += 1
        else:
            self._debug_count = 1

        if self._debug_count <= 5:
            phone = raw_message.metadata.get("Contact Phone Number", "NOT_FOUND")
            sent_at = raw_message.metadata.get("Sent At", "NOT_FOUND")
            logger.info(f"CONTENT_FILTER MESSAGE {self._debug_count} (csv_row={raw_message.csv_row_index}):")
            logger.info(f"  phone='{phone}', sent_at='{sent_at}'")
            logger.info(f"  passed={filter_result.passed}")
            logger.info(f"  metadata_keys={list(raw_message.metadata.keys())}")
            logger.info(f"  filtered_msg_metadata_keys={list(filtered_message.metadata.keys())}")

        return filtered_message

    def filter_messages_batch(self, raw_messages: List[RawMessage]) -> List[FilteredMessage]:
        """Filter a batch of messages"""
        filtered_messages = []

        for raw_message in raw_messages:
            filter_result = self.filter_message(raw_message)
            filtered_message = self.create_filtered_message(raw_message, filter_result)
            filtered_messages.append(filtered_message)

        return filtered_messages

    def get_passed_messages(self, filtered_messages: List[FilteredMessage]) -> List[FilteredMessage]:
        """Get only the messages that passed filtering"""
        return [msg for msg in filtered_messages if msg.filter_result.passed]

    def analyze_filtering_impact(self, filtered_messages: List[FilteredMessage]) -> Dict[str, Any]:
        """Analyze the impact of filtering on the dataset"""
        total_messages = len(filtered_messages)
        passed_messages = len(self.get_passed_messages(filtered_messages))
        filtered_out = total_messages - passed_messages

        # Count different types of filtering
        reason_counts = {}
        for msg in filtered_messages:
            if not msg.filter_result.passed:
                for reason in msg.filter_result.reasons:
                    reason_type = reason.split(':')[0]  # Get the main reason type
                    reason_counts[reason_type] = reason_counts.get(reason_type, 0) + 1

        # Campaign-specific stats
        campaign_stats = {}
        for msg in filtered_messages:
            campaign = msg.campaign_source
            if campaign not in campaign_stats:
                campaign_stats[campaign] = {"total": 0, "passed": 0}
            campaign_stats[campaign]["total"] += 1
            if msg.filter_result.passed:
                campaign_stats[campaign]["passed"] += 1

        # Calculate pass rates per campaign
        for campaign, stats in campaign_stats.items():
            stats["pass_rate"] = stats["passed"] / stats["total"] if stats["total"] > 0 else 0

        return {
            "total_messages": total_messages,
            "passed_messages": passed_messages,
            "filtered_out": filtered_out,
            "pass_rate": passed_messages / total_messages if total_messages > 0 else 0,
            "filter_reasons": reason_counts,
            "campaign_stats": campaign_stats,
            "average_original_length": sum(len(msg.original_text) for msg in filtered_messages) / total_messages if total_messages > 0 else 0,
            "average_filtered_length": sum(len(msg.filtered_text) for msg in self.get_passed_messages(filtered_messages)) / passed_messages if passed_messages > 0 else 0
        }

    def generate_filtering_report(self, filtered_messages: List[FilteredMessage]) -> str:
        """Generate human-readable filtering report"""
        stats = self.analyze_filtering_impact(filtered_messages)

        report_lines = [
            "Content Filtering Report:",
            f"  Total Messages: {stats['total_messages']:,}",
            f"  Passed Filtering: {stats['passed_messages']:,} ({stats['pass_rate']:.1%})",
            f"  Filtered Out: {stats['filtered_out']:,} ({(1-stats['pass_rate']):.1%})",
            "",
            "Filtering Reasons:"
        ]

        for reason, count in sorted(stats["filter_reasons"].items(), key=lambda x: x[1], reverse=True):
            percentage = (count / stats['total_messages']) * 100
            report_lines.append(f"  {reason}: {count:,} ({percentage:.1f}%)")

        report_lines.extend([
            "",
            "Campaign Performance:"
        ])

        for campaign, campaign_stats in stats["campaign_stats"].items():
            pass_rate = campaign_stats["pass_rate"]
            report_lines.append(f"  {campaign}: {campaign_stats['passed']:,}/{campaign_stats['total']:,} ({pass_rate:.1%})")

        report_lines.extend([
            "",
            f"Average Message Length:",
            f"  Original: {stats['average_original_length']:.1f} chars",
            f"  After Filtering: {stats['average_filtered_length']:.1f} chars"
        ])

        return "\n".join(report_lines)

def content_filter_stage(raw_messages: List[RawMessage], config: PipelineConfig) -> List[FilteredMessage]:
    """Main entry point for content filtering stage"""
    logger.info("=== CONTENT FILTERING STAGE ===")

    if not config.filtering.get("enabled", True):
        logger.info("Content filtering is disabled, creating pass-through filtered messages")
        # Create filtered messages that all pass
        filtered_messages = []
        for raw_message in raw_messages:
            filter_result = FilterResult(passed=True, reasons=[], filter_stats={})
            filtered_message = FilteredMessage(
                id=str(uuid.uuid4()),
                original_message_id=raw_message.id,
                csv_file=raw_message.csv_file,
                csv_row_index=raw_message.csv_row_index,
                filtered_text=raw_message.original_text,
                filter_result=filter_result,
                original_text=raw_message.original_text,
                campaign_source=raw_message.campaign_source,
                metadata=raw_message.metadata,  # Pass through metadata including phone number
                created_at=datetime.now()
            )
            filtered_messages.append(filtered_message)
        return filtered_messages

    try:
        filter_engine = EnhancedContentFilter(config)

        # Filter messages
        filtered_messages = filter_engine.filter_messages_batch(raw_messages)

        # Generate and log report
        report = filter_engine.generate_filtering_report(filtered_messages)
        logger.info(f"\n{report}")

        return filtered_messages

    except Exception as e:
        logger.error(f"Content filtering failed: {e}")
        raise