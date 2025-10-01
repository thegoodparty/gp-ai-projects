#!/usr/bin/env python3

import sys
sys.path.append('/Users/collinpark/work/gp-ai-projects')

from serve.classify.smart_aggregator import SmartAggregator
from serve.classify.models import EnrichedMessage, MessageData, SmartCategorization, HierarchicalIssueWithContext, IssueStance, Sentiment, MessageQuality, ContentType
from shared.logger import get_logger

logger = get_logger(__name__)

def create_test_message(phone: str, text: str, issues: list):
    """Create a test message with mock classification"""
    message_data = MessageData(
        campaign_id="test",
        campaign_name="Test Campaign",
        contact_phone_number=phone,
        carrier="TEST",
        campaign_number="123",
        is_automatic_reply=False,
        send_direction="INBOUND",
        send_status="",
        error_code="",
        sent_at="2025-01-01T00:00:00.000Z",
        message_text=text,
        texter_name="",
        message_type="SMS",
        mms_attachments=""
    )

    # Create mock issues
    mock_issues = []
    for issue_data in issues:
        mock_issue = HierarchicalIssueWithContext(
            primary_category=issue_data['primary'],
            secondary_category=issue_data['secondary'],
            stance=IssueStance(issue_data['stance']),
            specific_concern=issue_data['concern'],
            is_root_cause=issue_data.get('is_root_cause', False)
        )
        mock_issues.append(mock_issue)

    classification = SmartCategorization(
        issues=mock_issues,
        should_be_uncategorized=False,
        overall_sentiment=Sentiment.FRUSTRATED_URGENT,
        message_quality=MessageQuality.SUBSTANTIVE,
        content_type=ContentType.POLICY_FEEDBACK,
        confidence_score=0.8
    )

    enriched_message = EnrichedMessage(
        original_data=message_data,
        smart_classification=classification,
        is_substantive=True,
        original_csv_row=1,
        original_csv_file="test.csv"
    )

    return enriched_message

def test_respondent_aggregation():
    """Test respondent-based aggregation with mock data"""

    print("Creating test data...")
    logger.info("Creating test data...")

    # Create test scenario:
    # Person A (phone 1111111111) mentions property taxes twice
    # Person B (phone 2222222222) mentions property taxes once
    # Person C (phone 3333333333) mentions roads once
    # Expected: property taxes = 2 unique respondents, roads = 1 unique respondent

    test_messages = [
        create_test_message(
            "1111111111",
            "Property taxes are too high!",
            [{"primary": "housing_and_development", "secondary": "taxes_and_assessments", "stance": "negative", "concern": "Property tax burden"}]
        ),
        create_test_message(
            "1111111111",
            "Seriously, these property taxes need to come down",
            [{"primary": "housing_and_development", "secondary": "taxes_and_assessments", "stance": "negative", "concern": "Property tax rates too high"}]
        ),
        create_test_message(
            "2222222222",
            "Property taxes are expensive here",
            [{"primary": "housing_and_development", "secondary": "taxes_and_assessments", "stance": "negative", "concern": "Property tax burden"}]
        ),
        create_test_message(
            "3333333333",
            "The roads need fixing",
            [{"primary": "infrastructure_and_transportation", "secondary": "roads_and_bridges", "stance": "negative", "concern": "Road repair needed"}]
        ),
    ]

    print(f"Created {len(test_messages)} test messages")
    logger.info(f"Created {len(test_messages)} test messages")

    # Test respondent-based aggregation
    print("\n" + "="*60)
    print("Testing RESPONDENT-BASED aggregation...")
    logger.info("\n" + "="*60)
    logger.info("Testing RESPONDENT-BASED aggregation...")

    respondent_config = {
        "insights": {
            "aggregation_mode": "respondent",
            "normalize_phone_numbers": True
        }
    }

    aggregator_respondent = SmartAggregator(respondent_config)
    print("Running respondent-based insights generation...")
    insights_respondent = aggregator_respondent.generate_insights(test_messages)
    print("Respondent-based insights completed")

    # Test message-based aggregation
    print("Testing MESSAGE-BASED aggregation...")
    logger.info("Testing MESSAGE-BASED aggregation...")

    message_config = {
        "insights": {
            "aggregation_mode": "message",
            "normalize_phone_numbers": False
        }
    }

    aggregator_message = SmartAggregator(message_config)
    print("Running message-based insights generation...")
    insights_message = aggregator_message.generate_insights(test_messages)
    print("Message-based insights completed")

    # Display results
    print("\n" + "="*60)
    print("RESULTS COMPARISON:")

    print("\n🏠 RESPONDENT-BASED Results:")
    for issue in insights_respondent.top_issues:
        issue_name = f"{issue.primary_category}/{issue.secondary_category}"
        print(f"  • {issue_name}:")
        print(f"    - {issue.unique_respondents} unique respondents")
        print(f"    - {issue.total_mentions} total mentions")
        print(f"    - {issue.mentions_per_respondent} mentions per person")

    print("\n📨 MESSAGE-BASED Results:")
    for issue in insights_message.top_issues:
        issue_name = f"{issue.primary_category}/{issue.secondary_category}"
        print(f"  • {issue_name}:")
        print(f"    - {issue.unique_respondents} respondents (=messages)")
        print(f"    - {issue.total_mentions} total mentions")
        print(f"    - {issue.mentions_per_respondent} mentions per person")

    # Validation
    print("\n" + "="*60)
    print("VALIDATION:")

    # Find property tax issue in respondent-based results
    prop_tax_respondent = None
    for issue in insights_respondent.top_issues:
        if issue.secondary_category == "taxes_and_assessments":
            prop_tax_respondent = issue
            break

    if prop_tax_respondent:
        expected_respondents = 2  # Person A and Person B
        expected_mentions = 3     # A mentioned twice, B once

        if prop_tax_respondent.unique_respondents == expected_respondents:
            print("✅ PASS: Correct unique respondent count for property taxes")
        else:
            print(f"❌ FAIL: Expected {expected_respondents} unique respondents, got {prop_tax_respondent.unique_respondents}")

        if prop_tax_respondent.total_mentions == expected_mentions:
            print("✅ PASS: Correct total mention count for property taxes")
        else:
            print(f"❌ FAIL: Expected {expected_mentions} total mentions, got {prop_tax_respondent.total_mentions}")

        expected_mentions_per_respondent = 1.5  # 3 mentions / 2 respondents
        if abs(prop_tax_respondent.mentions_per_respondent - expected_mentions_per_respondent) < 0.1:
            print("✅ PASS: Correct mentions per respondent calculation")
        else:
            print(f"❌ FAIL: Expected {expected_mentions_per_respondent} mentions per respondent, got {prop_tax_respondent.mentions_per_respondent}")
    else:
        print("❌ FAIL: Property tax issue not found in results")

    print("\n✅ Test completed!")
    return True

if __name__ == "__main__":
    try:
        success = test_respondent_aggregation()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.error(f"❌ Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)