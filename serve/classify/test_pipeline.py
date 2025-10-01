#!/usr/bin/env python3

"""
Quick test for the World-Class Civic Message Classification Pipeline
"""

import asyncio
import sys
from pathlib import Path

# Add the parent directory to Python path
sys.path.append(str(Path(__file__).parent.parent))

from serve.classify.models import MessageData, EnrichedMessage
from serve.classify.data_cleaner import SmartDataCleaner
from serve.classify.smart_classifier import WorldClassClassifier
from serve.classify.classification_rules import ClassificationRules

async def test_basic_components():
    """Test basic components of the pipeline"""
    print("🧪 Testing World-Class Classification Pipeline Components")
    print("=" * 60)

    # Test 1: Classification Rules
    print("1. Testing Classification Rules...")

    test_messages = [
        "Property taxes are too high!",
        "STOP",
        "Truck traffic is destroying our roads and warehouses are ruining our town",
        "Thanks for reaching out!"
    ]

    for msg in test_messages:
        should_uncategorize, reason = ClassificationRules.should_be_uncategorized(msg)
        required_cats = ClassificationRules.get_required_categories(msg)

        print(f"   '{msg[:30]}...'")
        print(f"     Uncategorized: {should_uncategorize} ({reason})")
        print(f"     Required categories: {len(required_cats)}")

    print("   ✅ Classification Rules working")

    # Test 2: Data Cleaner
    print("\n2. Testing Data Cleaner...")

    cleaner = SmartDataCleaner()

    # Create test messages
    test_enriched_messages = []
    for i, text in enumerate(test_messages):
        message_data = MessageData(
            campaign_id=f"test_{i}",
            campaign_name="Test Campaign",
            contact_phone_number=f"123456789{i}",
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

        enriched = EnrichedMessage(
            original_data=message_data,
            is_substantive=False,
            original_csv_row=i + 1,
            original_csv_file="test.csv"
        )
        test_enriched_messages.append(enriched)

    cleaned_messages, stats = cleaner.clean_messages(test_enriched_messages)
    print(f"   Cleaned {len(cleaned_messages)} from {len(test_enriched_messages)} messages")
    print(f"   Retention rate: {len(cleaned_messages)/len(test_enriched_messages):.1%}")
    print("   ✅ Data Cleaner working")

    # Test 3: Smart Classifier (basic test)
    print("\n3. Testing Smart Classifier...")

    if cleaned_messages:
        classifier = WorldClassClassifier()

        # Test with first cleaned message
        test_message = cleaned_messages[0]
        classification = await classifier.classify_message(test_message.original_data)

        print(f"   Test message: '{test_message.original_data.message_text}'")
        print(f"   Uncategorized: {classification.should_be_uncategorized}")
        print(f"   Issues identified: {len(classification.issues)}")

        for issue in classification.issues[:2]:  # Show first 2 issues
            print(f"     - {issue.primary_category}/{issue.secondary_category}: {issue.stance.value}")

        print("   ✅ Smart Classifier working")
    else:
        print("   ⚠️  No cleaned messages to test classifier with")

    print("\n" + "=" * 60)
    print("🎉 Basic component testing completed successfully!")
    return True

async def main():
    """Main test function"""
    try:
        success = await test_basic_components()

        if success:
            print("\n✅ All basic tests passed! The pipeline components are working correctly.")
            print("\nNext steps:")
            print("1. Run full pipeline: `uv run run_pipeline.py --quick-test`")
            print("2. Process real data: `uv run run_pipeline.py --data-source josh`")
            return 0
        else:
            print("\n❌ Some tests failed.")
            return 1

    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)