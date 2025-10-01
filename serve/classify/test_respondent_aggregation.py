#!/usr/bin/env python3

import sys
sys.path.append('/Users/collinpark/work/gp-ai-projects')

from serve.classify.data_loader import DataLoader
from serve.classify.smart_aggregator import SmartAggregator
from serve.classify.data_cleaner import SmartDataCleaner
from serve.classify.smart_classifier import WorldClassClassifier
from shared.logger import get_logger
import asyncio

logger = get_logger(__name__)

async def test_respondent_aggregation():
    """Test the respondent-based aggregation with real data"""

    try:
        # Load some test data
        logger.info("Loading test data...")
        loader = DataLoader("./data")
        messages, summary = loader.load_for_classification("berkley", inbound_only=True)
        logger.info(f"Loaded {len(messages)} messages")

        # Take just the first 20 messages for quick testing
        test_messages = messages[:20]
        logger.info(f"Testing with {len(test_messages)} messages")

        # Clean the messages
        logger.info("Cleaning messages...")
        cleaner = SmartDataCleaner()
        cleaned_messages, cleaning_stats = cleaner.clean_messages(test_messages)
        logger.info(f"Cleaned to {len(cleaned_messages)} messages")

        if len(cleaned_messages) == 0:
            logger.warning("No messages left after cleaning - trying more messages")
            test_messages = messages[:50]
            cleaned_messages, cleaning_stats = cleaner.clean_messages(test_messages)
            logger.info(f"Cleaned to {len(cleaned_messages)} messages from 50")

        # Classify a few messages for testing
        logger.info("Classifying messages...")
        classifier = WorldClassClassifier(temperature=0.0, target_concurrency=10)

        # Process just a few messages
        classified_messages = []
        for i, message in enumerate(cleaned_messages[:5]):
            logger.info(f"Classifying message {i+1}: {message.original_data.message_text[:50]}...")
            classification = await classifier.classify_message(message.original_data)
            message.smart_classification = classification
            message.is_substantive = not classification.should_be_uncategorized
            classified_messages.append(message)

        logger.info(f"Classified {len(classified_messages)} messages")

        # Test both aggregation modes
        respondent_config = {
            "insights": {
                "aggregation_mode": "respondent",
                "normalize_phone_numbers": True
            }
        }

        message_config = {
            "insights": {
                "aggregation_mode": "message",
                "normalize_phone_numbers": False
            }
        }

        # Test respondent-based aggregation
        logger.info("\n" + "="*50)
        logger.info("Testing RESPONDENT-BASED aggregation...")
        aggregator_respondent = SmartAggregator(respondent_config)
        insights_respondent = aggregator_respondent.generate_insights(classified_messages)

        # Test message-based aggregation
        logger.info("\n" + "="*50)
        logger.info("Testing MESSAGE-BASED aggregation...")
        aggregator_message = SmartAggregator(message_config)
        insights_message = aggregator_message.generate_insights(classified_messages)

        # Compare results
        logger.info("\n" + "="*50)
        logger.info("COMPARISON RESULTS:")
        logger.info(f"Respondent-based aggregation found {len(insights_respondent.top_issues)} issues")
        logger.info(f"Message-based aggregation found {len(insights_message.top_issues)} issues")

        logger.info("\nTop 3 issues from RESPONDENT-based:")
        for i, issue in enumerate(insights_respondent.top_issues[:3], 1):
            issue_name = f"{issue.primary_category}/{issue.secondary_category}"
            logger.info(f"  {i}. {issue_name}: {issue.unique_respondents} unique respondents, {issue.total_mentions} mentions")

        logger.info("\nTop 3 issues from MESSAGE-based:")
        for i, issue in enumerate(insights_message.top_issues[:3], 1):
            issue_name = f"{issue.primary_category}/{issue.secondary_category}"
            logger.info(f"  {i}. {issue_name}: {issue.unique_respondents} respondents (=messages), {issue.total_mentions} mentions")

        # Check phone number tracking
        logger.info("\nPhone numbers found:")
        phones = set()
        for message in classified_messages:
            phone = message.original_data.contact_phone_number
            if phone:
                phones.add(phone)
        logger.info(f"  {len(phones)} unique phone numbers in test data")

        logger.info("\n✅ Test completed successfully!")

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    return True

if __name__ == "__main__":
    success = asyncio.run(test_respondent_aggregation())
    sys.exit(0 if success else 1)